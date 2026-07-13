"""Precompute gold SQL result hashes against pg_rename_decoy.

Executes the rename(+SELECT*-expanded) gold for every train/test question on
``pg_rename_decoy`` and records lenient + strict SHA-256 hashes of the
normalised result multisets. Downstream eval can later skip re-running gold
and compare generated-result hashes against this cache.

Hash algorithm (for external replication): docs/reference/gold-result-hashes.md
Reference helpers: ``hash_normalised_result`` / ``hash_normalised_result_strict``
in ``_db.py``.

Writes:
  artifacts/gold_result_hashes_rename_decoy.jsonl

Refresh the git-tracked snapshot after a rebuild:
  uv run python eval_dataset/build_eval_dataset.py

Run (from repo root):
  uv run python pipeline/precompute_gold_result_hashes.py
  uv run python pipeline/precompute_gold_result_hashes.py --split test --limit 50
  uv run python pipeline/precompute_gold_result_hashes.py --status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _db import (  # noqa: E402
    PG_RENAME_DECOY_DSN,
    exec_pg,
    hash_normalised_result,
    hash_normalised_result_strict,
    new_connection,
)
from _eval_helpers import append_result, dataset_path, utc_now_iso  # noqa: E402

ARTIFACTS = Path("artifacts")
OUT_PATH = ARTIFACTS / "gold_result_hashes_rename_decoy.jsonl"
STAR_EXPANDED_PATH = ARTIFACTS / "gold_star_expanded.jsonl"
DSN_KEY = "rename_decoy"
# Same gold field wiring as ablation arm ``all`` (eval_ablation.ARM_SPEC).
SQL_FIELD = "sql_rename"
EXPANDED_FIELD = "sql_rename_expanded"


def resolve_gold_sql(question_row: dict, expanded_rec: dict | None) -> str | None:
    """Prefer SELECT*-expanded rename gold when present; else ``sql_rename``."""
    if expanded_rec:
        exp_sql = expanded_rec.get(EXPANDED_FIELD)
        if exp_sql:
            return exp_sql
    return question_row.get(SQL_FIELD) or None


def sql_sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def load_expanded() -> dict[str, dict]:
    path = STAR_EXPANDED_PATH if STAR_EXPANDED_PATH.exists() else dataset_path(
        "gold_star_expanded.jsonl"
    )
    if not path.exists():
        return {}
    by_id: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            by_id[rec["question_id"]] = rec
    return by_id


def load_done(path: Path, *, retry_errors: bool) -> dict[str, dict]:
    """Return question_id -> latest record. With retry_errors, omit error rows."""
    if not path.exists():
        return {}
    done: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                qid = rec["question_id"]
            except (json.JSONDecodeError, KeyError):
                continue
            if retry_errors and rec.get("error"):
                done.pop(qid, None)
                continue
            done[qid] = rec
    return done


def load_questions(split: str) -> list[dict]:
    path = dataset_path(f"{split}_final.jsonl")
    if not path.exists():
        raise FileNotFoundError(f"Missing dataset: {path}")
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec["_split"] = split
            rows.append(rec)
    return rows


def make_read_only(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET default_transaction_read_only = on")


def print_status(path: Path) -> None:
    done = load_done(path, retry_errors=False)
    ok = sum(1 for r in done.values() if not r.get("error"))
    err = sum(1 for r in done.values() if r.get("error"))
    train = sum(1 for r in done.values() if r.get("split") == "train")
    test = sum(1 for r in done.values() if r.get("split") == "test")
    print(f"{path}: {len(done)} records ({ok} ok, {err} error; train={train}, test={test})")


def process_one(conn, q: dict, expanded: dict[str, dict]) -> dict:
    qid = q["question_id"]
    gold_sql = resolve_gold_sql(q, expanded.get(qid))
    record = {
        "question_id": qid,
        "db_id": q["db_id"],
        "split": q["_split"],
        "dsn_key": DSN_KEY,
        "sql_sha256": None,
        "nrows": None,
        "hash_lenient": None,
        "hash_strict": None,
        "error": None,
        "recorded_at_utc": utc_now_iso(),
    }
    if not gold_sql:
        record["error"] = "missing_gold_sql"
        return record

    record["sql_sha256"] = sql_sha256(gold_sql)
    try:
        rows = exec_pg(conn, gold_sql)
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        return record

    record["nrows"] = len(rows)
    record["hash_lenient"] = hash_normalised_result(rows)
    record["hash_strict"] = hash_normalised_result_strict(rows)
    return record


def run(args: argparse.Namespace) -> None:
    if args.status:
        print_status(OUT_PATH)
        return

    splits = ["train", "test"] if args.split == "both" else [args.split]
    questions: list[dict] = []
    for split in splits:
        questions.extend(load_questions(split))

    if args.limit is not None:
        questions = questions[: args.limit]

    if args.regenerate and OUT_PATH.exists():
        OUT_PATH.unlink()
        print(f"Removed existing {OUT_PATH}")

    done = load_done(OUT_PATH, retry_errors=args.retry_errors)
    pending = [q for q in questions if q["question_id"] not in done]
    print(
        f"rename_decoy hashes: {len(questions)} selected, "
        f"{len(done)} cached, {len(pending)} pending"
    )
    if not pending:
        print_status(OUT_PATH)
        return

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    expanded = load_expanded()
    conn = new_connection(PG_RENAME_DECOY_DSN)
    make_read_only(conn)
    wrote = 0
    errors = 0
    try:
        for q in pending:
            record = process_one(conn, q, expanded)
            append_result(record, OUT_PATH)
            wrote += 1
            if record.get("error"):
                errors += 1
            if wrote % 50 == 0 or wrote == len(pending):
                print(
                    f"{wrote}/{len(pending)} done "
                    f"({errors} errors this run)",
                    flush=True,
                )
    finally:
        conn.close()

    print(f"Wrote {wrote} record(s) to {OUT_PATH} ({errors} errors)")
    print_status(OUT_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Execute gold SQL on pg_rename_decoy and record lenient/strict "
            "result hashes for train/test questions."
        )
    )
    parser.add_argument(
        "--split",
        choices=["train", "test", "both"],
        default="both",
        help="which final JSONL split(s) to process (default: both)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process only the first N questions after split selection",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print cache progress and exit",
    )
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="delete the existing cache file and recompute from scratch",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="re-run question_ids whose cached record has an error",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
