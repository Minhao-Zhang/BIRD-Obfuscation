"""
Step 6: Rename tables/columns in pg_rename per schema_rename_map.

pg_rename's Docker volume must already be a clone of pg_base's
before running this script (see AGENTS.md "Running the pipeline" for the
docker run command) — this script does NOT load any data and does NOT
connect to pg_base. It only renames identifiers in an already-populated
pg_rename, table-by-table and column-by-column, via ALTER TABLE ...
RENAME. Renaming is a catalog-only metadata operation in PostgreSQL (no
table rewrite), so row data, row counts, and column types are identical
to pg_base by construction — there is no second type-inference pass
that could disagree with pgloader's.

Reads:
  artifacts/retained_dbs.json
  artifacts/schema_rename_map.json

Writes: Renames tables/columns in-place in pg_rename (port 5433)

Run: uv run python pipeline/06_build_pg_rename.py
"""

import json
import sys
from pathlib import Path

import psycopg2

PG_RENAME_DSN = "host=127.0.0.1 port=5433 dbname=bird user=bird password=bird"
ARTIFACTS = Path("artifacts")


def rename_schema_objects(pg_cur, db_id: str, rename_map: dict) -> tuple[int, int]:
    """Rename all tables and columns in db_id's schema per rename_map.
    Returns (n_tables_renamed, n_columns_renamed)."""
    pg_cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        (db_id,),
    )
    tables = [r[0] for r in pg_cur.fetchall()]

    n_cols = 0
    for tbl in tables:
        pg_cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (db_id, tbl),
        )
        cols = [r[0] for r in pg_cur.fetchall()]
        for col in cols:
            new_col = rename_map.get(col, col)
            if new_col != col:
                pg_cur.execute(
                    f'ALTER TABLE "{db_id}"."{tbl}" '
                    f'RENAME COLUMN "{col}" TO "{new_col}"'
                )
                n_cols += 1

    n_tables = 0
    for tbl in tables:
        new_tbl = rename_map.get(tbl, tbl)
        if new_tbl != tbl:
            pg_cur.execute(
                f'ALTER TABLE "{db_id}"."{tbl}" RENAME TO "{new_tbl}"'
            )
            n_tables += 1

    return n_tables, n_cols


def main():
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)
    with open(ARTIFACTS / "schema_rename_map.json", encoding="utf-8") as f:
        all_rename_maps = json.load(f)

    pg_conn = psycopg2.connect(PG_RENAME_DSN)
    pg_cur = pg_conn.cursor()

    total_tables = total_cols = 0
    failures = []
    for i, db_id in enumerate(dbs):
        print(f"[{i+1}/{len(dbs)}] {db_id}", end=" ... ", flush=True)
        rename_map = all_rename_maps.get(db_id, {})
        try:
            n_tables, n_cols = rename_schema_objects(pg_cur, db_id, rename_map)
            pg_conn.commit()
            total_tables += n_tables
            total_cols += n_cols
            print(f"{n_tables} tables renamed, {n_cols} columns renamed")
        except Exception as e:
            pg_conn.rollback()
            print(f"ERROR: {e}")
            failures.append((db_id, str(e)))

    pg_cur.close()
    pg_conn.close()
    print(f"\nDone. Total: {total_tables:,} tables renamed, {total_cols:,} columns renamed.")

    # Fail LOUD: a per-DB rollback leaves that DB UNRENAMED (identical to pg_base),
    # which would silently drop it from the obfuscated deliverable with only a
    # console line as the tell (e.g. a same-scope rename-map collision -> duplicate
    # identifier -> the whole DB rolls back). Surface it and exit non-zero so the
    # run can't be mistaken for success.
    if failures:
        print(f"\n!! {len(failures)} database(s) FAILED to rename and were left unrenamed:")
        for db_id, err in failures:
            print(f"   - {db_id}: {err}")
        print("   Fix the rename map (e.g. resolve identifier collisions) and re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
