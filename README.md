**English** · [中文](README-zh.md)

# BIRD Obfuscation

> A cleaned, contamination-resistant, adversarial rebuild of the
> [BIRD](https://bird-bench.github.io/) Text-to-SQL benchmark, curated as the substrate for a
> **semantic-layer** agent evaluation, not for one-shot Text-to-SQL.

![status](https://img.shields.io/badge/status-active-brightgreen)
![python](https://img.shields.io/badge/python-3.13-blue)
![postgres](https://img.shields.io/badge/PostgreSQL-18-336791)
[![dataset](https://img.shields.io/badge/🤗%20dataset-BIRD__Obfuscation-orange)](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
[![agent eval](https://img.shields.io/badge/agent%20eval-governed--bi-8A2BE2)](https://github.com/Minhao-Zhang/governed-bi)
[![License: CC BY-SA 4.0](https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-sa/4.0/)

**57 databases · 6,743 question/SQL pairs · 5,392 train / 1,351 test · 4 obfuscation variants**

## Three problems this fixes

| Problem | What was done |
| --- | --- |
| **Contamination.** BIRD ships questions, gold SQL, and schema names in the open, so a frontier model can score from *having seen the benchmark*. | Schema identifiers renamed into one of five languages; questions paraphrased SQL-preservingly. Each surface is an independently-toggleable dimension. |
| **Wrong gold SQL.** BIRD's own annotations are substantially and *systematically* mis-annotated. Published audits report 49-61% error rates, and upstream has since corrected or withdrawn much of it. | 2,739 questions removed by joining against the official [`bird_sql_dev_20251106`](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106) and [`bird23-train-filtered`](https://huggingface.co/datasets/birdsql/bird23-train-filtered) releases, then 11 under-populated databases dropped and the rest re-split. Provenance for every question is shipped in `gold_quality_flags.jsonl`. |
| **Nothing adversarial to probe.** An agent that explores a database by *running queries* faces no traps in stock BIRD. | 1,486 corrupted "evil-twin" columns and 162 cloned tables holding subtly wrong copies of real data under plausible synonym names. Strictly additive, so the ground-truth task is provably intact. |

Question filtering is part of what the dataset offers, not an apology for it. Gold quality matters
more here than in a per-question benchmark: a wrong (question, SQL) pair in the corpus does not
waste one row, it teaches a wrong mapping that then propagates. Full evidence, method, and citations:
[gold-quality-audit.md](docs/reference/gold-quality-audit.md).

## What it is for

The dataset targets a **semantic-layer** task rather than one-shot Text-to-SQL. An agent reads the
`train` split, which is (question, SQL) pairs over a schema, induces a reusable mapping from it, then
answers unseen `test` questions over the same schema without being handed that mapping. The harness,
metrics (`decoy_touch_rate`, `routing_recall`) and results live in
[**governed-bi**](https://github.com/Minhao-Zhang/governed-bi).

Two things follow for the data itself. Per-schema question count is close to the independent
variable: how much layer an agent can induce depends on how many *correct* prior questions it had.
Corpus sizes span 61 to 383 across the 57 retained schemas, so report them alongside any per-schema
result. And the split boundary must not leak: a corpus question that near-duplicates a test question
lets the agent retrieve instead of induce. BIRD contains such duplicates, so they were removed
before splitting, which takes measured leakage to 0.22% of the test set
([gold-quality-audit.md §6](docs/reference/gold-quality-audit.md)).

Separately, this repo carries a small five-arm obfuscation eval (`base` / `rename` / `decoy` /
`paraphrase` / `all`) run one-shot with full context given. Its only claim is the modest one: the
renaming does remove some memorised information from a frontier model. Design and numbers:
[evaluation.md](docs/methodology/evaluation.md) §8 and §9. Those were measured before the
gold-quality purge and are therefore superseded.

## Get the dataset

Two homes: the **databases** are on Hugging Face (≈12 GB, too large for git), the **gold SQL and
manifests** are git-tracked in [`eval_dataset/`](eval_dataset/).

```bash
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
docker compose --profile decoy up -d
docker compose cp bird_obf_dumps/pg_base.dump pg_base:/tmp/pg_base.dump
docker compose exec pg_base pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
```

Repeat for `pg_rename`, `pg_decoy` and `pg_rename_decoy`, keeping **at most two hot at once on a
laptop** (OOM). Full restore and eval
instructions: [using-the-dataset.md](docs/reference/using-the-dataset.md). Eval scripts read
`artifacts/` and fall back to `eval_dataset/`, so a fresh clone runs with no regeneration; DSNs are
env-configurable (`PG_*_DSN`) for remote Postgres.

## Documentation

| Doc | Covers |
| --- | --- |
| [gold-quality-audit.md](docs/reference/gold-quality-audit.md) | BIRD's annotation errors, the purge, the resplit, what is still open |
| [dataset.md](docs/methodology/dataset.md) | Schema lake, inclusion criteria, train/test split |
| [obfuscation.md](docs/methodology/obfuscation.md) | Obfuscation design and physical realisation |
| [evaluation.md](docs/methodology/evaluation.md) | Integrity checks, contamination delta, ablation |
| [corrupted-decoys-design.md](docs/reference/corrupted-decoys-design.md) | Trap design, risk register, as-built parameters |
| [limitations.md](docs/reference/limitations.md) | Scope caveats; read before citing any number |
| [using-the-dataset.md](docs/reference/using-the-dataset.md) | Download, restore, run |
| [pipeline-invariants.md](docs/reference/pipeline-invariants.md) | Why each pipeline rule exists, with the evidence |
| [development.md](docs/development.md) | Run and extend the pipeline; setup, conventions, invariants |
| [hf-dataset-card.md](docs/hf-dataset-card.md) | Source of truth for the Hugging Face dataset card |

## License

[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Share and adapt with credit,
under the same license. This project is a derivative of the
[BIRD benchmark](https://bird-bench.github.io/); please credit BIRD as the upstream source.
