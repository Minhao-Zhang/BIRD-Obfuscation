"""Prepare immutable file bundles for split-machine evaluation.

This command runs only on the PostgreSQL machine. It reads the live stripped
DDL and benchmark artifacts, then writes:

  requests.jsonl                 safe to copy to the LLM/API machine
  grading_manifest.private.jsonl keep on the PostgreSQL machine
  manifest.json                  safe bundle metadata and integrity hashes
  README.txt                     handoff instructions

The API machine returns one JSONL record per request containing request_id,
request_sha256, and generated_sql. Gold SQL and database routing never leave
the PostgreSQL machine.

Examples:
  uv run python pipeline/prepare_offline_eval.py --eval contamination
  uv run python pipeline/prepare_offline_eval.py --eval contamination --split train
  uv run python pipeline/prepare_offline_eval.py --eval ablation --arms base
  uv run python pipeline/prepare_offline_eval.py --eval ablation --arms rename --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# Load DSN overrides before importing _db or either eval module: their DSN
# constants are resolved at import time.
load_dotenv()

from _db import new_connection  # noqa: E402
from _eval_helpers import (  # noqa: E402
    SYSTEM_INSTRUCTIONS,
    build_prompt,
    dataset_path,
    get_schema_ddl,
    utc_now_iso,
)
from _offline_eval import (  # noqa: E402
    MANIFEST_NAME,
    OFFLINE_BUNDLE_SCHEMA_VERSION,
    PRIVATE_GRADING_NAME,
    REQUESTS_NAME,
    canonical_sha256,
    default_bundle_dir,
    sha256_bytes,
    sha256_file,
)
from eval_ablation import (  # noqa: E402
    ARMS,
    ARM_SPEC,
    DSN_FOR_KEY,
    PROMPT_VERSION as ABLATION_PROMPT_VERSION,
    build_tasks,
    group_by_dsn_and_db,
    load_jsonl_map,
    parse_arms,
)
from eval_contamination import (  # noqa: E402
    CONDITIONS,
    CONDITION_SPEC,
    DSN_FOR_SCHEMA,
    PROMPT_VERSION as CONTAMINATION_PROMPT_VERSION,
    group_by_schema_and_db,
)

README_NAME = "README.txt"


def git_output(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return result.stdout.strip()


def parse_conditions(spec: str) -> list[str]:
    requested = [item.strip() for item in spec.split(",") if item.strip()]
    unknown = [item for item in requested if item not in CONDITION_SPEC]
    if unknown:
        raise SystemExit(
            f"Unknown condition(s): {unknown}. Choose from {CONDITIONS}."
        )
    selected = [condition for condition in CONDITIONS if condition in requested]
    if not selected:
        raise SystemExit("At least one contamination condition is required.")
    return selected


def load_questions(split: str, limit: int | None) -> list[dict]:
    path = dataset_path(f"{split}_final.jsonl")
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    seen: dict[str, dict] = {}
    for row in rows:
        seen[row["question_id"]] = row
    questions = list(seen.values())
    if limit:
        questions = questions[:limit]
    return questions


def request_record(
    *,
    eval_name: str,
    condition: str,
    question_id: str,
    db_id: str,
    prompt_version: str,
    prompt: str,
    schema_sha256: str,
) -> dict:
    identity = {
        "eval_name": eval_name,
        "condition": condition,
        "question_id": question_id,
        "db_id": db_id,
        "prompt_version": prompt_version,
        "instructions": SYSTEM_INSTRUCTIONS,
        "input": prompt,
        "schema_sha256": schema_sha256,
    }
    request_sha256 = canonical_sha256(identity)
    return {
        "request_schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
        "request_id": f"{eval_name}:{condition}:{question_id}",
        **identity,
        "request_sha256": request_sha256,
    }


def grading_record(
    request: dict,
    *,
    dsn_key: str,
    gold_sql: str,
) -> dict:
    return {
        "grading_schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "question_id": request["question_id"],
        "db_id": request["db_id"],
        "condition": request["condition"],
        "dsn_key": dsn_key,
        "gold_sql": gold_sql,
    }


def prepare_contamination(
    split: str,
    limit: int | None,
    conditions: list[str],
) -> tuple[list[dict], list[dict], list[str], str]:
    questions = load_questions(split, limit)
    tasks = [(q, condition) for q in questions for condition in conditions]
    groups = group_by_schema_and_db(tasks)
    schemas_needed = {CONDITION_SPEC[c]["schema"] for c in conditions}
    connections = {
        schema: new_connection(DSN_FOR_SCHEMA[schema])
        for schema in schemas_needed
    }
    requests: list[dict] = []
    grading: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for (schema, db_id), group in groups:
            ddl = get_schema_ddl(connections[schema], db_id)
            if not ddl:
                raise RuntimeError(
                    f"No tables found for schema {db_id!r} on {schema!r}."
                )
            ddl_sha256 = sha256_bytes(ddl.encode("utf-8"))
            for question, condition in group:
                spec = CONDITION_SPEC[condition]
                evidence_field = spec["evidence_field"]
                evidence = question.get(evidence_field) if evidence_field else None
                prompt = build_prompt(db_id, ddl, question["question"], evidence)
                request = request_record(
                    eval_name="contamination",
                    condition=condition,
                    question_id=question["question_id"],
                    db_id=db_id,
                    prompt_version=CONTAMINATION_PROMPT_VERSION,
                    prompt=prompt,
                    schema_sha256=ddl_sha256,
                )
                if request["request_id"] in seen_ids:
                    raise RuntimeError(f"Duplicate request id: {request['request_id']}")
                seen_ids.add(request["request_id"])
                gold_sql = question.get(spec["sql_field"])
                if not gold_sql:
                    raise RuntimeError(
                        f"Missing gold SQL for {request['request_id']}."
                    )
                requests.append(request)
                grading.append(
                    grading_record(
                        request,
                        dsn_key=schema,
                        gold_sql=gold_sql,
                    )
                )
    finally:
        for connection in connections.values():
            connection.close()
    return (
        requests,
        grading,
        [f"{split}_final.jsonl"],
        CONTAMINATION_PROMPT_VERSION,
    )


def prepare_ablation(
    split: str,
    limit: int | None,
    arms: list[str],
) -> tuple[list[dict], list[dict], list[str], str]:
    questions = load_questions(split, limit)
    paraphrases = load_jsonl_map(
        dataset_path("question_paraphrases.jsonl"),
        "question_paraphrase",
    )
    expanded = load_jsonl_map(dataset_path("gold_star_expanded.jsonl"), None)
    tasks, skipped = build_tasks(questions, arms, paraphrases, expanded, set())
    if skipped:
        raise RuntimeError(
            f"Cannot create a complete bundle: {skipped} task(s) are missing "
            "question paraphrases. For train, first run "
            "`uv run python pipeline/09_paraphrase_questions.py --include-train`."
        )
    expected = len(questions) * len(arms)
    if len(tasks) != expected:
        raise RuntimeError(
            f"Expected {expected} ablation tasks, built {len(tasks)}."
        )

    groups = group_by_dsn_and_db(tasks)
    dsn_keys_needed = {ARM_SPEC[arm]["dsn_key"] for arm in arms}
    connections = {
        dsn_key: new_connection(DSN_FOR_KEY[dsn_key])
        for dsn_key in dsn_keys_needed
    }
    requests: list[dict] = []
    grading: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for (dsn_key, db_id), group in groups:
            ddl = get_schema_ddl(connections[dsn_key], db_id)
            if not ddl:
                raise RuntimeError(
                    f"No tables found for schema {db_id!r} on {dsn_key!r}."
                )
            ddl_sha256 = sha256_bytes(ddl.encode("utf-8"))
            for task in group:
                question = task["question"]
                arm = task["arm"]
                gold_sql = task["gold_sql"]
                if not gold_sql:
                    raise RuntimeError(
                        f"Missing gold SQL for ablation:{arm}:{question['question_id']}."
                    )
                prompt = build_prompt(
                    db_id,
                    ddl,
                    task["question_text"],
                    evidence=None,
                )
                request = request_record(
                    eval_name="ablation",
                    condition=arm,
                    question_id=question["question_id"],
                    db_id=db_id,
                    prompt_version=ABLATION_PROMPT_VERSION,
                    prompt=prompt,
                    schema_sha256=ddl_sha256,
                )
                if request["request_id"] in seen_ids:
                    raise RuntimeError(f"Duplicate request id: {request['request_id']}")
                seen_ids.add(request["request_id"])
                requests.append(request)
                grading.append(
                    grading_record(
                        request,
                        dsn_key=dsn_key,
                        gold_sql=gold_sql,
                    )
                )
    finally:
        for connection in connections.values():
            connection.close()
    # Keep metadata identical across separately prepared arms so their graded
    # rows can be summarized together. These are the full ablation inputs even
    # when a particular arm does not consume every auxiliary file.
    dataset_names = [
        f"{split}_final.jsonl",
        "gold_star_expanded.jsonl",
        "question_paraphrases.jsonl",
    ]
    return requests, grading, dataset_names, ABLATION_PROMPT_VERSION


def write_jsonl(path: Path, records: list[dict]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def write_json(path: Path, value: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def write_readme(path: Path, eval_name: str, split: str) -> None:
    text = f"""Offline {eval_name} evaluation bundle ({split} split)

COPY TO THE API MACHINE:
  - {REQUESTS_NAME}
  - {MANIFEST_NAME}

KEEP ONLY ON THE POSTGRESQL MACHINE:
  - {PRIVATE_GRADING_NAME}

On the API machine:
  uv run python pipeline/run_offline_generations.py --bundle-dir <bundle-dir> --model <model>

Never send grading_manifest.private.jsonl to the API machine: it contains gold SQL.

Copy generations.jsonl back to this directory, then on the PostgreSQL machine:
  uv run python pipeline/grade_offline_eval.py --bundle-dir <bundle-dir> --model <model>

Both commands verify the request file hash and every request_id/request_sha256
pair. The dsn_key in the private manifest selects the local database without
exposing DSN credentials.
"""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def prepare(args: argparse.Namespace) -> None:
    output_dir = args.output_dir
    output_files = [
        output_dir / REQUESTS_NAME,
        output_dir / PRIVATE_GRADING_NAME,
        output_dir / MANIFEST_NAME,
        output_dir / README_NAME,
    ]
    existing = [path for path in output_files if path.exists()]
    if existing and not args.overwrite:
        names = ", ".join(str(path) for path in existing)
        raise SystemExit(
            f"Refusing to overwrite existing bundle file(s): {names}. "
            "Pass --overwrite to replace the complete bundle."
        )

    if args.eval_name == "contamination":
        selected = parse_conditions(args.conditions)
        requests, grading, dataset_names, prompt_version = prepare_contamination(
            args.split,
            args.limit,
            selected,
        )
        selection_key = "conditions"
    else:
        selected = parse_arms(args.arms)
        if not selected:
            raise SystemExit("At least one ablation arm is required.")
        requests, grading, dataset_names, prompt_version = prepare_ablation(
            args.split,
            args.limit,
            selected,
        )
        selection_key = "arms"

    output_dir.mkdir(parents=True, exist_ok=True)
    requests_path = output_dir / REQUESTS_NAME
    grading_path = output_dir / PRIVATE_GRADING_NAME
    write_jsonl(requests_path, requests)
    write_jsonl(grading_path, grading)

    dataset_files = {}
    for name in dataset_names:
        path = dataset_path(name)
        dataset_files[name] = {
            "path": str(path),
            "sha256": sha256_file(path) if path.exists() else None,
        }
    manifest = {
        "offline_bundle_schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
        "created_at_utc": utc_now_iso(),
        "eval_name": args.eval_name,
        "split": args.split,
        "prompt_version": prompt_version,
        selection_key: selected,
        "question_limit": args.limit,
        "request_count": len(requests),
        "requests_file": REQUESTS_NAME,
        "requests_sha256": sha256_file(requests_path),
        "grading_manifest_sha256": sha256_file(grading_path),
        "dataset_files": dataset_files,
        "git_commit": git_output(["rev-parse", "HEAD"]),
        "git_dirty": bool(git_output(["status", "--porcelain"])),
    }
    write_json(output_dir / MANIFEST_NAME, manifest)
    write_readme(output_dir / README_NAME, args.eval_name, args.split)

    print(f"Prepared {len(requests)} requests in {output_dir}")
    print(f"  copy: {requests_path}")
    print(f"  copy: {output_dir / MANIFEST_NAME}")
    print(f"  keep private: {grading_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export prompts and private grading data for split-machine evaluation."
    )
    parser.add_argument(
        "--eval",
        dest="eval_name",
        required=True,
        choices=["contamination", "ablation"],
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="bundle directory (default: eval/offline/<eval>)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="limit number of questions from the selected split",
    )
    parser.add_argument(
        "--split",
        choices=["test", "train"],
        default="test",
        help="dataset split to prepare (default: test)",
    )
    parser.add_argument(
        "--conditions",
        default=",".join(CONDITIONS),
        help=f"contamination conditions; comma-separated subset of {CONDITIONS}",
    )
    parser.add_argument(
        "--arms",
        default=",".join(ARMS),
        help=f"ablation arms; comma-separated subset of {ARMS}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing complete bundle",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero.")
    if args.output_dir is None:
        args.output_dir = default_bundle_dir(args.eval_name, args.split)
        if args.eval_name == "ablation":
            selected_arms = parse_arms(args.arms)
            if selected_arms != ARMS:
                args.output_dir = Path(
                    f"{args.output_dir}-{'-'.join(selected_arms)}"
                )
        else:
            selected_conditions = parse_conditions(args.conditions)
            if selected_conditions != CONDITIONS:
                args.output_dir = Path(
                    f"{args.output_dir}-{'-'.join(selected_conditions)}"
                )
    prepare(args)


if __name__ == "__main__":
    main()
