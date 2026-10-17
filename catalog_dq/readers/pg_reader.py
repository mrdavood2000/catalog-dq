"""
pg_reader.py — Catalog reader for PostgreSQL 16.

Available statistics:
  ✓  null_frac          — pg_stats.null_frac
  ✓  histogram_min/max  — pg_stats.histogram_bounds[0] and [-1]
  ✓  mcv_vals/freqs     — pg_stats.most_common_vals / most_common_freqs
  ✓  n_distinct_norm    — |pg_stats.n_distinct| / pg_class.reltuples
  ✓  correlation        — pg_stats.correlation
  ✓  row_count_est      — pg_class.reltuples
  ✓  analyze_time       — pg_stat_all_tables.last_analyze / last_autoanalyze
                          (whichever is newer)

Requires: psycopg2-binary
"""
from __future__ import annotations
import ast
import datetime
from typing import Optional, List

import psycopg2
import psycopg2.extras

from ..snapshot import Snapshot, ColStats


def _parse_pg_array(raw) -> Optional[List]:
    """
    Parse a PostgreSQL array value returned by psycopg2.
    psycopg2 often returns these as Python lists already, but sometimes
    as strings like '{1.0,2.0}'. Handle both.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    # String form: '{a,b,c}'
    s = str(raw).strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1]
        if not inner:
            return []
        # Split on commas, strip quotes
        parts = []
        for tok in inner.split(","):
            tok = tok.strip().strip('"')
            parts.append(tok)
        return parts
    return None


class PostgresReader:
    """
    Reads catalog statistics from a PostgreSQL 16 database.

    Usage
    -----
    reader = PostgresReader("host=localhost dbname=mydb user=postgres")
    conn = reader.connect()
    cur = conn.cursor(); cur.execute("ANALYZE my_table"); conn.commit()
    snap = reader.read_snapshot("my_table", schema="public")
    conn.close()
    """

    def __init__(self, dsn: str):
        """
        dsn: libpq connection string or DSN dict keywords.
        e.g. "host=localhost port=5432 dbname=catalog_dq user=postgres password=secret"
        """
        self.dsn = dsn

    def connect(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = False
        return conn

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #
    def _get_reltuples(
        self, conn, table: str, schema: str = "public"
    ) -> Optional[int]:
        sql = """
            SELECT reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s AND n.nspname = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (table, schema))
            row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _get_analyze_time(
        self, conn, table: str, schema: str = "public"
    ) -> Optional[str]:
        sql = """
            SELECT GREATEST(last_analyze, last_autoanalyze)
            FROM pg_stat_all_tables
            WHERE relname = %s AND schemaname = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (table, schema))
            row = cur.fetchone()
        if row and row[0] is not None:
            ts = row[0]
            if isinstance(ts, datetime.datetime):
                return ts.isoformat()
            return str(ts)
        return None

    def _get_pg_stats(self, conn, table: str, schema: str = "public") -> dict:
        """
        Returns a dict col_name → raw pg_stats row (as dict).
        """
        sql = """
            SELECT
                attname,
                null_frac,
                n_distinct,
                most_common_vals,
                most_common_freqs,
                histogram_bounds,
                correlation
            FROM pg_stats
            WHERE tablename = %s AND schemaname = %s
        """
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (table, schema))
            rows = cur.fetchall()
        return {r["attname"]: dict(r) for r in rows}

    def _get_columns(self, conn, table: str, schema: str = "public") -> List[str]:
        sql = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(sql, (table, schema))
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #
    def get_actual_row_count(self, conn, table: str) -> int:
        """Exact row count via COUNT(*) — used for F2 rule."""
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            return cur.fetchone()[0]

    def read_snapshot(self, table: str, schema: str = "public") -> Snapshot:
        """
        Read a catalog snapshot for *table*.
        ANALYZE must have been run (and committed) before calling this.
        """
        conn = self.connect()
        try:
            cols = self._get_columns(conn, table, schema)
            row_count_est = self._get_reltuples(conn, table, schema)
            analyze_time = self._get_analyze_time(conn, table, schema)
            pg_stats = self._get_pg_stats(conn, table, schema)

            n_total = row_count_est or 1  # avoid division by zero

            col_stats: dict[str, ColStats] = {}
            for col in cols:
                s = pg_stats.get(col, {})

                null_frac = s.get("null_frac")
                null_frac = float(null_frac) if null_frac is not None else None

                # n_distinct normalisation
                n_dist_raw = s.get("n_distinct")
                n_dist_norm = None
                if n_dist_raw is not None:
                    nd = float(n_dist_raw)
                    if nd < 0:
                        # PostgreSQL encodes as -fraction_of_rows
                        n_dist_norm = abs(nd)
                    else:
                        n_dist_norm = nd / n_total if n_total > 0 else 0.0
                    n_dist_norm = min(n_dist_norm, 1.0)

                # Histogram bounds → min/max
                hb = _parse_pg_array(s.get("histogram_bounds"))
                hist_min = hist_max = None
                if hb:
                    try:
                        hist_min = float(hb[0])
                        hist_max = float(hb[-1])
                    except (ValueError, TypeError):
                        pass  # non-numeric column

                # MCV
                mcv_vals = _parse_pg_array(s.get("most_common_vals"))
                mcv_freqs_raw = _parse_pg_array(s.get("most_common_freqs"))
                mcv_freqs = None
                if mcv_freqs_raw:
                    try:
                        mcv_freqs = [float(x) for x in mcv_freqs_raw]
                    except (ValueError, TypeError):
                        mcv_freqs = None

                # Correlation
                corr = s.get("correlation")
                corr = float(corr) if corr is not None else None

                col_stats[col] = ColStats(
                    null_frac=null_frac,
                    n_distinct_norm=n_dist_norm,
                    histogram_min=hist_min,
                    histogram_max=hist_max,
                    mcv_vals=mcv_vals,
                    mcv_freqs=mcv_freqs,
                    correlation=corr,
                )

            return Snapshot(
                timestamp=Snapshot.now_iso(),
                db_type="postgres",
                db_path=self.dsn,
                table=table,
                analyze_time=analyze_time,
                row_count_est=row_count_est,
                columns=col_stats,
            )
        finally:
            conn.close()
