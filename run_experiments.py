#!/usr/bin/env python3
"""
run_experiments.py -- CLI entry point for the full experiment matrix.

Usage (SQLite - Path A)
-----------------------
  python run_experiments.py \\
      --db sqlite \\
      --dataset-dir /path/to/datasets \\
      --datasets hospital flights beers rayyan movies_1 \\
      --reps 20 --K 5 --z 2.0 \\
      --out-dir results/sqlite

Usage (PostgreSQL - Path B)
----------------------------
  python run_experiments.py \\
      --db postgres \\
      --dsn "host=localhost port=5432 dbname=catalog_dq user=catalogdq password=catalogdq" \\
      --dataset-dir /path/to/datasets \\
      --datasets hospital flights beers rayyan movies_1 \\
      --reps 20 --K 5 --z 2.0 \\
      --out-dir results/postgres

Sensitivity runs (D10) - PostgreSQL only
-----------------------------------------
  python run_experiments.py \\
      --db postgres --dsn "..." \\
      --dataset-dir /path/to/datasets \\
      --reps 20 --statistics-target 10 \\
      --out-dir results/sensitivity_10

  python run_experiments.py \\
      --db postgres --dsn "..." \\
      --dataset-dir /path/to/datasets \\
      --reps 20 --statistics-target 1000 \\
      --out-dir results/sensitivity_1000

Output files
------------
  results/<db>/results_quality.csv  -- one row per run (status=ok|error)
  results/<db>/results_summary.csv  -- mean +/- CI per cell (error rows excluded)

Exit codes
----------
  0 : all runs completed without errors
  1 : at least one run failed (see status=error rows in quality CSV)
  2 : bad arguments or dataset not found
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def parse_args():
    p = argparse.ArgumentParser(
        description="catalog-dq experiment harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", choices=["sqlite", "postgres"], default="sqlite")
    p.add_argument(
        "--dsn", default=None,
        help="PostgreSQL DSN (required when --db=postgres)",
    )
    p.add_argument(
        "--dataset-dir", default="datasets",
        help="Directory containing <dataset>/clean.csv subfolders",
    )
    p.add_argument(
        "--datasets", nargs="+",
        default=["hospital", "flights", "beers", "rayyan", "movies_1"],
    )
    p.add_argument("--reps",  type=int,   default=20)
    p.add_argument("--K",     type=int,   default=5)
    p.add_argument("--z",     type=float, default=2.0)
    p.add_argument("--out-dir", default="results")
    p.add_argument(
        "--fault-type", nargs="+",
        default=["null", "range", "swap", "stale"],
    )
    p.add_argument(
        "--statistics-target",
        type=int,
        default=None,
        dest="statistics_target",
        metavar="N",
        help=(
            "PostgreSQL STATISTICS target (e.g. 10 or 1000). "
            "When set, ALTER all work-table columns to this target before each "
            "ANALYZE. Used for sensitivity runs (D10). Ignored for SQLite."
        ),
    )
    return p.parse_args()


def main():
    args = parse_args()

    datasets = {}
    for ds in args.datasets:
        csv_path = os.path.join(args.dataset_dir, ds, "clean.csv")
        if not os.path.isfile(csv_path):
            print(f"[WARN] {csv_path} not found; skipping {ds}")
            continue
        datasets[ds] = csv_path

    if not datasets:
        print("[ERROR] No valid dataset paths found. Check --dataset-dir.")
        sys.exit(2)

    out_dir = os.path.join(args.out_dir, args.db)

    if args.db == "sqlite":
        from catalog_dq.harness import run_all, FAULT_TYPES

        active_faults = [f for f in FAULT_TYPES if f in args.fault_type]
        if not active_faults:
            print("[ERROR] No valid fault types specified.")
            sys.exit(2)

        import catalog_dq.harness as harness_mod
        orig_faults = harness_mod.FAULT_TYPES
        harness_mod.FAULT_TYPES = tuple(active_faults)

        print(
            f"SQLite experiments | datasets={list(datasets)} | "
            f"faults={active_faults} | reps={args.reps} | K={args.K} z={args.z}"
        )
        print()
        try:
            q_path, s_path = run_all(
                datasets=datasets,
                reps=args.reps,
                K=args.K,
                z=args.z,
                out_dir=out_dir,
                verbose=True,
            )
            print(f"\nDone. Quality: {q_path}  Summary: {s_path}")
        except RuntimeError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        finally:
            harness_mod.FAULT_TYPES = orig_faults

    elif args.db == "postgres":
        if not args.dsn:
            print("[ERROR] --dsn is required for --db=postgres")
            sys.exit(2)

        from catalog_dq.harness_pg import run_all_pg

        st_info = (
            f", statistics_target={args.statistics_target}"
            if args.statistics_target is not None
            else ""
        )
        print(f"PostgreSQL experiments | dsn={args.dsn}{st_info}")
        print()
        try:
            q_path, s_path = run_all_pg(
                dsn=args.dsn,
                datasets=datasets,
                reps=args.reps,
                K=args.K,
                z=args.z,
                out_dir=out_dir,
                verbose=True,
                statistics_target=args.statistics_target,
            )
            print(f"\nDone. Quality: {q_path}  Summary: {s_path}")
        except RuntimeError as e:
            print(e, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
