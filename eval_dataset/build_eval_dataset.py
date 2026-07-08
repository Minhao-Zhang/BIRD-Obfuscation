"""Build (refresh) eval_dataset/ — the git-tracked, self-contained snapshot of
the FINAL obfuscation benchmark plus every mapping/manifest the local eval needs.

`artifacts/` is the pipeline's working directory (several of these files are
gitignored there because they are large or LLM-regeneratable); THIS folder is the
frozen, versioned deliverable that is checked in. Re-run after any rebuild to
refresh the snapshot:

    python eval_dataset/build_eval_dataset.py

The FILES list below is the canonical definition of what the deliverable
contains; keep README.md in sync with it.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts"
DST = ROOT / "eval_dataset"

# (filename, short description) — copied verbatim from artifacts/.
FILES = [
    # --- gold question / SQL dataset (the benchmark itself) ---
    ("train_final.jsonl", "Validated train split (R1==R2). Fields: question_id, db_id, "
     "question, evidence, evidence_rename, difficulty, sql_sqlite, sql_base, sql_rename."),
    ("test_final.jsonl", "Validated test split (same fields). The eval runs on this split."),
    # --- obfuscation dimension 1: identifier rename ---
    ("schema_rename_map.json", "Per-db english->renamed identifier map (tables + columns). "
     "Ground truth for the rename dimension."),
    ("db_language_map.json", "Per-db target language for the rename dimension "
     "(english/french/german/spanish/pinyin/...)."),
    # --- obfuscation dimension 2: decoys / traps ---
    ("trap_manifest.json", "Evil-twin decoy COLUMNS (additive corrupted copies of real "
     "columns). Per trap: db, table, source_column, source_type, operator, is_key, "
     "in_correlated_group, salt, names:{base, rename}."),
    ("trap_table_manifest.json", "Corrupted decoy clone TABLES. Per clone: db, source_table, "
     "columns:[{source_column,source_type,operator,is_key}], names:{base:{table,columns}, "
     "rename:{table,columns}}."),
    ("decoy_map.json", "Step-08 STRUCTURAL (empty) decoy tables/columns map. Superseded for "
     "interactive execute-and-observe agents by the corrupted traps above; kept for provenance."),
    # --- obfuscation dimension 3: question paraphrase ---
    ("question_paraphrases.jsonl", "One SQL-preserving paraphrase per test question "
     "(question_id -> question_paraphrase). Covers the 2030 test questions."),
    # --- eval support ---
    ("gold_star_expanded.jsonl", "SELECT*-expanded gold for the ~5 star queries "
     "(sql_base_expanded / sql_rename_expanded) so decoy columns can never leak into the gold answer."),
    ("order_sensitive_qids.json", "qids to EXCLUDE from strict EX scoring: order_sensitive "
     "(LIMIT-tie / float-accumulation non-determinism) + exec_failed (pre-existing degenerate gold)."),
]


def main():
    DST.mkdir(exist_ok=True)
    missing, copied = [], []
    for name, _desc in FILES:
        s = SRC / name
        if not s.exists():
            missing.append(name)
            continue
        shutil.copy2(s, DST / name)
        try:
            n = sum(1 for _ in open(DST / name, encoding="utf-8"))
        except Exception:
            n = -1
        copied.append((name, n))
        print(f"  copied {name:32s} ({n} lines)")
    print(f"\n{len(copied)}/{len(FILES)} files -> {DST}")
    if missing:
        print("  !! MISSING from artifacts/ (rebuild the pipeline?):", missing)


if __name__ == "__main__":
    main()
