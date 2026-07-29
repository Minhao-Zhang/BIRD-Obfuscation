"""Flag questions whose BIRD gold annotation is superseded or withdrawn upstream.

Two official birdsql releases post-date BIRD 2023 and disagree with the gold this
dataset was built from:

  * bird_sql_dev_20251106 (1534 rows, ids 0-1533) - a *corrected* dev split. Joins
    to dev-origin questions directly on question_id.
  * bird23-train-filtered (6601 of 9428 rows) - a *filtered* train split: BIRD
    removed the questions it would not stand behind rather than fixing them. It
    carries no question_id, so the join is on (db_id, normalised question); a
    question's absence is the signal.

Emits artifacts/gold_quality_flags.jsonl, one row per question_id, so downstream
harnesses can recompute metrics over the clean subset by filtering on question_id
alone - no re-generation and no re-grading required.

Usage:
    python pipeline/build_gold_quality_flags.py [--dev1106 PATH] [--train-filtered PATH]

Both inputs default to artifacts/upstream/. Download them with:
    huggingface-cli download birdsql/bird_sql_dev_20251106 --repo-type dataset
    huggingface-cli download birdsql/bird23-train-filtered --repo-type dataset
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ARTIFACTS = Path("artifacts")
UPSTREAM = ARTIFACTS / "upstream"

# The 11 databases BIRD ships in its dev split; every other retained db_id is
# train-origin. Cheaper and more robust than re-reading the source directories.
BIRD_DEV_DBS = frozenset({
    "california_schools", "card_games", "codebase_community",
    "debit_card_specializing", "european_football_2", "financial",
    "formula_1", "student_club", "superhero", "thrombosis_prediction",
    "toxicology",
})


def load_records(path: Path) -> list[dict]:
    """Read either a JSON array or JSONL, whichever the file happens to be."""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", sql or "").strip().rstrip(";").lower()


def norm_question(text: str | None) -> str:
    """Aggressive normalisation - the join must survive punctuation and casing
    drift between BIRD releases, and questions are distinctive enough that
    collisions are not a concern."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev1106", type=Path,
                    default=UPSTREAM / "dev_20251106-00000-of-00001.json")
    ap.add_argument("--train-filtered", type=Path,
                    default=UPSTREAM / "train-00000-of-00001.jsonl")
    ap.add_argument("--out", type=Path,
                    default=ARTIFACTS / "gold_quality_flags.jsonl")
    args = ap.parse_args()

    dev1106 = {str(r["question_id"]): r for r in load_records(args.dev1106)}
    kept_train = {
        (r["db_id"], norm_question(r["question"]))
        for r in load_records(args.train_filtered)
    }

    rows: list[dict] = []
    for split, path in (("train", ARTIFACTS / "train_final.jsonl"),
                        ("test", ARTIFACTS / "test_final.jsonl")):
        for rec in load_records(path):
            qid = str(rec["question_id"])
            flag = {
                "question_id": qid,
                "db_id": rec["db_id"],
                "split": split,
                "bird_origin": "dev" if rec["db_id"] in BIRD_DEV_DBS else "train",
                "clean": True,
                "reason": "",
            }

            if flag["bird_origin"] == "dev":
                gold = dev1106.get(qid)
                if gold is None:
                    # No dev-origin question should be missing from a contiguous
                    # 0-1533 release; surface it rather than silently pass it.
                    flag["clean"] = False
                    flag["reason"] = "absent_from_dev1106"
                else:
                    flag["dev1106_question_changed"] = (
                        norm_question(gold["question"]) != norm_question(rec["question"]))
                    flag["dev1106_evidence_changed"] = (
                        norm_question(gold.get("evidence")) != norm_question(rec.get("evidence")))
                    if norm_sql(gold["SQL"]) != norm_sql(rec["sql_sqlite"]):
                        flag["clean"] = False
                        flag["reason"] = "dev1106_gold_sql_changed"
                        # Carried so the 398 affected rows can optionally be
                        # re-translated and re-graded instead of dropped.
                        flag["dev1106_gold_sql"] = gold["SQL"]
            elif (rec["db_id"], norm_question(rec["question"])) not in kept_train:
                flag["clean"] = False
                flag["reason"] = "dropped_by_bird23_train_filter"

            rows.append(flag)

    rows.sort(key=lambda r: (r["split"], r["db_id"], r["question_id"]))
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"wrote {args.out} ({len(rows)} rows)")
    for split in ("train", "test"):
        sub = [r for r in rows if r["split"] == split]
        clean = sum(r["clean"] for r in sub)
        print(f"  {split}: {clean}/{len(sub)} clean ({100 * clean / len(sub):.1f}%)")
        reasons: dict[str, int] = {}
        for r in sub:
            if not r["clean"]:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"      {reason}: {n}")


if __name__ == "__main__":
    main()
