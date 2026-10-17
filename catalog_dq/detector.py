"""
detector.py — Catalog-only detector (Method A).

Applies all seven rules to (s0, st) and returns a structured result dict.
"""
from __future__ import annotations
import time
from typing import Optional, Dict, List

from .snapshot import Snapshot
from .rules import ALL_RULES


def detect(
    s0: Snapshot,
    st: Snapshot,
    thresholds: dict,
    reader=None,
    table: Optional[str] = None,
) -> Dict:
    """
    Run all seven catalog rules on (s0, st).

    Parameters
    ----------
    s0         : baseline Snapshot (clean reference)
    st         : current Snapshot (after potential fault)
    thresholds : output of calibrate.compute_thresholds()
    reader     : reader instance needed for F2 COUNT(*)
    table      : table name (needed when reader is provided)

    Returns
    -------
    {
      "flags":            {col: [rule_ids]},
      "flagged_cols":     set of col names,
      "rule_hits":        {rule_id: [col names]},
      "elapsed_ms":       float,   # total detection time (includes F2 COUNT(*))
      "elapsed_f2_ms":    float,   # F2 COUNT(*) time only (reported separately, D6)
      "row_count_actual": int or None,
    }
    """
    t0 = time.perf_counter()

    # ── F2 sub-timer: COUNT(*) is a table scan; report its latency separately
    # so that catalog latency can be quoted both with and without F2.
    t_f2 = time.perf_counter()
    row_count_actual: Optional[int] = None
    if reader is not None and table is not None:
        conn = None
        try:
            conn = reader.connect()
            row_count_actual = reader.get_actual_row_count(conn, table)
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    elapsed_f2_ms = (time.perf_counter() - t_f2) * 1000.0

    flags: Dict[str, List[str]] = {}
    rule_hits: Dict[str, List[str]] = {r: [] for r in ALL_RULES}

    for col in list(st.columns.keys()):
        fired: List[str] = []
        for rule_id, rule_fn in ALL_RULES.items():
            if rule_id == "F2":
                hit = rule_fn(s0, st, thresholds, col,
                              row_count_actual=row_count_actual)
            else:
                hit = rule_fn(s0, st, thresholds, col)
            if hit:
                fired.append(rule_id)
                rule_hits[rule_id].append(col)
        if fired:
            flags[col] = fired

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "flags":            flags,
        "flagged_cols":     set(flags.keys()),
        "rule_hits":        rule_hits,
        "elapsed_ms":       elapsed_ms,
        "elapsed_f2_ms":    elapsed_f2_ms,
        "row_count_actual": row_count_actual,
    }
