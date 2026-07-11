**English** · [中文](README-zh.md)

# Evaluation exports: questions + gold + generated SQL

Flat, self-contained tables pairing each benchmark question with its gold SQL and the
model's generated SQL, plus the execution-accuracy verdict. They are meant to be reused
without the PostgreSQL machine or the eval harness — load the JSONL (or CSV) and go.

Everything here comes from the **`claude opus 4.8 high`** run on the **test split** (2,030
questions). See [../docs/methodology/evaluation.md](../docs/methodology/evaluation.md) §8
(contamination) and §9.4 (ablation) for the aggregate numbers these rows roll up to.

## Files

The data ships as one compressed archive; the loose `.jsonl`/`.csv` are git-ignored and
regenerable (see below).

**`Claude-Opus-4.8_high_qa_sql.zip`** — unzip to get four files:

| File | Rows | Content |
| --- | --- | --- |
| `contamination_qsql.jsonl` / `.csv` | 8,120 | 4 conditions × 2,030: schema (base/rename) × hints (hint/nohint) |
| `ablation_qsql.jsonl` / `.csv` | 10,150 | 5 arms × 2,030: base, rename, decoy, paraphrase, all |

JSONL and CSV hold the same rows and columns; use whichever your tooling prefers. UTF-8;
the CSV is quoted per RFC 4180 (SQL text with commas/newlines is safe).

## Columns

| Column | Meaning |
| --- | --- |
| `eval` | `contamination` or `ablation` |
| `condition` | contamination condition (`base_hint`, `base_nohint`, `rename_hint`, `rename_nohint`) or ablation arm (`base`, `rename`, `decoy`, `paraphrase`, `all`) |
| `question_id` | BIRD-origin question id (stable join key) |
| `db_id` | database name |
| `obfuscation_language` | `english` (identity/control), `french`, `german`, `spanish`, `pinyin` |
| `difficulty` | BIRD difficulty label when present (empty for train-origin questions) |
| `schema_instance` | which PostgreSQL instance the SQL runs on: `base`, `rename`, `decoy`, `rename_decoy` |
| `hints` | whether the evidence hint was shown (`true` only for contamination `*_hint`) |
| `question` | the exact natural-language question shown to the model (paraphrased for the `paraphrase`/`all` arms, original otherwise) |
| `evidence` | the evidence hint text shown (empty when no hint) |
| `gold_sql` | the exact gold SQL graded against (`SELECT *`-expanded on the decoy arms) |
| `generated_sql` | the model's SQL output |
| `correct` | execution accuracy, lenient (BIRD-style, type-collapsing) |
| `correct_strict` | execution accuracy, strict (no cross-type collapse) |
| `error` | grading failure reason (`result_mismatch`, `generated_exec_failed`, …) or empty when correct |
| `model` / `effort` | generating model and reasoning effort (`Claude-Opus-4.8` / `high`) |

## How they were produced

```bash
uv run python pipeline/export_qa_sql.py --model "Claude-Opus-4.8" --effort high
```

This writes the loose `.jsonl`/`.csv` and bundles them into the committed `.zip`. The
script joins the graded results (`eval/*_results.jsonl`) with the per-condition gold
SQL (offline grading manifests), the questions and difficulty (`test_final.jsonl`), the
paraphrased text (`question_paraphrases.jsonl`), and the obfuscation language
(`db_language_map.json`). Re-run it after a new eval to refresh these files.

## Reuse notes

- **A question appears once per condition/arm**, so the same `question_id` recurs with
  different schema, gold, and generated SQL. Filter on `condition` for a single view.
- `generated_sql` reflects one specific one-shot run; it is not a canonical answer. For a
  correct reference query, use `gold_sql`.
- The `english` rows are the noise-floor control (identity rename), not an obfuscation
  arm — see [../docs/reference/limitations.md](../docs/reference/limitations.md) §1.
- To execute any of this SQL yourself, restore the PostgreSQL instances as described in
  [../docs/reference/using-the-dataset.md](../docs/reference/using-the-dataset.md).
