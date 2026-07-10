"""Grade API-machine generations against PostgreSQL on the DB machine."""

from __future__ import annotations

import argparse
from pathlib import Path

from _db import (
    PG_BASE_DSN,
    PG_DECOY_DSN,
    PG_RENAME_DECOY_DSN,
    PG_RENAME_DSN,
    new_connection,
)
from _eval_helpers import append_result, grade, metadata_matches, utc_now_iso
from _offline_eval import (
    GENERATIONS_NAME,
    eval_metadata_from_manifest,
    read_jsonl,
    verify_private_bundle,
)

DSN_BY_KEY = {
    "base": PG_BASE_DSN,
    "rename": PG_RENAME_DSN,
    "decoy": PG_DECOY_DSN,
    "rename_decoy": PG_RENAME_DECOY_DSN,
}
RESULTS_BY_EVAL = {
    "contamination": Path("eval/contamination_results.jsonl"),
    "ablation": Path("eval/ablation_results.jsonl"),
}


def load_offline_done(
    results_path: Path,
    expected_metadata: dict,
) -> set[tuple[str, str]]:
    if not results_path.exists():
        return set()
    expected_hash = expected_metadata["offline_bundle"]["requests_sha256"]
    done: set[tuple[str, str]] = set()
    for record in read_jsonl(results_path):
        actual_metadata = record.get("eval_metadata")
        if not metadata_matches(actual_metadata, expected_metadata):
            continue
        if (
            actual_metadata.get("offline_bundle", {}).get("requests_sha256")
            != expected_hash
        ):
            continue
        done.add((record["question_id"], record["condition"]))
    return done


def load_matching_generations(
    path: Path,
    *,
    request_by_id: dict[str, dict],
    model: str,
    effort: str,
) -> dict[str, dict]:
    if not path.exists():
        raise FileNotFoundError(f"Generations file not found: {path}")
    matching: dict[str, dict] = {}
    for record in read_jsonl(path):
        if record.get("model") != model or record.get("effort") != effort:
            continue
        request_id = record.get("request_id")
        if request_id not in request_by_id:
            raise ValueError(f"Unknown generation request_id: {request_id!r}")
        if record.get("request_sha256") != request_by_id[request_id].get(
            "request_sha256"
        ):
            raise ValueError(f"Generation request hash mismatch for {request_id}")
        matching[request_id] = record
    return matching


def make_read_only(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET default_transaction_read_only = on")


def run(args: argparse.Namespace) -> None:
    manifest, requests, grading_by_id = verify_private_bundle(args.bundle_dir)
    request_by_id = {request["request_id"]: request for request in requests}
    generations_path = (
        args.generations or args.bundle_dir / GENERATIONS_NAME
    )
    generations = load_matching_generations(
        generations_path,
        request_by_id=request_by_id,
        model=args.model,
        effort=args.effort,
    )
    missing = len(requests) - len(generations)
    if missing and args.require_complete:
        raise SystemExit(
            f"Generation set is incomplete: {missing}/{len(requests)} missing."
        )

    eval_name = manifest["eval_name"]
    results_path = args.results_path or RESULTS_BY_EVAL[eval_name]
    results_path.parent.mkdir(parents=True, exist_ok=True)
    eval_metadata = eval_metadata_from_manifest(
        manifest,
        model=args.model,
        effort=args.effort,
    )
    done = load_offline_done(results_path, eval_metadata)
    pending_ids = [
        request["request_id"]
        for request in requests
        if request["request_id"] in generations
        and (
            request["question_id"],
            request["condition"],
        )
        not in done
    ]
    print(
        f"{eval_name}/{manifest.get('split', 'test')}: "
        f"{len(requests)} requests, {len(generations)} generations, "
        f"{len(done)} graded, {len(pending_ids)} pending"
    )
    if missing:
        print(f"  {missing} request(s) have no matching generation yet")
    if not pending_ids:
        return

    dsn_keys = {grading_by_id[request_id]["dsn_key"] for request_id in pending_ids}
    connections = {
        key: new_connection(DSN_BY_KEY[key])
        for key in dsn_keys
    }
    for connection in connections.values():
        make_read_only(connection)

    graded = 0
    try:
        for request_id in pending_ids:
            private = grading_by_id[request_id]
            generation = generations[request_id]
            generated_sql = generation.get("generated_sql")
            record = {
                "question_id": private["question_id"],
                "db_id": private["db_id"],
                "condition": private["condition"],
                "eval_metadata": eval_metadata,
                "recorded_at_utc": generation.get("recorded_at_utc")
                or utc_now_iso(),
                "generated_sql": generated_sql,
                "latency_sec": generation.get("latency_sec"),
                "usage": generation.get("usage"),
            }
            if eval_name == "ablation":
                record["arm"] = private["condition"]

            generation_error = generation.get("error")
            if generation_error or not generated_sql:
                record.update(
                    correct=False,
                    correct_strict=False,
                    error=generation_error or "llm_call_failed: empty SQL output",
                )
            else:
                correct, correct_strict, error = grade(
                    connections[private["dsn_key"]],
                    private["gold_sql"],
                    generated_sql,
                )
                record["correct"] = correct
                record["correct_strict"] = correct_strict
                if error is not None:
                    record["error"] = error
            append_result(record, results_path)
            graded += 1
            if graded % 50 == 0:
                print(f"{graded}/{len(pending_ids)} graded", flush=True)
    finally:
        for connection in connections.values():
            connection.close()
    print(f"Wrote {graded} graded result(s) to {results_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grade an offline generation file on the PostgreSQL machine."
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--generations", type=Path, default=None)
    parser.add_argument("--results-path", type=Path, default=None)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument(
        "--effort",
        default="low",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="fail instead of grading the available subset",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
