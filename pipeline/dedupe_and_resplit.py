"""Remove duplicate questions, then rebuild the train/test split.

Why dedupe rather than just split more carefully: BIRD contains duplicate and
near-duplicate questions within a single database. They are harmless redundancy in
BIRD's own split, because its train and dev share no databases at all, so a
duplicate can never straddle that boundary. This dataset pools train+dev and
re-splits *within* each database, which is required by the semantic-layer task
(the agent must be tested on a schema it has seen questions from). That pooling is
what turns BIRD's within-split duplication into cross-split leakage, where a test
question is recoverable by retrieval instead of induction.

Three signals count as duplicate, all compared within one database:

  exact question text   normalised text identical
  identical gold SQL    normalised SQL identical, however the question is worded
  fuzzy near-duplicate  word-token Jaccard >= 0.80

Linked rows are collapsed transitively into clusters, and one representative
survives per cluster (lowest `question_id`, so the choice is deterministic).

Template collision is deliberately **not** a dedupe signal. Masking numbers and
quoted literals makes "the page titled Anys 90" collide with "the page titled
Abril", but those have different gold SQL and are legitimately distinct questions.
Removing them would cost real coverage. They are still reported by
`check_split_leakage.py` so the effect stays visible.

After deduplication the `MIN_QUESTIONS` floor is re-applied and the split is
rebuilt with step 01's mechanism, so `Random(SEED ^ crc32(db_id))` and the same
SEED. Rows are only ever dropped or reassigned, never edited, so `sql_sqlite` /
`sql_base` / `sql_rename` carry through untouched and R0==R1 / R1==R2 still hold.

    python pipeline/dedupe_and_resplit.py [--dry-run] [--threshold 0.80]
    python eval_dataset/build_eval_dataset.py

Rationale and measured before/after: docs/reference/gold-quality-audit.md §6.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import random
import re
import zlib
from pathlib import Path

ARTIFACTS = Path("artifacts")

# Kept identical to pipeline/01_split.py.
SEED = 42
TEST_FRACTION = 0.20
MIN_QUESTIONS = 60

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


def norm_question(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "")).strip().rstrip(";").lower()


def word_tokens(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def find_clusters(pool: list[dict], threshold: float) -> list[list[str]]:
    """Union-find over the three duplicate signals, within each database."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_db: dict[str, list[dict]] = collections.defaultdict(list)
    for row in pool:
        by_db[row["db_id"]].append(row)

    for rows in by_db.values():
        annotated = [(r, norm_question(r["question"]), norm_sql(r["sql_sqlite"]),
                      word_tokens(r["question"])) for r in rows]
        for (ra, qa, sa, ta), (rb, qb, sb, tb) in itertools.combinations(annotated, 2):
            if qa == qb or sa == sb or jaccard(ta, tb) >= threshold:
                union(ra["question_id"], rb["question_id"])

    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for qid in parent:
        grouped[find(qid)].append(qid)
    return [sorted(g) for g in grouped.values() if len(g) > 1]


def split_per_db(questions: list[dict], seed: int, test_frac: float):
    """Verbatim from 01_split.py. The per-database seed matters: one shared
    Random(seed) would apply an identical permutation index-for-index across every
    database, correlating the split with any positional structure in the source."""
    by_db: dict[str, list[dict]] = collections.defaultdict(list)
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
    ap.add_argument("--threshold", type=float, default=0.80,
                    help="Jaccard cutoff for the fuzzy duplicate signal")
    args = ap.parse_args()

    pool = read_jsonl(ARTIFACTS / "train_final.jsonl") + \
        read_jsonl(ARTIFACTS / "test_final.jsonl")
    print(f"pool: {len(pool)} questions across "
          f"{len({r['db_id'] for r in pool})} databases")

    # --- 1. deduplicate -------------------------------------------------------
    clusters = find_clusters(pool, args.threshold)
    drop = {qid for c in clusters for qid in c[1:]}
    kept = [r for r in pool if r["question_id"] not in drop]
    print(f"\nduplicate clusters: {len(clusters)}  "
          f"rows involved: {sum(len(c) for c in clusters)}  dropped: {len(drop)}")
    print(f"after dedupe: {len(kept)} questions")

    # --- 2. re-apply the floor ------------------------------------------------
    per_db: dict[str, int] = collections.Counter(r["db_id"] for r in kept)
    below = sorted((n, db) for db, n in per_db.items() if n < MIN_QUESTIONS)
    if below:
        print(f"\ndropping {len(below)} database(s) now below "
              f"MIN_QUESTIONS={MIN_QUESTIONS}:")
        for n, db in below:
            print(f"    {db:28s} {n}")
    survivors = [r for r in kept if per_db[r["db_id"]] >= MIN_QUESTIONS]
    evaluated = sorted({r["db_id"] for r in survivors})

    # --- 3. re-split ----------------------------------------------------------
    # Canonical input order, so the shuffle does not depend on the on-disk layout
    # and the script stays idempotent.
    survivors.sort(key=lambda r: (r["db_id"], r["question_id"]))
    train, test = split_per_db(survivors, SEED, TEST_FRACTION)
    train.sort(key=lambda r: (r["db_id"], r["question_id"]))
    test.sort(key=lambda r: (r["db_id"], r["question_id"]))

    print(f"\nfinal: {len(evaluated)} databases, {len(survivors)} questions")
    print(f"  train {len(train)} ({100 * len(train) / len(survivors):.1f}%)")
    print(f"  test  {len(test)} ({100 * len(test) / len(survivors):.1f}%)")
    fr: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for r in train:
        fr[r["db_id"]][0] += 1
    for r in test:
        fr[r["db_id"]][1] += 1
    ratios = [t / (tr + t) for tr, t in fr.values()]
    print(f"  per-db test fraction: min {min(ratios):.3f} max {max(ratios):.3f}")

    # --- 4. confirm leakage is gone ------------------------------------------
    train_ids = {r["question_id"] for r in train}
    leaked = {qid for c in clusters
              for qid in c
              if any(o in train_ids for o in c) and qid in {r["question_id"] for r in test}}
    print(f"  cross-split duplicate leakage: {len(leaked)} test questions")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    write_jsonl(ARTIFACTS / "train_final.jsonl", train)
    write_jsonl(ARTIFACTS / "test_final.jsonl", test)
    (ARTIFACTS / "evaluated_dbs.json").write_text(
        json.dumps(evaluated, indent=2) + "\n", encoding="utf-8")

    surviving = {r["question_id"] for r in survivors}
    for name in COMPANIONS:
        path = ARTIFACTS / name
        rows = read_jsonl(path)
        keep = [r for r in rows if r["question_id"] in surviving]
        print(f"  {name:40s} {len(rows):6d} -> {len(keep):6d}")
        write_jsonl(path, keep)

    path = ARTIFACTS / QID_LIST_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in QID_LIST_KEYS:
        payload[key] = sorted(q for q in payload[key] if q in surviving)
    if "counts" in payload:
        payload["counts"] = {k: len(payload[k]) for k in QID_LIST_KEYS}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"  {QID_LIST_FILE:40s} {payload.get('counts')}")

    # Audit trail: what was collapsed, and which representative survived. Only the
    # run that actually removes rows has anything to record, so a later no-op run
    # (the tree is already deduped, so no clusters are found) must not clobber it.
    out = ARTIFACTS / "dedupe_clusters.json"
    if not clusters and out.exists():
        print(f"\nno duplicates found; leaving existing {out} intact")
        return
    out.write_text(json.dumps({
        "note": "Duplicate clusters collapsed before the split. Signals: exact question "
                "text, identical gold SQL, word-token Jaccard >= threshold, compared "
                "within one database. The first id of each cluster survived; the rest "
                "were dropped. Template collision was deliberately excluded, see "
                "pipeline/dedupe_and_resplit.py.",
        "threshold": args.threshold,
        "clusters": [{"kept": c[0], "dropped": c[1:]} for c in
                     sorted(clusters, key=lambda c: c[0])],
        "counts": {"clusters": len(clusters), "rows_dropped": len(drop)},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
