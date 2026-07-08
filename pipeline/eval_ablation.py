"""
Ablation eval (evaluation.md §9): measure the independent contribution of each
extended obfuscation layer to execution-accuracy drop, over the BIRD test set,
all NO-HINT, one-shot (no retry, no feedback loop).

Five arms, each = (PostgreSQL instance, gold SQL field, question source):

  base        pg_base          (5432)  sql_base                         question
  rename      pg_rename        (5433)  sql_rename                       question
  decoy       pg_decoy         (5434)  sql_base   (SELECT*-expanded)    question
  paraphrase  pg_base          (5432)  sql_base                         question_paraphrase
  all         pg_rename_decoy  (5435)  sql_rename (SELECT*-expanded)    question_paraphrase

For each (question, arm): build a no-hint prompt (db_id + stripped DDL of that
arm's instance + question text), ask the model for one SQL query, execute it once
against that arm's instance, and grade by exact normalise_result equality against
the arm's own gold (SELECT*-expanded where applicable, so decoy columns can never
leak into the gold answer). The model sees decoy columns automatically because
get_schema_ddl reads the decoy instance's information_schema — no special-casing.

This is downstream evaluation, a sibling of eval_contamination.py (the contamination run), not a
core pipeline step. It depends on artifacts produced later in the extension:
  - step 08 (08_inject_decoys.py): the two *_decoy instances (5434/5435) must be
    built + injected, and artifacts/gold_star_expanded.jsonl must exist.
  - step 09 (09_paraphrase_questions.py): artifacts/question_paraphrases.jsonl.
    Questions without a paraphrase yet are SKIPPED for paraphrase/all (count logged).

Reads:
  artifacts/test_final.jsonl
  artifacts/gold_star_expanded.jsonl   (SELECT*-expanded gold for the ~5 affected)
  artifacts/question_paraphrases.jsonl (question_id -> question_paraphrase)
  artifacts/db_language_map.json       (per-language breakdown)
  live pg_base (5432) / pg_rename (5433) / pg_decoy (5434) /
       pg_rename_decoy (5435) for schema DDL + grading

Writes:
  eval/ablation_results.jsonl  — one record per (question_id, arm), resumable

Run:
  Copy .env.example to .env and set OPENAI_API_KEY, then:
  uv run python pipeline/eval_ablation.py --model gpt-5.5 --limit 20
  uv run python pipeline/eval_ablation.py --model gpt-5.5            # full test set, 5 arms
  uv run python pipeline/eval_ablation.py --arms base,rename         # subset of arms
  uv run python pipeline/eval_ablation.py --summarize                # EX/deltas/McNemar/CIs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import time
from pathlib import Path

from _db import (
    PG_BASE_DSN,
    PG_RENAME_DSN,
    PG_DECOY_DSN,
    PG_RENAME_DECOY_DSN,
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

# load_dotenv and the OpenAI SDK are imported lazily in the run path so that
# importing this module offline (py_compile, unit tests) needs neither.

ARTIFACTS = Path("artifacts")
EVAL_DIR = Path("eval")
RESULTS_PATH = EVAL_DIR / "ablation_results.jsonl"
PROMPT_VERSION = "ablation-v1"

# Bootstrap CI reproducibility: fixed sample count + seed so --summarize is
# deterministic across runs of the same results file.
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260703

# dsn_key -> DSN string (only the keys the selected arms touch get a connection).
DSN_FOR_KEY = {
    "base": PG_BASE_DSN,
    "rename": PG_RENAME_DSN,
    "decoy": PG_DECOY_DSN,
    "rename_decoy": PG_RENAME_DECOY_DSN,
}

# One entry per arm. sql_field: gold field in test_final.jsonl. expanded_field:
# the SELECT*-expanded gold field in gold_star_expanded.jsonl to prefer when the
# question_id is present there (None = never expanded). question_field: which
# question text to show — "question" (verbatim row) or "question_paraphrase"
# (looked up from question_paraphrases.jsonl).
ARM_SPEC = {
    "base":       {"dsn_key": "base", "sql_field": "sql_base",
                   "expanded_field": None, "question_field": "question"},
    "rename":     {"dsn_key": "rename", "sql_field": "sql_rename",
                   "expanded_field": None, "question_field": "question"},
    "decoy":      {"dsn_key": "decoy", "sql_field": "sql_base",
                   "expanded_field": "sql_base_expanded", "question_field": "question"},
    "paraphrase": {"dsn_key": "base", "sql_field": "sql_base",
                   "expanded_field": None, "question_field": "question_paraphrase"},
    "all":        {"dsn_key": "rename_decoy", "sql_field": "sql_rename",
                   "expanded_field": "sql_rename_expanded", "question_field": "question_paraphrase"},
}
ARMS = ["base", "rename", "decoy", "paraphrase", "all"]


# --------------------------------------------------------------------------- #
# Pure resolvers (unit-testable offline, no DB / no API)
# --------------------------------------------------------------------------- #

def resolve_arm(arm: str) -> tuple[str, str, str]:
    """Return (dsn, gold_sql_field, question_field) for an arm — the static
    wiring, before any per-question expanded-gold / paraphrase lookup."""
    spec = ARM_SPEC[arm]
    return DSN_FOR_KEY[spec["dsn_key"]], spec["sql_field"], spec["question_field"]


def resolve_gold_sql(arm: str, question_row: dict, expanded_rec: dict | None) -> str | None:
    """Gold SQL for this (arm, question): prefer the SELECT*-expanded gold when
    the arm expands and the question_id is present in gold_star_expanded, so
    decoy columns never leak into the gold answer. Falls back to the plain gold
    field. Returns None if the arm's gold field is absent/empty on the row."""
    spec = ARM_SPEC[arm]
    if spec["expanded_field"] and expanded_rec:
        exp_sql = expanded_rec.get(spec["expanded_field"])
        if exp_sql:
            return exp_sql
    return question_row.get(spec["sql_field"]) or None


def resolve_question_text(arm: str, question_row: dict, paraphrases: dict[str, str]) -> str | None:
    """Question text for this (arm, question). Paraphrase arms read from the
    paraphrase map and return None when the question has no paraphrase yet
    (produced later by step 09) — the caller SKIPs those."""
    field = ARM_SPEC[arm]["question_field"]
    if field == "question_paraphrase":
        return paraphrases.get(question_row["question_id"])
    return question_row.get(field)


# --------------------------------------------------------------------------- #
# Paired statistics (no new dependencies)
# --------------------------------------------------------------------------- #

def mcnemar(b: int, c: int) -> dict:
    """Paired McNemar test from the two discordant-pair counts:
      b = baseline correct & arm wrong,  c = baseline wrong & arm correct.
    Uses the continuity-corrected chi-square (1 df) and a closed-form p-value
    (survival function of chi-square_1 = erfc(sqrt(x/2)) — no scipy)."""
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "n_discordant": 0, "statistic": 0.0, "p_value": 1.0}
    corrected = max(0.0, abs(b - c) - 1.0)
    chi = (corrected * corrected) / n
    p_value = math.erfc(math.sqrt(chi / 2.0))
    return {"b": b, "c": c, "n_discordant": n, "statistic": chi, "p_value": p_value}


def bootstrap_ci(paired: list[tuple[int, int]], n_boot: int, seed: int,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap 95% CI for the paired delta (arm EX - baseline EX).
    ``paired`` is a list of (baseline_correct, arm_correct) 0/1 pairs over the
    common question set; resampling is by question id with a seeded RNG."""
    m = len(paired)
    if m == 0:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    deltas = []
    for _ in range(n_boot):
        s_base = 0
        s_arm = 0
        for _ in range(m):
            i = rng.randrange(m)
            s_base += paired[i][0]
            s_arm += paired[i][1]
        deltas.append((s_arm - s_base) / m)
    deltas.sort()
    lo = deltas[int((alpha / 2.0) * n_boot)]
    hi = deltas[min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot))]
    return (lo, hi)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

def load_test_questions(limit: int | None) -> list[dict]:
    with open(dataset_path("test_final.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    seen: dict[str, dict] = {}
    for r in rows:
        seen[r["question_id"]] = r  # de-dupe, keep last (matches eval_contamination/step 7)
    rows = list(seen.values())
    if limit:
        rows = rows[:limit]
    return rows


def load_jsonl_map(path: Path, value_key: str | None) -> dict:
    """Load a jsonl keyed by question_id. If value_key is given, map
    question_id -> record[value_key]; otherwise question_id -> full record.
    Missing file -> empty dict (the artifact is produced by a later step)."""
    out: dict = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec["question_id"]] = rec[value_key] if value_key else rec
    return out


def load_lang_map() -> dict:
    path = dataset_path("db_language_map.json")
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def is_reasoning_model(model: str) -> bool:
    """gpt-5* and o1/o3/o4-style models take reasoning={"effort": ...};
    gpt-4o-mini and friends reject it. (Copied from 08_inject_decoys.py.)"""
    m = (model or "").lower()
    if m.startswith("gpt-5"):
        return True
    return len(m) >= 2 and m[0] == "o" and m[1].isdigit()


def build_tasks(questions: list[dict], arms: list[str], paraphrases: dict[str, str],
                expanded: dict[str, dict], done: set[tuple[str, str]]) -> tuple[list[dict], int]:
    """Build the todo list of per-(question, arm) tasks, resolving gold + question
    text up front (pure). Skips arms whose paraphrase is not yet available.
    Returns (tasks, n_skipped_no_paraphrase)."""
    tasks: list[dict] = []
    skipped_no_paraphrase = 0
    for q in questions:
        qid = q["question_id"]
        for arm in arms:
            question_text = resolve_question_text(arm, q, paraphrases)
            if question_text is None:
                # only paraphrase arms can hit this (verbatim question always present)
                skipped_no_paraphrase += 1
                continue
            if (qid, arm) in done:
                continue
            gold_sql = resolve_gold_sql(arm, q, expanded.get(qid))
            tasks.append({
                "question": q,
                "arm": arm,
                "dsn_key": ARM_SPEC[arm]["dsn_key"],
                "question_text": question_text,
                "gold_sql": gold_sql,
            })
    return tasks, skipped_no_paraphrase


def group_by_dsn_and_db(tasks: list[dict]) -> list[tuple[tuple[str, str], list[dict]]]:
    """Group tasks by (dsn_key, db_id) in first-seen order so the run finishes
    one instance/DB's calls before the next — keeps prompt-cache hits high (same
    DDL prefix) instead of letting concurrent calls to unrelated DBs interleave
    and evict each other's cache entries (mirrors eval_contamination.group_by_schema_and_db)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in tasks:
        key = (t["dsn_key"], t["question"]["db_id"])
        groups.setdefault(key, []).append(t)
    return list(groups.items())


async def run_one(client, sem, model, effort, eval_metadata, task, ddl_cache, conn_pool, lock):
    import openai  # local import: only the run path needs the SDK

    q = task["question"]
    arm = task["arm"]
    dsn_key = task["dsn_key"]
    db_id = q["db_id"]

    async with sem:
        cache_key = (dsn_key, db_id)
        async with lock:
            if cache_key not in ddl_cache:
                ddl_cache[cache_key] = get_schema_ddl(conn_pool[dsn_key], db_id)
        schema_ddl = ddl_cache[cache_key]

        prompt = build_prompt(db_id, schema_ddl, task["question_text"], evidence=None)

        record = {
            "question_id": q["question_id"],
            "db_id": db_id,
            "arm": arm,
            # "condition" mirrors "arm" purely so the shared load_done_keys (which
            # keys on rec["condition"]) makes resume work by (question_id, arm)
            # without a bespoke loader.
            "condition": arm,
            "eval_metadata": eval_metadata,
            "recorded_at_utc": utc_now_iso(),
        }

        gold_sql = task["gold_sql"]
        if not gold_sql:
            record.update(generated_sql=None, correct=False, correct_strict=False, error="gold_missing")
            append_result(record, RESULTS_PATH)
            return record

        call_start = time.monotonic()
        try:
            kwargs = dict(model=model, instructions=SYSTEM_INSTRUCTIONS, input=prompt)
            if is_reasoning_model(model):
                kwargs["reasoning"] = {"effort": effort}
            response = await client.responses.create(**kwargs)
            generated_sql = extract_sql(response.output_text)
            record["usage"] = usage_dict(response.usage)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError) as e:
            # Transient — SDK already retried internally. Don't write a record:
            # leaving (question_id, arm) absent means load_done_keys picks it up
            # again next run instead of marking a hiccup as a permanent failure.
            print(f"  transient error on {q['question_id']}/{arm}, will retry next run: {e}",
                  flush=True)
            return None
        except Exception as e:
            record.update(
                generated_sql=None, correct=False, error=f"llm_call_failed: {e}",
                latency_sec=round(time.monotonic() - call_start, 3),
            )
            append_result(record, RESULTS_PATH)
            return record

        record["latency_sec"] = round(time.monotonic() - call_start, 3)
        record["generated_sql"] = generated_sql

        correct, correct_strict, error = grade(conn_pool[dsn_key], gold_sql, generated_sql)
        record["correct"] = correct
        record["correct_strict"] = correct_strict
        if error is not None:
            record["error"] = error

        append_result(record, RESULTS_PATH)
        return record


def run_metadata(model: str, effort: str) -> dict:
    return build_eval_metadata(
        eval_name="ablation",
        model=model,
        effort=effort,
        prompt_version=PROMPT_VERSION,
        dataset_files=[
            "test_final.jsonl",
            "gold_star_expanded.jsonl",
            "question_paraphrases.jsonl",
        ],
    )


async def run_eval(model: str, effort: str, limit: int | None, concurrency: int,
                   arms: list[str]) -> None:
    import openai
    from dotenv import load_dotenv
    load_dotenv()

    EVAL_DIR.mkdir(exist_ok=True)
    questions = load_test_questions(limit)
    paraphrases = load_jsonl_map(dataset_path("question_paraphrases.jsonl"), "question_paraphrase")
    expanded = load_jsonl_map(dataset_path("gold_star_expanded.jsonl"), None)
    eval_metadata = run_metadata(model, effort)
    done = load_done_keys(RESULTS_PATH, expected_metadata=eval_metadata)

    tasks, skipped_no_paraphrase = build_tasks(questions, arms, paraphrases, expanded, done)

    total = len(questions) * len(arms)
    print(f"{len(questions)} questions x {len(arms)} arms = {total} total; "
          f"{len(done)} done, {len(tasks)} remaining")
    if skipped_no_paraphrase:
        print(f"  skipped {skipped_no_paraphrase} paraphrase-arm task(s) with no paraphrase yet "
              f"(run step 09 to fill artifacts/question_paraphrases.jsonl)")

    if not tasks:
        print("Nothing to do.")
        return

    groups = group_by_dsn_and_db(tasks)

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set. Copy .env.example to .env and fill it in.")

    client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    ddl_cache: dict[tuple[str, str], str] = {}
    dsn_keys_needed = {ARM_SPEC[a]["dsn_key"] for a in arms}
    conn_pool = {k: new_connection(DSN_FOR_KEY[k]) for k in dsn_keys_needed}

    completed = 0
    run_start = time.monotonic()
    try:
        for gi, (key, group) in enumerate(groups, 1):
            coros = [
                run_one(client, sem, model, effort, eval_metadata, t, ddl_cache, conn_pool, lock)
                for t in group
            ]
            for coro in asyncio.as_completed(coros):
                await coro
                completed += 1
                if completed % 50 == 0:
                    print(f"{completed}/{len(tasks)} done", flush=True)
            print(f"[{gi}/{len(groups)}] finished dsn={key[0]} db_id={key[1]} "
                  f"({len(group)} calls)", flush=True)
    finally:
        for conn in conn_pool.values():
            conn.close()

    elapsed = time.monotonic() - run_start
    if completed:
        print(f"Done. {completed}/{len(tasks)} processed this run in {elapsed:.1f}s "
              f"({elapsed / completed:.2f}s/call avg).")
    else:
        print(f"Done. {completed}/{len(tasks)} processed this run in {elapsed:.1f}s.")


# --------------------------------------------------------------------------- #
# Summarize
# --------------------------------------------------------------------------- #

def summarize(expected_metadata: dict | None = None) -> None:
    if not RESULTS_PATH.exists():
        print(f"No {RESULTS_PATH} yet.")
        return

    lang_map = load_lang_map()

    # last-write-wins per (question_id, arm)
    by_key: dict[tuple[str, str], dict] = {}
    with open(RESULTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not metadata_matches(rec.get("eval_metadata"), expected_metadata):
                continue
            by_key[(rec["question_id"], rec["arm"])] = rec

    # arm -> {qid: correct(0/1)} and arm -> db_id lookup
    correct_by_arm: dict[str, dict[str, int]] = {a: {} for a in ARMS}
    strict_by_arm: dict[str, dict[str, int]] = {a: {} for a in ARMS}
    db_of_qid: dict[str, str] = {}
    present_arms = set()
    for (qid, arm), rec in by_key.items():
        if arm not in correct_by_arm:
            correct_by_arm[arm] = {}
            strict_by_arm[arm] = {}
        correct_by_arm[arm][qid] = int(bool(rec.get("correct", False)))
        strict_by_arm[arm][qid] = int(bool(rec.get("correct_strict", rec.get("correct", False))))
        db_of_qid[qid] = rec.get("db_id", "")
        present_arms.add(arm)

    ordered_arms = [a for a in ARMS if a in present_arms] + \
                   [a for a in present_arms if a not in ARMS]

    def _ex(table: dict, arm: str) -> tuple[float, int, int]:
        d = table.get(arm, {})
        n = len(d)
        c = sum(d.values())
        return (c / n if n else float("nan"), c, n)

    def ex(arm: str) -> tuple[float, int, int]:
        return _ex(correct_by_arm, arm)

    print("Execution accuracy (EX) by arm — lenient (BIRD-style, type-collapsing) "
          "vs strict (no cross-type match):")
    for arm in ordered_arms:
        e, c, n = ex(arm)
        es, cs, _ = _ex(strict_by_arm, arm)
        print(f"  {arm:9s}: lenient {e:.4f} ({c}/{n})   strict {es:.4f} ({cs}/{n})")

    base = correct_by_arm.get("base", {})
    if not base:
        print("\nNo base arm in results — cannot compute deltas.")
    else:
        base_ex = sum(base.values()) / len(base)
        print(f"\nPaired deltas vs base (base EX = {base_ex:.4f}, on each arm's common set):")
        print(f"  {'arm':9s} {'delta':>8s}  {'95% CI (bootstrap)':>22s}  "
              f"{'McNemar p':>10s}  {'disc(b/c)':>10s}  n_paired")
        for arm in ordered_arms:
            if arm == "base":
                continue
            arm_d = correct_by_arm.get(arm, {})
            common = [qid for qid in arm_d if qid in base]
            if not common:
                print(f"  {arm:9s}   (no common questions with base)")
                continue
            paired = [(base[qid], arm_d[qid]) for qid in common]
            delta = (sum(p[1] for p in paired) - sum(p[0] for p in paired)) / len(paired)
            b = sum(1 for pb, pa in paired if pb == 1 and pa == 0)
            c = sum(1 for pb, pa in paired if pb == 0 and pa == 1)
            mc = mcnemar(b, c)
            lo, hi = bootstrap_ci(paired, BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED)
            print(f"  {arm:9s} {delta:+8.4f}  [{lo:+.4f}, {hi:+.4f}]  "
                  f"{mc['p_value']:10.4f}  {b:4d}/{c:<4d}  {len(paired)}")

    # per-language breakdown
    if lang_map:
        print("\nBy obfuscation language (EX per arm):")
        langs = sorted({lang_map.get(db_of_qid.get(qid, ""), "unknown")
                        for arm in correct_by_arm for qid in correct_by_arm[arm]})
        header = "  " + f"{'language':18s}" + "".join(f"{a:>9s}" for a in ordered_arms)
        print(header)
        for lang in langs:
            cells = []
            for arm in ordered_arms:
                d = correct_by_arm.get(arm, {})
                qs = [qid for qid in d if lang_map.get(db_of_qid.get(qid, ""), "unknown") == lang]
                if qs:
                    cells.append(f"{sum(d[q] for q in qs) / len(qs):9.3f}")
                else:
                    cells.append(f"{'-':>9s}")
            print("  " + f"{lang:18s}" + "".join(cells))

    print(
        "\nCaveats:\n"
        "  - This is a ONE-AT-A-TIME + 'all' design, NOT a full 2^3 factorial. So\n"
        "    'all - sum(individual deltas vs baseline)' is NOT a clean interaction\n"
        "    term; interpret rename/decoy/paraphrase as separate mechanisms (identifier\n"
        "    recall / schema-linking distractors / question-form recall), not one scale.\n"
        "  - The contamination-eval English control established a ~0.4pp noise floor — treat that\n"
        "    as the null, not zero. Effects here are ~1pp, so read the CIs and McNemar\n"
        "    p-values, not the point deltas alone."
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_arms(spec: str) -> list[str]:
    arms = [a.strip() for a in spec.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARM_SPEC]
    if unknown:
        raise SystemExit(f"Unknown arm(s): {unknown}. Choose from {ARMS}.")
    # keep canonical order, de-duped
    return [a for a in ARMS if a in arms]


def main():
    parser = argparse.ArgumentParser(description="Ablation eval (evaluation.md §9): 5 arms, no-hint")
    parser.add_argument("--model", default="gpt-5.5", help="default matches the contamination run for comparability")
    parser.add_argument("--effort", default="low",
                        choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    parser.add_argument("--limit", type=int, default=None, help="limit number of test questions (dry run)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--arms", default=",".join(ARMS),
                        help=f"comma-separated subset of {ARMS}")
    parser.add_argument("--summarize", action="store_true",
                        help="print EX/deltas/McNemar/CIs from existing results and exit")
    args = parser.parse_args()

    if args.summarize:
        summarize(run_metadata(args.model, args.effort))
        return

    arms = parse_arms(args.arms)
    asyncio.run(run_eval(args.model, args.effort, args.limit, args.concurrency, arms))
    summarize(run_metadata(args.model, args.effort))


if __name__ == "__main__":
    main()
