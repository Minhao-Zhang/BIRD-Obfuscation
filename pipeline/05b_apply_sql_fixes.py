"""
Step 5 pass 2: Validate agent-proposed SQL fixes and merge into transpiled output.

Agents (coding subagents) append one line per fix to workdir/transpilation_fixes.jsonl:
  {"question_id": "...", "sql_base": "SELECT ..."}

This script re-runs R0==R1 for each proposed fix against pg_base. On success the
question moves to {train,test}_transpiled.jsonl; on failure it goes to
transpilation_failures.jsonl.

Run after agents have written fixes:
  uv run python pipeline/05b_apply_sql_fixes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from _transpile_helpers import (  # noqa: E402
    FAILURES_PATH,
    FIXES_PATH,
    NEEDS_FIX_PATH,
    OUTPUT_PATHS,
    append_jsonl,
    compare_r0_r1,
    load_done_ids,
    load_jsonl,
    postprocess_pg_sql,
    transpile_status,
)

PG_BASE_DSN = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"


def main():
    if not FIXES_PATH.exists():
        print(f"No {FIXES_PATH} — nothing to apply.")
        return

    needs_by_id = {r["question_id"]: r for r in load_jsonl(NEEDS_FIX_PATH)}
    done_ids = load_done_ids(*OUTPUT_PATHS.values(), FAILURES_PATH)

    fixes: list[dict] = []
    seen: set[str] = set()
    with open(FIXES_PATH, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            qid = rec["question_id"]
            if qid in seen:
                continue
            seen.add(qid)
            if qid in done_ids:
                continue
            fixes.append(rec)

    print(f"Applying {len(fixes)} agent fixes ({len(done_ids)} already resolved)")

    pg_conn = psycopg2.connect(PG_BASE_DSN)
    pg_conn.autocommit = False
    ok = fail = 0

    try:
        for fix in tqdm(fixes, desc="apply-fixes", unit="q"):
            qid = fix["question_id"]
            pending = needs_by_id.get(qid)
            if not pending:
                append_jsonl(FAILURES_PATH, {
                    **fix,
                    "error": "fix_unknown_question_id",
                })
                fail += 1
                continue

            db_id = pending["db_id"]
            split = pending["split"]
            sqlite_sql = pending["sql_sqlite"]
            pg_sql = postprocess_pg_sql(fix["sql_base"], db_id)

            try:
                match, pg_error = compare_r0_r1(db_id, sqlite_sql, pg_sql, pg_conn)
            except Exception as e:
                append_jsonl(FAILURES_PATH, {
                    **pending,
                    "sql_base": pg_sql,
                    "error": f"sqlite_exec_failed: {e}",
                })
                fail += 1
                continue

            if match:
                out = {k: v for k, v in pending.items()
                       if k not in ("error", "pg_error", "split")}
                out["sql_base"] = pg_sql
                append_jsonl(OUTPUT_PATHS[split], out)
                ok += 1
            else:
                append_jsonl(FAILURES_PATH, {
                    **pending,
                    "sql_base": pg_sql,
                    "error": "r0_r1_mismatch_after_agent_fix",
                    "pg_error": pg_error,
                })
                fail += 1
    finally:
        pg_conn.close()

    s = transpile_status()
    print(f"Apply done: +{ok} ok, +{fail} rejected. Pending needs_fix: {s['needs_fix_pending']}")


if __name__ == "__main__":
    main()
