"""Export a neat, reusable table of (question, gold SQL, generated SQL) records.

Joins graded eval results with the questions, per-condition gold SQL, obfuscation
language, and difficulty into flat JSONL + CSV files under exports/. One row per
graded (eval, condition/arm, question_id). Intended as a shareable artifact for
downstream reuse (analysis, few-shot pools, error review) without needing the
PostgreSQL machine or the eval harness.

Sources (read-only):
  eval/contamination_results.jsonl              graded rows (generated_sql, correctness)
  eval/ablation_results.jsonl
  eval/offline/<bundle>/grading_manifest.private.jsonl   exact gold SQL + dsn_key
  artifacts|eval_dataset/test_final.jsonl       question, evidence, difficulty, gold fields
  artifacts|eval_dataset/question_paraphrases.jsonl      paraphrased question text
  artifacts|eval_dataset/db_language_map.json   db_id -> obfuscation language

Usage:
  uv run python pipeline/export_qa_sql.py --model "Claude-Opus-4.8" --effort high
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _eval_helpers import dataset_path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EXPORT_DIR = REPO / "exports"

# Per-condition metadata for the contamination eval (schema x hints).
CONTAM_META = {
    "base_hint": dict(schema="base", hints=True, evidence_field="evidence"),
    "base_nohint": dict(schema="base", hints=False, evidence_field=None),
    "rename_hint": dict(schema="rename", hints=True, evidence_field="evidence_rename"),
    "rename_nohint": dict(schema="rename", hints=False, evidence_field=None),
}
# Per-arm metadata for the ablation eval (all no-hint).
ABLATION_META = {
    "base": dict(schema="base", question="original"),
    "rename": dict(schema="rename", question="original"),
    "decoy": dict(schema="decoy", question="original"),
    "paraphrase": dict(schema="base", question="paraphrase"),
    "all": dict(schema="rename_decoy", question="paraphrase"),
}
CONTAM_BUNDLE = "eval/offline/contamination"
ABLATION_BUNDLES = {
    "base": "eval/offline/ablation-base",
    "rename": "eval/offline/ablation-rename",
    "decoy": "eval/offline/ablation-decoy",
    "paraphrase": "eval/offline/ablation-paraphrase",
    "all": "eval/offline/ablation-all",
}

COLUMNS = [
    "eval", "condition", "question_id", "db_id", "obfuscation_language",
    "difficulty", "schema_instance", "hints", "question", "evidence",
    "gold_sql", "generated_sql", "correct", "correct_strict", "error",
    "model", "effort",
]


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_gold(bundle_rel: str) -> dict[str, dict]:
    """request_id -> {gold_sql, dsn_key} from a private grading manifest."""
    path = REPO / bundle_rel / "grading_manifest.private.jsonl"
    out: dict[str, dict] = {}
    for r in read_jsonl(path):
        out[r["request_id"]] = {"gold_sql": r["gold_sql"], "dsn_key": r["dsn_key"]}
    return out


def matches(row: dict, model: str, effort: str) -> bool:
    md = row.get("eval_metadata") or {}
    return md.get("model") == model and md.get("effort") == effort


def build_rows(
    *,
    eval_name: str,
    results_rel: str,
    gold_by_reqid: dict[str, dict],
    questions: dict[str, dict],
    paraphrases: dict[str, str],
    lang: dict[str, str],
    model: str,
    effort: str,
) -> list[dict]:
    rows = []
    for r in read_jsonl(REPO / results_rel):
        if not matches(r, model, effort):
            continue
        qid = r["question_id"]
        cond = r["condition"]
        q = questions.get(qid, {})
        request_id = f"{eval_name}:{cond}:{qid}"
        gold = gold_by_reqid.get(request_id, {})

        if eval_name == "contamination":
            meta = CONTAM_META[cond]
            question_text = q.get("question", "")
            ev_field = meta["evidence_field"]
            evidence = q.get(ev_field, "") if ev_field else ""
            hints = meta["hints"]
            schema = meta["schema"]
        else:
            meta = ABLATION_META[cond]
            question_text = (
                paraphrases.get(qid, "")
                if meta["question"] == "paraphrase"
                else q.get("question", "")
            )
            evidence = ""
            hints = False
            schema = meta["schema"]

        rows.append({
            "eval": eval_name,
            "condition": cond,
            "question_id": qid,
            "db_id": r.get("db_id", q.get("db_id", "")),
            "obfuscation_language": lang.get(r.get("db_id", q.get("db_id")), ""),
            "difficulty": q.get("difficulty", ""),
            "schema_instance": schema,
            "hints": hints,
            "question": question_text,
            "evidence": evidence or "",
            "gold_sql": gold.get("gold_sql", ""),
            "generated_sql": r.get("generated_sql") or "",
            "correct": bool(r.get("correct", False)),
            "correct_strict": bool(r.get("correct_strict", False)),
            "error": r.get("error", "") or "",
            "model": model,
            "effort": effort,
        })
    return rows


def write_outputs(name: str, rows: list[dict]) -> list[Path]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = EXPORT_DIR / f"{name}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    csv_path = EXPORT_DIR / f"{name}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    n_ok = sum(r["correct"] for r in rows)
    missing_gold = sum(1 for r in rows if not r["gold_sql"])
    print(
        f"{name}: {len(rows)} rows -> {jsonl_path.name}, {csv_path.name} "
        f"(correct={n_ok}, missing_gold={missing_gold})"
    )
    return [jsonl_path, csv_path]


def write_zip(zip_name: str, files: list[Path]) -> None:
    """Bundle the loose export files into one compressed archive (the committed
    form; the loose .jsonl/.csv are gitignored and regenerable by this script)."""
    zip_path = EXPORT_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            z.write(path, path.name)
    mib = zip_path.stat().st_size / 2**20
    print(f"zip: {zip_path.name} ({len(files)} files, {mib:.1f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Claude-Opus-4.8")
    parser.add_argument("--effort", default="high")
    args = parser.parse_args()

    questions = {r["question_id"]: r for r in read_jsonl(dataset_path("test_final.jsonl"))}
    paraphrases = {
        r["question_id"]: r["question_paraphrase"]
        for r in read_jsonl(dataset_path("question_paraphrases.jsonl"))
        if "question_paraphrase" in r
    }
    with open(dataset_path("db_language_map.json"), encoding="utf-8") as f:
        lang = json.load(f)

    written: list[Path] = []
    contam_gold = load_gold(CONTAM_BUNDLE)
    contam_rows = build_rows(
        eval_name="contamination",
        results_rel="eval/contamination_results.jsonl",
        gold_by_reqid=contam_gold,
        questions=questions, paraphrases=paraphrases, lang=lang,
        model=args.model, effort=args.effort,
    )
    written += write_outputs("contamination_qsql", contam_rows)

    ablation_gold: dict[str, dict] = {}
    for bundle in ABLATION_BUNDLES.values():
        ablation_gold.update(load_gold(bundle))
    ablation_rows = build_rows(
        eval_name="ablation",
        results_rel="eval/ablation_results.jsonl",
        gold_by_reqid=ablation_gold,
        questions=questions, paraphrases=paraphrases, lang=lang,
        model=args.model, effort=args.effort,
    )
    written += write_outputs("ablation_qsql", ablation_rows)

    slug = f"{args.model}_{args.effort}".replace(" ", "-").replace("/", "-")
    write_zip(f"{slug}_qa_sql.zip", written)


if __name__ == "__main__":
    main()
