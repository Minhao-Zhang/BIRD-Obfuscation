"""
Step 5 pass 1: Transpile SQLite gold SQL → PostgreSQL via sqlglot and validate R0==R1.

No LLM / API calls. Questions sqlglot cannot fix are queued for manual agent repair.

  Match  → workdir/{train,test}_transpiled.jsonl
  Miss   → workdir/transpilation_needs_fix.jsonl  (agent/subagent fixes these)
  SQLite exec error → workdir/transpilation_failures.jsonl

After pass 1, use coding agents to fix the queue in parallel, append proposed SQL to
workdir/transpilation_fixes.jsonl, then run pipeline/05b_apply_sql_fixes.py to validate.

Run:
  uv run python pipeline/05_transpile_sql.py
  uv run python pipeline/05_transpile_sql.py --status
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
    FIXES_PATH,
    NEEDS_FIX_PATH,
    OUTPUT_PATHS,
    append_jsonl,
    compare_r0_r1,
    load_done_ids,
    postprocess_pg_sql,
    transpile_sql,
    transpile_status,
)

PG_BASE_DSN = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"
ARTIFACTS = Path("artifacts")


def pass1_question(q: dict, pg_conn) -> tuple[dict | None, dict | None, dict | None]:
    """Returns (success, needs_fix, hard_failure)."""
    db_id = q["db_id"]
    sqlite_sql = q["sql_sqlite"]

    try:
        pg_sql = postprocess_pg_sql(transpile_sql(sqlite_sql, db_id), db_id)
        match, pg_error = compare_r0_r1(db_id, sqlite_sql, pg_sql, pg_conn)
    except Exception as e:
        return None, None, {**q, "error": f"sqlite_exec_failed: {e}"}

    if match:
        return {**q, "sql_base": pg_sql}, None, None

    return None, {
        **q,
        "sql_base": pg_sql,
        "error": "pg_exec_error" if pg_error else "r0_r1_mismatch",
        "pg_error": pg_error,
    }, None


def pass1_file(
    input_path: Path,
    output_path: Path,
    needs_fix_path: Path,
    failures_path: Path,
    pg_conn,
    label: str,
    split: str,
):
    with open(input_path, encoding="utf-8") as f:
        questions = [json.loads(line) for line in f]

    done_ids = load_done_ids(output_path, needs_fix_path, failures_path)
    remaining = [q for q in questions if q["question_id"] not in done_ids]
    total = len(questions)
    split_done = total - len(remaining)
    print(
        f"\n{label}: {total} total, {split_done} done, "
        f"{len(remaining)} remaining (sqlglot only)"
    )

    ok = queued = fail = 0
    for i, q in enumerate(remaining, 1):
        good, needs, hard = pass1_question(q, pg_conn)
        if good:
            append_jsonl(output_path, good)
            ok += 1
        elif needs:
            append_jsonl(needs_fix_path, {**needs, "split": split})
            queued += 1
        elif hard:
            append_jsonl(failures_path, hard)
            fail += 1

        done = split_done + i
        pct = 100 * done / total
        print(
            f"{label}: {done}/{total} ({pct:.1f}%) "
            f"+{ok} ok, +{queued} needs_fix, +{fail} fail "
            f"[{q['question_id']}]",
            flush=True,
        )

    print(f"  Done {label}: +{ok} ok, +{queued} needs_fix, +{fail} hard fail")


def main():
    parser = argparse.ArgumentParser(description="Step 5 pass 1: sqlglot transpile + R0==R1")
    parser.add_argument("--status", action="store_true", help="Print progress counts and exit")
    args = parser.parse_args()

    if args.status:
        s = transpile_status()
        print(json.dumps(s, indent=2))
        return

    pg_conn = psycopg2.connect(PG_BASE_DSN)
    pg_conn.autocommit = False
    try:
        pass1_file(
            ARTIFACTS / "train.jsonl",
            OUTPUT_PATHS["train"],
            NEEDS_FIX_PATH,
            FAILURES_PATH,
            pg_conn,
            "train",
            "train",
        )
        pass1_file(
            ARTIFACTS / "test.jsonl",
            OUTPUT_PATHS["test"],
            NEEDS_FIX_PATH,
            FAILURES_PATH,
            pg_conn,
            "test",
            "test",
        )
    finally:
        pg_conn.close()

    s = transpile_status()
    print(
        f"\nPass 1 complete. {s['needs_fix_pending']} questions need agent fixes "
        f"-> see {NEEDS_FIX_PATH.name}. Then run 05b after agents append to "
        f"{FIXES_PATH.name}."
    )


if __name__ == "__main__":
    main()
