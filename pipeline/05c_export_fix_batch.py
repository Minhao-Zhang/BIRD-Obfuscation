"""
Export a batch of transpilation_needs_fix rows for coding subagents to repair.

Each exported record includes everything an agent needs: question, evidence, SQLite
gold SQL, failed sqlglot attempt, pg_error, and pg_base schema DDL.

Subagents should write fixes to workdir/transpilation_fixes.jsonl:
  {"question_id": "<same id>", "sql_base": "<corrected PostgreSQL SQL>"}

Run:
  uv run python pipeline/05c_export_fix_batch.py --limit 20 --out workdir/fix_batches/batch_0.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from _transpile_helpers import (  # noqa: E402
    FAILURES_PATH,
    NEEDS_FIX_PATH,
    OUTPUT_PATHS,
    get_pg_schema_ddl,
    load_done_ids,
    load_jsonl,
)

PG_BASE_DSN = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"


def main():
    parser = argparse.ArgumentParser(description="Export needs_fix batch for agents")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    done_ids = load_done_ids(*OUTPUT_PATHS.values(), FAILURES_PATH)
    pending = [r for r in load_jsonl(NEEDS_FIX_PATH) if r["question_id"] not in done_ids]
    batch = pending[args.offset : args.offset + args.limit]

    pg_conn = psycopg2.connect(PG_BASE_DSN)
    pg_conn.autocommit = False
    ddl_cache: dict[str, str] = {}
    enriched = []
    try:
        for rec in batch:
            db_id = rec["db_id"]
            if db_id not in ddl_cache:
                ddl_cache[db_id] = get_pg_schema_ddl(pg_conn, db_id)
            enriched.append({**rec, "pg_schema_ddl": ddl_cache[db_id]})
    finally:
        pg_conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for rec in enriched:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Exported {len(enriched)} / {len(pending)} pending -> {args.out}")


if __name__ == "__main__":
    main()
