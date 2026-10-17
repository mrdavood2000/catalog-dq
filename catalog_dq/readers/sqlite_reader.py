"""
sqlite_reader.py — Catalog reader for SQLite 3.

Available statistics:
  ✓  null_frac       — computed via SQL aggregate (not truly in-catalog,
                        but single-column COUNT with no value reading)
  ✓  n_distinct_norm — from sqlite_stat1 if an index exists, else SQL COUNT DISTINCT
  ✗  histogram_min/max — not stored by SQLite
  ✗  mcv_vals/freqs  — not stored by SQLite
  ✗  correlation     — not stored by SQLite
  ✓  row_count_est   — first token of sqlite_stat1.stat for table entry
  ~  analyze_time    — must be recorded externally by the caller;
                       call record_analyze_time() immediately after ANALYZE
"""
from __future__ import annotations
import datetime
import sqlite3
from typing import Optional, List

from ..snapshot import Snapshot, ColStats


class SQLiteReader:
    """
    Reads catalog statistics from a SQLite database file.

    Usage
    -----
    reader = SQLiteReader("path/to/db.sqlite")
    with reader.connect() as conn:
        conn.execute("ANALYZE")
    reader.record_analyze_time()            # call RIGHT AFTER ANALYZE
    snap = reader.read_snapshot("my_table")
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._analyze_time: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Connection helper                                                    #
    # ------------------------------------------------------------------ #
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ #
    # Analyze-time tracking (F1 support)                                  #
    # ------------------------------------------------------------------ #
    def record_analyze_time(self, when: Optional[str] = None) -> None:
        """
        Record the timestamp of the most recent ANALYZE.
        Call immediately after executing ANALYZE on the connection.
        'when' is an ISO-8601 string; defaults to utcnow().
        """
        self._analyze_time = when or datetime.datetime.utcnow().isoformat()

    @property
    def analyze_time(self) -> Optional[str]:
        return self._analyze_time

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #
    def _get_columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        return [row["name"] for row in cur.fetchall()]

    def _get_row_count_est(self, conn: sqlite3.Connection, table: str) -> Optional[int]:
        """Row count from sqlite_stat1 (first token of stat for the table row)."""
        try:
            cur = conn.execute(
                "SELECT stat FROM sqlite_stat1 WHERE tbl=? AND idx=?",
                (table, table),
            )
            row = cur.fetchone()
            if row:
                return int(str(row["stat"]).split()[0])
        except Exception:
            pass
        # Fallback: sqlite_stat1 may not have a bare-table entry if ANALYZE
        # was run but there are no indexes.  Try any entry for the table.
        try:
            cur = conn.execute(
                "SELECT stat FROM sqlite_stat1 WHERE tbl=? LIMIT 1", (table,)
            )
            row = cur.fetchone()
            if row:
                return int(str(row["stat"]).split()[0])
        except Exception:
            pass
        return None

    def _get_n_distinct_from_stat1(
        self, conn: sqlite3.Connection, table: str, col: str
    ) -> Optional[float]:
        """
        Try to derive n_distinct from sqlite_stat1 via an index on this column.
        Returns the normalised value (n_distinct / row_count_est), or None if
        no suitable index entry exists.
        """
        try:
            # Find indexes on this table and inspect their column lists
            idx_cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (table,),
            )
            index_names = [r[0] for r in idx_cur.fetchall()]

            n_total = self._get_row_count_est(conn, table)
            if n_total is None or n_total == 0:
                return None

            for idx_name in index_names:
                # Which columns does this index cover?
                info_cur = conn.execute(f'PRAGMA index_info("{idx_name}")')
                idx_cols = [r["name"] for r in info_cur.fetchall()]
                if not idx_cols or idx_cols[0] != col:
                    continue
                # Get stat for this index
                stat_cur = conn.execute(
                    "SELECT stat FROM sqlite_stat1 WHERE tbl=? AND idx=?",
                    (table, idx_name),
                )
                stat_row = stat_cur.fetchone()
                if not stat_row:
                    continue
                parts = str(stat_row["stat"]).split()
                if len(parts) < 2:
                    continue
                rows_per_val = float(parts[1])
                if rows_per_val <= 0:
                    continue
                n_distinct = n_total / rows_per_val
                return min(n_distinct / n_total, 1.0)
        except Exception:
            pass
        return None

    def _compute_null_frac(self, conn: sqlite3.Connection, table: str, col: str) -> float:
        """
        Null fraction via a single SQL aggregate (no value reading).
        This IS the catalog method for SQLite (the statistic is approximately computed, not stored).
        """
        sql = (
            f'SELECT CAST(SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) AS REAL)'
            f' / CAST(COUNT(*) AS REAL) FROM "{table}"'
        )
        cur = conn.execute(sql)
        row = cur.fetchone()
        v = row[0] if row else None
        return float(v) if v is not None else 0.0

    def _compute_n_distinct_norm(
        self, conn: sqlite3.Connection, table: str, col: str
    ) -> float:
        """
        Normalised n_distinct via SQL aggregate fallback (when no index exists).
        COUNT(DISTINCT col) / COUNT(*) — still a single-pass aggregate.
        """
        sql = (
            f'SELECT CAST(COUNT(DISTINCT "{col}") AS REAL)'
            f' / CAST(COUNT(*) AS REAL) FROM "{table}"'
        )
        cur = conn.execute(sql)
        row = cur.fetchone()
        v = row[0] if row else None
        return float(v) if v is not None else 0.0

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #
    def get_actual_row_count(self, conn: sqlite3.Connection, table: str) -> int:
        """Exact row count via COUNT(*) — used for F2 rule."""
        cur = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
        return cur.fetchone()[0]

    def read_snapshot(self, table: str) -> Snapshot:
        """
        Read a catalog snapshot for *table*.
        ANALYZE must have been run before calling this.
        """
        with self.connect() as conn:
            cols = self._get_columns(conn, table)
            row_count_est = self._get_row_count_est(conn, table)

            col_stats: dict[str, ColStats] = {}
            for col in cols:
                null_frac = self._compute_null_frac(conn, table, col)

                # Try sqlite_stat1 first for n_distinct
                n_dist = self._get_n_distinct_from_stat1(conn, table, col)
                if n_dist is None:
                    n_dist = self._compute_n_distinct_norm(conn, table, col)

                col_stats[col] = ColStats(
                    null_frac=null_frac,
                    n_distinct_norm=n_dist,
                    # V1, V2, K2 are not available from the SQLite catalog
                    histogram_min=None,
                    histogram_max=None,
                    mcv_vals=None,
                    mcv_freqs=None,
                    correlation=None,
                )

            return Snapshot(
                timestamp=Snapshot.now_iso(),
                db_type="sqlite",
                db_path=self.db_path,
                table=table,
                analyze_time=self._analyze_time,
                row_count_est=row_count_est,
                columns=col_stats,
            )
