"""
catalog-dq  —  Lightweight Statistical Approach for Automatic Detection
               of Data Quality Issues in Relational Tables.

Seven inference rules: C1, V1, V2, K1, K2, F1, F2
Three detection methods: catalog-only, direct scan (baseline), hybrid
"""
__version__ = "0.1.0"
__author__  = "Mohammadreza Davoodabadi"

from .snapshot  import Snapshot, ColStats
from .rules     import ALL_RULES, jaccard_distance
from .calibrate import compute_thresholds, run_calibration
from .detector  import detect
from .baseline  import build_scan_snapshot, baseline_scan
from .hybrid    import hybrid_detect
from .injector  import inject_fault_sqlite, FAULT_TYPES
from .harness   import run_all, one_quality_run, evaluate

__all__ = [
    "Snapshot", "ColStats",
    "ALL_RULES", "jaccard_distance",
    "compute_thresholds", "run_calibration",
    "detect",
    "build_scan_snapshot", "baseline_scan",
    "hybrid_detect",
    "inject_fault_sqlite", "FAULT_TYPES",
    "run_all", "one_quality_run", "evaluate",
]
