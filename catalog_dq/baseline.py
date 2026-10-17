"""
baseline.py — Direct scan baseline (Method B).

Computes the same statistics as the catalog reader but by scanning all
column values in the table.  Uses the same seven rules and thresholds.

The only difference from the catalog method is HOW statistics are gathered:
  - null_frac      : COUNT(col IS NULL) / COUNT(*)    [same as SQLite catalog]
  - n_distinct_norm: COUNT(DISTINCT col) / COUNT(*)   [same as SQLite catalog]
  - histogram_min  : MIN(col)
  - histogram_max  : MAX(col)
  - mcv_vals       : top-10 most common values via GROUP BY
  - correlation    : Pearson correlation between value and row-number
  - row_count      : COUNT(*)

All queries read column values (not just catalog metadata), so the scan
scales with table size.  Time is the primary measured cost.
"""
from __future__ import annotations
import math
import time
from typing import Optional, List, Dict, Any

from .snapshot import Snapshot, ColStats
from .rules import ALL_RULES, jaccard_distance


# ------------------------------------------------------------------ #
# SQLite scan helpers                                                  #
# ------------------------------------------------------------------ #

def _sqlite_scan_col(conn, table: str, col: str, total_rows: int) -> ColStats:
    """Full column scan for one column in SQLite."""
    # null_frac
    cur = conn.execute(
        f'SELECT SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) * 1.0 / COUNT(*) FROM "{table}"'
    )
    null_frac = float(cur.fetchone()[0] or 0.0)

    # n_distinct_norm
    cur = conn.execute(
        f'SELECT COUNT(DISTINCT "{col}") * 1.0 / COUNT(*) FROM "{table}"'
    )
    n_dist = float(cur.fetchone()[0] or 0.0)

    # min / max
    cur = conn.execute(f'SELECT MIN(CAST("{col}" AS REAL)), MAX(CAST("{col}" AS REAL)) FROM "{table}" WHERE "{col}" IS NOT NULL')
    row = cur.fetchone()
    hist_min = hist_max = None
    if row and row[0] is not None:
        try:
            hist_min = float(row[0])
            hist_max = float(row[1])
        except (TypeError, ValueError):
            pass

    # MCV: top-10 most common values
    cur = conn.execute(
        f'SELECT "{col}", COUNT(*) AS cnt FROM "{table}" '
        f'WHERE "{col}" IS NOT NULL GROUP BY "{col}" ORDER BY cnt DESC LIMIT 10'
    )
    mcv_rows = cur.fetchall()
    mcv_vals  = [str(r[0]) for r in mcv_rows] if mcv_rows else None
    mcv_freqs = [r[1] / total_rows for r in mcv_rows] if mcv_rows else None

    # Pearson correlation between 1-based row position and column value (numeric only)
    correlation = None
    if hist_min is not None:
        try:
            cur = conn.execute(
                f'SELECT rowid, CAST("{col}" AS REAL) FROM "{table}" WHERE "{col}" IS NOT NULL'
            )
            rows = cur.fetchall()
            if len(rows) >= 2:
                n = len(rows)
                xs = [r[0] for r in rows]
                ys = [r[1] for r in rows]
                mean_x = sum(xs) / n
                mean_y = sum(ys) / n
                num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
                denom_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
                denom_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
                if denom_x > 0 and denom_y > 0:
                    correlation = num / (denom_x * denom_y)
        except Exception:
            pass

    return ColStats(
        null_frac=null_frac,
        n_distinct_norm=n_dist,
        histogram_min=hist_min,
        histogram_max=hist_max,
        mcv_vals=mcv_vals,
        mcv_freqs=mcv_freqs,
        correlation=correlation,
    )


def _build_scan_snapshot_sqlite(
    reader, table: str, analyze_time: Optional[str] = None
) -> Snapshot:
    with reader.connect() as conn:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        cols = [r[1] for r in cur.fetchall()]
        cur = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
        total_rows = cur.fetchone()[0]

        col_stats = {col: _sqlite_scan_col(conn, table, col, total_rows) for col in cols}

    return Snapshot(
        timestamp=Snapshot.now_iso(),
        db_type="sqlite",
        db_path=reader.db_path,
        table=table,
        analyze_time=analyze_time,
        row_count_est=total_rows,   # exact for scan
        columns=col_stats,
    )


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #

def baseline_scan(
    reader, table: str, s0_scan: Snapshot, st_scan: Snapshot, thresholds: dict
) -> Dict:
    """
    Apply all seven rules to two SCAN-derived snapshots and return result dict.

    s0_scan : scan snapshot of clean table (baseline)
    st_scan : scan snapshot of potentially-faulted table (current)
    """
    from .detector import detect
    t0 = time.perf_counter()

    cols = list(st_scan.columns.keys())
    flags: Dict[str, List[str]] = {}
    rule_hits: Dict[str, List[str]] = {r: [] for r in ALL_RULES}

    row_count_actual = st_scan.row_count_est  # exact for scan

    for col in cols:
        fired = []
        for rule_id, rule_fn in ALL_RULES.items():
            if rule_id == "F2":
                hit = rule_fn(s0_scan, st_scan, thresholds, col,
                              row_count_actual=row_count_actual)
            else:
                hit = rule_fn(s0_scan, st_scan, thresholds, col)
            if hit:
                fired.append(rule_id)
                rule_hits[rule_id].append(col)
        if fired:
            flags[col] = fired

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "flags": flags,
        "flagged_cols": set(flags.keys()),
        "rule_hits": rule_hits,
        "elapsed_ms": elapsed_ms,
        "row_count_actual": row_count_actual,
    }


def build_scan_snapshot(reader, table: str, analyze_time: Optional[str] = None) -> Snapshot:
    """Build a full-scan snapshot.  Works for SQLite (PG support via same API)."""
    db_type = getattr(reader, "db_path", None)
    # Dispatch on reader type
    if hasattr(reader, "db_path"):
        return _build_scan_snapshot_sqlite(reader, table, analyze_time)
    else:
        return _build_scan_snapshot_pg(reader, table, analyze_time)


def _build_scan_snapshot_pg(reader, table: str, analyze_time=None) -> Snapshot:
    """PostgreSQL full-scan snapshot."""
    conn = reader.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            total_rows = cur.fetchone()[0]

        # Get column list
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND table_schema='public' ORDER BY ordinal_position",
                (table,)
            )
            cols = [r[0] for r in cur.fetchall()]

        col_stats = {}
        for col in cols:
            col_stats[col] = _pg_scan_col(conn, table, col, total_rows)

        if analyze_time is None:
            analyze_time = reader._get_analyze_time(conn, table)

    finally:
        conn.close()

    return Snapshot(
        timestamp=Snapshot.now_iso(),
        db_type="postgres",
        db_path=reader.dsn,
        table=table,
        analyze_time=analyze_time,
        row_count_est=total_rows,
        columns=col_stats,
    )


def _pg_scan_col(conn, table: str, col: str, total_rows: int) -> ColStats:
    import psycopg2.extras

    with conn.cursor() as cur:
        cur.execute(
            f'SELECT SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END)::float / COUNT(*),'
            f'       COUNT(DISTINCT "{col}")::float / COUNT(*) FROM "{table}"'
        )
        row = cur.fetchone()
        null_frac = float(row[0] or 0)
        n_dist = float(row[1] or 0)

    hist_min = hist_max = None
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT MIN("{col}"::float), MAX("{col}"::float) FROM "{table}" WHERE "{col}" IS NOT NULL')
            row = cur.fetchone()
            if row and row[0] is not None:
                hist_min, hist_max = float(row[0]), float(row[1])
    except Exception:
        conn.rollback()

    mcv_vals = mcv_freqs = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT "{col}"::text, COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL '
                f'GROUP BY "{col}" ORDER BY COUNT(*) DESC LIMIT 10'
            )
            rows = cur.fetchall()
            mcv_vals  = [r[0] for r in rows]
            mcv_freqs = [r[1] / total_rows for r in rows]
    except Exception:
        conn.rollback()

    correlation = None
    if hist_min is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT CORR(row_number() OVER ()::float, "{col}"::float) FROM "{table}"'
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    correlation = float(row[0])
        except Exception:
            conn.rollback()

    return ColStats(
        null_frac=null_frac,
        n_distinct_norm=n_dist,
        histogram_min=hist_min,
        histogram_max=hist_max,
        mcv_vals=mcv_vals,
        mcv_freqs=mcv_freqs,
        correlation=correlation,
    )
