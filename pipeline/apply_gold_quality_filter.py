"""Drop questions whose BIRD gold annotation is superseded or withdrawn upstream.

Consumes artifacts/gold_quality_flags.jsonl (see build_gold_quality_flags.py) and
rewrites every question-keyed artifact in place, keeping only `clean: true` rows.

Nothing schema-level is touched: schema_rename_map, decoy_map, trap_manifest,
trap_table_manifest, db_language_map and the four published PostgreSQL dumps are
question-agnostic and remain byte-identical. The surviving rows keep their existing
train/test assignment and their already-validated sql_base / sql_rename, so this
filter needs no re-transpilation (step 05), no R1==R2 re-validation (step 07) and
no database rebuild.

Run build_gold_quality_flags.py first, then:

    python pipeline/apply_gold_quality_filter.py [--dry-run]
    python eval_dataset/build_eval_dataset.py     # refresh the tracked snapshot

Rationale and citations: docs/reference/gold-quality-audit.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ARTIFACTS = Path("artifacts")
FLAGS = ARTIFACTS / "gold_quality_flags.jsonl"

# Question-keyed JSONL: every row carries a question_id and is dropped with it.
JSONL_FILES = [
    "train.jsonl",
    "test.jsonl",
    "train_final.jsonl",
    "test_final.jsonl",
    "question_paraphrases.jsonl",
    "gold_star_expanded.jsonl",
    "gold_result_hashes_rename_decoy.jsonl",
]

# Exclusion lists of bare qids, plus a derived `counts` block to keep in sync.
QID_LIST_FILE = "order_sensitive_qids.json"
QID_LIST_KEYS = ["order_sensitive", "exec_failed"]


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be dropped without writing")
    args = ap.parse_args()

    flags = read_jsonl(FLAGS)
    clean = {r["question_id"] for r in flags if r["clean"]}
    dropped = {r["question_id"] for r in flags if not r["clean"]}
    print(f"{FLAGS}: {len(clean)} clean / {len(flags)} total "
          f"({len(dropped)} to drop)\n")

    for name in JSONL_FILES:
        path = ARTIFACTS / name
        if not path.exists():
            print(f"  {name:40s} MISSING - skipped")
            continue
        rows = read_jsonl(path)
        kept = [r for r in rows if r["question_id"] in clean]
        print(f"  {name:40s} {len(rows):6d} -> {len(kept):6d} "
              f"({len(rows) - len(kept)} dropped)")
        if not args.dry_run:
            write_jsonl(path, kept)

    path = ARTIFACTS / QID_LIST_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in QID_LIST_KEYS:
        before = payload[key]
        payload[key] = sorted(q for q in before if q in clean)
        print(f"  {QID_LIST_FILE}:{key:24s} {len(before):6d} -> "
              f"{len(payload[key]):6d}")
    if "counts" in payload:
        payload["counts"] = {k: len(payload[k]) for k in QID_LIST_KEYS}
    if not args.dry_run:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    # Per-database survival, so a rebuilt split can be thresholded on real counts.
    final = read_jsonl(ARTIFACTS / "train_final.jsonl") + \
        read_jsonl(ARTIFACTS / "test_final.jsonl")
    per_db: dict[str, int] = {}
    for row in final:
        per_db[row["db_id"]] = per_db.get(row["db_id"], 0) + 1
    below = sorted((n, db) for db, n in per_db.items() if n < 60)
    print(f"\n{len(final)} questions remain across {len(per_db)} databases")
    print(f"databases below the step-01 MIN_QUESTIONS=60 floor: {len(below)}")
    for n, db in below:
        print(f"    {db:28s} {n}")


if __name__ == "__main__":
    main()
