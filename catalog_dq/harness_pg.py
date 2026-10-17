"""
harness_pg.py — PostgreSQL 16 experiment harness (Path B).

Per-run restore strategy:
  For each dataset:
    1. Load clean CSV → "<ds>_clean"  (once, column names normalised)
    2. Calibration: K times { copy clean → "<ds>_work", ANALYZE, snapshot }
    3. Thresholds cached, reused for all runs
    4. Baseline S₀ from a fresh clean copy
    Per run:
      5. DROP + CREATE AS SELECT "<ds>_clean" (fast in-DB restore)
      6. ANALYZE work table (Fix 2: establishes valid reltuples as the
         "previously-analysed production table" premise of the method)
      7. Inject fault
      8. ANALYZE post-injection (if not stale)
      9. Take Sₜ
      10. Detect → evaluate → write CSV row

Timing windows (Fix 3 — D6):
  * Scan: st_scan build is INSIDE the timing window (symmetric with SQLite harness).
  * Catalog: elapsed_f2_ms records the COUNT(*) sub-cost separately so
    catalog latency can be quoted both with and without F2.

Results integrity:
  * Failed runs written as status='error'; metrics fields empty.
  * range injection returning n_injected=0 is treated as status=error
    (Fix 1: the Python-float sampler now handles TEXT cols with empty strings,
    so n_injected=0 means a genuine FAULT_COLS configuration error).
  * Summary counts n_errors per cell; excludes error rows from means.
  * RuntimeError raised at end if any errors occurred.

Column normalisation:
  load_csv_to_pg() applies normalize_col_name() to every header.
  FAULT_COLS uses normalised names (same as harness.py).
"""
from __future__ import annotations

import csv
import os
import math
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
import psycopg2.extras

from .calibrate import compute_thresholds
from .detector import detect
from .baseline import build_scan_snapshot, baseline_scan, _build_scan_snapshot_pg
from .hybrid import hybrid_detect
from .injector import inject_fault_postgres, FAULT_TYPES
from .readers.pg_reader import PostgresReader
from .snapshot import Snapshot

# Re-use constants and helpers from the SQLite harness
from .harness import (
    normalize_col_name,
    SEVERITIES, METHODS, FAULT_COLS,
    evaluate, bootstrap_ci, t_ci,
    QUALITY_FIELDNAMES, SUMMARY_FIELDNAMES,
    _error_row,
)


# ------------------------------------------------------------------ #
# PostgreSQL connection helpers                                         #
# ------------------------------------------------------------------ #

def _pg_connect(dsn: str, autocommit: bool = False):
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    return conn


# ------------------------------------------------------------------ #
# CSV loader (normalises column names)                                 #
# ------------------------------------------------------------------ #

def load_csv_to_pg(dsn: str, csv_path: str, table: str) -> List[str]:
    """
    Load a CSV into a PostgreSQL table (all columns as TEXT).
    Column names are normalised (lowercase snake_case) at load time.
    Returns list of normalised column names.
    """
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    df = df.where(df != "", other=None)
    df.columns = [normalize_col_name(c) for c in df.columns]
    cols = list(df.columns)

    col_defs     = ", ".join(f'"{c}" TEXT' for c in cols)
    col_names    = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))

    conn = _pg_connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
            cur.execute(f'CREATE TABLE "{table}" ({col_defs})')
        rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
        sql  = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    finally:
        conn.close()
    return cols


def _copy_clean_to_work(dsn: str, table_clean: str, table_work: str) -> None:
    """DROP table_work and refill from table_clean (in-DB, fast)."""
    conn = _pg_connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table_work}"')
            cur.execute(
                f'CREATE TABLE "{table_work}" AS SELECT * FROM "{table_clean}"'
            )
        conn.commit()
    finally:
        conn.close()


def _pg_analyze(dsn: str, table: str) -> str:
    """Run ANALYZE (autocommit=True) and return UTC ISO timestamp."""
    import datetime
    conn = _pg_connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'ANALYZE "{table}"')
    finally:
        conn.close()
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _pg_set_statistics(dsn: str, table: str, cols: List[str], target: int) -> None:
    """
    Set statistics_target for all columns in `table`, then ANALYZE.
    Used by sensitivity runs (D10): target ∈ {10, 100, 1000}.
    """
    conn = _pg_connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            for col in cols:
                cur.execute(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{col}" '
                    f'SET STATISTICS {int(target)}'
                )
        conn.commit()
    finally:
        conn.close()
    _pg_analyze(dsn, table)


def _pg_get_columns(dsn: str, table: str) -> List[str]:
    conn = _pg_connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND table_schema='public' "
                "ORDER BY ordinal_position",
                (table,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Single-run logic (PostgreSQL)                                        #
# ------------------------------------------------------------------ #

def one_quality_run_pg(
    dsn: str,
    dataset_name: str,
    table_clean: str,
    table_work: str,
    fault_type: str,
    fault_col: str,
    severity: float,
    method: str,
    rep: int,
    thresholds: dict,
    s0: Snapshot,
    s0_scan: Snapshot,
    all_cols: List[str],
    seed_base: int = 0,
    statistics_target: Optional[int] = None,
) -> Dict:
    """
    Execute one quality measurement run for PostgreSQL.
    Raises on error (caller records status='error').
    """
    seed = seed_base + rep

    # ── 1. Restore clean state ──────────────────────────────────────
    _copy_clean_to_work(dsn, table_clean, table_work)

    reader = PostgresReader(dsn)

    # ── Fix 2: baseline ANALYZE establishes valid reltuples ─────────
    # The method's premise is a previously-analysed production table
    # CREATE TABLE … AS SELECT leaves reltuples=−1,
    # making F2 arithmetic incorrect.  One ANALYZE here mirrors the real
    # scenario and is excluded from the measured detection window.
    if statistics_target is not None:
        _pg_set_statistics(dsn, table_work, all_cols, statistics_target)
    else:
        _pg_analyze(dsn, table_work)

    # ── 2. Inject fault ─────────────────────────────────────────────
    conn_inj = _pg_connect(dsn, autocommit=False)
    try:
        inj_meta = inject_fault_postgres(
            conn_inj, table_work, fault_type, fault_col, severity, seed=seed
        )
    finally:
        conn_inj.close()

    # Fix 1: range injection returning n_injected=0 means the column
    # is non-numeric (e.g. TEXT with empty-string NULLs that passed the
    # old SAVEPOINT path).  After the Python-float sampler fix this only
    # happens for a genuinely wrong FAULT_COLS entry → treat as error.
    if fault_type == "range" and inj_meta.get("n_injected", 0) == 0:
        raise ValueError(
            f"range injection returned n_injected=0 for column '{fault_col}' "
            f"in dataset '{dataset_name}'. "
            f"Check FAULT_COLS or the CSV column type."
        )

    # ── 3. Post-injection ANALYZE (skip for stale) ──────────────────
    if fault_type != "stale":
        if statistics_target is not None:
            _pg_set_statistics(dsn, table_work, all_cols, statistics_target)
        else:
            _pg_analyze(dsn, table_work)

    # ── 4. Take Sₜ (catalog snapshot) ──────────────────────────────
    st = reader.read_snapshot(table_work)

    # ── 5. Detect — timing window starts here (Fix 3) ───────────────
    # For scan, st_scan is built INSIDE the window (symmetric with the
    # SQLite harness which also times snapshot build for Method B).
    # elapsed_f2_ms is extracted from detect() result (D6).
    t0 = time.perf_counter()

    elapsed_f2_ms_val = 0.0

    if method == "catalog":
        result = detect(s0, st, thresholds, reader=reader, table=table_work)
        elapsed_f2_ms_val = result.get("elapsed_f2_ms", 0.0)

    elif method == "scan":
        st_scan = _build_scan_snapshot_pg(reader, table_work)   # timed
        result  = baseline_scan(reader, table_work, s0_scan, st_scan, thresholds)

    elif method == "hybrid":
        cat_result = detect(s0, st, thresholds, reader=reader, table=table_work)
        elapsed_f2_ms_val = cat_result.get("elapsed_f2_ms", 0.0)
        result = hybrid_detect(s0, st, thresholds, cat_result, reader, table_work)

    else:
        raise ValueError(f"Unknown method: {method!r}")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # ── 6. Evaluate ─────────────────────────────────────────────────
    faulted_cols = {fault_col} if fault_type != "stale" else set(all_cols)
    ev = evaluate(result["flagged_cols"], faulted_cols, all_cols)

    return {
        "status":    "ok",
        "error_msg": "",
        "dataset":   dataset_name,
        "fault_type": fault_type,
        "fault_col": fault_col,
        "severity":  severity,
        "method":    method,
        "rep":       rep,
        "tp": ev["tp"], "fp": ev["fp"],
        "fn": ev["fn"], "tn": ev["tn"],
        "precision":           ev["precision"],
        "recall":              ev["recall"],
        "f1":                  ev["f1"],
        "elapsed_catalog_ms":  result.get("elapsed_ms", 0.0),
        "elapsed_f2_ms":       elapsed_f2_ms_val,
        "elapsed_total_ms":    elapsed_ms,
        "n_injected":          inj_meta.get("n_injected", 0),
        "flagged_cols":        "|".join(sorted(result["flagged_cols"])),
        "rules_fired":         "|".join(
            f"{c}:{','.join(rs)}"
            for c, rs in sorted(result.get("flags", {}).items())
        ),
    }


# ------------------------------------------------------------------ #
# Full experiment runner                                               #
# ------------------------------------------------------------------ #

def run_all_pg(
    dsn: str,
    datasets: Dict[str, str],
    fault_col_map: Optional[Dict[str, str]] = None,
    reps: int = 20,
    K: int = 5,
    z: float = 2.0,
    out_dir: str = "results",
    verbose: bool = True,
    statistics_target: Optional[int] = None,
) -> Tuple[str, str]:
    """
    Run the full quality experiment matrix against PostgreSQL 16.

    statistics_target : if set (10 / 100 / 1000), ALTER all columns in the
                        work table to that target before every ANALYZE.
                        Used for sensitivity runs (D10).

    Raises RuntimeError (after writing all rows) if any run errored.
    Returns (quality_csv_path, summary_csv_path).
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    quality_path = os.path.join(out_dir, "results_quality.csv")
    summary_path = os.path.join(out_dir, "results_summary.csv")

    fcmap = {**FAULT_COLS, **(fault_col_map or {})}

    # Verify PG connection
    try:
        test_conn = _pg_connect(dsn, autocommit=True)
        test_conn.close()
        if verbose:
            print("[OK] PostgreSQL connection verified.")
    except Exception as e:
        raise RuntimeError(f"Cannot connect to PostgreSQL: {e}") from e

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

            table_clean = f"{ds_name}_clean"
            table_work  = f"{ds_name}_work"

            if verbose:
                print(f"\n{'='*60}")
                print(f"Dataset: {ds_name}  (fault col: {fault_col})")
                print(f"Loading {csv_path} → PostgreSQL …")

            # ── Load clean CSV (once, normalised column names) ──────
            load_csv_to_pg(dsn, csv_path, table_clean)
            all_cols = _pg_get_columns(dsn, table_clean)

            if verbose:
                stat_info = (f", statistics_target={statistics_target}"
                             if statistics_target is not None else "")
                print(f"  Loaded {len(all_cols)} columns into '{table_clean}'{stat_info}")

            # ── Calibration (K ANALYZE runs) ────────────────────────
            if verbose:
                print(f"  Calibrating (K={K}) …")

            calib_snaps = []
            reader = PostgresReader(dsn)

            for _ in range(K):
                _copy_clean_to_work(dsn, table_clean, table_work)
                if statistics_target is not None:
                    _pg_set_statistics(dsn, table_work, all_cols, statistics_target)
                else:
                    _pg_analyze(dsn, table_work)
                snap = reader.read_snapshot(table_work)
                calib_snaps.append(snap)

            thresholds = compute_thresholds(calib_snaps, z=z)

            # ── Baseline S₀ ─────────────────────────────────────────
            _copy_clean_to_work(dsn, table_clean, table_work)
            if statistics_target is not None:
                _pg_set_statistics(dsn, table_work, all_cols, statistics_target)
            else:
                _pg_analyze(dsn, table_work)
            s0      = reader.read_snapshot(table_work)
            s0_scan = _build_scan_snapshot_pg(reader, table_work)

            # ── Experiment cells ────────────────────────────────────
            total_cells = len(FAULT_TYPES) * len(SEVERITIES) * len(METHODS) * reps
            done_cells  = 0

            for ft in FAULT_TYPES:
                for sev in SEVERITIES:
                    for meth in METHODS:
                        for rep in range(reps):
                            try:
                                row = one_quality_run_pg(
                                    dsn=dsn,
                                    dataset_name=ds_name,
                                    table_clean=table_clean,
                                    table_work=table_work,
                                    fault_type=ft,
                                    fault_col=fault_col,
                                    severity=sev,
                                    method=meth,
                                    rep=rep,
                                    thresholds=thresholds,
                                    s0=s0,
                                    s0_scan=s0_scan,
                                    all_cols=all_cols,
                                    seed_base=1000 * (list(FAULT_TYPES).index(ft) + 1),
                                    statistics_target=statistics_target,
                                )
                                writer.writerow(row)
                            except Exception as exc:
                                n_errors += 1
                                err_row = _error_row(
                                    ds_name, ft, fault_col, sev, meth, rep, exc
                                )
                                writer.writerow(err_row)
                                if verbose:
                                    print(
                                        f"  [ERROR] {ds_name}/{ft}/sev={sev}"
                                        f"/{meth}/rep{rep}: {exc}"
                                    )
                            qf.flush()
                            done_cells += 1
                            if verbose and done_cells % 20 == 0:
                                pct = done_cells / total_cells * 100
                                print(f"  {ds_name}: {done_cells}/{total_cells} ({pct:.0f}%)")

            # ── Clean up staging tables ─────────────────────────────
            conn_drop = _pg_connect(dsn, autocommit=False)
            try:
                with conn_drop.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS "{table_clean}"')
                    cur.execute(f'DROP TABLE IF EXISTS "{table_work}"')
                conn_drop.commit()
            finally:
                conn_drop.close()

    # ── Build summary (ok rows only) ─────────────────────────────────
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
                boot_lo, boot_hi = bootstrap_ci(f1s)
                t_lo,    t_hi    = t_ci(f1s)
                writer.writerow({
                    "dataset": ds, "fault_type": ft,
                    "severity": sev, "method": meth,
                    "mean_f1":               sum(f1s)   / len(f1s),
                    "boot_ci_lo":            boot_lo,
                    "boot_ci_hi":            boot_hi,
                    "t_ci_lo":               t_lo,
                    "t_ci_hi":               t_hi,
                    "mean_precision":        sum(precs) / len(precs),
                    "mean_recall":           sum(recs)  / len(recs),
                    "mean_elapsed_catalog_ms": sum(c_ms)  / len(c_ms),
                    "mean_elapsed_f2_ms":      sum(f2_ms) / len(f2_ms),
                    "mean_elapsed_total_ms":   sum(t_ms)  / len(t_ms),
                    "n_reps":   len(grp),
                    "n_errors": n_err,
                })
            else:
                writer.writerow({
                    "dataset": ds, "fault_type": ft,
                    "severity": sev, "method": meth,
                    "mean_f1": "", "boot_ci_lo": "", "boot_ci_hi": "",
                    "t_ci_lo": "", "t_ci_hi": "",
                    "mean_precision": "", "mean_recall": "",
                    "mean_elapsed_catalog_ms": "",
                    "mean_elapsed_f2_ms":      "",
                    "mean_elapsed_total_ms":   "",
                    "n_reps": 0, "n_errors": n_err,
                })

    if verbose:
        print(f"\nQuality CSV → {quality_path}  "
              f"({len(all_rows)} rows, {len(ok_rows)} ok, {n_errors} errors)")
        print(f"Summary CSV → {summary_path}")

    # Integrity gate
    if n_errors > 0:
        banner = (
            f"\n{'='*60}\n"
            f"  EXPERIMENT INTEGRITY ERROR\n"
            f"  {n_errors} run(s) failed and were recorded as status=error.\n"
            f"  They have been EXCLUDED from summary metrics.\n"
            f"  Fix the errors and re-run before reporting results.\n"
            f"  Quality CSV: {quality_path}\n"
            f"{'='*60}\n"
        )
        raise RuntimeError(banner)

    return quality_path, summary_path
