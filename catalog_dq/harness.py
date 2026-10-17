"""
harness.py — Full SQLite experiment harness.

Quality runs:  5 datasets × 4 fault types × 3 severities × 3 methods × 20 reps
               = 3,600 runs

Efficiency design (matches harness_pg.py):
  For each dataset:
    1. Load clean CSV → temp "clean" SQLite DB  (once)
    2. Calibration: K times { copy clean → work DB, ANALYZE, snapshot }
    3. Compute thresholds once, reuse for all 720 runs of this dataset
    4. Take S₀ catalog and scan snapshots from one final fresh work copy
    Per run:
      5. SQLite backup-API copy of clean DB → work DB  (~0 ms for small DBs)
      6. Inject fault
      7. ANALYZE (if not stale)
      8. Take Sₜ
      9. Detect → evaluate → write CSV row

Results integrity:
  * Failed runs recorded as status='error' with blank metric fields.
  * Errored rows excluded from summary means and CIs.
  * RuntimeError raised at end if any errors occurred (caller exits nonzero).

Column normalisation:
  load_csv_to_sqlite() applies normalize_col_name() to every header.
  FAULT_COLS uses normalised names.

Fault column selection:
  hospital : zipcode        — 5-digit integer (100% numeric)
  flights  : tuple_id       — only numeric column in dataset
  beers    : abv            — alcohol-by-volume, float
  rayyan   : article_jvolumn — journal volume, integer
  movies_1 : ratingvalue    — IMDb rating, float (normalised from 'RatingValue')
"""
from __future__ import annotations
import csv
import math
import os
import random
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from .calibrate import compute_thresholds, run_calibration
from .detector import detect
from .baseline import build_scan_snapshot, baseline_scan
from .hybrid import hybrid_detect
from .injector import inject_fault_sqlite, FAULT_TYPES
from .readers.sqlite_reader import SQLiteReader
from .snapshot import Snapshot


# ------------------------------------------------------------------ #
# Column name normalisation                                            #
# ------------------------------------------------------------------ #

def normalize_col_name(s: str) -> str:
    """Lowercase snake_case — applied to every CSV header at load time."""
    return s.strip().lower().replace(" ", "_").replace("-", "_")


# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

SEVERITIES = [0.01, 0.05, 0.20]
METHODS    = ["catalog", "scan", "hybrid"]

FAULT_COLS = {
    "toy":      "lord",
    "hospital": "zipcode",
    "flights":  "tuple_id",
    "beers":    "abv",
    "rayyan":   "article_jvolumn",
    "movies_1": "ratingvalue",
}

SYNTHETIC_SIZES = [1_000_000, 5_000_000, 10_000_000]


# ------------------------------------------------------------------ #
# Evaluation helpers                                                   #
# ------------------------------------------------------------------ #

def evaluate(
    flagged_cols: Set[str],
    faulted_cols: Set[str],
    all_cols: List[str],
) -> Dict:
    tp = len(flagged_cols & faulted_cols)
    fp = len(flagged_cols - faulted_cols)
    fn = len(faulted_cols - flagged_cols)
    tn = len(set(all_cols) - flagged_cols - faulted_cols)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = (2 * precision * recall / (precision + recall)
                 if (precision + recall) > 0 else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def bootstrap_ci(
    values: List[float],
    confidence: float = 0.95,
    n_boot: int = 2000,
    seed: int = 0,
) -> Tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n   = len(values)
    means = [sum(values[rng.randrange(n)] for _ in range(n)) / n
             for _ in range(n_boot)]
    means.sort()
    alpha  = 1 - confidence
    lo_idx = int(math.floor(alpha / 2 * n_boot))
    hi_idx = int(math.ceil((1 - alpha / 2) * n_boot)) - 1
    return (means[lo_idx], means[hi_idx])


def t_ci(
    values: List[float],
    confidence: float = 0.95,
) -> Tuple[float, float]:
    if len(values) < 2:
        return (float("nan"), float("nan"))
    try:
        import scipy.stats as _st
        n = len(values)
        m = statistics.mean(values)
        s = statistics.stdev(values)
        t = _st.t.ppf((1 + confidence) / 2, df=n - 1)
        return (m - t * s / math.sqrt(n), m + t * s / math.sqrt(n))
    except ImportError:
        n = len(values)
        m = statistics.mean(values)
        s = statistics.stdev(values)
        margin = 1.96 * s / math.sqrt(n)
        return (m - margin, m + margin)


# ------------------------------------------------------------------ #
# DB helpers                                                           #
# ------------------------------------------------------------------ #

def load_csv_to_sqlite(
    csv_path: str,
    db_path: str,
    table: str = "data",
) -> List[str]:
    """
    Load CSV into SQLite; normalise column names.
    Returns list of normalised column names.
    """
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    df = df.where(df != "", other=None)
    df.columns = [normalize_col_name(c) for c in df.columns]
    conn = sqlite3.connect(db_path)
    df.to_sql(table, conn, if_exists="replace", index=False)
    conn.execute("ANALYZE")
    conn.close()
    return list(df.columns)


def _copy_sqlite_db(src: str, dst: str) -> None:
    """Fast DB copy using SQLite backup API (in-process, no shell)."""
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()


def get_all_columns(db_path: str, table: str = "data") -> List[str]:
    conn = sqlite3.connect(db_path)
    cur  = conn.execute(f'PRAGMA table_info("{table}")')
    cols = [r[1] for r in cur.fetchall()]
    conn.close()
    return cols


# ------------------------------------------------------------------ #
# CSV schema                                                           #
# ------------------------------------------------------------------ #

QUALITY_FIELDNAMES = [
    "status", "error_msg",
    "dataset", "fault_type", "fault_col", "severity", "method", "rep",
    "tp", "fp", "fn", "tn", "precision", "recall", "f1",
    "elapsed_catalog_ms", "elapsed_f2_ms", "elapsed_total_ms", "n_injected",
    "flagged_cols", "rules_fired",
]

SUMMARY_FIELDNAMES = [
    "dataset", "fault_type", "severity", "method",
    "mean_f1", "boot_ci_lo", "boot_ci_hi", "t_ci_lo", "t_ci_hi",
    "mean_precision", "mean_recall",
    "mean_elapsed_catalog_ms", "mean_elapsed_f2_ms", "mean_elapsed_total_ms",
    "n_reps", "n_errors",
]


def _error_row(
    dataset: str, fault_type: str, fault_col: str,
    severity: float, method: str, rep: int,
    exc: Exception,
) -> Dict:
    return {
        "status": "error", "error_msg": repr(exc)[:500],
        "dataset": dataset, "fault_type": fault_type,
        "fault_col": fault_col, "severity": severity,
        "method": method, "rep": rep,
        "tp": "", "fp": "", "fn": "", "tn": "",
        "precision": "", "recall": "", "f1": "",
        "elapsed_catalog_ms": "", "elapsed_f2_ms": "", "elapsed_total_ms": "",
        "n_injected": "", "flagged_cols": "", "rules_fired": "",
    }


# ------------------------------------------------------------------ #
# Single-run helper (used by smoke_test and one-off calls)            #
# ------------------------------------------------------------------ #

def one_quality_run(
    clean_csv: str,
    dataset_name: str,
    fault_type: str,
    fault_col: str,
    severity: float,
    method: str,
    rep: int,
    K: int = 5,
    z: float = 2.0,
    seed_base: int = 0,
) -> Dict:
    """Single isolated run (creates its own temp DB; calibrates K times)."""
    seed = seed_base + rep
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        db_path = tf.name
    try:
        all_cols = load_csv_to_sqlite(clean_csv, db_path)
        table  = "data"
        reader = SQLiteReader(db_path)

        def analyze_fn():
            with reader.connect() as conn:
                conn.execute(f"ANALYZE {table}")
            reader.record_analyze_time()

        thresholds, _ = run_calibration(reader, table, analyze_fn, K=K, z=z)
        s0 = reader.read_snapshot(table)

        with reader.connect() as conn:
            inj_meta = inject_fault_sqlite(
                conn, table, fault_type, fault_col, severity, seed=seed
            )

        if fault_type != "stale":
            with reader.connect() as conn:
                conn.execute(f"ANALYZE {table}")
            reader.record_analyze_time()

        st = reader.read_snapshot(table)
        t0 = time.perf_counter()

        if method == "catalog":
            result = detect(s0, st, thresholds, reader=reader, table=table)
        elif method == "scan":
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf2:
                db_clean = tf2.name
            try:
                load_csv_to_sqlite(clean_csv, db_clean, table)
                s0_scan = build_scan_snapshot(SQLiteReader(db_clean), table)
            finally:
                try: os.unlink(db_clean)
                except: pass
            st_scan = build_scan_snapshot(reader, table)
            result  = baseline_scan(reader, table, s0_scan, st_scan, thresholds)
        elif method == "hybrid":
            cat_result = detect(s0, st, thresholds, reader=reader, table=table)
            result     = hybrid_detect(s0, st, thresholds, cat_result, reader, table)
        else:
            raise ValueError(f"Unknown method: {method!r}")

        elapsed_ms   = (time.perf_counter() - t0) * 1000.0
        faulted_cols = {fault_col} if fault_type != "stale" else set(all_cols)
        ev = evaluate(result["flagged_cols"], faulted_cols, all_cols)

        return {
            "status": "ok", "error_msg": "",
            "dataset": dataset_name, "fault_type": fault_type,
            "fault_col": fault_col, "severity": severity,
            "method": method, "rep": rep,
            "tp": ev["tp"], "fp": ev["fp"], "fn": ev["fn"], "tn": ev["tn"],
            "precision": ev["precision"], "recall": ev["recall"], "f1": ev["f1"],
            "elapsed_catalog_ms": result.get("elapsed_ms", 0.0),
            "elapsed_total_ms":   elapsed_ms,
            "n_injected": inj_meta.get("n_injected", 0),
            "flagged_cols": "|".join(sorted(result["flagged_cols"])),
            "rules_fired": "|".join(
                f"{c}:{','.join(rs)}"
                for c, rs in sorted(result.get("flags", {}).items())
            ),
        }
    finally:
        try: os.unlink(db_path)
        except: pass


# ------------------------------------------------------------------ #
# Efficient per-dataset runner                                         #
# ------------------------------------------------------------------ #

def _run_dataset(
    ds_name: str,
    csv_path: str,
    fault_col: str,
    writer: csv.DictWriter,
    qf,
    reps: int,
    K: int,
    z: float,
    verbose: bool,
) -> int:
    """
    Run all 720 cells for one dataset.
    Returns number of errors encountered.
    """
    table = "data"

    # 1. Load clean CSV once
    with tempfile.NamedTemporaryFile(suffix="_clean.sqlite", delete=False) as tf:
        db_clean = tf.name
    try:
        all_cols = load_csv_to_sqlite(csv_path, db_clean)
        if verbose:
            print(f"  Loaded {len(all_cols)} cols, {get_all_columns(db_clean, table) and ''}"
                  f"CSV→SQLite OK")

        # 2. Calibrate K times (copy clean → work, ANALYZE, snapshot)
        calib_snaps = []
        for _ in range(K):
            with tempfile.NamedTemporaryFile(suffix="_cal.sqlite", delete=False) as tf2:
                db_cal = tf2.name
            try:
                _copy_sqlite_db(db_clean, db_cal)
                reader_cal = SQLiteReader(db_cal)
                conn = sqlite3.connect(db_cal)
                conn.execute("ANALYZE")
                conn.close()
                reader_cal.record_analyze_time()
                calib_snaps.append(reader_cal.read_snapshot(table))
            finally:
                try: os.unlink(db_cal)
                except: pass

        thresholds = compute_thresholds(calib_snaps, z=z)

        # 3. Baseline S₀ (one more fresh copy)
        with tempfile.NamedTemporaryFile(suffix="_s0.sqlite", delete=False) as tf3:
            db_s0 = tf3.name
        try:
            _copy_sqlite_db(db_clean, db_s0)
            reader_s0 = SQLiteReader(db_s0)
            conn = sqlite3.connect(db_s0)
            conn.execute("ANALYZE")
            conn.close()
            reader_s0.record_analyze_time()
            s0      = reader_s0.read_snapshot(table)
            s0_scan = build_scan_snapshot(reader_s0, table)
        finally:
            try: os.unlink(db_s0)
            except: pass

        # 4. Experiment loop
        total = len(FAULT_TYPES) * len(SEVERITIES) * len(METHODS) * reps
        done  = 0
        n_err = 0

        for ft in FAULT_TYPES:
            for sev in SEVERITIES:
                for meth in METHODS:
                    for rep in range(reps):
                        seed = 1000 * (list(FAULT_TYPES).index(ft) + 1) + rep
                        with tempfile.NamedTemporaryFile(
                            suffix="_work.sqlite", delete=False
                        ) as tf4:
                            db_work = tf4.name
                        try:
                            _copy_sqlite_db(db_clean, db_work)
                            reader_work = SQLiteReader(db_work)

                            # Inject
                            conn = sqlite3.connect(db_work)
                            inj_meta = inject_fault_sqlite(
                                conn, table, ft, fault_col, sev, seed=seed
                            )
                            conn.close()
                            # Guard: range injection returning 0 means the column
                            # is non-numeric — that is a configuration error.
                            if ft == "range" and inj_meta.get("n_injected", 0) == 0:
                                raise ValueError(
                                    f"range injection returned n_injected=0 for "
                                    f"column '{fault_col}' in dataset '{ds_name}'. "
                                    f"Check FAULT_COLS or the CSV column type."
                                )

                            # ANALYZE
                            if ft != "stale":
                                conn = sqlite3.connect(db_work)
                                conn.execute("ANALYZE")
                                conn.close()
                                reader_work.record_analyze_time()

                            st = reader_work.read_snapshot(table)

                            # Detect
                            t_start = time.perf_counter()
                            if meth == "catalog":
                                result = detect(
                                    s0, st, thresholds,
                                    reader=reader_work, table=table
                                )
                            elif meth == "scan":
                                st_scan = build_scan_snapshot(reader_work, table)
                                result  = baseline_scan(
                                    reader_work, table, s0_scan, st_scan, thresholds
                                )
                            elif meth == "hybrid":
                                cat_res = detect(
                                    s0, st, thresholds,
                                    reader=reader_work, table=table
                                )
                                result = hybrid_detect(
                                    s0, st, thresholds, cat_res, reader_work, table
                                )
                            else:
                                raise ValueError(f"Unknown method: {meth!r}")

                            elapsed_ms = (time.perf_counter() - t_start) * 1000.0

                            faulted_cols = (
                                {fault_col} if ft != "stale" else set(all_cols)
                            )
                            ev = evaluate(
                                result["flagged_cols"], faulted_cols, all_cols
                            )

                            row = {
                                "status": "ok", "error_msg": "",
                                "dataset": ds_name, "fault_type": ft,
                                "fault_col": fault_col, "severity": sev,
                                "method": meth, "rep": rep,
                                "tp": ev["tp"], "fp": ev["fp"],
                                "fn": ev["fn"], "tn": ev["tn"],
                                "precision": ev["precision"],
                                "recall":    ev["recall"],
                                "f1":        ev["f1"],
                                "elapsed_catalog_ms": result.get("elapsed_ms", 0.0),
                                "elapsed_f2_ms":      result.get("elapsed_f2_ms", 0.0),
                                "elapsed_total_ms":   elapsed_ms,
                                "n_injected": inj_meta.get("n_injected", 0),
                                "flagged_cols": "|".join(
                                    sorted(result["flagged_cols"])
                                ),
                                "rules_fired": "|".join(
                                    f"{c}:{','.join(rs)}"
                                    for c, rs in sorted(
                                        result.get("flags", {}).items()
                                    )
                                ),
                            }
                            writer.writerow(row)

                        except Exception as exc:
                            n_err += 1
                            writer.writerow(_error_row(
                                ds_name, ft, fault_col, sev, meth, rep, exc
                            ))
                            if verbose:
                                print(f"    [ERROR] {ft}/sev={sev}/{meth}/rep{rep}: {exc}")
                        finally:
                            try: os.unlink(db_work)
                            except: pass

                        qf.flush()
                        done += 1
                        if verbose and done % 60 == 0:
                            print(f"    {done}/{total} ({done/total*100:.0f}%)")

        return n_err

    finally:
        try: os.unlink(db_clean)
        except: pass


# ------------------------------------------------------------------ #
# Full experiment runner                                               #
# ------------------------------------------------------------------ #

def run_all(
    datasets: Dict[str, str],
    fault_col_map: Optional[Dict[str, str]] = None,
    reps: int = 20,
    K: int = 5,
    z: float = 2.0,
    out_dir: str = "results",
    verbose: bool = True,
) -> Tuple[str, str]:
    """
    Run the full quality experiment matrix for SQLite.

    Raises RuntimeError after writing all rows if any run errored.
    Returns (quality_csv_path, summary_csv_path).
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    quality_path = os.path.join(out_dir, "results_quality.csv")
    summary_path = os.path.join(out_dir, "results_summary.csv")

    fcmap    = {**FAULT_COLS, **(fault_col_map or {})}
    n_errors = 0

    with open(quality_path, "w", newline="", encoding="utf-8") as qf:
        writer = csv.DictWriter(qf, fieldnames=QUALITY_FIELDNAMES)
        writer.writeheader()

        for ds_name, csv_path in datasets.items():
            fault_col = fcmap.get(ds_name)
            if fault_col is None:
                if verbose:
                    print(f"[WARN] No fault column for '{ds_name}'; skipping.")
                continue
            if verbose:
                print(f"\n{'='*55}")
                print(f"Dataset: {ds_name}  (fault col: {fault_col})")
            t_ds = time.perf_counter()
            n_err = _run_dataset(
                ds_name, csv_path, fault_col, writer, qf, reps, K, z, verbose
            )
            n_errors += n_err
            if verbose:
                elapsed = time.perf_counter() - t_ds
                print(f"  Done in {elapsed:.1f}s  errors={n_err}")

    # Build summary
    with open(quality_path, encoding="utf-8") as qf:
        all_rows = list(csv.DictReader(qf))

    ok_rows = [r for r in all_rows if r.get("status") == "ok"]
    groups:     Dict[tuple, List[Dict]] = defaultdict(list)
    err_groups: Dict[tuple, int]        = defaultdict(int)

    for r in ok_rows:
        key = (r["dataset"], r["fault_type"], r["severity"], r["method"])
        groups[key].append(r)
    for r in all_rows:
        if r.get("status") == "error":
            key = (r["dataset"], r["fault_type"], r["severity"], r["method"])
            err_groups[key] += 1

    with open(summary_path, "w", newline="", encoding="utf-8") as sf:
        writer = csv.DictWriter(sf, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        all_keys = set(groups.keys()) | set(err_groups.keys())
        for key in sorted(all_keys):
            ds, ft, sev, meth = key
            grp   = groups.get(key, [])
            n_err = err_groups.get(key, 0)
            if grp:
                f1s   = [float(r["f1"])       for r in grp]
                precs = [float(r["precision"]) for r in grp]
                recs  = [float(r["recall"])    for r in grp]
                c_ms  = [float(r["elapsed_catalog_ms"]) for r in grp]
                f2_ms = [float(r["elapsed_f2_ms"])      for r in grp]
                t_ms  = [float(r["elapsed_total_ms"])   for r in grp]
                bl, bh = bootstrap_ci(f1s)
                tl, th = t_ci(f1s)
                writer.writerow({
                    "dataset": ds, "fault_type": ft,
                    "severity": sev, "method": meth,
                    "mean_f1":               sum(f1s)/len(f1s),
                    "boot_ci_lo": bl, "boot_ci_hi": bh,
                    "t_ci_lo":    tl, "t_ci_hi":    th,
                    "mean_precision":        sum(precs)/len(precs),
                    "mean_recall":           sum(recs)/len(recs),
                    "mean_elapsed_catalog_ms": sum(c_ms)/len(c_ms),
                    "mean_elapsed_f2_ms":      sum(f2_ms)/len(f2_ms),
                    "mean_elapsed_total_ms":   sum(t_ms)/len(t_ms),
                    "n_reps": len(grp), "n_errors": n_err,
                })
            else:
                writer.writerow({
                    "dataset": ds, "fault_type": ft,
                    "severity": sev, "method": meth,
                    "mean_f1": "", "boot_ci_lo": "", "boot_ci_hi": "",
                    "t_ci_lo": "", "t_ci_hi": "",
                    "mean_precision": "", "mean_recall": "",
                    "mean_elapsed_catalog_ms": "", "mean_elapsed_f2_ms": "", "mean_elapsed_total_ms": "",
                    "n_reps": 0, "n_errors": n_err,
                })

    if verbose:
        print(f"\nQuality CSV → {quality_path}  "
              f"({len(all_rows)} rows, {len(ok_rows)} ok, {n_errors} errors)")
        print(f"Summary CSV → {summary_path}")

    if n_errors > 0:
        raise RuntimeError(
            f"\n{'='*60}\n  EXPERIMENT INTEGRITY ERROR\n"
            f"  {n_errors} run(s) failed (status=error).\n"
            f"  Excluded from summary metrics. Fix and re-run.\n"
            f"  Quality CSV: {quality_path}\n{'='*60}\n"
        )

    return quality_path, summary_path
