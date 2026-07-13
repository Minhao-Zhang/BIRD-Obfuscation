**English** · [中文](README-zh.md)

# Final evaluation dataset

The frozen, **git-tracked** deliverable for the BIRD text-to-SQL **obfuscation
benchmark**: the validated gold question/SQL pairs plus every mapping and manifest
the evaluation needs. This is a **snapshot of `artifacts/`** (the pipeline's working
directory, where several of these files are gitignored because they are large or
LLM-regeneratable).

> **The database instances themselves** (the four PostgreSQL dumps these gold pairs
> run against) are too large for git and are hosted on Hugging Face:
> [minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation).
> To download, restore, and run the eval end-to-end, see
> [../docs/reference/using-the-dataset.md](../docs/reference/using-the-dataset.md).

Refresh this snapshot after any rebuild with:

```bash
python eval_dataset/build_eval_dataset.py
```

`build_eval_dataset.py` is the canonical definition of the contents; this README is
the human-readable index.

---

## The four database instances

The benchmark is served from four PostgreSQL 18 instances (Docker, `decoy` compose
profile for the last two). **Never run more than 2 hot at once locally** (OOM, see
[AGENTS.md](../AGENTS.md)); on a provisioned server that limit is lifted.

| instance | port | identifiers | decoys/traps | obfuscation dims |
| --- | --- | --- | --- | --- |
| `pg_base` | 5432 | original English | none | - (control) |
| `pg_rename` | 5433 | renamed (target language) | none | rename |
| `pg_decoy` | 5434 | original English | corrupted traps | decoy |
| `pg_rename_decoy` | 5435 | renamed | corrupted traps | rename + decoy |

Real data is byte-identical across all four (traps are strictly additive). Only
identifiers and the presence of decoy columns/tables differ.

---

## Files

### Gold question / SQL dataset (the benchmark itself)
- **`train_final.jsonl`** (8,134): validated train split.
- **`test_final.jsonl`** (2,030): validated test split; **the eval runs on this**.

  Fields (both): `question_id`, `db_id`, `question`, `evidence`, `evidence_rename`,
  `difficulty`, `sql_sqlite` (original BIRD gold), `sql_base` (gold transpiled for
  `pg_base`), `sql_rename` (gold rewritten for `pg_rename`). Every kept pair is
  R1==R2 verified: `sql_base` on `pg_base` and `sql_rename` on `pg_rename` return
  equal results.

### Dimension 1: identifier rename
- **`schema_rename_map.json`**: per-db `{english_identifier: renamed_identifier}` for
  tables and columns. Ground truth for the rename dimension; also the key to resolving
  any English name in the manifests to its renamed form.
- **`db_language_map.json`**: per-db target language of the rename
  (english / french / german / spanish / pinyin / …).

### Dimension 2: decoys / traps (additive; on the `*_decoy` instances)
- **`trap_manifest.json`**: evil-twin decoy **columns** (a corrupted copy of a real
  column under a synonym name). Per entry: `db, table, source_column, source_type,
  operator, is_key, in_correlated_group, salt, names:{base, rename}`. The decoy column
  added to `<db>.<table>` is `names.<variant>`.
- **`trap_table_manifest.json`**: corrupted decoy clone **tables**. Per entry:
  `db, source_table, columns:[{source_column, source_type, operator, is_key}],
  names:{base:{table, columns}, rename:{table, columns}}`. `operator: null` = copied
  exact (uncorrupted). R1==R2-safe by construction (gold never references a decoy table).
- **`decoy_map.json`**: step-08 **structural** (empty) decoy tables/columns.
  *Superseded* for interactive execute-and-observe agents by the corrupted traps above
  (empty decoys unmask themselves); retained for provenance.

  `names.base` is the English decoy identifier, `names.rename` the target-language one.
  The corruption `salt` is variant-independent, so `pg_decoy` and `pg_rename_decoy`
  corrupt the same rows the same way. Design + operators:
  [docs/reference/corrupted-decoys-design.md](../docs/reference/corrupted-decoys-design.md).

### Dimension 3: question paraphrase
- **`question_paraphrases.jsonl`**: one SQL-preserving paraphrase per question
  (`question_id -> question_paraphrase`). The `eval_dataset/` snapshot has 2,030 test rows;
  train paraphrases (8,134 more) live in `artifacts/question_paraphrases.jsonl` after
  step 09 with `--include-train`.

### Eval support
- **`gold_star_expanded.jsonl`**: `SELECT *`-expanded gold for the ~5 star queries
  (`sql_base_expanded` / `sql_rename_expanded`) so decoy columns can never leak into a
  gold answer.
- **`order_sensitive_qids.json`**: qids to **exclude from strict EX scoring**:
  `order_sensitive` (153: gold has a `LIMIT` without a total order or a float aggregate,
  so the heap-reorder from trap UPDATEs yields a different-but-valid result on the decoy
  instances) + `exec_failed` (21: pre-existing degenerate BIRD gold, >200k rows / 60s
  timeout). Real data is verified intact; these are comparison artifacts, not corruption.
- **`gold_result_hashes_rename_decoy.jsonl`**: lenient and strict SHA-256 hashes of gold
  SQL results on `pg_rename_decoy` (all train and test rows). Hash the model result and
  compare; you do not need to re-run gold. Fields and algorithm:
  [docs/reference/gold-result-hashes.md](../docs/reference/gold-result-hashes.md).
  Rebuild with `uv run python pipeline/precompute_gold_result_hashes.py`.

---

## Eval arm → (instance, gold field, question source)

The 5-arm no-hint ablation (see [pipeline/eval_ablation.py](../pipeline/eval_ablation.py)):

| arm | instance | port | gold SQL field | question text |
| --- | --- | --- | --- | --- |
| `base` | `pg_base` | 5432 | `sql_base` | `question` |
| `rename` | `pg_rename` | 5433 | `sql_rename` | `question` |
| `decoy` | `pg_decoy` | 5434 | `sql_base` (star-expanded) | `question` |
| `paraphrase` | `pg_base` | 5432 | `sql_base` | `question_paraphrase` |
| `all` | `pg_rename_decoy` | 5435 | `sql_rename` (star-expanded) | `question_paraphrase` |

---

## Running the offline eval

```bash
# PostgreSQL machine: one arm at a time keeps <=2 instances hot
uv run python pipeline/eval_ablation.py --arms base --prepare-only

# API machine
uv run python pipeline/run_offline_generations.py \
  --bundle-dir eval/offline/ablation-base --model "Claude-Opus-4.8" --effort high

# PostgreSQL machine, after copying generations.jsonl back
uv run python pipeline/eval_ablation.py --arms base \
  --generations eval/offline/ablation-base/generations.jsonl \
  --model "Claude-Opus-4.8" --effort high
```

The eval scripts (`eval_ablation.py`, `eval_contamination.py`, `probe_schema_recall.py`)
resolve each input via `_eval_helpers.dataset_path(name)`: **prefer `artifacts/<name>`
if present, else fall back to `eval_dataset/<name>`.** So a full local checkout uses the
working copies in `artifacts/`, while a fresh clone that only has this checked-in folder
(no `artifacts/`) runs against the snapshot automatically, with no extra flags or edits
needed. If you keep `artifacts/` populated, re-run `build_eval_dataset.py` after a rebuild
to refresh this snapshot. Downstream consumers should rely on **this** folder.
