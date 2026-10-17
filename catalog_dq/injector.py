"""
injector.py — Fault injector for controlled experiments.

Four fault types:
  null  — Replace p% of non-NULL values with NULL.
  range — Replace p% of values with out-of-range numeric values.
          SKIPS non-numeric columns silently (returns n_injected=0).
  swap  — Shuffle p% of values within the column (Fisher-Yates partial).
  stale — INSERT p% extra rows WITHOUT running ANALYZE (triggers F2).

All operations are IN-PLACE on the target table. The caller restores
clean state between runs by reloading from clean.csv.

p : severity fraction [0, 1]  (e.g. 0.05 = 5%)
seed : random seed for reproducibility
"""
from __future__ import annotations
import math
import random
import sqlite3
from typing import List, Tuple


# ------------------------------------------------------------------ #
# SQLite injectors                                                     #
# ------------------------------------------------------------------ #

def _get_rowids(conn: sqlite3.Connection, table: str) -> List[int]:
    cur = conn.execute(f'SELECT rowid FROM "{table}" ORDER BY rowid')
    return [r[0] for r in cur.fetchall()]


def inject_null_sqlite(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    p: float,
    seed: int = 42,
) -> Tuple[List[int], int]:
    """Replace p-fraction of non-NULL values in col with NULL."""
    rng = random.Random(seed)
    cur = conn.execute(
        f'SELECT rowid FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY rowid'
    )
    candidates = [r[0] for r in cur.fetchall()]
    n_inject = max(1, math.floor(len(candidates) * p)) if candidates else 0
    chosen = rng.sample(candidates, min(n_inject, len(candidates)))
    for rid in chosen:
        conn.execute(f'UPDATE "{table}" SET "{col}" = NULL WHERE rowid = ?', (rid,))
    conn.commit()
    return chosen, len(chosen)


def inject_range_sqlite(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    p: float,
    seed: int = 42,
) -> Tuple[List[int], int]:
    """
    Replace p-fraction of values with out-of-range numeric values.
    Non-numeric columns are skipped gracefully (returns 0 injections).

    NOTE: SQLite's CAST(x AS REAL) is permissive — it converts '97%' to
    97.0 and 'empty' to 0.0, never returning NULL.  A SQL-only IS NOT NULL
    filter therefore cannot detect non-numeric columns.  Instead we sample
    up to 50 values and try Python float() on each; if fewer than 50% parse
    we treat the column as non-numeric and return 0 injections.
    """
    rng = random.Random(seed)
    import math as _math

    # Python-level numericity check (immune to SQLite CAST permissiveness)
    try:
        cur = conn.execute(
            f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 50'
        )
        samples = [r[0] for r in cur.fetchall()]
    except Exception:
        return [], 0

    if not samples:
        return [], 0

    numeric = []
    for v in samples:
        try:
            fv = float(str(v))
            if _math.isfinite(fv):
                numeric.append(fv)
        except (ValueError, TypeError):
            pass

    # Require at least 50% of sampled values to be numeric
    if len(numeric) / len(samples) < 0.5:
        return [], 0

    col_min = min(numeric)
    col_max = max(numeric)

    spread = col_max - col_min
    delta  = 0.1 * spread if spread > 0 else 1.0
    lo_val = col_min - delta
    hi_val = col_max + delta

    cur = conn.execute(
        f'SELECT rowid FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY rowid'
    )
    candidates = [r[0] for r in cur.fetchall()]
    n_inject = max(1, math.floor(len(candidates) * p)) if candidates else 0
    chosen = rng.sample(candidates, min(n_inject, len(candidates)))
    for i, rid in enumerate(chosen):
        val = lo_val if i % 2 == 0 else hi_val
        conn.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (val, rid))
    conn.commit()
    return chosen, len(chosen)


def inject_swap_sqlite(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    p: float,
    seed: int = 42,
) -> Tuple[List[int], int]:
    """Partial Fisher-Yates shuffle: disrupts K2 without changing n_distinct (K1)."""
    rng = random.Random(seed)
    cur = conn.execute(
        f'SELECT rowid, "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY rowid'
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return [], 0
    n_swap = max(2, math.floor(len(rows) * p))
    chosen_indices = rng.sample(range(len(rows)), min(n_swap, len(rows)))
    chosen_rows = [rows[i] for i in chosen_indices]
    chosen_rids = [r[0] for r in chosen_rows]
    chosen_vals = [r[1] for r in chosen_rows]
    rng.shuffle(chosen_vals)
    for rid, val in zip(chosen_rids, chosen_vals):
        conn.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (val, rid))
    conn.commit()
    return chosen_rids, len(chosen_rids)


def inject_stale_sqlite(
    conn: sqlite3.Connection,
    table: str,
    p: float,
    seed: int = 42,
) -> Tuple[int, int]:
    """
    INSERT p-fraction extra rows WITHOUT running ANALYZE.
    Makes sqlite_stat1.stat stale → triggers F2.
    """
    rng = random.Random(seed)
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    cols = [r[1] for r in cur.fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)

    cur = conn.execute(f'SELECT rowid FROM "{table}" ORDER BY rowid')
    all_rids = [r[0] for r in cur.fetchall()]
    n_existing = len(all_rids)
    n_insert = max(1, math.floor(n_existing * p))

    chosen_rids = rng.choices(all_rids, k=n_insert)
    for rid in chosen_rids:
        cur = conn.execute(f'SELECT {col_list} FROM "{table}" WHERE rowid = ?', (rid,))
        src_row = cur.fetchone()
        if src_row:
            placeholders = ", ".join("?" * len(cols))
            conn.execute(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                src_row,
            )
    conn.commit()
    # NOTE: deliberately do NOT run ANALYZE — staleness is the fault
    return n_existing, n_insert


# ------------------------------------------------------------------ #
# Dispatch (SQLite)                                                    #
# ------------------------------------------------------------------ #

FAULT_TYPES = ("null", "range", "swap", "stale")


def inject_fault_sqlite(
    conn: sqlite3.Connection,
    table: str,
    fault_type: str,
    col: str,
    p: float,
    seed: int = 42,
) -> dict:
    """
    Inject one fault into a SQLite table.
    Returns dict with injection metadata (for logging / CSV).
    """
    if fault_type == "null":
        rids, n = inject_null_sqlite(conn, table, col, p, seed)
        return {"fault_type": "null", "col": col, "n_injected": n}
    elif fault_type == "range":
        rids, n = inject_range_sqlite(conn, table, col, p, seed)
        return {"fault_type": "range", "col": col, "n_injected": n}
    elif fault_type == "swap":
        rids, n = inject_swap_sqlite(conn, table, col, p, seed)
        return {"fault_type": "swap", "col": col, "n_injected": n}
    elif fault_type == "stale":
        n_exist, n_ins = inject_stale_sqlite(conn, table, p, seed)
        return {"fault_type": "stale", "col": "ALL", "n_injected": n_ins}
    else:
        raise ValueError(
            f"Unknown fault type: {fault_type!r}. Expected one of {FAULT_TYPES}"
        )


# ------------------------------------------------------------------ #
# PostgreSQL injectors                                                 #
# ------------------------------------------------------------------ #

def inject_fault_postgres(
    conn, table: str, fault_type: str, col: str, p: float, seed: int = 42
) -> dict:
    """
    Inject one fault into a PostgreSQL table.
    conn: open psycopg2 connection (autocommit=False).

    range injection: skips non-numeric columns gracefully — the cast
    MIN(col::float) raises DataError for text-only columns; we catch
    it, ROLLBACK the sub-transaction, and return n_injected=0.
    """
    rng = random.Random(seed)

    if fault_type == "null":
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT ctid FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY ctid'
            )
            candidates = [r[0] for r in cur.fetchall()]
        n = max(1, math.floor(len(candidates) * p))
        chosen = rng.sample(candidates, min(n, len(candidates)))
        with conn.cursor() as cur:
            for ctid in chosen:
                cur.execute(
                    f'UPDATE "{table}" SET "{col}" = NULL WHERE ctid = %s', (ctid,)
                )
        conn.commit()
        return {"fault_type": "null", "col": col, "n_injected": len(chosen)}

    elif fault_type == "range":
        # Python-level numericity check — mirrors the SQLite path.
        # Avoids the SAVEPOINT/DataError pattern that silently returned
        # n_injected=0 for TEXT columns containing numeric-looking values
        # mixed with empty strings (e.g. beers.abv, movies_1.ratingvalue).
        import math as _math
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT "{col}" FROM "{table}" ' 
                    f'WHERE "{col}" IS NOT NULL LIMIT 50'
                )
                samples = [r[0] for r in cur.fetchall()]
        except Exception:
            return {"fault_type": "range", "col": col, "n_injected": 0}

        if not samples:
            return {"fault_type": "range", "col": col, "n_injected": 0}

        numeric: list = []
        for v in samples:
            try:
                fv = float(str(v))
                if _math.isfinite(fv):
                    numeric.append(fv)
            except (ValueError, TypeError):
                pass

        if not numeric or len(numeric) / len(samples) < 0.5:
            return {"fault_type": "range", "col": col, "n_injected": 0}

        col_min = min(numeric)
        col_max = max(numeric)
        if not (_math.isfinite(col_min) and _math.isfinite(col_max)):
            return {"fault_type": "range", "col": col, "n_injected": 0}

        spread = col_max - col_min
        delta  = 0.1 * spread if spread > 0 else 1.0
        lo_val, hi_val = col_min - delta, col_max + delta

        with conn.cursor() as cur:
            cur.execute(
                f'SELECT ctid FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY ctid'
            )
            candidates = [r[0] for r in cur.fetchall()]
        n = max(1, math.floor(len(candidates) * p))
        chosen = rng.sample(candidates, min(n, len(candidates)))
        with conn.cursor() as cur:
            for i, ctid in enumerate(chosen):
                val = str(lo_val if i % 2 == 0 else hi_val)
                cur.execute(
                    f'UPDATE "{table}" SET "{col}" = %s WHERE ctid = %s', (val, ctid)
                )
        conn.commit()
        return {"fault_type": "range", "col": col, "n_injected": len(chosen)}

    elif fault_type == "swap":
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT ctid, "{col}" FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL ORDER BY ctid'
            )
            rows = cur.fetchall()
        if len(rows) < 2:
            return {"fault_type": "swap", "col": col, "n_injected": 0}
        n = max(2, math.floor(len(rows) * p))
        chosen_idx = rng.sample(range(len(rows)), min(n, len(rows)))
        chosen_rows = [rows[i] for i in chosen_idx]
        ctids = [r[0] for r in chosen_rows]
        vals  = [r[1] for r in chosen_rows]
        rng.shuffle(vals)
        with conn.cursor() as cur:
            for ctid, val in zip(ctids, vals):
                cur.execute(
                    f'UPDATE "{table}" SET "{col}" = %s WHERE ctid = %s', (val, ctid)
                )
        conn.commit()
        return {"fault_type": "swap", "col": col, "n_injected": len(ctids)}

    elif fault_type == "stale":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND table_schema='public' ORDER BY ordinal_position",
                (table,),
            )
            cols = [r[0] for r in cur.fetchall()]
            col_list = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            n_exist = cur.fetchone()[0]
        n_insert = max(1, math.floor(n_exist * p))
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{table}" ({col_list}) '
                f'SELECT {col_list} FROM "{table}" ORDER BY random() LIMIT %s',
                (n_insert,),
            )
        conn.commit()
        # Do NOT run ANALYZE — staleness is the fault
        return {"fault_type": "stale", "col": "ALL", "n_injected": n_insert}

    else:
        raise ValueError(f"Unknown fault type: {fault_type!r}")
