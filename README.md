# BIRD Data Obfuscation

> A contamination-resistant rebuild of the [BIRD](https://bird-bench.github.io/) Text-to-SQL benchmark — plus the eval that measures how much benchmark scores depend on memorised schema identifiers.

![status](https://img.shields.io/badge/status-work_in_progress-yellow)
![python](https://img.shields.io/badge/python-uv-blue)
![postgres](https://img.shields.io/badge/PostgreSQL-18-336791)
[![dataset](https://img.shields.io/badge/🤗%20dataset-BIRD__Obfuscation-orange)](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
[![License: CC BY-SA 4.0](https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-sa/4.0/)

Public benchmarks like BIRD ship their questions, gold SQL, and schema names in the open,
so a frontier model may score partly from *having seen the benchmark* rather than from
reasoning over the schema in front of it. This project rebuilds BIRD into a version that
keeps the SQL task intact but strips away the memorisable surface — renamed identifiers,
adversarial decoy data, paraphrased questions — and runs a controlled evaluation to measure
how much accuracy that surface was actually buying.

> [!NOTE]
> **🚧 Work in progress.** The dataset is **built, validated, and published**; the
> **effectiveness numbers are being re-run on a stronger model** and are intentionally
> not reported yet. See [Project status](#project-status) for exactly what is done vs. pending.

---

## At a glance

| | |
| --- | --- |
| **Problem** | Frontier models may inflate Text-to-SQL scores via memorised BIRD identifiers, questions, and SQL — not schema reasoning. |
| **Deliverable** | 69-database multilingual PostgreSQL Text-to-SQL corpus (10,164 execution-validated question/SQL pairs) in four obfuscation variants, published on Hugging Face. |
| **Eval** | A paired contamination-delta study + 5-arm ablation isolating each obfuscation dimension, with McNemar tests and bootstrap CIs. |
| **Status** | Data pipeline complete; evaluation implemented and **re-running on a stronger model** (numbers pending). |

---

## The problem: benchmark contamination

A model evaluated on the public BIRD corpus can benefit from having encountered its schema
identifiers (`movie_release_year`, `user_subscriber`), question phrasings, or SQL fragments
during training — so a headline score conflates two very different things: **schema reasoning**
and **benchmark recall**. This project attacks the recall channel while preserving a
semantically equivalent SQL task, then measures the gap.

The design targets three independent contamination surfaces:

- **Schema identifiers** — table/column names renamed into one of five languages (English,
  French, German, Spanish, Mandarin Pinyin).
- **Schema probing** — *corrupted decoy traps*: additive "evil-twin" columns and cloned tables
  holding subtly corrupted copies of real data under plausible synonym names, designed to
  mislead an agent that explores the schema by *executing* queries.
- **Question phrasing** — SQL-preserving paraphrases of each natural-language question.

Each surface is a separate, independently-toggleable dimension so the eval can attribute the
accuracy drop to a *mechanism*, not a single blurred "obfuscation" knob.

## What this produces

- **A validated multilingual Postgres Text-to-SQL corpus.** 69 databases, **10,164 of 10,541**
  candidate questions pass end-to-end execution validation (8,134 train / 2,030 test, every
  database represented in both). See [docs/methodology/dataset.md §7](docs/methodology/dataset.md).
- **Obfuscated gold SQL and evidence hints**, rewritten to the renamed identifiers.
- **Four PostgreSQL instances** covering the obfuscation combinations — `pg_base` (original),
  `pg_rename` (renamed), `pg_decoy` (traps), `pg_rename_decoy` (renamed + traps) — published as
  compressed dumps on [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation).
- **Corrupted decoy traps** — 1,486 evil-twin columns + 162 cloned tables of corrupted data
  ([design + risk register](docs/reference/corrupted-decoys-design.md)).
- **A two-oracle integrity guarantee** — obfuscated SQL stays execution-equivalent to validated
  original SQL (R0==R1 against SQLite ground truth, R1==R2 across instances), preserved because
  every trap is strictly *additive* (real rows/columns/tables are never modified).
- **The evaluation harness** — a four-condition contamination-delta study and a five-arm
  ablation (`base` / `rename` / `decoy` / `paraphrase` / `all`).

## Evaluation design

The evaluation asks one question: **how much of a model's BIRD accuracy survives when the
memorisable surface is removed?** It is built to answer that credibly rather than just produce
a number:

- **Paired conditions.** Every arm runs the same test set through the same model in the same run;
  deltas are per-question paired against `base` and read with **McNemar tests + bootstrap CIs**,
  not point estimates.
- **An empirical null, not zero.** 14 databases keep an identity (English→English) rename, so
  their rename delta is guaranteed ≈0 by construction — they serve as the **noise-floor control**.
  The rename effect is therefore reported *per-language*, never as a single pooled number diluted
  by the control ([limitations §1](docs/reference/limitations.md)).
- **Strict *and* lenient scoring.** EX is reported under a BIRD-style type-lenient comparator
  *and* a strict one (no cross-type collapse, case-sensitive); the leniency cancels in the deltas,
  and the strict column is quoted for any absolute-accuracy claim ([limitations §2](docs/reference/limitations.md)).
- **Ablation by mechanism.** `rename−base` probes identifier recall; `decoy−base` probes
  robustness to schema-probing traps; `paraphrase−base` probes question-form recall; `all−base`
  the combined effect. Design: [evaluation.md §9](docs/methodology/evaluation.md).

### Results — pending

> Results are **not reported yet.** Earlier numbers were discarded to re-run the full evaluation
> on a stronger model for results that hold up; nothing is quoted in the interim.

| Metric | Status |
| --- | --- |
| Pipeline integrity (R0==R1, R1==R2) over 10,164 questions | ✅ done |
| Contamination delta (four conditions) | ⏳ re-running on a stronger model |
| Five-arm ablation (per-mechanism deltas + CIs) | ⏳ pending the same run |

Setup for the run is documented and reproducible — see [evaluation.md §8–§9](docs/methodology/evaluation.md).

## Project status

**The asset is finished and published; the measurement is the part in flight.**

| Component | State |
| --- | --- |
| Core pipeline (steps 0–7): split → rename map → load → transpile → rename → validate | ✅ complete & validated |
| Extended obfuscation (decoy traps, paraphrases) | ✅ built & applied |
| Four PostgreSQL instances + git-tracked eval artifacts | ✅ published (HF + [`eval_dataset/`](eval_dataset/)) |
| Contamination-delta eval harness | ✅ implemented — ⏳ **re-running (numbers pending)** |
| Five-arm ablation harness | ✅ implemented — ⏳ **run pending** |
| Interactive execute-and-observe agent that exercises the traps | ⛔ out of scope (separate downstream repo) |

Full history, decisions, and what's next: [PROGRESS.md](PROGRESS.md).

### Scope boundaries

- This repo **prepares and validates** the dataset; it does **not** evaluate a downstream agent
  or schema routing — the correct database is supplied upfront in all conditions.
- It does **not modify real data** — clean instances are untouched and decoy instances only *add*
  corrupted columns/tables, so R1==R2 holds.
- It does **not** claim to remove every contamination path (memorised literals or high-level SQL
  templates remain); it targets the identifier, schema-probing, and question-phrasing surfaces.

## What this project demonstrates

For anyone reviewing this as an engineering sample, the transferable pieces are:

- **Eval design under contamination** — controlled conditions, an empirical null, per-mechanism
  ablation, and paired significance testing instead of raw leaderboard numbers.
- **Adversarial data design** — decoy traps engineered specifically against execute-and-observe
  agents while provably preserving the ground-truth task ([design doc](docs/reference/corrupted-decoys-design.md)).
- **Correct data infrastructure** — SQLite→PostgreSQL migration with an execution-equivalence
  guarantee and a documented set of hard-won [pipeline invariants](docs/reference/pipeline-invariants.md)
  (pgloader DDL bugs, an AST-mutation infinite loop, unbounded result sets, connection-latency traps).
- **Honest scoping** — a standalone [limitations doc](docs/reference/limitations.md) written before
  publishing any effectiveness claim.

## How it works

A 10-step pipeline turns raw BIRD SQLite into the four validated PostgreSQL instances. Each step
reads the previous step's output; operational detail and invariants live in [AGENTS.md](AGENTS.md).

### Pipeline steps

| # | Step | Output |
| --- | --- | --- |
| 1 | Split (per-DB 80/20, seeded) | `artifacts/{train,test}.jsonl` |
| 2 | Assign a schema language per DB | `artifacts/db_language_map.json` |
| 3 | Generate the rename map (LLM translation) | `artifacts/schema_rename_map.json` |
| 4 | Load `pg_base` via pgloader | `pg_base` (5432) |
| 5 | Transpile gold SQL to Postgres + validate R0==R1 | `workdir/*_transpiled.jsonl` |
| 6 | Clone `pg_base` volume, rename identifiers in place | `pg_rename` (5433) |
| 7 | Rename SQL + validate R1==R2 → **deliverable** | `artifacts/{train,test}_final.jsonl` |
| 8–9 | Structural decoys (superseded) + question paraphrases | `artifacts/question_paraphrases.jsonl` |
| 10 | Inject corrupted decoy traps | `pg_decoy` (5434), `pg_rename_decoy` (5435) |

Run with `uv run python pipeline/<script>.py` from the repo root, after `docker compose up -d`.
Two evaluation entrypoints — `pipeline/eval_contamination.py` and `pipeline/eval_ablation.py` — sit
downstream of the numbered steps.

### Repository layout

| Path | What's in it |
| --- | --- |
| [`pipeline/`](pipeline/) | The numbered pipeline (`00`–`10`), the eval harnesses (`eval_contamination.py`, `eval_ablation.py`, `probe_schema_recall.py`), and shared helpers (`_db.py`, `_traps.py`, `_corruption.py`, …) |
| [`eval_dataset/`](eval_dataset/) | Git-tracked deliverable: validated gold question/SQL pairs, rename map, trap manifests, paraphrases |
| [`artifacts/`](artifacts/) | Pipeline working outputs (git-tracked subset: rename map, retained DBs, trap plans/manifests) |
| [`docs/methodology/`](docs/methodology/) | Why each design decision was made (dataset, obfuscation, evaluation) |
| [`docs/reference/`](docs/reference/) | Operational detail: pipeline invariants, decoy-trap design, limitations, dataset usage |
| [`data/`](data/README.md) | Raw BIRD source (not tracked — download instructions in `data/README.md`) |

## Get the dataset

The deliverable ships in two homes:

- **Databases** — four PostgreSQL dumps (base / rename / decoy / rename+decoy) on Hugging Face:
  [minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) (too large for git).
- **Gold SQL + rename map + trap manifests** — git-tracked in [`eval_dataset/`](eval_dataset/).

```bash
# 1. get the database dumps (≈12 GB, four PostgreSQL instances)
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps

# 2. bring up the empty instances and restore each dump into its match
docker compose --profile decoy up -d
docker compose cp   bird_obf_dumps/pg_base.dump pg_base:/tmp/pg_base.dump
docker compose exec pg_base pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
#   ...repeat for pg_rename / pg_decoy / pg_rename_decoy (two at a time on a laptop — see OOM note)

# 3. run one ablation arm (gold + mappings resolve from the checked-in eval_dataset/)
uv run python pipeline/eval_ablation.py --arms base --model <model>
```

Full download, restore, and local-eval instructions: [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset.md).
Eval scripts read `artifacts/` and fall back to `eval_dataset/`, so a fresh clone runs with no
regeneration; Postgres DSNs are env-configurable (`PG_*_DSN`) to target remote Postgres / RDS.

## Documentation

| Doc | What it covers |
| --- | --- |
| [docs/methodology/dataset.md](docs/methodology/dataset.md) | Schema-lake construction, inclusion criteria, train/test split |
| [docs/methodology/obfuscation.md](docs/methodology/obfuscation.md) | Obfuscation design, decisions, physical realisation |
| [docs/methodology/obfuscation-extensions.md](docs/methodology/obfuscation-extensions.md) | Decoy traps + paraphrase dimensions and the ablation |
| [docs/methodology/evaluation.md](docs/methodology/evaluation.md) | Integrity check, contamination delta, ablation (§9) |
| [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design.md) | Decoy-trap design, risk register, as-built parameters |
| [docs/reference/limitations.md](docs/reference/limitations.md) | Known limitations & scope caveats — read before citing any number |
| [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset.md) | Download, restore, and run the eval |
| [docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants.md) | Rules to preserve when editing the pipeline, with rationale |
| [docs/eda-report.md](docs/eda-report.md) | Exploratory analysis of the BIRD corpus |
| [AGENTS.md](AGENTS.md) | How to run and extend the pipeline (operational) |
| [PROGRESS.md](PROGRESS.md) | History, status snapshot, and what's next |

## Corpus facts

- **Combined corpus**: 80 SQLite databases, 10,962 questions (BIRD train + dev pooled).
- **After exclusions**: 69 databases, 10,541 questions (11 databases with < 60 questions excluded).
- **Split**: random 80/20 holdout within each database, seeded; no difficulty stratification
  (BIRD train questions carry no difficulty labels).

The `data/` directory holds the raw BIRD dataset (not in version control) —
see [data/README.md](data/README.md) for download instructions.

## Python

Always use `uv`:

```bash
uv run python pipeline/<script>.py
uv run pytest
uv pip install <package>
```

The `.venv` directory is managed by `uv`; do not activate it manually or use bare `python`/`pip`.

## License

This work is licensed under a
[Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/).

You are free to share and adapt the material for any purpose, provided you give appropriate
credit and distribute your contributions under the same license.

This project is a derivative of the [BIRD benchmark](https://bird-bench.github.io/); please
credit BIRD as the upstream source when using this dataset.
