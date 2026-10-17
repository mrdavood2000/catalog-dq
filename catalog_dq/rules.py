"""
rules.py — The seven inference rules (C1, V1, V2, K1, K2, F1, F2).

Each rule takes:
  - s0  : Snapshot (baseline — taken on clean data after calibration)
  - st  : Snapshot (current  — taken after potential fault injection)
  - thresholds : dict  (output of calibrate.compute_thresholds)
  - col : column name

Rules return True (flag raised) or False.
Rules that require statistics not available in the current backend
(e.g. V1 on SQLite) silently return False rather than crashing.
"""
from __future__ import annotations
import datetime
from typing import Optional

from .snapshot import Snapshot, ColStats


# ====================================================================== #
#  Jaccard distance helper (used by V2)                                   #
# ====================================================================== #

def jaccard_distance(a: Optional[list], b: Optional[list]) -> Optional[float]:
    """
    Jaccard distance between two value-lists treated as sets.
    Returns None when either list is None or empty.

    J_dist = 1 - |A∩B| / |A∪B|
    Bounded in [0, 1]; 0 = identical sets, 1 = disjoint sets.
    """
    if not a or not b:
        return None
    sa = set(str(v) for v in a)
    sb = set(str(v) for v in b)
    if not sa and not sb:
        return 0.0
    intersection = len(sa & sb)
    union = len(sa | sb)
    return 1.0 - intersection / union if union > 0 else 0.0


# ====================================================================== #
#  Individual rules                                                        #
# ====================================================================== #

def rule_C1(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    C1 — Completeness.
    Flag if null_frac increased by more than θ_null(col).
    """
    cs0: ColStats = s0.columns.get(col)
    cst: ColStats = st.columns.get(col)
    if cs0 is None or cst is None:
        return False
    if cs0.null_frac is None or cst.null_frac is None:
        return False
    delta = abs(cst.null_frac - cs0.null_frac)
    theta = thresholds.get(col, {}).get("null_frac", 0.0)
    return delta > theta


def rule_V1(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    V1 — Validity: range bounds.
    Flag if histogram min decreased or max increased beyond calibrated noise.

    Definition:
      min_val(Sₜ) < min_val(S₀) - θ_min(col)   OR
      max_val(Sₜ) > max_val(S₀) + θ_max(col)

    Returns False if histogram stats are unavailable (SQLite catalog) or
    if any value is non-finite (protects against degenerate TEXT columns).
    """
    cs0 = s0.columns.get(col)
    cst = st.columns.get(col)
    if cs0 is None or cst is None:
        return False
    if cs0.histogram_min is None or cst.histogram_min is None:
        return False   # not available (SQLite catalog)
    if cs0.histogram_max is None or cst.histogram_max is None:
        return False
    import math
    # Guard against NaN/Inf that can arise from failed float conversions
    vals = [cs0.histogram_min, cs0.histogram_max,
            cst.histogram_min, cst.histogram_max]
    if any(not math.isfinite(v) for v in vals):
        return False
    theta_min = thresholds.get(col, {}).get("histogram_min", 0.0)
    theta_max = thresholds.get(col, {}).get("histogram_max", 0.0)
    flag_min = cst.histogram_min < cs0.histogram_min - theta_min
    flag_max = cst.histogram_max > cs0.histogram_max + theta_max
    return flag_min or flag_max


def rule_V2(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    V2 — Validity: MCV pattern shift.
    Flag if Jaccard distance between MCV sets exceeds θ_mcv(col).
    """
    cs0 = s0.columns.get(col)
    cst = st.columns.get(col)
    if cs0 is None or cst is None:
        return False
    jd = jaccard_distance(cs0.mcv_vals, cst.mcv_vals)
    if jd is None:
        return False
    theta = thresholds.get(col, {}).get("mcv_jaccard", 0.0)
    return jd > theta


def rule_K1(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    K1 — Consistency: distinct count.
    Flag if normalised n_distinct changed by more than θ_ndist(col).
    """
    cs0 = s0.columns.get(col)
    cst = st.columns.get(col)
    if cs0 is None or cst is None:
        return False
    if cs0.n_distinct_norm is None or cst.n_distinct_norm is None:
        return False
    delta = abs(cst.n_distinct_norm - cs0.n_distinct_norm)
    theta = thresholds.get(col, {}).get("n_distinct_norm", 0.0)
    return delta > theta


def rule_K2(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    K2 — Consistency: spatial correlation (internal consistency signal).
    Flag if Pearson correlation changed by more than θ_corr(col).
    """
    cs0 = s0.columns.get(col)
    cst = st.columns.get(col)
    if cs0 is None or cst is None:
        return False
    if cs0.correlation is None or cst.correlation is None:
        return False
    delta = abs(cst.correlation - cs0.correlation)
    theta = thresholds.get(col, {}).get("correlation", 0.0)
    return delta > theta


def rule_F1(s0: Snapshot, st: Snapshot, thresholds: dict, col: str) -> bool:
    """
    F1 — Freshness: analyze age.
    Flag if time since last ANALYZE exceeds θ_age hours.

    For PostgreSQL: analyze_time comes from pg_stat_all_tables.last_analyze,
    which is a timezone-aware datetime.  psycopg2 returns it as
    datetime(tzinfo=UTC); we store it as ISO-8601 with offset, e.g.
    '2026-07-07T12:34:56+00:00'.  datetime.datetime.fromisoformat() in
    Python ≥ 3.7 parses that correctly, yielding a tz-aware object.

    BUG FIX (v2): replaced datetime.utcnow() (naive) with
    datetime.now(timezone.utc) (aware) so subtraction always works
    regardless of whether analyze_time carries a UTC offset.
    """
    if st.analyze_time is None:
        return False
    try:
        analyzed_at = datetime.datetime.fromisoformat(st.analyze_time)
    except Exception:
        return False

    # Normalise: if the stored timestamp is naive (SQLite path, which
    # records datetime.utcnow().isoformat()), treat it as UTC.
    if analyzed_at.tzinfo is None:
        analyzed_at = analyzed_at.replace(tzinfo=datetime.timezone.utc)

    now = datetime.datetime.now(datetime.timezone.utc)
    age_hours = (now - analyzed_at).total_seconds() / 3600.0
    theta = thresholds.get("__table__", {}).get("age_hours", 24.0)
    return age_hours > theta


def rule_F2(s0: Snapshot, st: Snapshot, thresholds: dict, col: str,
            row_count_actual: Optional[int] = None) -> bool:
    """
    F2 — Freshness: row count drift.
    Flag if COUNT(*) differs from row_count_est by more than θ_drift fraction.

    Definition:
      |row_count_actual - row_count_est(Sₜ)| / row_count_est(Sₜ) > θ_drift
    """
    if row_count_actual is None:
        return False
    est = st.row_count_est
    if est is None or est == 0:
        return False
    drift = abs(row_count_actual - est) / est
    theta = thresholds.get("__table__", {}).get("drift_frac", 0.05)
    return drift > theta


# ====================================================================== #
#  Full rule set                                                           #
# ====================================================================== #

ALL_RULES = {
    "C1": rule_C1,
    "V1": rule_V1,
    "V2": rule_V2,
    "K1": rule_K1,
    "K2": rule_K2,
    "F1": rule_F1,
    "F2": rule_F2,
}
