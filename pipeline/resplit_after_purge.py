"""Re-apply the MIN_QUESTIONS floor and re-split train/test after the gold purge.

Step 01 applied `MIN_QUESTIONS = 60` to the *raw* BIRD pool and split 80/20 per
database. Two rounds of attrition have happened since — step 05/07 validation
failures, then the 2026-07-29 gold-quality purge — so 11 databases have fallen
below the floor and the surviving per-database test fraction has drifted to
12-26% instead of a uniform 20%.

This script restores both invariants over the surviving rows:

  1. Drop every database with fewer than MIN_QUESTIONS surviving questions.
  2. Re-split the rest 80/20 per database using step 01's exact mechanism —
     `Random(SEED ^ crc32(db_id))`, same SEED, same `max(1, round(n * 0.20))`.

It reassigns rows between the two splits; it never edits a row. `sql_sqlite`,
`sql_base` and `sql_rename` are carried through untouched, so the R0==R1 / R1==R2
guarantees still hold and no re-transpilation or database work is needed.

`retained_dbs.json` is deliberately NOT reduced: it describes the 69 schemas
physically present in the four published PostgreSQL dumps, which are unchanged.
The evaluated subset is written to `evaluated_dbs.json` instead.

    python pipeline/resplit_after_purge.py [--dry-run]
    python eval_dataset/build_eval_dataset.py   # refresh the tracked snapshot

Rationale and the full audit trail: docs/reference/gold-quality-audit.md
"""

from __future__ import annotations

import argparse
import json
import random
import zlib
from collections import defaultdict
from pathlib import Path

ARTIFACTS = Path("artifacts")

# Kept identical to pipeline/01_split.py so the resplit is the same procedure,
# not a similar one.
SEED = 42
TEST_FRACTION = 0.20
MIN_QUESTIONS = 60

# Question-keyed companions that must be filtered to the surviving qid set when
# databases are dropped. (train/test membership does not matter to these.)
COMPANIONS = [
    "question_paraphrases.jsonl",
    "gold_star_expanded.jsonl",
    "gold_result_hashes_rename_decoy.jsonl",
]
QID_LIST_FILE = "order_sensitive_qids.json"
QID_LIST_KEYS = ["order_sensitive", "exec_failed"]


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def split_per_db(questions: list[dict], seed: int, test_frac: float):
    """Verbatim from 01_split.py: per-DB crc32-derived seed so the shuffle is
    reproducible without correlating across databases."""
    by_db: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        by_db[q["db_id"]].append(q)

    train, test = [], []
    for db_id in sorted(by_db):
        qs = by_db[db_id]
        rng = random.Random(seed ^ zlib.crc32(db_id.encode()))
        rng.shuffle(qs)
        n_test = max(1, round(len(qs) * test_frac))
        test.extend(qs[:n_test])
        train.extend(qs[n_test:])
    return train, test


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pool = read_jsonl(ARTIFACTS / "train_final.jsonl") + \
        read_jsonl(ARTIFACTS / "test_final.jsonl")
    per_db: dict[str, int] = defaultdict(int)
    for row in pool:
        per_db[row["db_id"]] += 1

    dropped = sorted(db for db, n in per_db.items() if n < MIN_QUESTIONS)
    kept = sorted(db for db, n in per_db.items() if n >= MIN_QUESTIONS)
    print(f"pool: {len(pool)} questions across {len(per_db)} databases")
    print(f"dropping {len(dropped)} databases below MIN_QUESTIONS={MIN_QUESTIONS}:")
    for db in sorted(dropped, key=lambda d: per_db[d]):
        print(f"    {db:28s} {per_db[db]:4d}")

    # Canonicalise the input order before shuffling. rng.shuffle() permutes the
    # list it is given, so without this the assignment would depend on how the
    # rows happened to be arranged across train_final/test_final on disk — and
    # the script would not be idempotent or reproducible from a different
    # starting arrangement.
    survivors = sorted((r for r in pool if r["db_id"] in set(kept)),
                       key=lambda r: (r["db_id"], r["question_id"]))
    train, test = split_per_db(survivors, SEED, TEST_FRACTION)
    # split_per_db shuffles, so restore a deterministic on-disk order.
    train.sort(key=lambda r: (r["db_id"], r["question_id"]))
    test.sort(key=lambda r: (r["db_id"], r["question_id"]))

    print(f"\nkept {len(kept)} databases, {len(survivors)} questions")
    print(f"  train {len(train)} ({100 * len(train) / len(survivors):.1f}%)")
    print(f"  test  {len(test)} ({100 * len(test) / len(survivors):.1f}%)")
    fracs = defaultdict(lambda: [0, 0])
    for r in train:
        fracs[r["db_id"]][0] += 1
    for r in test:
        fracs[r["db_id"]][1] += 1
    ratios = [t / (tr + t) for tr, t in fracs.values()]
    print(f"  per-db test fraction: min {min(ratios):.3f} max {max(ratios):.3f}")
    thin = sorted((t, db) for db, (tr, t) in fracs.items())[:3]
    print(f"  smallest test sets: {', '.join(f'{db} ({t})' for t, db in thin)}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    write_jsonl(ARTIFACTS / "train_final.jsonl", train)
    write_jsonl(ARTIFACTS / "test_final.jsonl", test)

    surviving_qids = {r["question_id"] for r in survivors}
    for name in COMPANIONS:
        path = ARTIFACTS / name
        rows = read_jsonl(path)
        keep = [r for r in rows if r["question_id"] in surviving_qids]
        print(f"  {name:40s} {len(rows):6d} -> {len(keep):6d}")
        write_jsonl(path, keep)

    path = ARTIFACTS / QID_LIST_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in QID_LIST_KEYS:
        payload[key] = sorted(q for q in payload[key] if q in surviving_qids)
    if "counts" in payload:
        payload["counts"] = {k: len(payload[k]) for k in QID_LIST_KEYS}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"  {QID_LIST_FILE:40s} "
          f"{payload['counts'] if 'counts' in payload else ''}")

    # The evaluated subset, distinct from retained_dbs.json (the 69 schemas in
    # the published dumps, which are unchanged).
    out = ARTIFACTS / "evaluated_dbs.json"
    out.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out} ({len(kept)} databases)")
    print(f"dropped databases remain as unreferenced schemas in the four dumps: "
          f"{', '.join(dropped)}")


if __name__ == "__main__":
    main()
