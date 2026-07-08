"""
Step 0 (pre-migration audit): scan all retained SQLite databases for two
risks that matter for the pgloader-based step 4/5 migration:

1. Identifier risk: table/column names containing uppercase letters, spaces,
   or punctuation. PostgreSQL lowercases unquoted identifiers, and pgloader's
   documented default WITH clause for SQLite sources includes
   `downcase identifiers` with no documented override for SQLite specifically
   (unlike MySQL/PostgreSQL sources, which expose `quote identifiers`). Any
   identifier outside [a-z0-9_] is a candidate for silent mismatch between
   what `artifacts/schema_rename_map.json` expects and what actually lands
   in `pg_base`.
2. Type risk: columns declared INTEGER/REAL/NUMERIC/etc. in SQLite's schema
   but containing non-numeric string values in their actual data (e.g. a
   "Price" column holding "$4.99"). This was the original justification for
   moving step 4 from a hand-rolled Python loader to pgloader — auditing
   confirms whether that diagnosis was correct.

This is read-only against the SQLite source files; it does not touch
PostgreSQL or any pipeline output. Run it before (re)running step 4 to know
which databases need manual attention.

Reads:  artifacts/retained_dbs.json
        data/{split}/{split}_databases/<db_id>/<db_id>.sqlite

Writes: artifacts/sqlite_identifier_audit.jsonl (one JSON object per db_id)

Run: uv run python pipeline/00_audit_sqlite_identifiers.py
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _pg_helpers import find_sqlite_path

ARTIFACTS = Path("artifacts")
OUT_PATH = ARTIFACTS / "sqlite_identifier_audit.jsonl"
SAMPLE_SIZE = 500
IDENT_RISK_RE = re.compile(r"[^a-z0-9_]")  # anything not lowercase/digit/underscore


def is_numeric_value(v) -> bool:
    if v is None:
        return True
    try:
        float(str(v))
        return True
    except (ValueError, TypeError):
        return False


def scan_one(db_id: str) -> dict:
    path = find_sqlite_path(db_id)
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]

    risky_identifiers = []  # {table, column_or_null, name, reason}
    mixed_type_columns = []  # {table, column, declared_type, sample_offenders}

    for tbl in tables:
        if IDENT_RISK_RE.search(tbl):
            risky_identifiers.append({
                "table": tbl, "column": None, "name": tbl,
                "reason": "non-lowercase/space/punct in table name",
            })
        cur.execute(f'PRAGMA table_info("{tbl}")')
        cols = cur.fetchall()
        for r in cols:
            col_name = r[1]
            declared = (r[2] or "").strip()
            if IDENT_RISK_RE.search(col_name):
                risky_identifiers.append({
                    "table": tbl, "column": col_name, "name": col_name,
                    "reason": "non-lowercase/space/punct in column name",
                })

            upper = declared.upper()
            base = upper.split("(")[0].strip()
            looks_numeric_declared = any(k in base for k in ("INT", "REAL", "FLOAT", "DOUBLE", "NUM", "DEC"))
            if not looks_numeric_declared:
                continue
            try:
                cur.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{tbl}" WHERE "{col_name}" IS NOT NULL LIMIT {SAMPLE_SIZE}'
                )
                sample = [row[0] for row in cur.fetchall()]
            except sqlite3.OperationalError:
                continue
            offenders = [v for v in sample if not is_numeric_value(v)]
            if offenders:
                mixed_type_columns.append({
                    "table": tbl,
                    "column": col_name,
                    "declared_type": declared,
                    "sample_offenders": [str(o) for o in offenders[:5]],
                })

    conn.close()
    return {
        "db_id": db_id,
        "n_tables": len(tables),
        "risky_identifiers": risky_identifiers,
        "mixed_type_columns": mixed_type_columns,
    }


def main():
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)

    OUT_PATH.parent.mkdir(exist_ok=True)
    results = []
    with open(OUT_PATH, "w", encoding="utf-8") as out:
        for i, db_id in enumerate(dbs):
            print(f"[{i+1}/{len(dbs)}] {db_id}", end=" ... ", flush=True)
            try:
                result = scan_one(db_id)
            except Exception as e:
                result = {"db_id": db_id, "error": str(e)}
            results.append(result)
            out.write(json.dumps(result) + "\n")
            n_risky = len(result.get("risky_identifiers", []))
            n_mixed = len(result.get("mixed_type_columns", []))
            print(f"{n_risky} risky identifiers, {n_mixed} mixed-type columns")

    total_risky = sum(len(r.get("risky_identifiers", [])) for r in results)
    total_mixed = sum(len(r.get("mixed_type_columns", [])) for r in results)
    dbs_with_risky = sum(1 for r in results if r.get("risky_identifiers"))
    dbs_with_mixed = sum(1 for r in results if r.get("mixed_type_columns"))
    print(f"\nDone. {len(results)} DBs scanned.")
    print(f"Risky identifiers: {total_risky} total, across {dbs_with_risky} DBs.")
    print(f"Mixed-type columns: {total_mixed} total, across {dbs_with_mixed} DBs.")
    print(f"Full results written to {OUT_PATH}")


if __name__ == "__main__":
    main()
