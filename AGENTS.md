# BIRD Obfuscation

`AGENTS.md` ([agents.md convention](https://agents.md/)) — operational guidance for coding
agents working in this repo. The human-facing overview is in [README.md](README.md); the
*why* behind each design decision is in [docs/methodology/](docs/methodology/); project
history, status, and decisions are in [docs/PROGRESS.md](docs/PROGRESS.md). Keep this file
instructional and current — how to run and extend the pipeline, and invariants to preserve —
not a changelog or run history.

## Project overview

This repo transforms the [BIRD benchmark](https://bird-bench.github.io/) into an **obfuscated
Text-to-SQL dataset** that measures how much benchmark accuracy depends on memorised schema
identifiers, and stress-tests execute-and-observe SQL agents over a multi-database schema lake.

Standard BIRD is public — questions, gold SQL, and schema identifiers — so a frontier model may
score partly from recall rather than from reasoning over the schema in front of it. The pipeline
renames tables and columns (and adds decoy traps and question paraphrases) while preserving a
semantically equivalent SQL task, then measures the accuracy that the memorisable surface was
buying. This repository **prepares and validates** that dataset; it does not itself evaluate
schema routing (the correct database is supplied upfront in the evaluations here). The downstream
agent evaluation that consumes this dataset — an execute-and-observe SQL agent scored on
`decoy_touch_rate` and execution accuracy — lives in the sibling repo
[governed-bi](https://github.com/Minhao-Zhang/governed-bi). Detailed
empirical rationale for the invariants below lives in
[docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants.md).

## Data

The `data/` directory holds the BIRD dataset (not in version control). See
[data/README.md](data/README.md) for download instructions, directory structure, and file formats.

- **Dev split**: 11 SQLite databases, 1,534 questions
- **Train split**: 69 SQLite databases, 9,428 questions
- Each question has a natural-language question, optional evidence hint, gold SQL, and a
  difficulty label (`simple` / `moderate` / `challenging`).

## Setup commands

- **Always use `uv`** for anything involving Python — never bare `python`/`pip`, and do not
  activate `.venv` manually (it is uv-managed):

  ```bash
  uv run python pipeline/<script>.py
  uv pip install <package>
  ```

- Bring up both PostgreSQL instances first: `docker compose up -d`. `pg_base` is
  `127.0.0.1:5432`, `pg_rename` is `127.0.0.1:5433` (both DSN: `dbname=bird user=bird
  password=bird`). DSNs are overridable per instance via `PG_*_DSN` env vars (`_db.py`, see
  `.env.example`) to target remote Postgres / AWS RDS; keep the local default on `127.0.0.1`
  (see invariants).
- Step 4 needs only Docker running: pgloader itself runs as a container
  (`dimitri/pgloader:v3.6.7`), so no host install is required.
- The two decoy instances are gated behind the `decoy` compose profile (default
  `docker compose up -d` is unchanged): `docker compose --profile decoy up -d` brings up
  `pg_decoy` (5434) and `pg_rename_decoy` (5435).

## Running the pipeline

Run scripts in order from the repo root with `uv run python pipeline/<script>.py`. Each step
reads the previous step's output; do not skip or reorder.

| # | Script | Depends on running first |
| --- | --- | --- |
| 0 | `00_audit_sqlite_identifiers.py` | step 1 (`retained_dbs.json`) |
| 1 | `01_split.py` | - |
| 2 | `02_assign_languages.py` | step 1 |
| 3 | `03_generate_rename_map.py` | steps 1-2 |
| 3b | `03b_check_translation_quality.py` | step 3 (advisory only, see below) |
| 4 | `04_load_pg_base.py` | step 1, `pg_base` running |
| 5 | `05_transpile_sql.py` | steps 1, 4 |
| 5b | `05b_apply_sql_fixes.py` | step 5 pass 1 + agent-written `transpilation_fixes.jsonl` |
| 5c | `05c_export_fix_batch.py` | step 5 pass 1 (advisory, export batches for agents) |
| 6 | `06_build_pg_rename.py` | steps 1, 3, `pg_base_data` volume cloned into `pg_rename_data` (see below), `pg_rename` running |
| 7 | `07_rename_sql_and_validate.py` | steps 3, 5, 6, both PG instances running |
| 8 | `08_inject_decoys.py` | steps 3, 7, both `*_decoy` instances cloned + running (extended obfuscation, see below) |
| 9 | `09_paraphrase_questions.py` | step 7, `pg_rename` running (extended obfuscation) |

`artifacts/schema_rename_map.json` is **git-tracked** (regeneratable via step 3 with Bedrock, but
checked in so steps 6-7 and downstream don't depend on re-running LLM translation).
`artifacts/db_language_map.json` remains gitignored (deterministic from step 2).

Step 0 is diagnostic (not the critical path). Run it before step 4 when adding a source database
or changing identifier-handling logic, to catch risky identifiers before they reach the loader.
See [docs/reference/audit-findings.md](docs/reference/audit-findings.md).

Step 3b is advisory, not a gate. It cross-checks `schema_rename_map.json` against BIRD's
`database_description/*.csv` and writes questionable translations to
`artifacts/translation_quality_flags.jsonl` for manual review; it never modifies the rename map.
Run it after step 3, before steps 6-7 consume the map. Detail:
[docs/methodology/obfuscation.md §4](docs/methodology/obfuscation.md).

Step 5 pass 1 (`05_transpile_sql.py`) is sqlglot-only (no LLM): it transpiles gold SQL, validates
R0==R1, writes matches to `workdir/*_transpiled.jsonl`, and queues mismatches in
`workdir/transpilation_needs_fix.jsonl`. Pass 2 is manual agent repair appending
`{"question_id", "sql_base"}` to `workdir/transpilation_fixes.jsonl`, merged by
`05b_apply_sql_fixes.py`; `05c_export_fix_batch.py` exports batches; `--status` shows progress.

**Artifact semantics, R0==R1 definition, VALUES materialization, and failure buckets:** see
[docs/reference/step5-transpilation.md](docs/reference/step5-transpilation.md).

**Step 6 requires `pg_rename`'s Docker volume to be a clone of `pg_base`'s before running the
script.** `pg_base` and `pg_rename` are separate containers with separate named volumes
(`pg_base_data`, `pg_rename_data`); `06_build_pg_rename.py` no longer reads SQLite at all. It only
renames tables/columns in an already-populated `pg_rename`. Clone the volume first:

```bash
docker compose stop pg_base
docker run --rm -v pg_base_data:/from:ro -v pg_rename_data:/to alpine sh -c "rm -rf /to/* && cp -a /from/. /to/"
docker compose start pg_base
docker compose up -d pg_rename
uv run python pipeline/06_build_pg_rename.py
```

The `:ro` source mount is what makes the clone safe. Volume names are the Compose-generated ones.
Run `docker volume ls` to confirm the exact prefixed name (e.g. `bird-data-obfuscation_pg_base_data`).

Step 7 (`07_rename_sql_and_validate.py`) applies the rename map and checks R1==R2 (`sql_base` on
`pg_base` vs `sql_rename` on `pg_rename`, equal results), writing matches to
`artifacts/{train,test}_final.jsonl` (the deliverable) and failures to
`workdir/rename_failures.jsonl`. Resumable via `question_id`; progress with
`wc -l artifacts/*_final.jsonl workdir/rename_failures.jsonl`. Validated counts:
[docs/methodology/dataset.md §7](docs/methodology/dataset.md).

`pipeline/eval_contamination.py` is downstream four-condition obfuscation-effectiveness
evaluation, not a numbered pipeline step (scope stops at step 7). Its default is the split-machine
offline workflow: prepare public requests on the PostgreSQL machine, run
`run_offline_generations.py` on the API machine, then return generations for DB-side grading.
`--split {test,train}` selects the dataset; `--local` explicitly enables the legacy same-machine
path. Detail: [docs/methodology/evaluation.md §4](docs/methodology/evaluation.md) (conditions) and
§8.5 (offline workflow), plus
[docs/reference/using-the-dataset.md §3](docs/reference/using-the-dataset.md).

## Extended obfuscation (decoy + paraphrase)

Optional decoy/paraphrase dimension steps and their ablation: design in
[docs/methodology/obfuscation.md §7-§11](docs/methodology/obfuscation.md), full build spec in
[docs/reference/extension-implementation-plan.md](docs/reference/extension-implementation-plan.md).

Two extra PostgreSQL instances hold decoy-augmented clones, gated behind the `decoy` compose
profile (default `docker compose up -d` is unchanged): `pg_decoy` (5434), `pg_rename_decoy`
(5435). Build them by cloning the clean volumes, then injecting: clone commands in
[extension-implementation-plan.md §3c](docs/reference/extension-implementation-plan.md).

**⚠️ Do NOT run all four instances under heavy load at once on a local Docker Desktop / WSL
setup. It can OOM the WSL VM, and with `fsync=off` an OOM crash can corrupt the volumes.** Bring
up only the instances the current step/arm needs (a clone touches 2; each ablation arm queries
exactly 1: `base`/`paraphrase`→`pg_base`, `rename`→`pg_rename`, `decoy`→`pg_decoy`,
`all`→`pg_rename_decoy`, so run `eval_ablation.py --arms <one>` sequentially, `docker compose
stop`ping the others). Keep eval `--concurrency` low (≤3), and never overlap step-08's validate
pass with the ablation eval. Capping the WSL VM's memory in `.wslconfig` is a useful backstop. On
a well-provisioned server this limit does not apply.

- `08_inject_decoys.py`: generate `artifacts/decoy_map.json` (cheap LLM, seeded), inject decoy
  tables + confusable columns into both `*_decoy` instances, expand the handful of `SELECT *` gold
  queries (`artifacts/gold_star_expanded.jsonl`), and re-validate R1==R2 (acceptance gate →
  `workdir/decoy_failures.jsonl`, expect 0). `--phase {generate,inject,validate,all}`,
  `--regenerate`, `--validate-only`.
- `09_paraphrase_questions.py`: one SQL-conditioned paraphrase per question →
  `artifacts/question_paraphrases.jsonl` (resumable; `--model` chosen at run time, `--concurrency`).
- `10_inject_traps.py`: corrupted-decoy **traps** for the interactive execute-and-observe agent
  paradigm (empty/NULL decoys unmask themselves for free). Strictly **additive** corrupted copies
  on the `*_decoy` instances, so real columns/tables stay byte-identical and R1==R2 holds. Phase 1
  = evil-twin columns (`--phase rowcounts,plan,name,inject`, ≤500k-row tables); Phase 2 =
  corrupted clone tables (`--phase plan-tables,name-tables,inject-tables`, ≤50k-row sources,
  R1==R2-safe since gold never references a decoy table). Deterministic hash-seeded corruption with
  a **variant-independent** salt (permute for join-keys → RI-preserving; sparse
  perturb/cat-remap/date-offset/null otherwise); LLM-named per variant (`--model`, `--effort`).
  Emits `artifacts/trap_manifest.json` + `artifacts/trap_table_manifest.json`. Inject **one variant
  at a time** with `--variants base|rename` (OOM); `--regenerate` drops+recreates. Design + risk
  register: [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design.md).
- `pipeline/eval_ablation.py`: 5-arm no-hint ablation (base/rename/decoy/paraphrase/all),
  defaulting to the same offline prepare/generate/grade workflow; needs step 08's outputs + step
  09's paraphrases. Train `paraphrase`/`all` additionally require step 09 with `--include-train`.
  `--summarize` prints EX/deltas/CIs. Design:
  [docs/methodology/evaluation.md §9](docs/methodology/evaluation.md).

## Testing & validation

There is no unit-test suite; correctness is enforced by **execution-equivalence gates** built into
the pipeline, plus reproducible eval summaries. Preserve these gates when editing:

- **R0==R1** (step 5): transpiled PostgreSQL gold returns the same rows as the original SQLite
  gold (normalized multiset equality). Mismatches queue for agent repair and are re-validated
  before merge. See [docs/reference/step5-transpilation.md](docs/reference/step5-transpilation.md).
- **R1==R2** (step 7): obfuscated SQL on `pg_rename` returns the same rows as validated SQL on
  `pg_base`. Steps 08/10 re-check R1==R2 as their acceptance gate — decoy traps are strictly
  additive, so real rows/columns/tables are never modified and the gate must stay at 0 failures.
- Every pgloader load is verified with `verify_casing()` + `verify_row_counts()`: `check=True` is
  not enough, because pgloader exits 0 even on data-losing failures.
- Reproduce eval figures (no regeneration needed on a fresh clone) with:

  ```bash
  uv run python pipeline/eval_contamination.py --summarize --model "<model>" --effort <effort> --bundle-dir eval/offline/contamination
  uv run python pipeline/eval_ablation.py      --summarize --model "<model>" --effort <effort> --bundle-dir eval/offline/ablation-base
  ```

  Resumability only reuses rows whose `eval_metadata` (model / effort / prompt version / commit /
  input artifact hashes) matches the current invocation. See
  [docs/methodology/evaluation.md](docs/methodology/evaluation.md).

## Code style & invariants

Python is always run via `uv` (see Setup); `.venv` is uv-managed and never activated by hand.
Beyond that, the rules below are **hard invariants** confirmed against a live Postgres and the real
worst-case DBs, not assumptions. **Read the detailed rationale in
[docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants.md) before changing the
code a rule protects.**

**Step 4 (pgloader load):**

- Runs as a container (`dimitri/pgloader:v3.6.7`), not a host install; `load_db()` bind-mounts the
  SQLite file read-only and pipes the LOAD script over `/dev/stdin` (not `-`). Loads an unrenamed
  exact copy into `db_id.table_name`.
- Reaches `pg_base` via `host.docker.internal` (pgloader's DSN grammar rejects the underscore in
  the Compose service names).
- WITH clause is `create tables, create no indexes, quote identifiers, no foreign keys` + CAST
  rules. Do **not** add back `reset sequences`, `create indexes`, or `foreign keys`. Each hits a
  confirmed pgloader bug, and `no foreign keys` is also a methodology choice (§2 "Deliberately
  absent").
- CAST rules are required for `DEFAULT CURRENT_TIMESTAMP`→`timestamptz`, `'0000-00-00'`→`date`, and
  `blob`→`text`; without them a load aborts or silently drops rows.
- `EXTRA_CASTS` (column-scoped `to bigint`, **no `using`**) guards the FIXNUM-overflow **hang**:
  pgloader stalls at 0% CPU, doesn't error. List `EXTRA_CASTS` **before** the global CAST rules
  (first match wins).
- Verify every load with `verify_casing()` + `verify_row_counts()`: `check=True` is not enough:
  pgloader exits 0 even on data-losing failures.

**sqlglot transpile / rename (steps 5, 7):**

- Quote every emitted identifier: no "looks lowercase, skip quoting" special-case.
- Schema-qualification skips CTE aliases (a `WITH x` reference parses as `exp.Table`).
- Never mutate a sqlglot node mid-`walk()`: collect nodes first, then mutate (else infinite loop +
  unbounded memory).
- Never rename an `Identifier` that is a Table's `db`/`catalog` arg (3 DBs (`superhero`,
  `sales_in_weather`, `university`) have a table named after their `db_id`).
- Fix-batch schema context comes from `pg_base`'s `information_schema`, not SQLite.
- Evidence hints get the same rename-map substitution as SQL (both `evidence` and
  `evidence_rename` are emitted; downstream consumes the obfuscated one).

**Step 6:** renames in place inside the pre-cloned `pg_rename` volume. It does **not** reload
SQLite or connect to `pg_base` (avoids a divergent second type-inference pass). `_pg_helpers.py`'s
`infer_pg_type()`/`copy_data()`/`get_sqlite_schema()` are unused; `find_sqlite_path()` is still
used by step 0.

**Execution & connections:**

- Step 7's `exec_pg()` uses `fetchmany(MAX_RESULT_ROWS)`, never `fetchall()` (one gold query
  returns 19.4M rows).
- Under `autocommit=True` use plain `SET`, not `SET LOCAL` (which silently no-ops).
- Postgres DSNs **default** to `host=127.0.0.1`, never `localhost` (20s+ IPv6 tax on this setup).
  They are overridable per instance via `PG_*_DSN` env vars (`_db.py`, see `.env.example`) to
  target remote Postgres / AWS RDS; keep the local default on `127.0.0.1`.

**Cross-cutting:**

- Pass `encoding="utf-8"` explicitly on every read of `schema_rename_map.json` / question /
  evidence text (Windows defaults to `cp1252` and crashes on non-ASCII identifiers).
- `01_split.py` stays per-DB-independent and reproducible: seed off `zlib.crc32((SEED, db_id))`,
  never salted `hash()`, never a shared `Random` across DBs.
- Keep the Docker Compose WAL tuning (`fsync=off`, `wal_level=minimal`, …): bulk-load speed; both
  DBs are rebuildable.

## Commit & docs conventions

- **Commits:** short scope-prefixed subject, sentence case, imperative — matching the existing
  `git log` (e.g. `Docs: …`, `Eval: …`, `Repo: …`, `Add …`).
- **Bilingual docs:** most files under `docs/`, the root `README.md`, and package READMEs ship a
  `<name>-zh.md` Simplified-Chinese counterpart with a nav line at the top linking the two
  (`**English** · [中文](…-zh.md)` and its mirror). Update both sides together.
- **English-only exceptions:** `AGENTS.md` and `CLAUDE.md` have **no** `-zh` counterpart.
- **Keep `AGENTS.md` instructional only** — operational guidance and invariants, not progress logs
  or history (those live in [docs/PROGRESS.md](docs/PROGRESS.md)).
- `CLAUDE.md` is a thin, git-ignored pointer to this file; edit `AGENTS.md`, not `CLAUDE.md`.
