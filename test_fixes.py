"""
test_fixes.py -- Unit tests for the five bug fixes (no PostgreSQL server needed).

Run:
    python3 test_fixes.py
"""
import sys
import datetime
import sqlite3
import tempfile
import os

sys.path.insert(0, os.path.dirname(__file__))


# ================================================================== #
# 1. Timezone fix in rule_F1
# ================================================================== #

def test_f1_timezone_aware():
    """
    rule_F1 must handle a tz-aware analyze_time (as returned by PostgreSQL's
    pg_stat_all_tables.last_analyze / stored as ISO-8601 with UTC offset).
    Prior to the fix, datetime.utcnow() (naive) - datetime_with_tzinfo raised
    TypeError: can't subtract offset-naive and offset-aware datetimes.
    """
    from catalog_dq.rules import rule_F1
    from catalog_dq.snapshot import Snapshot, ColStats

    # Fake analyze_time 48 hours ago -- tz-aware UTC (simulates PG output)
    analyzed_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    analyze_time_str = analyzed_at.isoformat()   # e.g. "2026-07-05T12:34:56+00:00"

    snap = Snapshot(
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        db_type="postgres",
        db_path="host=localhost",
        table="test",
        analyze_time=analyze_time_str,
        row_count_est=100,
        columns={"col1": ColStats()},
    )

    thresholds = {"__table__": {"age_hours": 24.0}}  # flag if older than 24h

    try:
        result = rule_F1(snap, snap, thresholds, "col1")
    except TypeError as e:
        print(f"FAIL test_f1_timezone_aware: {e}")
        return False

    if result is not True:
        print(f"FAIL test_f1_timezone_aware: expected True (48h > 24h threshold), got {result}")
        return False

    # Also test: recent analyze_time should NOT flag
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    snap2 = Snapshot(
        timestamp=recent.isoformat(),
        db_type="postgres",
        db_path="host=localhost",
        table="test",
        analyze_time=recent.isoformat(),
        row_count_est=100,
        columns={"col1": ColStats()},
    )
    result2 = rule_F1(snap2, snap2, thresholds, "col1")
    if result2 is not False:
        print(f"FAIL test_f1_timezone_aware: recent analyze should not flag, got {result2}")
        return False

    print("PASS test_f1_timezone_aware")
    return True


def test_f1_naive_timestamp():
    """
    rule_F1 must also handle naive timestamps (SQLite path, stored as
    datetime.utcnow().isoformat() without timezone suffix).
    """
    from catalog_dq.rules import rule_F1
    from catalog_dq.snapshot import Snapshot, ColStats

    # Naive ISO timestamp 48 hours ago
    analyzed_at = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    ).replace(tzinfo=None).isoformat()

    snap = Snapshot(
        timestamp=datetime.datetime.utcnow().isoformat(),
        db_type="sqlite",
        db_path="/tmp/test.db",
        table="data",
        analyze_time=analyzed_at,
        row_count_est=100,
        columns={"col1": ColStats()},
    )
    thresholds = {"__table__": {"age_hours": 24.0}}

    try:
        result = rule_F1(snap, snap, thresholds, "col1")
    except TypeError as e:
        print(f"FAIL test_f1_naive_timestamp: {e}")
        return False

    if result is not True:
        print(f"FAIL test_f1_naive_timestamp: expected True, got {result}")
        return False

    print("PASS test_f1_naive_timestamp")
    return True


# ================================================================== #
# 2. Range injection on text-typed column
# ================================================================== #

def test_range_injection_text_column():
    """
    inject_range_sqlite must NOT raise an exception for a column containing
    non-numeric strings like 'empty', '7:16 a.m.', '97%'.
    It must return n_injected=0 silently.
    """
    from catalog_dq.injector import inject_range_sqlite

    # Build a tiny SQLite DB with a text column that looks like hospital.Score
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        db_path = tf.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (score TEXT, zipcode TEXT)")
        conn.executemany(
            "INSERT INTO t VALUES (?,?)",
            [("empty", "35001"), ("97%", "35002"), ("empty", "35003"),
             ("100%", "35004"), ("empty", "35005")],
        )
        conn.commit()

        # range injection on text column -- must return 0 injections, not raise
        try:
            rids, n = inject_range_sqlite(conn, "t", "score", p=0.5, seed=42)
        except Exception as e:
            print(f"FAIL test_range_injection_text_column: raised {type(e).__name__}: {e}")
            conn.close()
            return False

        if n != 0:
            print(f"FAIL test_range_injection_text_column: expected 0 for text col, got {n}")
            conn.close()
            return False

        # range injection on numeric column -- must inject
        rids2, n2 = inject_range_sqlite(conn, "t", "zipcode", p=0.5, seed=42)
        if n2 == 0:
            print("FAIL test_range_injection_text_column: expected >0 for numeric col, got 0")
            conn.close()
            return False

        conn.close()
    finally:
        os.unlink(db_path)

    print("PASS test_range_injection_text_column")
    return True


def test_column_name_normalisation():
    """
    load_csv_to_sqlite must produce lowercase snake_case column names.
    'RatingValue' -> 'ratingvalue', 'Filming Locations' -> 'filming_locations'.
    """
    import pandas as pd
    from catalog_dq.harness import load_csv_to_sqlite, normalize_col_name

    # Test normalize_col_name directly
    cases = {
        "RatingValue":       "ratingvalue",
        "Filming Locations": "filming_locations",
        "beer-name":         "beer_name",
        "Score":             "score",
        " ZipCode ":         "zipcode",
        "article_jvolumn":   "article_jvolumn",
    }
    for inp, expected in cases.items():
        got = normalize_col_name(inp)
        if got != expected:
            print(f"FAIL test_column_name_normalisation: "
                  f"normalize('{inp}') = '{got}', expected '{expected}'")
            return False

    # Test via actual CSV load
    csv_content = "RatingValue,Filming Locations,beer-name\n7.5,New York,IPA\n8.0,London,Stout\n"
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as tf:
        tf.write(csv_content)
        csv_path = tf.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf2:
        db_path = tf2.name
    try:
        cols = load_csv_to_sqlite(csv_path, db_path, "data")
        expected_cols = ["ratingvalue", "filming_locations", "beer_name"]
        if cols != expected_cols:
            print(f"FAIL test_column_name_normalisation: "
                  f"got cols={cols}, expected {expected_cols}")
            return False
    finally:
        os.unlink(csv_path)
        os.unlink(db_path)

    print("PASS test_column_name_normalisation")
    return True


def test_error_row_integrity():
    """
    A run that raises must be written as status='error' with empty metric fields.
    Errored rows must be excluded from summary mean_f1.
    """
    from catalog_dq.harness import (
        QUALITY_FIELDNAMES, SUMMARY_FIELDNAMES, _error_row
    )

    err = _error_row("ds", "null", "col1", 0.05, "catalog", 3,
                     ValueError("test error"))
    assert err["status"] == "error", f"Expected status=error, got {err['status']}"
    assert err["f1"] == "", f"f1 should be empty for error row, got {err['f1']}"
    assert "ValueError" in err["error_msg"], "error_msg should contain exception type"

    print("PASS test_error_row_integrity")
    return True


# ================================================================== #
# 6. Fix 1 -- PG range injection with Python float() sampler
# ================================================================== #

def test_pg_range_fix_python_float_sampling():
    """
    The Python float() sampler in inject_fault_postgres / inject_range_sqlite
    must handle a column that mixes valid numeric strings with empty strings
    (the exact situation that broke beers/abv and movies_1/ratingvalue).

    Column has 9 numeric values + 1 empty string = 90% parseable.
    The >= 50% threshold must accept this and inject range faults.
    """
    from catalog_dq.injector import inject_range_sqlite

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        db_path = tf.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (abv TEXT)")
        # 9 numeric values + 1 empty string -> 90% parseable -> should inject
        values = [("5.5",), ("6.0",), ("4.8",), ("7.2",), ("5.1",),
                  ("6.3",), ("4.9",), ("8.0",), ("5.7",), ("",)]
        conn.executemany("INSERT INTO t VALUES (?)", values)
        conn.commit()

        rids, n = inject_range_sqlite(conn, "t", "abv", p=0.5, seed=42)
        conn.close()
    finally:
        os.unlink(db_path)

    if n == 0:
        print("FAIL test_pg_range_fix_python_float_sampling: "
              "expected n_injected > 0 for column with 90% numeric values, got 0")
        return False

    print(f"PASS test_pg_range_fix_python_float_sampling  (n_injected={n})")
    return True


# ================================================================== #
# 7. Fix 2 -- F2 rule requires positive row_count_est (reltuples fix)
# ================================================================== #

def test_f2_requires_valid_row_count_est():
    """
    F2 (row-count drift) must NOT fire when row_count_est = -1 (PG reltuples
    before ANALYZE). The drift formula abs(actual - est) / est gives a
    negative value for est=-1, so drift < theta -> F2 = False.

    After the harness fix (ANALYZE before injection), row_count_est is a real
    positive count, so a 20% stale injection should be detected.
    """
    from catalog_dq.rules import rule_F2
    from catalog_dq.snapshot import Snapshot, ColStats

    def make_snap(est):
        return Snapshot(
            timestamp=datetime.datetime.utcnow().isoformat(),
            db_type="sqlite",
            db_path=":memory:",
            table="t",
            analyze_time=datetime.datetime.utcnow().isoformat(),
            row_count_est=est,
            columns={"col": ColStats()},
        )

    thresholds = {"__table__": {"row_drift": 0.10}}  # 10% drift threshold

    # Bug scenario: est=-1 (unanalysed PG table) -- F2 must NOT fire
    # rule_F2(s0, st, thresholds, col, row_count_actual=N)
    snap_neg = make_snap(est=-1)
    result_bug = rule_F2(snap_neg, snap_neg, thresholds, "col", row_count_actual=1200)
    if result_bug is not False:
        print(f"FAIL test_f2_requires_valid_row_count_est: "
              f"est=-1 should NOT fire F2, got {result_bug}")
        return False

    # Fixed scenario: est=1000, actual=1200 (20% drift > 10% threshold) -> True
    snap_pos = make_snap(est=1000)
    result_fixed = rule_F2(snap_pos, snap_pos, thresholds, "col", row_count_actual=1200)
    if result_fixed is not True:
        print(f"FAIL test_f2_requires_valid_row_count_est: "
              f"est=1000 actual=1200 should fire F2, got {result_fixed}")
        return False

    # No drift: est=1000, actual=1000 -> False
    result_nodrift = rule_F2(snap_pos, snap_pos, thresholds, "col", row_count_actual=1000)
    if result_nodrift is not False:
        print(f"FAIL test_f2_requires_valid_row_count_est: "
              f"est=1000 actual=1000 should NOT fire F2, got {result_nodrift}")
        return False

    print("PASS test_f2_requires_valid_row_count_est")
    return True


# ================================================================== #
# 8. Fix 3 -- detect() returns elapsed_f2_ms (D6 timing split)
# ================================================================== #

def test_detect_elapsed_f2_ms_column():
    """
    detect() must always return 'elapsed_f2_ms' in its result dict.

    Without a reader: elapsed_f2_ms must be present and == 0.0 (no COUNT(*)).
    With a reader: elapsed_f2_ms must be > 0 (COUNT(*) was called).
    """
    from catalog_dq.detector import detect
    from catalog_dq.snapshot import Snapshot, ColStats

    col = ColStats()
    snap = Snapshot(
        timestamp=datetime.datetime.utcnow().isoformat(),
        db_type="sqlite",
        db_path=":memory:",
        table="t",
        analyze_time=datetime.datetime.utcnow().isoformat(),
        row_count_est=100,
        columns={"col": col},
    )
    thresholds = {}

    # Without reader
    result_no_reader = detect(snap, snap, thresholds, reader=None, table=None)
    if "elapsed_f2_ms" not in result_no_reader:
        print("FAIL test_detect_elapsed_f2_ms_column: "
              "'elapsed_f2_ms' missing from detect() result (no reader)")
        return False
    # Without a reader no COUNT(*) runs, so elapsed_f2_ms should be
    # essentially zero (timer overhead only -- allow up to 1.0 ms).
    if result_no_reader["elapsed_f2_ms"] >= 1.0:
        print(f"FAIL test_detect_elapsed_f2_ms_column: "
              f"expected ~0 without reader, got {result_no_reader['elapsed_f2_ms']}")
        return False

    # With a mock reader backed by an in-memory SQLite table
    class _MockReader:
        def connect(self):
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE t (col TEXT)")
            conn.execute("INSERT INTO t VALUES ('a')")
            conn.commit()
            return conn

        def get_actual_row_count(self, conn, table):
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]

    result_with_reader = detect(
        snap, snap, thresholds, reader=_MockReader(), table="t"
    )
    if "elapsed_f2_ms" not in result_with_reader:
        print("FAIL test_detect_elapsed_f2_ms_column: "
              "'elapsed_f2_ms' missing from detect() result (with reader)")
        return False
    if result_with_reader["elapsed_f2_ms"] <= 0.0:
        print(f"FAIL test_detect_elapsed_f2_ms_column: "
              f"expected > 0 with reader, got {result_with_reader['elapsed_f2_ms']}")
        return False

    print(f"PASS test_detect_elapsed_f2_ms_column  "
          f"(elapsed_f2_ms={result_with_reader['elapsed_f2_ms']:.4f} ms)")
    return True


# ================================================================== #
# Main runner
# ================================================================== #

if __name__ == "__main__":
    tests = [
        test_f1_timezone_aware,
        test_f1_naive_timestamp,
        test_range_injection_text_column,
        test_column_name_normalisation,
        test_error_row_integrity,
        # ---- three new tests covering the three harness fixes ----
        test_pg_range_fix_python_float_sampling,
        test_f2_requires_valid_row_count_est,
        test_detect_elapsed_f2_ms_column,
    ]
    results = [t() for t in tests]
    n_pass = sum(results)
    n_fail = len(results) - n_pass
    print(f"\n{n_pass}/{len(results)} tests passed", "ok" if n_fail == 0 else "FAILED")
    sys.exit(0 if n_fail == 0 else 1)
