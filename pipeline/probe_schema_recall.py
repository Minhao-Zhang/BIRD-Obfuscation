"""
Schema recall probe (evaluation.md §4.3): a direct identifier-memorisation
probe. Run the SAME test-set question text under two conditions and measure how
often the model emits the CORRECT schema identifiers *without ever being shown
any schema DDL*. A large positive original-minus-obfuscated recall delta means
the model knows the original BIRD identifiers it was never shown — evidence of
identifier-level memorisation, independent of execution accuracy.

No database is needed: the gold identifiers come straight from the gold SQL
already stored in artifacts/test_final.jsonl (sql_base for original, sql_rename
for obfuscated). This is a downstream probe like pipeline/eval_contamination.py — not a
numbered pipeline step (see docs/methodology/evaluation.md §2 "Scope Boundary").

Two conditions per test question (the question text is identical in both — it is
never modified; only the framing and the gold identifier set differ):
  original    prompt = question only + "Write a PostgreSQL query.", NO schema.
              Compare emitted identifiers to those in gold sql_base (BIRD names).
  obfuscated  prompt = question + obfuscated db_id label + "Write a query against
              this database's schema.", still NO schema DDL. Compare to gold
              sql_rename (renamed names).

Per (question, condition) we record recall = |G ∩ P| / |G| and hit = 1 if the
model recalled ANY correct identifier, plus the raw SQL and the two id sets.

Reads:
  artifacts/test_final.jsonl
  artifacts/db_language_map.json  (for the per-language breakdown)

Writes:
  eval/schema_recall_results.jsonl  — one record per (question_id, condition), resumable

Run:
  Copy .env.example to .env and set OPENAI_API_KEY, then:
  uv run python pipeline/probe_schema_recall.py --model gpt-5.5 --limit 20
  uv run python pipeline/probe_schema_recall.py --model gpt-5.5           # full test set
  uv run python pipeline/probe_schema_recall.py --summarize               # print recall/deltas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import sqlglot
from dotenv import load_dotenv
from sqlglot import exp

from _eval_helpers import (
    append_result,
    build_eval_metadata,
    dataset_path,
    extract_sql,
    load_done_keys,
    metadata_matches,
    utc_now_iso,
    usage_dict,
)

load_dotenv()

ARTIFACTS = Path("artifacts")
EVAL_DIR = Path("eval")
RESULTS_PATH = EVAL_DIR / "schema_recall_results.jsonl"
PROMPT_VERSION = "schema-recall-v1"

# The probe deliberately shows NO schema. The two conditions differ only in the
# framing shown to the model and in which gold-SQL field their emitted
# identifiers are graded against.
CONDITIONS = ["original", "obfuscated"]
CONDITION_SPEC = {
    "original": {
        "sql_field": "sql_base",
        "show_db": False,
        "instruction": "Write a PostgreSQL query.",
    },
    "obfuscated": {
        "sql_field": "sql_rename",
        "show_db": True,
        "instruction": "Write a query against this database's schema.",
    },
}

PROBE_SYSTEM_INSTRUCTIONS = (
    "You are a PostgreSQL expert. You will be given a natural-language question "
    "about a database, but NO schema. Write the single PostgreSQL SQL query you "
    "believe answers it, using the table and column names you expect the "
    "database to have. Output ONLY the SQL query, no explanation, no markdown "
    "code fences."
)


def is_reasoning_model(model: str) -> bool:
    """gpt-5* and o1/o3/o4-style models take reasoning={"effort": ...};
    gpt-4o-mini and friends reject it. (Copied from pipeline/08_inject_decoys.py;
    that module can't be imported because its name starts with a digit.)"""
    m = (model or "").lower()
    if m.startswith("gpt-5"):
        return True
    return len(m) >= 2 and m[0] == "o" and m[1].isdigit()


# --------------------------------------------------------------------------- #
# Pure functions (no API, no DB) — unit-testable
# --------------------------------------------------------------------------- #

def identifiers(sql: str) -> set[str]:
    """Return the set of table and column identifiers in ``sql``, lowercased.

    Parse with sqlglot (read="postgres"; fall back to "sqlite"; if both fail,
    return an empty set). Collect exp.Table.name and exp.Column.name, excluding
    the pure ``*`` wildcard (e.g. from ``t.*`` or ``COUNT(*)``). Schema/db
    qualifiers and table aliases are not identifiers here — only the .name of
    each Table/Column node is collected."""
    if not sql:
        return set()
    tree = None
    for dialect in ("postgres", "sqlite"):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
            break
        except Exception:
            tree = None
    if tree is None:
        return set()

    ids: set[str] = set()
    for node in tree.find_all(exp.Table):
        if node.name and node.name != "*":
            ids.add(node.name.lower())
    for node in tree.find_all(exp.Column):
        if node.name and node.name != "*":
            ids.add(node.name.lower())
    return ids


def recall_hit(gold: set[str], pred: set[str]) -> tuple[float, int]:
    """Per-question metric: given gold identifier set G and predicted set P,
    recall = |G ∩ P| / |G| (0 if G empty) and hit = 1 if |G ∩ P| > 0 else 0."""
    if not gold:
        return 0.0, 0
    overlap = len(gold & pred)
    recall = overlap / len(gold)
    hit = 1 if overlap > 0 else 0
    return recall, hit


def build_prompt(question: str, db_id: str, spec: dict) -> str:
    parts = []
    if spec["show_db"]:
        parts.append(f"Database: {db_id}")
        parts.append("")
    parts.append(f"Question: {question}")
    parts.append("")
    parts.append(spec["instruction"])
    return "\n".join(parts)


def load_test_questions(limit: int | None) -> list[dict]:
    with open(dataset_path("test_final.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    seen = {}
    for r in rows:
        seen[r["question_id"]] = r  # de-dupe, keep last (matches step 7's convention)
    rows = list(seen.values())
    if limit:
        rows = rows[:limit]
    return rows


# --------------------------------------------------------------------------- #
# Async run path
# --------------------------------------------------------------------------- #

async def run_one(client, sem, model, effort, eval_metadata, question, condition):
    import openai  # local import: only the run path needs the SDK

    spec = CONDITION_SPEC[condition]
    db_id = question["db_id"]
    gold_ids = identifiers(question[spec["sql_field"]])
    prompt = build_prompt(question["question"], db_id, spec)

    record = {
        "question_id": question["question_id"],
        "db_id": db_id,
        "condition": condition,
        "eval_metadata": eval_metadata,
        "recorded_at_utc": utc_now_iso(),
    }

    async with sem:
        kwargs = dict(model=model, instructions=PROBE_SYSTEM_INSTRUCTIONS, input=prompt)
        if is_reasoning_model(model):
            kwargs["reasoning"] = {"effort": effort}
        else:
            kwargs["temperature"] = 0

        call_start = time.monotonic()
        try:
            response = await client.responses.create(**kwargs)
            generated_sql = extract_sql(response.output_text)
            record["usage"] = usage_dict(response.usage)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError) as e:
            # Transient — the SDK already retried internally before raising.
            # Don't write a record: leaving this (question_id, condition) absent
            # means load_done_keys() picks it up again next run instead of
            # permanently marking a rate-limit hiccup as a failure.
            print(f"  transient error on {question['question_id']}/{condition}, will retry next run: {e}",
                  flush=True)
            return None
        except Exception as e:
            record.update(
                generated_sql=None, gold_ids=sorted(gold_ids), pred_ids=[],
                recall=None, hit=None, error=f"llm_call_failed: {e}",
                latency_sec=round(time.monotonic() - call_start, 3),
            )
            append_result(record, RESULTS_PATH)
            return record

        record["latency_sec"] = round(time.monotonic() - call_start, 3)
        record["generated_sql"] = generated_sql

        pred_ids = identifiers(generated_sql)
        recall, hit = recall_hit(gold_ids, pred_ids)
        record["gold_ids"] = sorted(gold_ids)
        record["pred_ids"] = sorted(pred_ids)
        record["recall"] = recall
        record["hit"] = hit

        append_result(record, RESULTS_PATH)
        return record


def run_metadata(model: str, effort: str) -> dict:
    return build_eval_metadata(
        eval_name="schema_recall",
        model=model,
        effort=effort,
        prompt_version=PROMPT_VERSION,
        dataset_files=["test_final.jsonl"],
    )


async def run_probe(model: str, effort: str, limit: int | None, concurrency: int) -> None:
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

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(concurrency)

    coros = [run_one(client, sem, model, effort, eval_metadata, q, cond) for q, cond in tasks_todo]

    completed = 0
    run_start = time.monotonic()
    for coro in asyncio.as_completed(coros):
        await coro
        completed += 1
        if completed % 50 == 0:
            print(f"{completed}/{len(tasks_todo)} done", flush=True)

    elapsed = time.monotonic() - run_start
    print(f"Done. {completed}/{len(tasks_todo)} processed this run in {elapsed:.1f}s "
          f"({elapsed / completed:.2f}s/call avg)." if completed else
          f"Done. {completed}/{len(tasks_todo)} processed this run in {elapsed:.1f}s.")


# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #

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

    # per-condition accumulators
    agg = {c: {"n": 0, "recall_sum": 0.0, "hit_sum": 0} for c in CONDITIONS}
    by_lang: dict[str, dict] = {}
    tokens = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    error_counts: dict[str, int] = {}
    latencies = []

    for (qid, cond), rec in by_key.items():
        recall = rec.get("recall")
        hit = rec.get("hit")
        if recall is not None:  # graded row (not an errored call)
            agg[cond]["n"] += 1
            agg[cond]["recall_sum"] += recall
            agg[cond]["hit_sum"] += int(hit)
            lang = lang_map.get(rec["db_id"], "unknown")
            by_lang.setdefault(lang, {c: {"n": 0, "recall_sum": 0.0, "hit_sum": 0} for c in CONDITIONS})
            by_lang[lang][cond]["n"] += 1
            by_lang[lang][cond]["recall_sum"] += recall
            by_lang[lang][cond]["hit_sum"] += int(hit)

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

    def mean_recall(d):
        return d["recall_sum"] / d["n"] if d["n"] else float("nan")

    def hit_rate(d):
        return d["hit_sum"] / d["n"] if d["n"] else float("nan")

    print("Schema recall probe (no schema DDL shown) by condition:")
    for c in CONDITIONS:
        d = agg[c]
        print(f"  {c:<10} mean_recall={mean_recall(d):.4f}  hit_rate={hit_rate(d):.4f}  (n={d['n']})")

    recall_delta = mean_recall(agg["original"]) - mean_recall(agg["obfuscated"])
    hit_delta = hit_rate(agg["original"]) - hit_rate(agg["obfuscated"])
    print(f"\nRecall delta (original - obfuscated, memorisation signal) = {recall_delta:+.4f}")
    print(f"Hit-rate delta (original - obfuscated)                     = {hit_delta:+.4f}")
    print("A large positive recall delta = the model knows original BIRD "
          "identifiers it was never shown.")

    print("\nBy obfuscation language "
          "(English is the un-obfuscated control: obfuscated==original there, so ~0 delta expected):")
    for lang in sorted(by_lang):
        row = by_lang[lang]
        o, b = row["original"], row["obfuscated"]
        d_recall = mean_recall(o) - mean_recall(b)
        print(f"  {lang:<10} orig_recall={mean_recall(o):.3f} obf_recall={mean_recall(b):.3f} "
              f"delta={d_recall:+.3f} | orig_hit={hit_rate(o):.3f} obf_hit={hit_rate(b):.3f} "
              f"(n={o['n']}/{b['n']})")

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
    parser = argparse.ArgumentParser(description="Schema recall probe (evaluation.md §4.3)")
    parser.add_argument("--model", default="gpt-5.5", help="overridable; default gpt-5.5")
    parser.add_argument("--effort", default="low", choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    parser.add_argument("--limit", type=int, default=None, help="Limit number of test questions (for dry runs)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Keep modest — org TPM limit ~200k/min, bursts cause 429s")
    parser.add_argument("--summarize", action="store_true", help="Print recall/deltas from existing results and exit")
    args = parser.parse_args()

    if args.summarize:
        summarize(run_metadata(args.model, args.effort))
        return

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set. Copy .env.example to .env and fill it in.")

    asyncio.run(run_probe(args.model, args.effort, args.limit, args.concurrency))
    summarize(run_metadata(args.model, args.effort))


if __name__ == "__main__":
    main()
