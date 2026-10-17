"""
calibrate.py — Per-column threshold calibration.

Algorithm:
  1. Start with clean data (no faults injected).
  2. Run ANALYZE K times (default K=5).
  3. After each ANALYZE, take a Snapshot.
  4. For each (column, statistic) pair, compute std σ over the K snapshots.
  5. Set threshold θ = z × σ  (default z = 2.0).
  6. Floor each threshold at a per-stat minimum to avoid zero thresholds
     on small/stable tables.

Output: a nested dict
  thresholds[col][stat_key] = float
  thresholds["__table__"]["age_hours"] = float     (F1)
  thresholds["__table__"]["drift_frac"] = float    (F2)

The two table-level thresholds (F1, F2) are not calibrated from ANALYZE
variance; they are policy parameters set by the user.
"""
from __future__ import annotations
import math
import statistics
from typing import Callable, List, Optional

from .snapshot import Snapshot


# ------------------------------------------------------------------ #
# Per-statistic minimum thresholds (floor)                            #
# ------------------------------------------------------------------ #
STAT_FLOORS: dict[str, float] = {
    "null_frac":       0.005,   # 0.5 percentage-point floor
    "n_distinct_norm": 0.02,    # 2 % of distinct-fraction
    "histogram_min":   0.0,     # numeric; calibrated naturally
    "histogram_max":   0.0,
    "mcv_jaccard":     0.05,    # 5% Jaccard floor
    "correlation":     0.05,
}

# Default table-level policy thresholds (not calibrated from data)
DEFAULT_AGE_HOURS:  float = 24.0
DEFAULT_DRIFT_FRAC: float = 0.05   # 5% row-count drift


def _safe_stdev(values: List[Optional[float]]) -> float:
    """Population std of a list; returns 0.0 if <2 non-None values."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return 0.0
    try:
        return statistics.pstdev(clean)
    except Exception:
        return 0.0


def compute_thresholds(
    snapshots: List[Snapshot],
    z: float = 2.0,
    age_hours: float = DEFAULT_AGE_HOURS,
    drift_frac: float = DEFAULT_DRIFT_FRAC,
    floors: Optional[dict] = None,
) -> dict:
    """
    Derive per-column thresholds from K calibration snapshots.

    Parameters
    ----------
    snapshots : list of Snapshot
        K snapshots taken on clean data after K separate ANALYZE runs.
    z : float
        Multiplier for std (z = 2.0 ≈ 95th percentile of noise).
    age_hours : float
        F1 threshold: flag if last_analyze is older than this.
    drift_frac : float
        F2 threshold: flag if |COUNT(*) - reltuples| / reltuples > this.
    floors : dict or None
        Override per-stat minimum thresholds; defaults to STAT_FLOORS.

    Returns
    -------
    dict  — thresholds[col][stat_key] = float
    """
    if not snapshots:
        raise ValueError("At least one snapshot is required for calibration.")

    _floors = {**STAT_FLOORS, **(floors or {})}
    all_cols = set()
    for snap in snapshots:
        all_cols.update(snap.columns.keys())

    thresholds: dict = {"__table__": {
        "age_hours":  age_hours,
        "drift_frac": drift_frac,
    }}

    for col in all_cols:
        col_th: dict = {}

        # null_frac
        vals = [s.columns[col].null_frac for s in snapshots if col in s.columns]
        sigma = _safe_stdev(vals)
        col_th["null_frac"] = max(z * sigma, _floors["null_frac"])

        # n_distinct_norm
        vals = [s.columns[col].n_distinct_norm for s in snapshots if col in s.columns]
        sigma = _safe_stdev(vals)
        col_th["n_distinct_norm"] = max(z * sigma, _floors["n_distinct_norm"])

        # histogram_min
        vals = [s.columns[col].histogram_min for s in snapshots if col in s.columns]
        sigma = _safe_stdev(vals)
        col_th["histogram_min"] = max(z * sigma, _floors["histogram_min"])

        # histogram_max
        vals = [s.columns[col].histogram_max for s in snapshots if col in s.columns]
        sigma = _safe_stdev(vals)
        col_th["histogram_max"] = max(z * sigma, _floors["histogram_max"])

        # mcv_jaccard — calibrate from pairwise Jaccard variation between snapshots
        from .rules import jaccard_distance
        jd_vals: List[Optional[float]] = []
        mcv_series = [
            s.columns[col].mcv_vals for s in snapshots if col in s.columns
        ]
        for i in range(len(mcv_series)):
            for j in range(i + 1, len(mcv_series)):
                jd = jaccard_distance(mcv_series[i], mcv_series[j])
                if jd is not None:
                    jd_vals.append(jd)
        sigma = _safe_stdev(jd_vals)
        col_th["mcv_jaccard"] = max(z * sigma, _floors["mcv_jaccard"])

        # correlation
        vals = [s.columns[col].correlation for s in snapshots if col in s.columns]
        sigma = _safe_stdev(vals)
        col_th["correlation"] = max(z * sigma, _floors["correlation"])

        thresholds[col] = col_th

    return thresholds


def run_calibration(
    reader,
    table: str,
    analyze_fn: Callable,
    K: int = 5,
    z: float = 2.0,
    age_hours: float = DEFAULT_AGE_HOURS,
    drift_frac: float = DEFAULT_DRIFT_FRAC,
) -> tuple[dict, List[Snapshot]]:
    """
    High-level calibration runner.

    Parameters
    ----------
    reader : SQLiteReader | PostgresReader
    table  : table name
    analyze_fn : callable() — runs ANALYZE on the table and records analyze_time
    K      : number of ANALYZE repetitions
    z, age_hours, drift_frac : forwarded to compute_thresholds

    Returns
    -------
    (thresholds dict, list of K calibration snapshots)
    """
    snaps: List[Snapshot] = []
    for _ in range(K):
        analyze_fn()
        snap = reader.read_snapshot(table)
        snaps.append(snap)

    thresholds = compute_thresholds(
        snaps, z=z, age_hours=age_hours, drift_frac=drift_frac
    )
    return thresholds, snaps
