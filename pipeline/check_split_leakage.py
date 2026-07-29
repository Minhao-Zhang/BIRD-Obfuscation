"""Quantify train/test leakage: how many test questions are recoverable by retrieval?

Under the semantic-layer task the agent reads the train split and must *induce* a
mapping, then answer unseen test questions. A test question that near-duplicates a
train question breaks that: the agent can retrieve instead of induce, which inflates
exactly the capability being measured.

Four signals, all compared within a single database (a similar question about a
different schema is not leakage):

  exact_question_text   normalised question text also present in train
  exact_gold_sql        normalised gold SQL also present in train (the sharpest
                        signal: the agent has literally seen this query)
  template_collision    identical once numbers and quoted literals are masked,
                        i.e. the same template with a different constant
  fuzzy_jaccard_080     word-token Jaccard >= 0.80 against the nearest train question

Writes artifacts/leakage_test_qids.json with each signal's qids and their union, so a
downstream harness can report a retrieval-free score alongside the full one.

    python pipeline/check_split_leakage.py [--threshold 0.80] [--sweep]

Findings as of the 2026-07-29 split are summarised in
docs/reference/gold-quality-audit.md §6.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
from pathlib import Path

ARTIFACTS = Path("artifacts")


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def norm_question(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "")).strip().rstrip(";").lower()


def mask_literals(text: str | None) -> str:
    """Normalised text with constants masked, so `... in 1990` and `... in 1991`
    collide. Quoted strings become @, numbers become #."""
    s = (text or "").lower()
    s = re.sub(r"'[^']*'|\"[^\"]*\"", " @ ", s)
    s = re.sub(r"\d+(?:\.\d+)?", " # ", s)
    return " ".join(re.sub(r"[^a-z0-9@#]+", " ", s).split())


def word_tokens(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.80,
                    help="Jaccard cutoff for the fuzzy signal (default 0.80)")
    ap.add_argument("--sweep", action="store_true",
                    help="also print a threshold sweep and the similarity distribution")
    ap.add_argument("--out", type=Path, default=ARTIFACTS / "leakage_test_qids.json")
    args = ap.parse_args()

    train = read_jsonl(ARTIFACTS / "train_final.jsonl")
    test = read_jsonl(ARTIFACTS / "test_final.jsonl")
    print(f"train {len(train)}  test {len(test)}")

    train_questions = {(r["db_id"], norm_question(r["question"])) for r in train}
    train_sql = {(r["db_id"], norm_sql(r["sql_sqlite"])) for r in train}
    train_masked = {(r["db_id"], mask_literals(r["question"])) for r in train}

    exact_q = {r["question_id"] for r in test
               if (r["db_id"], norm_question(r["question"])) in train_questions}
    exact_sql = {r["question_id"] for r in test
                 if (r["db_id"], norm_sql(r["sql_sqlite"])) in train_sql}
    template = {r["question_id"] for r in test
                if (r["db_id"], mask_literals(r["question"])) in train_masked}

    # Nearest train neighbour per test question, restricted to the same database.
    by_db: dict[str, list[set[str]]] = collections.defaultdict(list)
    for row in train:
        by_db[row["db_id"]].append(word_tokens(row["question"]))
    nearest = {
        r["question_id"]: max(
            (jaccard(word_tokens(r["question"]), t) for t in by_db[r["db_id"]]),
            default=0.0)
        for r in test
    }
    fuzzy = {q for q, v in nearest.items() if v >= args.threshold}

    def pct(n: int) -> str:
        return f"{n:4d}  ({100 * n / len(test):5.2f}% of test)"

    print("\nTest questions recoverable from train (same database):")
    print(f"  exact question text : {pct(len(exact_q))}")
    print(f"  exact gold SQL      : {pct(len(exact_sql))}")
    print(f"  template collision  : {pct(len(template))}")
    print(f"  Jaccard >= {args.threshold:.2f}     : {pct(len(fuzzy))}")

    union = exact_q | exact_sql | template | fuzzy
    print(f"\n  UNION               : {pct(len(union))}")
    print(f"  clean               : {pct(len(test) - len(union))}")

    if args.sweep:
        print("\nThreshold sweep (nearest train neighbour, word-token Jaccard):")
        for th in (0.95, 0.90, 0.85, 0.80, 0.70, 0.60, 0.50):
            n = sum(1 for v in nearest.values() if v >= th)
            print(f"  >= {th:.2f} : {pct(n)}")
        vals = sorted(nearest.values())
        print(f"  median {statistics.median(vals):.3f}   "
              f"p90 {vals[int(0.9 * len(vals))]:.3f}   max {vals[-1]:.3f}")
        print("  Note: the median is low enough that cutoffs below ~0.70 mostly pick up "
              "generic phrasing\n  overlap (\"what is the name of the ...\"), not duplication.")

    args.out.write_text(json.dumps({
        "note": "Test question_ids recoverable from the train split by retrieval rather than "
                "induction. Same-database comparisons only. Generated by "
                "pipeline/check_split_leakage.py.",
        "threshold": args.threshold,
        "exact_question_text": sorted(exact_q),
        "exact_gold_sql": sorted(exact_sql),
        "template_collision": sorted(template),
        f"fuzzy_jaccard_{int(args.threshold * 100):03d}": sorted(fuzzy),
        "union": sorted(union),
        "counts": {"test_total": len(test), "union": len(union)},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
