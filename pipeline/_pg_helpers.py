"""Shared helper for locating BIRD SQLite source files.

find_sqlite_path() is the only function still used anywhere (step 0's audit
and step 4's pgloader-based load, both of which read SQLite directly). The
type-inference and streaming-COPY helpers that used to live here were
removed when step 6 stopped reloading from SQLite in favor of cloning
pg_base's Docker volume and renaming in place — see
06_build_pg_rename.py and docs/methodology/obfuscation.md §5 step 5.
"""

from pathlib import Path

DATA = Path("data")


def find_sqlite_path(db_id: str) -> Path:
    for split in ("train", "dev"):
        p = DATA / split / f"{split}_databases" / db_id / f"{db_id}.sqlite"
        if p.exists():
            return p
    raise FileNotFoundError(f"SQLite not found for {db_id}")
