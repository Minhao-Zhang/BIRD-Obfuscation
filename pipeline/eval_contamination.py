"""
Obfuscation effectiveness check (evaluation.md §4): run the BIRD test set
through an OpenAI model under 4 conditions and measure execution accuracy.

  base_hint:     base schema (original ids),   with evidence
  base_nohint:   base schema (original ids),   no evidence
  rename_hint:   rename schema (renamed ids),  with evidence
  rename_nohint: rename schema (renamed ids),  no evidence

This is downstream evaluation, not part of the core obfuscation pipeline
(see docs/methodology/evaluation.md §2 "Scope Boundary") — not numbered as
a pipeline step on purpose.

The default is split-machine offline evaluation: prepare frozen prompts from
PostgreSQL, generate SQL on an API-only machine, then return generations for
DB-side grading. ``--local`` retains the legacy same-machine path. In either
mode each condition gets one model query and one execution, with no feedback.

Reads:
  artifacts/test_final.jsonl
  artifacts/db_language_map.json (for the per-language breakdown)
  live pg_base (5432) / pg_rename (5433) for schema DDL + grading

Writes:
  eval/contamination_results.jsonl  — one record per (question_id, condition), resumable

Run:
  uv run python pipeline/eval_contamination.py --prepare-only
  uv run python pipeline/eval_contamination.py --split train --prepare-only
  uv run python pipeline/eval_contamination.py --local --model gpt-5.5   # legacy
  uv run python pipeline/eval_contamination.py --summarize                # print EX/deltas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from _db import (
    PG_BASE_DSN,
    PG_RENAME_DSN,
    new_connection,
)
from _eval_helpers import (
    SYSTEM_INSTRUCTIONS,
    append_result,
    build_eval_metadata,
    build_prompt,
    dataset_path,
    extract_sql,
    get_schema_ddl,
    grade,
    load_done_keys,
    metadata_matches,
    utc_now_iso,
    usage_dict,
)
from _offline_eval import (
    GENERATIONS_NAME,
    default_bundle_dir,
    eval_metadata_from_manifest,
    load_manifest,
)

load_dotenv()

ARTIFACTS = Path("artifacts")
EVAL_DIR = Path("eval")
RESULTS_PATH = EVAL_DIR / "contamination_results.jsonl"
PROMPT_VERSION = "contamination-v1"

CONDITIONS = ["base_hint", "base_nohint", "rename_hint", "rename_nohint"]
# schema: which PG instance's DDL/data to use. sql_field: which gold-SQL
# field in test_final.jsonl to grade against. evidence_field: None means
# no evidence is shown to the model for that condition.
CONDITION_SPEC = {
    "base_hint":     {"schema": "base", "sql_field": "sql_base", "evidence_field": "evidence"},
    "base_nohint":   {"schema": "base", "sql_field": "sql_base", "evidence_field": None},
    "rename_hint":   {"schema": "rename", "sql_field": "sql_rename", "evidence_field": "evidence_rename"},
    "rename_nohint": {"schema": "rename", "sql_field": "sql_rename", "evidence_field": None},
}
DSN_FOR_SCHEMA = {"base": PG_BASE_DSN, "rename": PG_RENAME_DSN}


def load_test_questions(limit: int | None) -> list[dict]:
    with open(dataset_path("test_final.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    seen = {}
    for r in rows:
        seen[r["question_id"]] = r  # de-dupe, keep last (matches step 7's own convention)
    rows = list(seen.values())
    if limit:
        rows = rows[:limit]
    return rows


async def run_one(client, sem, model, effort, eval_metadata, question, condition, ddl_cache, conn_pool, lock):
    import openai

    spec = CONDITION_SPEC[condition]
    db_id = question["db_id"]
    schema = spec["schema"]

    async with sem:
        cache_key = (schema, db_id)
        async with lock:
            if cache_key not in ddl_cache:
                conn = conn_pool[schema]
                ddl_cache[cache_key] = get_schema_ddl(conn, db_id)
        schema_ddl = ddl_cache[cache_key]

        evidence = question.get(spec["evidence_field"]) if spec["evidence_field"] else None
        prompt = build_prompt(db_id, schema_ddl, question["question"], evidence)

        record = {
            "question_id": question["question_id"],
            "db_id": db_id,
            "condition": condition,
            "eval_metadata": eval_metadata,
            "recorded_at_utc": utc_now_iso(),
        }

        call_start = time.monotonic()
        try:
            response = await client.responses.create(
                model=model,
                instructions=SYSTEM_INSTRUCTIONS,
                input=prompt,
                reasoning={"effort": effort},
            )
            generated_sql = extract_sql(response.output_text)
            record["usage"] = usage_dict(response.usage)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError) as e:
            # Transient — the SDK already retried this internally (max_retries,
            # default 2) before raising. Don't write a record: leaving this
            # (question_id, condition) pair absent from contamination_results.jsonl means
            # load_done_keys() will pick it up again on the next invocation
            # instead of permanently marking a TPD-limit hiccup as a failure.
            print(f"  transient error on {question['question_id']}/{condition}, will retry next run: {e}",
                  flush=True)
            return None
        except Exception as e:
            record.update(
                generated_sql=None, correct=False, correct_strict=False, error=f"llm_call_failed: {e}",
                latency_sec=round(time.monotonic() - call_start, 3),
            )
            append_result(record, RESULTS_PATH)
            return record

        record["latency_sec"] = round(time.monotonic() - call_start, 3)
        record["generated_sql"] = generated_sql
        conn = conn_pool[schema]
        gold_sql = question[spec["sql_field"]]

        correct, correct_strict, error = grade(conn, gold_sql, generated_sql)
        record["correct"] = correct
        record["correct_strict"] = correct_strict
        if error is not None:
            record["error"] = error

        append_result(record, RESULTS_PATH)
        return record


def group_by_schema_and_db(tasks_todo: list[tuple[dict, str]]) -> list[tuple[tuple[str, str], list[tuple[dict, str]]]]:
    """Group (question, condition) pairs by (schema, db_id) in first-seen order,
    so the caller can finish one DB/schema's calls before starting the next —
    keeps prompt-cache hits high (same DDL prefix) instead of letting concurrent
    calls to unrelated DBs interleave and evict each other's cache entries."""
    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for q, cond in tasks_todo:
        key = (CONDITION_SPEC[cond]["schema"], q["db_id"])
        groups.setdefault(key, []).append((q, cond))
    return list(groups.items())


def run_metadata(model: str, effort: str) -> dict:
    return build_eval_metadata(
        eval_name="contamination",
        model=model,
        effort=effort,
        prompt_version=PROMPT_VERSION,
        dataset_files=["test_final.jsonl"],
    )


async def run_eval(model: str, effort: str, limit: int | None, concurrency: int) -> None:
    from openai import AsyncOpenAI

    EVAL_DIR.mkdir(exist_ok=True)
    questions = load_test_questions(limit)
    eval_metadata = run_metadata(model, effort)
    done = load_done_keys(RESULTS_PATH, expected_metadata=eval_metadata)

    tasks_todo = [
        (q, cond) for q in questions for cond in CONDITIONS
        if (q["question_id"], cond) not in done
    ]
    total = len(questions) * len(CONDITIONS)
    print(f"{len(questions)} questions x {len(CONDITIONS)} conditions = {total} total, "
          f"{len(done)} done, {len(tasks_todo)} remaining")

    if not tasks_todo:
        print("Nothing to do.")
        return

    groups = group_by_schema_and_db(tasks_todo)

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    ddl_cache: dict[tuple[str, str], str] = {}
    conn_pool = {
        "base": new_connection(PG_BASE_DSN),
        "rename": new_connection(PG_RENAME_DSN),
    }

    completed = 0
    run_start = time.monotonic()
    try:
        for gi, (key, group) in enumerate(groups, 1):
            coros = [
                run_one(client, sem, model, effort, eval_metadata, q, cond, ddl_cache, conn_pool, lock)
                for q, cond in group
            ]
            for coro in asyncio.as_completed(coros):
                await coro
                completed += 1
                if completed % 50 == 0:
                    print(f"{completed}/{len(tasks_todo)} done", flush=True)
            print(f"[{gi}/{len(groups)}] finished schema={key[0]} db_id={key[1]} "
                  f"({len(group)} calls)", flush=True)
    finally:
        for conn in conn_pool.values():
            conn.close()

    elapsed = time.monotonic() - run_start
    print(f"Done. {completed}/{len(tasks_todo)} processed this run in {elapsed:.1f}s "
          f"({elapsed / completed:.2f}s/call avg)." if completed else
          f"Done. {completed}/{len(tasks_todo)} processed this run in {elapsed:.1f}s.")


def summarize(expected_metadata: dict | None = None) -> None:
    if not RESULTS_PATH.exists():
        print(f"No {RESULTS_PATH} yet.")
        return

    lang_map = {}
    lang_map_path = dataset_path("db_language_map.json")
    if lang_map_path.exists():
        with open(lang_map_path, encoding="utf-8") as f:
            lang_map = json.load(f)

    by_key: dict[tuple[str, str], dict] = {}
    with open(RESULTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not metadata_matches(rec.get("eval_metadata"), expected_metadata):
                continue
            by_key[(rec["question_id"], rec["condition"])] = rec  # last write wins (resume-safe)

    counts = {c: {"n": 0, "correct": 0, "correct_strict": 0} for c in CONDITIONS}
    by_lang = {}
    tokens = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    error_counts: dict[str, int] = {}
    latencies = []
    for (qid, cond), rec in by_key.items():
        counts[cond]["n"] += 1
        counts[cond]["correct"] += int(rec.get("correct", False))
        counts[cond]["correct_strict"] += int(rec.get("correct_strict", rec.get("correct", False)))
        lang = lang_map.get(rec["db_id"], "unknown")
        by_lang.setdefault(lang, {c: {"n": 0, "correct": 0} for c in CONDITIONS})
        by_lang[lang][cond]["n"] += 1
        by_lang[lang][cond]["correct"] += int(rec.get("correct", False))
        usage = rec.get("usage")
        if usage:
            for k in tokens:
                tokens[k] += usage.get(k, 0)
        error = rec.get("error")
        if error:
            error_type = error.split(":", 1)[0]
            error_counts[error_type] = error_counts.get(error_type, 0) + 1
        if rec.get("latency_sec") is not None:
            latencies.append(rec["latency_sec"])

    def ex(c):
        n = counts[c]["n"]
        return counts[c]["correct"] / n if n else float("nan")

    def ex_strict(c):
        n = counts[c]["n"]
        return counts[c]["correct_strict"] / n if n else float("nan")

    print("Execution accuracy (EX) by condition — lenient (BIRD-style, type-collapsing) "
          "vs strict (no cross-type match):")
    for c in CONDITIONS:
        print(f"  {c}: lenient {ex(c):.4f} ({counts[c]['correct']}/{counts[c]['n']})   "
              f"strict {ex_strict(c):.4f} ({counts[c]['correct_strict']}/{counts[c]['n']})")

    print(f"\nDelta (no hints, primary signal) = EX(base_nohint) - EX(rename_nohint) = {ex('base_nohint') - ex('rename_nohint'):.4f}")
    print(f"Delta (hints, BIRD-comparable)    = EX(base_hint) - EX(rename_hint) = {ex('base_hint') - ex('rename_hint'):.4f}")

    print("\nBy obfuscation language:")
    for lang in sorted(by_lang):
        row = by_lang[lang]

        def lex(c):
            n = row[c]["n"]
            return row[c]["correct"] / n if n else float("nan")

        print(f"  {lang}: base_hint={lex('base_hint'):.3f} base_nohint={lex('base_nohint'):.3f} "
              f"rename_hint={lex('rename_hint'):.3f} rename_nohint={lex('rename_nohint'):.3f} "
              f"delta(no-hints)={lex('base_nohint') - lex('rename_nohint'):.3f}")

    cache_hit_rate = tokens["cached_tokens"] / tokens["input_tokens"] if tokens["input_tokens"] else 0.0
    print(
        f"\nToken usage: input={tokens['input_tokens']:,} (cached={tokens['cached_tokens']:,}, "
        f"{cache_hit_rate:.1%} hit rate) output={tokens['output_tokens']:,} "
        f"(reasoning={tokens['reasoning_tokens']:,}) total={tokens['total_tokens']:,}"
    )

    total_recs = len(by_key)
    print(f"\nErrors ({sum(error_counts.values())}/{total_recs} records):")
    if error_counts:
        for error_type, n in sorted(error_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {error_type}: {n}")
    else:
        print("  none")

    if latencies:
        latencies_sorted = sorted(latencies)
        p50 = latencies_sorted[len(latencies_sorted) // 2]
        p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)]
        print(
            f"\nLLM call latency: mean={sum(latencies) / len(latencies):.2f}s "
            f"p50={p50:.2f}s p95={p95:.2f}s max={max(latencies):.2f}s (n={len(latencies)})"
        )


def main():
    parser = argparse.ArgumentParser(description="Contamination check (base/rename schema x hints)")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--effort", default="low", choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    parser.add_argument("--limit", type=int, default=None, help="Limit number of test questions (for dry runs)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--summarize", action="store_true", help="Print EX/deltas from existing results and exit")
    parser.add_argument("--local", action="store_true",
                        help="legacy single-machine API+PostgreSQL run")
    parser.add_argument("--split", choices=["test", "train"], default="test")
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument("--generations", type=Path, default=None,
                        help="returned API-machine generations JSONL")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--overwrite-bundle", action="store_true")
    args = parser.parse_args()

    bundle_dir = args.bundle_dir or default_bundle_dir("contamination", args.split)
    if args.summarize:
        if not args.local and (bundle_dir / "manifest.json").exists():
            summarize(eval_metadata_from_manifest(
                load_manifest(bundle_dir),
                model=args.model,
                effort=args.effort,
            ))
        else:
            summarize(run_metadata(args.model, args.effort))
        return

    if args.local:
        if args.split != "test":
            raise SystemExit("--local only supports --split test; use the default offline workflow for train.")
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY not set. Copy .env.example to .env and fill it in.")
        asyncio.run(run_eval(args.model, args.effort, args.limit, args.concurrency))
        summarize(run_metadata(args.model, args.effort))
        return

    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.exists() and not args.overwrite_bundle:
        existing = load_manifest(bundle_dir)
        if (
            existing.get("conditions") != CONDITIONS
            or existing.get("split", "test") != args.split
        ):
            raise SystemExit(
                f"Existing bundle {bundle_dir} has "
                f"conditions={existing.get('conditions')} "
                f"split={existing.get('split', 'test')}; pass "
                "--overwrite-bundle or choose another --bundle-dir."
            )
        print(f"Reusing existing offline bundle: {bundle_dir}")
    else:
        command = [
            sys.executable,
            str(Path(__file__).with_name("prepare_offline_eval.py")),
            "--eval", "contamination",
            "--split", args.split,
            "--output-dir", str(bundle_dir),
        ]
        if args.limit is not None:
            command.extend(["--limit", str(args.limit)])
        if args.overwrite_bundle:
            command.append("--overwrite")
        subprocess.run(command, check=True)

    generations = args.generations or bundle_dir / GENERATIONS_NAME
    if args.prepare_only or not generations.exists():
        print("\nOffline bundle is ready. On the API machine run:")
        print(
            f"  uv run python pipeline/run_offline_generations.py "
            f"--bundle-dir \"{bundle_dir}\" --model {args.model} --effort {args.effort}"
        )
        print("Copy generations.jsonl back, then rerun this command with --generations <path>.")
        return

    subprocess.run([
        sys.executable,
        str(Path(__file__).with_name("grade_offline_eval.py")),
        "--bundle-dir", str(bundle_dir),
        "--generations", str(generations),
        "--model", args.model,
        "--effort", args.effort,
    ], check=True)
    summarize(eval_metadata_from_manifest(
        load_manifest(bundle_dir),
        model=args.model,
        effort=args.effort,
    ))


if __name__ == "__main__":
    main()
