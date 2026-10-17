#!/usr/bin/env python3
"""
smoke_test.py — SQLite smoke test on the toy dataset.

What this test does
-------------------
1. Loads toy/clean.csv into a fresh in-memory SQLite database.
2. Calibrates thresholds with K=5 ANALYZE runs on clean data.
3. Injects a NULL fault (33% severity) into the 'lord' column.
4. Runs all three detection methods (catalog, scan, hybrid).
5. Prints flagged columns, rules that fired, timing, and precision/recall/F1.

Expected outcome (known ground truth: only 'lord' is faulted)
--------------------------------------------------------------
  catalog method: C1 fires for 'lord' (null_frac jumps from 0 to ~0.33)
  scan method:    same, plus V1/V2/K2 if applicable
  hybrid method:  confirms catalog flags via targeted scan

Usage
-----
  cd catalog-dq
  python smoke_test.py [path/to/toy/clean.csv]

Default path assumes the datasets/ symlink is set up, or CWD contains toy/.
"""
import os
import sys
import sqlite3
import tempfile
import time

# Allow running from the catalog-dq directory
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from catalog_dq.readers.sqlite_reader import SQLiteReader
from catalog_dq.calibrate import run_calibration
from catalog_dq.detector import detect
from catalog_dq.baseline import build_scan_snapshot, baseline_scan
from catalog_dq.hybrid import hybrid_detect
from catalog_dq.injector import inject_fault_sqlite
from catalog_dq.harness import evaluate, load_csv_to_sqlite, get_all_columns


# ------------------------------------------------------------------ #
# Paths                                                                #
# ------------------------------------------------------------------ #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def find_toy_csv(arg=None):
    candidates = [
        arg,
        os.path.join(SCRIPT_DIR, "datasets", "toy", "clean.csv"),
        os.path.join(SCRIPT_DIR, "..", "toy", "clean.csv"),
        os.path.join(SCRIPT_DIR, "toy", "clean.csv"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "toy/clean.csv not found. Pass the path as the first argument:\n"
        "  python smoke_test.py /path/to/toy/clean.csv"
    )


# ------------------------------------------------------------------ #
# Pretty print helpers                                                 #
# ------------------------------------------------------------------ #
SEP = "─" * 60

def section(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")

def show_result(label, result, faulted_cols, all_cols):
    flagged = result["flagged_cols"]
    ev = evaluate(flagged, faulted_cols, all_cols)
    rules_by_col = result.get("flags", {})
    print(f"\n  Method : {label}")
    print(f"  Elapsed: {result['elapsed_ms']:.2f} ms")
    if flagged:
        for col in sorted(flagged):
            rules = rules_by_col.get(col, [])
            print(f"  FLAG   : {col!r:20s}  rules={rules}")
    else:
        print("  (no columns flagged)")
    print(f"  TP={ev['tp']}  FP={ev['fp']}  FN={ev['fn']}  TN={ev['tn']}")
    print(f"  Precision={ev['precision']:.3f}  Recall={ev['recall']:.3f}  F1={ev['f1']:.3f}")
    return ev


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #
def main():
    toy_csv = find_toy_csv(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Toy dataset  : {toy_csv}")

    TABLE      = "data"
    FAULT_COL  = "lord"
    SEVERITY   = 0.33       # 33% → 2 of 6 rows become NULL
    FAULT_TYPE = "null"
    K          = 5
    Z          = 2.0
    SEED       = 42

    # ── 1. Load clean data into a temp SQLite file ──────────────────
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        db_path = tf.name

    try:
        section("1. Loading toy/clean.csv into SQLite")
        load_csv_to_sqlite(toy_csv, db_path, TABLE)
        all_cols = get_all_columns(db_path, TABLE)
        print(f"  Columns : {all_cols}")
        print(f"  Rows    : 6 (+ header)")

        reader = SQLiteReader(db_path)

        # ── 2. Calibration ──────────────────────────────────────────
        section(f"2. Calibrating thresholds (K={K}, z={Z})")
        def analyze_fn():
            with reader.connect() as conn:
                conn.execute(f"ANALYZE {TABLE}")
            reader.record_analyze_time()

        t0 = time.perf_counter()
        thresholds, calib_snaps = run_calibration(
            reader, TABLE, analyze_fn, K=K, z=Z
        )
        calib_ms = (time.perf_counter() - t0) * 1000.0
        print(f"  Calibration time: {calib_ms:.1f} ms")
        print("\n  Thresholds per column:")
        for col in all_cols:
            th = thresholds.get(col, {})
            print(f"    {col:20s}  null_frac={th.get('null_frac',0):.4f}"
                  f"  n_dist={th.get('n_distinct_norm',0):.4f}"
                  f"  mcv_j={th.get('mcv_jaccard',0):.4f}"
                  f"  corr={th.get('correlation',0):.4f}")

        # ── 3. Take baseline snapshot S₀ ────────────────────────────
        section("3. Baseline snapshot S₀ (clean data)")
        s0 = reader.read_snapshot(TABLE)
        s0_scan = build_scan_snapshot(reader, TABLE)
        print(f"  Snapshot timestamp: {s0.timestamp}")
        for col, cs in s0.columns.items():
            print(f"    {col:20s}  null_frac={cs.null_frac:.4f}"
                  f"  n_distinct_norm={cs.n_distinct_norm:.4f}")

        # ── 4. Inject fault ─────────────────────────────────────────
        section(f"4. Injecting fault: type={FAULT_TYPE!r}  col={FAULT_COL!r}  severity={SEVERITY:.0%}")
        with reader.connect() as conn:
            meta = inject_fault_sqlite(conn, TABLE, FAULT_TYPE, FAULT_COL, SEVERITY, seed=SEED)
        print(f"  Injected: {meta['n_injected']} NULL(s) into '{FAULT_COL}'")

        # Verify injection
        with reader.connect() as conn:
            cur = conn.execute(f'SELECT lord FROM data')
            vals = [r[0] for r in cur.fetchall()]
        print(f"  Lord column after injection: {vals}")

        # ── 5. ANALYZE after injection ──────────────────────────────
        section("5. Running ANALYZE after injection")
        with reader.connect() as conn:
            conn.execute(f"ANALYZE {TABLE}")
        reader.record_analyze_time()
        print(f"  analyze_time recorded: {reader.analyze_time}")

        # ── 6. Current snapshot Sₜ ──────────────────────────────────
        section("6. Current snapshot Sₜ (after fault)")
        st = reader.read_snapshot(TABLE)
        st_scan = build_scan_snapshot(reader, TABLE)
        print(f"  Snapshot timestamp: {st.timestamp}")
        for col, cs in st.columns.items():
            cs0 = s0.columns.get(col)
            delta_null = abs((cs.null_frac or 0) - (cs0.null_frac or 0)) if cs0 else None
            flag = "  ← changed" if (delta_null or 0) > thresholds.get(col, {}).get("null_frac", 0) else ""
            print(f"    {col:20s}  null_frac={cs.null_frac:.4f}"
                  f"  (Δ={delta_null:.4f}  θ={thresholds.get(col,{}).get('null_frac',0):.4f})"
                  f"{flag}")

        # ── 7. Run all three methods ─────────────────────────────────
        section("7. Detection results")
        faulted_cols = {FAULT_COL}

        # Catalog
        t0 = time.perf_counter()
        cat_result = detect(s0, st, thresholds, reader=reader, table=TABLE)
        cat_result["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
        ev_cat = show_result("catalog", cat_result, faulted_cols, all_cols)

        # Scan (baseline)
        t0 = time.perf_counter()
        scan_result = baseline_scan(reader, TABLE, s0_scan, st_scan, thresholds)
        scan_result["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
        ev_scan = show_result("scan (baseline)", scan_result, faulted_cols, all_cols)

        # Hybrid
        t0 = time.perf_counter()
        hyb_result = hybrid_detect(s0, st, thresholds, cat_result, reader, TABLE)
        hyb_result["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
        ev_hyb = show_result("hybrid", hyb_result, faulted_cols, all_cols)

        # ── 8. Summary ───────────────────────────────────────────────
        section("8. Summary")
        print(f"\n  {'Method':20s}  {'F1':>6}  {'Prec':>6}  {'Recall':>6}  {'ms':>8}")
        print(f"  {'-'*20}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}")
        for label, ev, ms in [
            ("catalog",       ev_cat,  cat_result["elapsed_ms"]),
            ("scan baseline", ev_scan, scan_result["elapsed_ms"]),
            ("hybrid",        ev_hyb,  hyb_result["elapsed_ms"]),
        ]:
            print(f"  {label:20s}  {ev['f1']:6.3f}  {ev['precision']:6.3f}  {ev['recall']:6.3f}  {ms:8.2f}")

        print(f"\n  Ground truth: faulted column = {FAULT_COL!r}")
        print(f"  Fault: {FAULT_TYPE}, severity={SEVERITY:.0%}, seed={SEED}")
        print(f"\n  Smoke test PASSED ✓" if ev_cat["f1"] > 0 else "\n  Smoke test needs review.")

    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
