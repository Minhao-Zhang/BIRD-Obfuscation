"""Shared eval machinery extracted from eval_contamination.py.

The contamination entrypoint (eval_contamination.py) and eval_ablation.py both
reuse these generic pieces: the system prompt, stripped-DDL reader, prompt
builder, SQL/usage extraction, resumability helpers, and the gold-vs-generated
grading core. Anything contamination-specific (conditions, DSN wiring, run_one,
the summarizer) stays in eval_contamination.py.

Grading semantics (grade) must stay byte-for-byte compatible with step 7's
comparison: gold executed fresh, generated executed fresh, compared by
normalise_result equality. The error strings (gold_exec_failed:,
result_mismatch, generated_exec_failed:) are part of the contract consumed by
the summarizers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from _db import exec_pg, new_connection, normalise_result, normalise_result_strict

# --------------------------------------------------------------------------- #
# Benchmark input resolution. Prefer the pipeline working dir (artifacts/); fall
# back to the git-tracked snapshot (eval_dataset/) so a fresh clone that only has
# the checked-in deliverable still runs the eval. Resolved relative to the CWD,
# i.e. the repo root (eval scripts are run as `uv run python pipeline/...`).
# --------------------------------------------------------------------------- #
ARTIFACTS = Path("artifacts")
EVAL_DATASET = Path("eval_dataset")


def dataset_path(name: str) -> Path:
    """artifacts/<name> if it exists (local working copy), else eval_dataset/<name>."""
    p = ARTIFACTS / name
    return p if p.exists() else EVAL_DATASET / name


SYSTEM_INSTRUCTIONS = (
    "You are a PostgreSQL expert. You will be given a database schema and a "
    "question about the data. Write a single PostgreSQL SQL query that answers "
    "the question. Quote all identifiers with double quotes. Output ONLY the "
    "SQL query, no explanation, no markdown code fences."
)


def get_schema_ddl(conn, db_id: str) -> str:
    """Stripped DDL: table/column names + dtypes only (evaluation.md §4.1 —
    no PRIMARY KEY, FOREIGN KEY, CHECK constraints, no column descriptions).
    This schema lake never creates those constraints anyway (see AGENTS.md),
    so there is nothing to strip beyond reading information_schema."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            (db_id,),
        )
        tables = [r[0] for r in cur.fetchall()]
        parts = []
        for tbl in tables:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (db_id, tbl),
            )
            cols = cur.fetchall()
            col_lines = ",\n".join(f'    "{c}" {t}' for c, t in cols)
            parts.append(f'CREATE TABLE "{db_id}"."{tbl}" (\n{col_lines}\n)')
    return "\n\n".join(parts)


def usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "cached_tokens": usage.input_tokens_details.cached_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
        "total_tokens": usage.total_tokens,
    }


def utc_now_iso() -> str:
    """UTC timestamp suitable for JSONL result records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_output(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return proc.stdout.strip()


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_eval_metadata(
    *,
    eval_name: str,
    model: str,
    effort: str,
    prompt_version: str,
    dataset_files: list[str],
) -> dict:
    """Stable metadata stamped onto every eval result row.

    Keep this block stable across resumed invocations with the same eval inputs:
    load_done_keys(..., expected_metadata=...) uses it to avoid silently reusing
    rows from a different model, prompt, code revision, or dataset snapshot.
    """
    git_commit = _git_output(["rev-parse", "HEAD"])
    git_status = _git_output(["status", "--porcelain"])
    resolved_files = {}
    for name in dataset_files:
        path = dataset_path(name)
        resolved_files[name] = {
            "path": str(path),
            "sha256": _sha256_file(path),
        }

    return {
        "result_schema_version": 1,
        "eval_name": eval_name,
        "model": model,
        "effort": effort,
        "prompt_version": prompt_version,
        "git_commit": git_commit,
        "git_dirty": bool(git_status),
        "dataset_files": resolved_files,
    }


def metadata_matches(actual: dict | None, expected: dict | None) -> bool:
    if expected is None:
        return True
    if not isinstance(actual, dict):
        return False
    # Compare the reproducibility-defining fields. Fields such as git_dirty are
    # stamped for auditability but intentionally do not invalidate resumption.
    keys = [
        "result_schema_version",
        "eval_name",
        "model",
        "effort",
        "prompt_version",
        "git_commit",
        "dataset_files",
    ]
    return all(actual.get(k) == expected.get(k) for k in keys)


def extract_sql(text: str) -> str:
    """Strip markdown code fences if the model added them anyway."""
    text = text.strip()
    fence = re.match(r"^```(?:sql)?\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def build_prompt(db_id: str, schema_ddl: str, question: str, evidence: str | None) -> str:
    parts = [
        f"Database: {db_id}",
        "",
        "Schema:",
        schema_ddl,
        "",
        f"Question: {question}",
    ]
    if evidence:
        parts.append(f"Hint: {evidence}")
    return "\n".join(parts)


def load_done_keys(results_path: Path, expected_metadata: dict | None = None) -> set[tuple[str, str]]:
    done = set()
    if not results_path.exists():
        return done
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not metadata_matches(rec.get("eval_metadata"), expected_metadata):
                    continue
                done.add((rec["question_id"], rec["condition"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def append_result(record: dict, results_path: Path) -> None:
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def grade(conn, gold_sql: str, generated_sql: str) -> tuple[bool, bool, str | None]:
    """Execute gold and generated SQL fresh against ``conn`` and compare.
    Returns (correct, correct_strict, error):

      - gold fails to execute      -> (False, False, "gold_exec_failed: <e>")
      - generated fails to execute -> (False, False, "generated_exec_failed: <e>")
      - results differ (lenient)   -> (False, False, "result_mismatch")
      - results match (lenient)    -> (True, <strict?>, None)

    `correct` uses the lenient, BIRD-style normalise_result (type-collapsing:
    1 == "1" == True). `correct_strict` uses normalise_result_strict (no
    cross-type collapse) as a conservative floor, so absolute accuracy is not
    over-credited; strict ⊆ lenient by construction. Report both. The
    transient-error handling and the LLM call itself stay in the caller."""
    try:
        gold_rows = exec_pg(conn, gold_sql)
    except Exception as e:
        # Gold SQL itself failed to execute — this question is unusable
        # for grading; record it distinctly from a model failure. Should
        # be near-zero: pipeline/07_rename_sql_and_validate.py already
        # executes every gold sql_base/sql_rename pair and only keeps
        # questions where both succeed and R1==R2 match, so this branch
        # is a defensive guard, not an expected occurrence.
        return False, False, f"gold_exec_failed: {e}"

    try:
        gen_rows = exec_pg(conn, generated_sql)
    except Exception as e:
        return False, False, f"generated_exec_failed: {e}"

    lenient = normalise_result(gen_rows) == normalise_result(gold_rows)
    strict = lenient and (normalise_result_strict(gen_rows) == normalise_result_strict(gold_rows))
    if lenient:
        return True, strict, None
    return False, False, "result_mismatch"
