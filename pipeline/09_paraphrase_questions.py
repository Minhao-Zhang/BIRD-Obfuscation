"""
Step 9: Paraphrase each question (Layer 4 of the extended obfuscation).

Produces exactly ONE natural-language paraphrase per question, conditioned on
the gold SQL + obfuscated schema DDL so the paraphrase stays anchored to the
question's intent (SQL2NL-style; see docs/methodology/obfuscation-extensions.md
§3 and docs/reference/extension-implementation-plan.md §6). Layer 4 attacks
question-form recall: the model can no longer lean on a memorised BIRD
question -> SQL template.

The generation model is chosen at RUNTIME (`--model`, default gpt-5-mini but
easily overridable — pass whatever model you like). Reasoning models
(gpt-5*, o1/o3/o4-*) are called with reasoning={"effort": ...} and WITHOUT a
temperature (the Responses API does not take a temperature for them the same
way); non-reasoning models are called with temperature (~0.7) for lexical
diversity and no reasoning param. The OpenAI SDK / python-dotenv are imported
lazily inside the run path, so offline `py_compile` / import never needs them.

Model calls run CONCURRENTLY (AsyncOpenAI + asyncio.Semaphore, --concurrency,
default 6): the full run is ~2,030 questions and serial calls would take
hours. Each question is one async task guarded by the semaphore; results are
consumed with asyncio.as_completed. The resume done-set is computed ONCE
before the tasks launch, so concurrent tasks never re-do an id.

Reads:
  artifacts/test_final.jsonl              (always)
  artifacts/train_final.jsonl             (only with --include-train)
  live pg_rename (5433)               (obfuscated schema DDL per db_id)

Writes:
  artifacts/question_paraphrases.jsonl    {"question_id", "question_paraphrase"}
                                          (+ "low_similarity": true when the
                                          optional cosine check keeps a drifted
                                          paraphrase). Append + fsync; resumable
                                          by question_id (ids already present
                                          are skipped).

The gold SQL (sql_rename) is unchanged, so R1==R2 is untouched by
paraphrase (obfuscation-extensions.md §3). This output feeds the paraphrase / `all`
arms of eval_ablation.py, which join it back on question_id.

Run:
  uv run python pipeline/09_paraphrase_questions.py --limit 20   # dry run
  uv run python pipeline/09_paraphrase_questions.py              # full test set
  uv run python pipeline/09_paraphrase_questions.py --include-train
  uv run python pipeline/09_paraphrase_questions.py --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
from pathlib import Path

from _db import PG_RENAME_DSN, new_connection
from _eval_helpers import get_schema_ddl

ARTIFACTS = Path("artifacts")
PARAPHRASES_PATH = ARTIFACTS / "question_paraphrases.jsonl"

EMBED_MODEL = "text-embedding-3-small"

SYSTEM_INSTRUCTIONS_PARAPHRASE = (
    "You rephrase natural-language questions about a database for a "
    "question-form robustness test. Rephrase the given question so it "
    "preserves its EXACT meaning and remains answerable by the same SQL "
    "query, but is materially reworded (different phrasing, not a synonym "
    "swap). Rules: use natural language ONLY; do NOT mention any table name, "
    "column name, or other database identifier; do NOT add or drop any "
    "condition, filter, or requested value. Return ONLY the rephrased "
    "question — no quotes, no preamble, no explanation, no markdown."
)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable offline, no DB / no API)
# --------------------------------------------------------------------------- #

def is_reasoning_model(model: str) -> bool:
    """gpt-5* and o1/o3/o4-style models take reasoning={"effort": ...} and do
    not take a temperature the same way; gpt-4o-mini and friends are the
    reverse. (Copied from 08_inject_decoys.py to keep the guard identical.)"""
    m = (model or "").lower()
    if m.startswith("gpt-5"):
        return True
    return len(m) >= 2 and m[0] == "o" and m[1].isdigit()


def strip_paraphrase(text: str) -> str:
    """Clean a model's paraphrase: strip a stray ```/```text fence, surrounding
    quotes, and a leading 'Paraphrase:'/'Rephrased question:' label the model
    may have added despite the instructions."""
    text = (text or "").strip()
    fence = re.match(r"^```(?:\w+)?\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    text = re.sub(
        r"^(?:paraphrase|rephrased question|rephrased|question)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Strip a single pair of wrapping quotes (straight or curly).
    if len(text) >= 2 and text[0] in "\"'“‘" and text[-1] in "\"'”’":
        text = text[1:-1].strip()
    return text.strip()


# Some gold SQL are VALUES-materialization fallbacks that embed an entire
# result set as literal rows (up to ~128MB observed). That blob conveys no
# query intent and would blow the model's token/TPM limit, so cap the SQL
# context — the question itself carries the intent; the SQL only anchors it.
MAX_SQL_CHARS = 3000


def build_paraphrase_prompt(db_id: str, schema_ddl: str, question: str, gold_sql: str) -> str:
    """Prompt for one paraphrase, conditioned on question + gold SQL + schema so
    the intent is anchored. The schema/SQL are CONTEXT ONLY — the instructions
    forbid leaking any identifier into the rephrased question."""
    if len(gold_sql) > MAX_SQL_CHARS:
        gold_sql = gold_sql[:MAX_SQL_CHARS] + "\n/* ...SQL truncated (VALUES blob); the question above carries the intent... */"
    return "\n".join(
        [
            f"Database: {db_id}",
            "",
            "Schema (context only — do NOT copy any identifier into your answer):",
            schema_ddl,
            "",
            "Gold SQL that answers the question (context only — anchors the intent):",
            gold_sql,
            "",
            f"Original question: {question}",
            "",
            "Rephrase the original question following the rules.",
        ]
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. Returns 0.0 if either has
    zero magnitude."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def load_done_ids(results_path: Path) -> set[str]:
    """question_ids already present in question_paraphrases.jsonl (resume)."""
    done: set[str] = set()
    if not results_path.exists():
        return done
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done.add(rec["question_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def append_paraphrase(record: dict, results_path: Path) -> None:
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_questions(include_train: bool) -> list[dict]:
    """Dedup {test[,train]}_final.jsonl by question_id (keep last). test first,
    then train, so a colliding id keeps the train row when --include-train."""
    names = ["test_final.jsonl"]
    if include_train:
        names.append("train_final.jsonl")
    seen: dict[str, dict] = {}
    for name in names:
        path = ARTIFACTS / name
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                seen[r["question_id"]] = r
    return list(seen.values())


# --------------------------------------------------------------------------- #
# OpenAI calls (AsyncOpenAI SDK imported lazily by the caller)
# --------------------------------------------------------------------------- #

async def call_model_for_paraphrase(client, model: str, effort: str, temperature: float | None,
                                    prompt: str) -> str:
    """One paraphrase via the Responses API. Reasoning models get
    reasoning={"effort": ...} and no temperature; non-reasoning models get a
    temperature and no reasoning param."""
    kwargs = dict(model=model, instructions=SYSTEM_INSTRUCTIONS_PARAPHRASE, input=prompt)
    if is_reasoning_model(model):
        kwargs["reasoning"] = {"effort": effort}
    elif temperature is not None:
        kwargs["temperature"] = temperature
    response = await client.responses.create(**kwargs)
    return strip_paraphrase(response.output_text)


async def embed(client, text: str) -> list[float]:
    resp = await client.embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

async def run_one(client, sem, model, effort, temp, q, conn, ddl_cache, lock,
                  cosine_check, cosine_min):
    """Paraphrase ONE question under the semaphore, append its record, and
    report a status. Returns a dict {"status": "ok"|"failed"|"transient",
    "flagged": bool}. On a transient error NO record is written, so the id is
    retried next run (mirrors eval_contamination.run_one). append_paraphrase is
    synchronous (no await mid-write) so concurrent tasks never interleave a
    line."""
    import openai  # local import: only the run path needs the SDK

    qid = q["question_id"]
    db_id = q["db_id"]

    async with sem:
        # Per-db_id DDL cache on the single shared obfuscated connection,
        # guarded by the lock so concurrent tasks don't hit get_schema_ddl /
        # the connection at the same time.
        async with lock:
            if db_id not in ddl_cache:
                ddl_cache[db_id] = get_schema_ddl(conn, db_id)
        schema_ddl = ddl_cache[db_id]

        prompt = build_paraphrase_prompt(db_id, schema_ddl, q["question"], q["sql_rename"])
        try:
            paraphrase = await call_model_for_paraphrase(client, model, effort, temp, prompt)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError) as e:
            # Transient — the SDK already retried internally. Don't write a
            # record: leaving this question_id absent means the next run
            # picks it up again instead of skipping it.
            print(f"  transient error on {qid}, will retry next run: {e}", flush=True)
            return {"status": "transient", "flagged": False}
        except Exception as e:
            print(f"  paraphrase failed on {qid}: {e}", flush=True)
            return {"status": "failed", "flagged": False}

        if not paraphrase:
            print(f"  empty paraphrase on {qid}, skipping (retry next run)", flush=True)
            return {"status": "failed", "flagged": False}

        record = {"question_id": qid, "question_paraphrase": paraphrase}
        flagged = False

        if cosine_check:
            try:
                sim = cosine_similarity(await embed(client, q["question"]),
                                        await embed(client, paraphrase))
                if sim < cosine_min:
                    # Retry once; keep whichever we end with, flagged.
                    retry = await call_model_for_paraphrase(client, model, effort, temp, prompt)
                    retry = strip_paraphrase(retry)
                    if retry:
                        retry_sim = cosine_similarity(
                            await embed(client, q["question"]), await embed(client, retry)
                        )
                        if retry_sim >= sim:
                            paraphrase, sim = retry, retry_sim
                            record["question_paraphrase"] = paraphrase
                    if sim < cosine_min:
                        record["low_similarity"] = True
                        flagged = True
            except Exception as e:
                print(f"  cosine check failed on {qid} (keeping paraphrase): {e}", flush=True)

        append_paraphrase(record, PARAPHRASES_PATH)
        return {"status": "ok", "flagged": flagged}


async def run(model: str, effort: str, temperature: float, include_train: bool, limit: int | None,
              cosine_check: bool, cosine_min: float, concurrency: int) -> None:
    import openai  # noqa: F401  (local import: only the run path needs the SDK)
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set. Copy .env.example to .env and fill it in.")

    ARTIFACTS.mkdir(exist_ok=True)
    questions = load_questions(include_train)
    # Compute the resume done-set ONCE, before any task launches, so concurrent
    # tasks never re-do a question_id already present in the output file.
    done = load_done_ids(PARAPHRASES_PATH)
    todo = [q for q in questions if q["question_id"] not in done]
    if limit:
        todo = todo[:limit]

    print(f"{len(questions)} question(s) total; {len(done)} already done; "
          f"{len(todo)} to paraphrase with {model} (concurrency={concurrency})"
          + (f" (cosine check on, min={cosine_min})" if cosine_check else ""))

    if not todo:
        print("Nothing to do.")
        return

    reasoning = is_reasoning_model(model)
    temp = None if reasoning else temperature

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    conn = new_connection(PG_RENAME_DSN)
    ddl_cache: dict[str, str] = {}
    ok = failed = flagged = completed = 0
    try:
        coros = [
            run_one(client, sem, model, effort, temp, q, conn, ddl_cache, lock,
                    cosine_check, cosine_min)
            for q in todo
        ]
        for coro in asyncio.as_completed(coros):
            result = await coro
            completed += 1
            if result["status"] == "ok":
                ok += 1
            elif result["status"] == "failed":
                failed += 1
            # "transient" results are not counted (retried next run)
            if result["flagged"]:
                flagged += 1
            if completed % 50 == 0:
                print(f"  {completed}/{len(todo)} done (ok={ok} failed={failed} "
                      f"flagged={flagged})", flush=True)
    finally:
        conn.close()

    print(f"done: ok={ok}, failed={failed}"
          + (f", low_similarity flagged={flagged}" if cosine_check else "")
          + f" -> {PARAPHRASES_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Step 9: paraphrase each question (Layer 4), conditioned on gold SQL + schema"
    )
    parser.add_argument("--model", default="gpt-5-mini",
                        help="generation model (override freely with your own model)")
    parser.add_argument("--effort", default="low",
                        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
                        help="reasoning effort (only used for reasoning models)")
    parser.add_argument("--temperature", type=float, default=0.7,
                        help="sampling temperature for non-reasoning models (ignored for "
                             "reasoning models, which do not take it)")
    parser.add_argument("--include-train", action="store_true",
                        help="also paraphrase train_final.jsonl (default: test set only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N not-done questions (dry run)")
    parser.add_argument("--cosine-check", action="store_true",
                        help="embed original vs paraphrase (text-embedding-3-small); "
                             "retry once if cosine < --cosine-min, then keep with a "
                             "low_similarity flag (default: OFF)")
    parser.add_argument("--cosine-min", type=float, default=0.6,
                        help="minimum acceptable cosine similarity when --cosine-check is on")
    parser.add_argument("--concurrency", type=int, default=6,
                        help="number of model calls in flight at once (AsyncOpenAI + semaphore)")
    args = parser.parse_args()

    asyncio.run(run(args.model, args.effort, args.temperature, args.include_train, args.limit,
                    args.cosine_check, args.cosine_min, args.concurrency))


if __name__ == "__main__":
    main()
