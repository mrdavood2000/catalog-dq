"""
hybrid.py — Hybrid method (Method C).

Algorithm:
  1. Run catalog detector -> flagged column set F.
  2. For each column c in F, run ONE targeted single-column aggregate query.
  3. Build a confirmed ColStats only for flagged columns.
  4. Re-evaluate the same rules on (s0, hybrid_snapshot) -> final verdict.

Cost: proportional to |F| scan queries, not N (total columns).
"""
from __future__ import annotations
import time
import math
from typing import Dict, List, Optional, Set

from .snapshot import Snapshot, ColStats
from .rules import ALL_RULES


def _targeted_scan_sqlite(
    conn, table: str, col: str, rules_fired: List[str], total_rows: int
) -> ColStats:
    """Run only the aggregates needed for the rules that fired."""
    null_frac       = None
    n_distinct_norm = None
    hist_min        = None
    hist_max        = None
    mcv_vals        = None
    mcv_freqs       = None
    correlation     = None

    rules = set(rules_fired)

    if "C1" in rules:
        cur = conn.execute(
            f'SELECT SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) * 1.0 / COUNT(*) FROM "{table}"'
        )
        null_frac = float(cur.fetchone()[0] or 0)

    if "K1" in rules:
        cur = conn.execute(
            f'SELECT COUNT(DISTINCT "{col}") * 1.0 / COUNT(*) FROM "{table}"'
        )
        n_distinct_norm = float(cur.fetchone()[0] or 0)

    if "V1" in rules:
        try:
            cur = conn.execute(
                f'SELECT MIN(CAST("{col}" AS REAL)), MAX(CAST("{col}" AS REAL)) '
                f'FROM "{table}" WHERE "{col}" IS NOT NULL'
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                hist_min, hist_max = float(row[0]), float(row[1])
        except Exception:
            pass

    if "V2" in rules:
        cur = conn.execute(
            f'SELECT "{col}", COUNT(*) AS cnt FROM "{table}" '
            f'WHERE "{col}" IS NOT NULL GROUP BY "{col}" ORDER BY cnt DESC LIMIT 10'
        )
        mcv_rows = cur.fetchall()
        if mcv_rows:
            mcv_vals  = [str(r[0]) for r in mcv_rows]
            mcv_freqs = [r[1] / total_rows for r in mcv_rows]

    if "K2" in rules:
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
                dx = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
                dy = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
                if dx > 0 and dy > 0:
                    correlation = num / (dx * dy)
        except Exception:
            pass

    return ColStats(
        null_frac=null_frac,
        n_distinct_norm=n_distinct_norm,
        histogram_min=hist_min,
        histogram_max=hist_max,
        mcv_vals=mcv_vals,
        mcv_freqs=mcv_freqs,
        correlation=correlation,
    )


def hybrid_detect(
    s0: Snapshot,
    st: Snapshot,
    thresholds: dict,
    catalog_result: Dict,
    reader,
    table: str,
) -> Dict:
    """
    Run the hybrid method.

    Parameters
    ----------
    s0, st         : catalog snapshots (baseline and current)
    thresholds     : from calibrate.compute_thresholds()
    catalog_result : output of detector.detect() — provides initial flags F
    reader         : SQLiteReader or PostgresReader
    table          : table name

    Returns the same schema as detector.detect() but with 'method': 'hybrid'.
    """
    t0 = time.perf_counter()

    catalog_flags: Dict[str, List[str]] = catalog_result.get("flags", {})
    flagged_cols: Set[str] = set(catalog_flags.keys())

    # F2 row_count_actual was already fetched by detect()
    row_count_actual: Optional[int] = catalog_result.get("row_count_actual")

    if not flagged_cols:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "flags": {},
            "flagged_cols": set(),
            "rule_hits": {r: [] for r in ALL_RULES},
            "elapsed_ms": elapsed_ms,
            "row_count_actual": row_count_actual,
            "method": "hybrid",
        }

    # Build hybrid snapshot: catalog stats + targeted scans for flagged cols
    hybrid_col_stats: Dict[str, ColStats] = dict(st.columns)

    is_sqlite = hasattr(reader, "db_path")

    # Open and close connection explicitly — context manager only commits/rolls back.
    conn = reader.connect()
    try:
        if is_sqlite:
            total_rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        else:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                total_rows = cur.fetchone()[0]

        for col, rules_fired in catalog_flags.items():
            if is_sqlite:
                hybrid_col_stats[col] = _targeted_scan_sqlite(
                    conn, table, col, rules_fired, total_rows
                )
            else:
                from .baseline import _pg_scan_col
                hybrid_col_stats[col] = _pg_scan_col(conn, table, col, total_rows)
    finally:
        conn.close()

    hybrid_snap = Snapshot(
        timestamp=Snapshot.now_iso(),
        db_type=st.db_type,
        db_path=st.db_path,
        table=table,
        analyze_time=st.analyze_time,
        row_count_est=st.row_count_est,
        columns=hybrid_col_stats,
    )

    # Re-evaluate rules using hybrid snapshot (flagged columns only)
    flags: Dict[str, List[str]] = {}
    rule_hits: Dict[str, List[str]] = {r: [] for r in ALL_RULES}

    for col in flagged_cols:
        fired: List[str] = []
        for rule_id, rule_fn in ALL_RULES.items():
            if rule_id == "F2":
                hit = rule_fn(s0, hybrid_snap, thresholds, col,
                              row_count_actual=row_count_actual)
            else:
                hit = rule_fn(s0, hybrid_snap, thresholds, col)
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
        "method": "hybrid",
    }
