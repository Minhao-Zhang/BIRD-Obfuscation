"""
Step 1: Filter databases and produce deterministic 80/20 train/test split.

Writes:
  artifacts/train.jsonl
  artifacts/test.jsonl

Each line:
  {question_id, db_id, question, evidence, difficulty, sql_sqlite}

sql_rename is added by a later step.

Run: uv run python pipeline/01_split.py
"""

import json
import random
import zlib
from pathlib import Path

SEED = 42
TEST_FRACTION = 0.20
MIN_QUESTIONS = 60

# Excluded databases (< 60 combined questions per dataset.md)
EXCLUDED_DBS = {
    "craftbeer", "citeseer", "genes", "shooting", "trains",
    "music_tracker", "movie", "coinmarketcap", "mental_health_survey",
    "european_football_1", "human_resources",
}

DATA = Path("data")
OUT = Path("artifacts")
OUT.mkdir(exist_ok=True)


def load_questions(path: Path, split_prefix: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    out = []
    for i, r in enumerate(rows):
        if r["db_id"] in EXCLUDED_DBS:
            continue
        # Dev has question_id; train does not — generate a stable synthetic id
        qid = str(r["question_id"]) if "question_id" in r else f"{split_prefix}_{i}"
        out.append({
            "question_id": qid,
            "db_id": r["db_id"],
            "question": r["question"],
            "evidence": r.get("evidence", ""),
            "difficulty": r.get("difficulty", ""),  # empty for train
            "sql_sqlite": r["SQL"],
        })
    return out


def split_per_db(questions: list[dict], seed: int, test_frac: float):
    from collections import defaultdict
    by_db: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        by_db[q["db_id"]].append(q)

    train, test = [], []
    for db_id in sorted(by_db):  # sorted for determinism
        qs = by_db[db_id]
        # Per-DB seed: reusing the same Random(seed) for every DB would apply
        # an identical shuffle permutation index-for-index across all 69 DBs,
        # correlating the split with any positional structure BIRD's source
        # JSON happens to have (e.g. questions grouped by difficulty). Python's
        # str hash() is randomised per-process, so derive the per-DB seed from
        # a stable hash (crc32) instead, to keep the split reproducible.
        rng = random.Random(seed ^ zlib.crc32(db_id.encode()))
        rng.shuffle(qs)
        n_test = max(1, round(len(qs) * test_frac))
        test.extend(qs[:n_test])
        train.extend(qs[n_test:])
    return train, test


def write_jsonl(records: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    train_qs = load_questions(DATA / "train" / "train.json", split_prefix="train")
    dev_qs = load_questions(DATA / "dev" / "dev.json", split_prefix="dev")
    all_qs = train_qs + dev_qs

    # Verify exclusion: count per DB
    from collections import Counter
    counts = Counter(q["db_id"] for q in all_qs)
    remaining_dbs = sorted(counts)
    print(f"Retained DBs: {len(remaining_dbs)} (excluded {len(EXCLUDED_DBS)})")
    print(f"Total questions: {len(all_qs)}")

    # Sanity-check: all retained DBs have >= MIN_QUESTIONS
    below = [(db, n) for db, n in counts.items() if n < MIN_QUESTIONS]
    if below:
        print(f"WARNING: DBs below {MIN_QUESTIONS} questions: {below}")

    train, test = split_per_db(all_qs, seed=SEED, test_frac=TEST_FRACTION)
    print(f"Train: {len(train)}, Test: {len(test)}")

    write_jsonl(train, OUT / "train.jsonl")
    write_jsonl(test, OUT / "test.jsonl")
    print(f"Written to {OUT}/train.jsonl and {OUT}/test.jsonl")

    # Write retained DB list for downstream steps
    with open(OUT / "retained_dbs.json", "w") as f:
        json.dump(sorted(remaining_dbs), f, indent=2)
    print(f"Written {OUT}/retained_dbs.json")


if __name__ == "__main__":
    main()
