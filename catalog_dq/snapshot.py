"""
snapshot.py — Column-level statistics snapshot data structures.

A Snapshot captures, for one (db, table) pair at one point in time,
all per-column statistics that the seven inference rules need.
"""
from __future__ import annotations
import json
import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


@dataclass
class ColStats:
    """Statistics for one column in one snapshot."""
    # C1 — Completeness
    null_frac: Optional[float] = None        # fraction of NULLs [0,1]

    # V1 — Validity: range bounds from histogram_bounds[0] and [-1]
    histogram_min: Optional[float] = None
    histogram_max: Optional[float] = None

    # V2 — Validity: most-common values (as sorted list for Jaccard)
    mcv_vals: Optional[List[Any]] = None     # most_common_vals (strings)
    mcv_freqs: Optional[List[float]] = None  # most_common_freqs

    # K1 — Consistency: normalised distinct count = |n_distinct| / row_count_est
    n_distinct_norm: Optional[float] = None

    # K2 — Consistency: Pearson spatial correlation (pg_stats.correlation)
    correlation: Optional[float] = None


@dataclass
class Snapshot:
    """
    One catalog snapshot for a table.

    analyze_time : ISO-8601 string or None.
        PostgreSQL: read from pg_stat_all_tables.last_analyze /
                    last_autoanalyze (whichever is newer).
        SQLite:     recorded externally by the caller immediately after
                    ANALYZE; None means never analyzed.
    row_count_est : approximate row count.
        PostgreSQL: pg_class.reltuples
        SQLite:     first token of sqlite_stat1.stat for the table entry
    """
    timestamp: str                           # when this snapshot was taken
    db_type: str                             # 'sqlite' | 'postgres'
    db_path: str                             # connection string / file path
    table: str
    analyze_time: Optional[str]
    row_count_est: Optional[int]
    columns: Dict[str, ColStats] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Serialisation                                                        #
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        cols_raw = d.pop("columns", {})
        snap = cls(**d)
        snap.columns = {k: ColStats(**v) for k, v in cols_raw.items()}
        return snap

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, default=str)

    @classmethod
    def load(cls, path: str) -> "Snapshot":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @staticmethod
    def now_iso() -> str:
        return datetime.datetime.utcnow().isoformat()
