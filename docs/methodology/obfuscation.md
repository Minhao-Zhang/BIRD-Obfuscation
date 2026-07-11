**English** · [中文](obfuscation-zh.md)

# Methodology: dataset obfuscation

Sections 1-6 are the core pipeline (the **rename** dimension); §7-§11 add the two extended
dimensions (decoy traps + question paraphrase) and their storage.

## 1. Motivation

Frontier language models may have been trained on data that includes the BIRD benchmark; its questions, gold SQL, and schema names are publicly available. A model evaluated on the original corpus may benefit from memorised question patterns, SQL fragments, table names, or column names rather than relying only on the schema supplied at evaluation time.

This project prepares data for an **agentic Text-to-SQL setting** where an agent builds a semantic memory layer from known true SQL paired with schema metadata (column names and dtypes only, with no column descriptions). In that setting, one important recall threat is identifier-level: a contaminated model may recognise a BIRD column name (`movie_release_year`, `user_subscriber`) and exploit memorised SQL structure rather than grounding its answer in the provided schema. The goal of obfuscation is to reduce that column-name recognition signal as much as practical while keeping the task semantically usable.

**Constraint:** column and table names must remain semantically meaningful after renaming. Opaque aliases (`COL_1`, `T2`) would remove much of the natural-language-to-schema grounding that the downstream agent depends on. The goal is a controlled synonym or language shift, not full anonymisation.

---

## 2. What is and is not obfuscated

### Obfuscated

- **Table names**: renamed to the target language (see the **rename** dimension)
- **Column names**: renamed to the target language
- **Evidence hints**: column name references within hints are substituted using the rename map (mechanical string replacement, no paraphrase)
- **Gold SQL**: every `FROM <table>`, `JOIN <table>`, and `<table>.<column>` reference is substituted using the rename map

> **Extensions (implemented):** two further, independently-toggleable dimensions (detailed in §7-§11 below) are built (pipeline steps 08-10 + `09`) and measured by the ablation in [evaluation.md §9](evaluation.md): **corrupted decoy traps** (additive "evil-twin" columns + corrupted clone tables holding subtly wrong copies of real data; [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md)) and **question paraphrase**. They live on the two `*_decoy` instances / in the paraphrase field and are part of the published deliverable (see [../reference/using-the-dataset.md](../reference/using-the-dataset.md)); the *core rename pipeline* described below is unchanged by them.

### Not obfuscated

- **Questions**: left unchanged in the core pipeline (see §3.1); an optional paraphrase layer is specified in §9
- **Database content**: rows and values are untouched (see §3.2)
- **SQL logical structure**: same joins, aggregations, filters, and orderings
- **Difficulty labels**
- **Table relationships and cardinality** implied by the data itself

### Deliberately absent from the schema lake

- **Foreign key constraints**: neither `pg_base` nor `pg_rename` declares FK constraints, even though BIRD's SQLite source does (see §4 and §5 step 1). This is a methodology choice, not an oversight or a bug workaround: the downstream agentic Text-to-SQL task is meant to evaluate whether the agent can infer table relationships from column names, values, and the questions/SQL it sees during memory building, not read them off an explicit FK catalogue that a real analyst exploring an unfamiliar schema wouldn't have been handed either. `pg_rename` is now built as an exact volume clone of `pg_base` (see §5 step 5), so it inherits this property automatically rather than by a second loader independently choosing to omit FKs.

---

## 3. Decisions and rationale

### 3.1 Questions are not paraphrased

An earlier design included LLM-based question paraphrase (the **paraphrase** dimension) to break exact question-string recall. This was dropped for two reasons:

1. **The primary recall vector for this repository is schema identifiers, not question text.** In the target setting the agent builds memory from SQL structure and column names, not from question strings. A contaminated model may still remember question wording or SQL templates, but schema renaming directly reduces the identifier signal that the downstream agent also relies on. Question paraphrase was solving a broader threat model with higher risk of semantic drift.

2. **Sampling suggests questions are already mostly natural language.** Only 0.3% of BIRD train questions embed schema identifiers (snake_case column names) directly in the question text. The remaining 99.7% are natural-language English sentences with little direct schema leakage. Paraphrasing them would add cost and possible meaning drift while addressing only a limited part of the contamination risk.

Removing question paraphrase eliminates ~10,000 LLM calls and the risk of meaning-drift invalidating gold SQL, leaving a simpler, more auditable pipeline.

**Revisited (2026-07-03), now implemented:** paraphrase was reintroduced as an *optional* dimension and is now **built** (`pipeline/09_paraphrase_questions.py`) and shipped in the deliverable (`eval_dataset/question_paraphrases.jsonl`, one per test question); see §9. It stays *separate from the core rename gold* (the `question_paraphrase` field parallels the original `question`, which is retained). The motivation changed: SPENCE (arXiv 2604.17771) and SQL2NL (arXiv 2509.04657) show the **question axis is the more sensitive contamination signal** than the identifier axis this pipeline primarily targets. Conditioning the paraphrase on the gold SQL (SQL2NL-style) mitigates the meaning-drift risk that motivated dropping it originally.

### 3.2 Database content is not modified

An earlier design included literal value substitution, swapping named entity values such as `'France'` → `'Brazil'` in the question and gold SQL WHERE clause. This was dropped because:

For R1==R2 to hold (see §4), the substituted value must exist in the actual database. Guaranteeing this without modifying database rows would require querying the database for a valid replacement before every substitution: a fragile, DB-aware transformation for every NER-identified entity across 10,541 questions. The alternative, modifying database content, contradicts the core constraint that schema structure and data are preserved.

Value-level recall (a model completing `WHERE pays = 'France'` from memory) is an accepted residual risk. The contamination threat this project primarily targets is schema-level recall (recognising `movie_release_year` as a BIRD column), which the **rename** dimension is intended to reduce. Value recall is out of scope for this data preparation project.

---

## 4. The obfuscation dimension

### The rename dimension: schema language assignment

Each of the 69 databases is assigned one schema language. All table names and column names within that database are translated into the assigned language. The assignment is random but fixed (seeded), so experiments are reproducible.

**Assignment is per-database, not per-table.** Within a single DB, all identifiers use the same language. This preserves within-DB coherence while making the original English BIRD identifier names less directly useful.

#### Language pool

| Language | Slot share | Example: `country` | Example: `first_name` |
| --- | --- | --- | --- |
| English (unchanged) | ~20% | `country` | `first_name` |
| French | ~20% | `pays` | `prenom` |
| German | ~20% | `land` | `vorname` |
| Spanish | ~20% | `pais` | `nombre` |
| Mandarin Pinyin | ~20% | `guojia` | `mingzi` |

With 69 databases and 5 languages, each language covers approximately 14 databases. English is included as one of the five slots so that a subset of DBs serves as a within-experiment control. Databases assigned to the English slot keep their original BIRD identifiers unchanged; no translation is applied.

Pinyin is expected to be the strongest obfuscation variant: it uses Latin characters (SQL-safe, no encoding issues) but produces identifiers that are much less likely to overlap with memorised English BIRD SQL. It should make direct identifier recall substantially harder, though it does not rule out other forms of memorised reasoning or template reuse.

#### Rules

- The language assignment map is stored as `artifacts/db_language_map.json` (`db_id → language`)
- Translations are produced by an LLM given the **full database context in a single prompt**: the database name, all table names, and all column names and types together. The model is instructed to produce terminology a native-speaking database designer would naturally use, not word-for-word dictionary lookup. Seeing the full schema before naming anything ensures domain coherence (e.g. a `detailed_date` column in a football database gets a football-appropriate translation, not a generic one).
- After all databases in a language slot are translated, a **consistency pass** is run: a second LLM prompt reviews translations of common cross-database concepts (`id`, `name`, `created_at`, `status`, etc.) and normalises them toward a canonical form per language. This reduces avoidable variation for common concepts across databases in the same language slot. Where a canonical form conflicts with a domain-coherent term already chosen for a specific database, the domain-coherent term takes precedence; within-DB coherence is the higher priority.
- **Advisory translation quality check (`03b_check_translation_quality.py`, non-blocking):** BIRD ships a `database_description/<table>.csv` per table (`original_column_name, column_name, column_description, data_format, value_description`), a human-authored, independent description of what each column actually means, separate from anything this pipeline generates. Step 3b hands each DB's translated column names alongside their BIRD-authored descriptions to an LLM and asks it to flag translations that are semantically wrong given the description, not just stylistically generic (e.g. translating `StreetAddress` → a word meaning only "street" when the description says it holds a full address). Flags are written to `artifacts/translation_quality_flags.jsonl` for manual review; this step never modifies `schema_rename_map.json` itself. It was considered as the basis for schema *migration* (reading every description and hand-writing a migration script per DB) but rejected for that purpose: none of pgloader's actual bugs (index/PK quoting, FK-DDL crashes, `CURRENT_TIMESTAMP` quoting; see §5 step 1) were caused by not knowing the schema, and BIRD's `data_format` field is coarser than the data-driven type inference `_pg_helpers.py` already does. It is a good fit for translation-quality review specifically, where BIRD's descriptions are a genuinely independent signal the translation LLM in step 3 didn't see.
- Translations use `snake_case` to match PostgreSQL identifier conventions (e.g., `date_of_birth` → `date_de_naissance`, `fecha_de_nacimiento`, `geburtsdatum`, `chushengriqi`)
- **Known risk:** PostgreSQL silently truncates identifiers at 63 bytes. Long Pinyin transliterations are unlikely to hit this in practice, but if a collision occurs during DDL load, the affected identifier is resolved manually and the rename map updated.
- The rename map is stored as `artifacts/schema_rename_map.json` with the structure `db_id → {bare_name: obfuscated_name}`. Keys are bare identifiers without schema qualification (e.g. `"country"`, not `"world.country"`). Table names and column names share the same key space within each `db_id`; the pipeline relies on SQL AST node types and the R1==R2 validation step to catch missed or ambiguous identifier substitutions.
- Gold SQL is rewritten using **sqlglot** AST passes: SQLite SQL is first transpiled toward PostgreSQL, then validated and separately renamed after the PostgreSQL form is known to execute equivalently. Identifier nodes (table names, column references) are substituted using the rename map while string literal nodes are left untouched. The result is intended to be PostgreSQL SQL throughout after validation; residual dialect gaps are handled by the R0==R1 check and, when needed, LLM-assisted correction.
- **Identifier quoting invariant:** PostgreSQL lowercases unquoted identifiers, while BIRD SQLite schemas may contain uppercase names, spaces, or punctuation. `04_load_pg_base.py` passes `quote identifiers` explicitly to pgloader (confirmed valid grammar for SQLite sources), so `pg_base`'s identifier spelling should match the original SQLite spelling exactly. The transpilation step schema-qualifies table references and quotes emitted identifiers consistently (for example, `"app_store"."AppleStore"."Price"`). Quoting all identifiers is intentional: it preserves mixed-case names and is harmless for all-lowercase names. Step 4 also runs an empirical post-load check: it diffs SQLite `PRAGMA table_info` identifiers against `information_schema` in `pg_base` for every DB and fails loudly on any mismatch, rather than deferring the check to a much later, more expensive R0==R1 SQL-execution failure.

  This is not a minor edge case: a full audit of all 69 retained SQLite databases (`pipeline/00_audit_sqlite_identifiers.py`, findings in [`docs/reference/audit-findings.md`](../reference/audit-findings.md)) found 2,351 risky identifiers (uppercase, embedded spaces, punctuation, even hyphenated table names that aren't valid unquoted SQL at all) across 48 of 69 databases, and found zero columns with the numeric/string type mismatch that originally motivated the pgloader rewrite. Identifier-quoting fidelity through pgloader → sqlglot transpile → rename-map, not type inference, is the highest-risk part of this pipeline stage.

  **Resolved (was previously an open question):** pgloader's SQLite loader does downcase identifiers by default, confirmed against pgloader's source (`src/params.lisp`'s `*identifier-case*` defaults to `:downcase`, applied uniformly to every source loader including SQLite), but that default only wins for identifiers matching `^[A-Za-z_][A-Za-z0-9_$]*$`; anything with spaces/punctuation was already forced into the quoted/case-preserved branch regardless. This meant plain PascalCase tables like `works_cycles`'s (65/65 tables affected) were being silently downcased even though they don't contain risky punctuation: exactly the failure mode the old `WITH create tables, create indexes, reset sequences` clause (no casing directive) would hit. `quote identifiers` is confirmed valid WITH-clause grammar for SQLite sources (shared rule with MySQL sources, not MySQL-exclusive as originally suspected) and now appears in step 4's WITH clause.

  **Quoting identifiers conflicts with pgloader's own auto-generated index/PK/FK DDL (verified empirically, not just from source).** With `quote identifiers` active, pgloader's `CREATE UNIQUE INDEX`/`ALTER TABLE ... ADD PRIMARY KEY`/`ADD FOREIGN KEY` statements leave the *column names inside those statements* unquoted (only the table/column definitions in `CREATE TABLE` are quoted correctly), so PostgreSQL folds a mixed-case column like `Id` to `id`, doesn't find it, and the constraint/index creation fails. Reproduced directly against `works_cycles` loaded into a live Postgres: 51 hard errors, one per table, every single index/PK creation failing. There is no way to keep index creation but only fix the column quoting inside it: pgloader's grammar bundles PK+index creation under one `create indexes`/`create no indexes` flag with no independent override for the quoting inside the generated DDL. Step 4 therefore passes `create no indexes`: `pg_base` has correctly-spelled tables and columns, verified row-for-row against SQLite, but no indexes or PK constraints. This pipeline only ever reads from `pg_base` (steps 5 and 7's R0==R1/R1==R2 checks), so the cost is query speed, not correctness.

  **Foreign key constraints are not created at all: a deliberate methodology decision, not a workaround.** See "Deliberately absent from the schema lake" in §2. This also happens to route around a separate, confirmed pgloader crash: SQLite's shorthand `FOREIGN KEY (col) REFERENCES OtherTable` (omitting the referenced column, legal SQLite, meaning "OtherTable's primary key") makes `PRAGMA foreign_key_list` return a null `to`-column, which crashes FK-DDL generation in some pgloader builds. 176 FKs across 15 of the 69 retained databases use this shorthand, so this was not a corner case pgloader could be trusted to handle quietly. Passing `no foreign keys` in the WITH clause also keeps `pg_base` consistent with `pg_rename`, which never created FK constraints in the first place (`_pg_helpers.py`'s obfuscated-schema loader only emits `CREATE TABLE`).

  **pgloader returns exit code 0 even on hard, data-losing failures (confirmed directly, not assumed).** Running pgloader v3 with `--on-error-stop` against a `FATAL` schema-creation error still exited 0. A second, separate bug (pgloader quoting SQLite's `DEFAULT CURRENT_TIMESTAMP` as the literal string `'current_timestamp'`, which a `timestamptz` column then rejects; affects 80 tables across `works_cycles` and `movie_3`) was fixed with an explicit `CAST` rule in the WITH clause, but even *without* that fix, the aborted load exited 0 with zero tables created. A subprocess `check=True` around the pgloader invocation is therefore necessary but not sufficient. Step 4's `verify_row_counts()` (comparing `SELECT COUNT(*)` per table between SQLite and `pg_base`) is the check that actually catches silent partial loads; two real, pre-existing data-quality defects in BIRD's own `works_cycles.sqlite` were only found this way (a literal header row baked into `CountryRegion`'s data, and BLOB columns stored as hex-string `TEXT` that pgloader's type inference misreads as base64 and fails to decode).
- Evidence hints have column/table name occurrences substituted using word-boundary regex (`\bcol_name\b`) against the rename map. Hints are natural language, not SQL, so the string literal ambiguity does not apply.
- **Known limitation:** 78% of BIRD hints use the structured pattern `"X refers to column_name = value"`. Short single-word column names (e.g. `critic`, `date`, `city`) also appear as natural language in hint prose; sampling shows this affects ~37% of hints. Because `\b` respects word boundaries, compound identifiers like `critic_likes` are not corrupted by a `critic` → `critique` substitution. Prose substitutions (e.g. `"the critic made by"` → `"la critique faite par"`) may make some hints less natural, even when the structured `refers to` portion remains usable. This is accepted as a known residual limitation.

---

## 5. Physical realisation

The pipeline produces PostgreSQL SQL throughout. No SQLite SQL exists after the sqlglot rewrite step. The ordered steps are:

1. **Load `pg_base`.** Load all 69 SQLite databases into PostgreSQL using pgloader. Each BIRD database maps to a PostgreSQL schema (`db_id.table_name`), as described in [dataset.md](dataset.md). After this load, inspect `information_schema.tables` and `information_schema.columns` if needed to confirm how pgloader represented original SQLite identifier casing and punctuation.

   **pgloader runs as a container (`dimitri/pgloader:v3.6.7`), not a host install.** There is no well-packaged native Windows build, and pgloader is a Common Lisp binary that's awkward to install reliably across environments; since Docker is already a hard dependency for this pipeline (both PostgreSQL instances run in Compose), running pgloader the same way collapses the environment prerequisite to "Docker is running" and nothing else. `04_load_pg_base.py` bind-mounts each SQLite file read-only into the container and pipes the `.load` command script over stdin (`pgloader /dev/stdin`; pgloader does not accept `-` for stdin, that must be the literal path). The container reaches `pg_base` via `host.docker.internal` rather than the Compose network/service name, because pgloader's DSN hostname grammar rejects underscores and both service names (`pg_base`, `pg_rename`) contain one.

   **The final WITH clause is `create tables, create no indexes, quote identifiers, no foreign keys` plus a `CAST` rule.** Each clause choice was verified empirically against a live Postgres and the real worst-case DB (`works_cycles`), not assumed from documentation; see §4 "Identifier quoting invariant" for the full account of why `create no indexes` and `no foreign keys` are both necessary (not just stylistic) and why the CAST rule exists. `reset sequences` is omitted entirely: pgloader v3 (the actively-maintained-tag lineage; the `:latest` tag on Docker Hub has been stale since 2022-08) has a separate unfixed bug combining `quote identifiers` with sequence reset: `pg_get_serial_sequence()` gets called with the column name already wrapped in `quote_ident()`, embedding literal double-quote characters into what should be a plain-text argument (dimitri/pgloader#1651; a fix was proposed in PR #1701 but closed unmerged in favour of the v4 Clojure rewrite, PR #1705, and never backported to v3). Since this pipeline only ever reads from `pg_base` after the load, sequence starting values are irrelevant; dropping the clause avoids the bug rather than working around it.

2. **Transpile gold SQL (SQLite → PostgreSQL).** For every question, run the original SQLite gold SQL through sqlglot (`read='sqlite', write='postgres'`) to produce transpiled original SQL. This handles common patterns automatically: `STRFTIME` → `DATE_PART`, `IIF` → `CASE WHEN`, etc. Schema-qualification (`FROM t` → `FROM "db_id"."t"`) is applied to every table reference in the AST except CTE aliases: a `WITH x AS (...)` alias parses as `exp.Table` when referenced later in the statement, and sqlglot's AST has no structural way to distinguish a CTE reference from a real table reference. Qualifying it anyway produces `"db_id"."x"`, which does not exist and fails execution. CTE alias names are collected first (`{cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}`) and excluded from qualification. This is not a hypothetical: 9 gold queries in the retained corpus use `WITH` (`card_games`, `formula_1` ×6, `toxicology`) and were hitting exactly this failure before the fix.

3. **Validate transpilation (R0==R1).** Execute original gold SQL against the original SQLite database → R0 (ground truth). Execute transpiled SQL against `pg_base` → R1. Require normalized multiset equality. Pass 1 (sqlglot) writes direct matches to `workdir/*_transpiled.jsonl`; mismatches go to `transpilation_needs_fix.jsonl`. Pass 2 uses coding-agent assistance (not an in-pipeline LLM) to propose fixes, validated again by `05b_apply_sql_fixes.py` before merge. Questions that never pass R0==R1 are recorded in `transpilation_failures.jsonl`. Some agent fixes use **VALUES materialization** (PostgreSQL SQL that embeds SQLite result rows); they pass R0==R1 on the loaded data but are not portable dialect translations. See [../reference/step5-transpilation.md](../reference/step5-transpilation.md) for artifact layout, timeouts, duplicates, and caveats.

   **Schema context for agents must describe `pg_base` as it actually loaded, not the SQLite source.** `get_pg_schema_ddl()` queries `information_schema` on the live `pg_base` connection because pgloader performs its own type inference and identifier spelling may differ from SQLite's `CREATE TABLE` text.

4. **Rename gold SQL.** Apply the rename map to the validated PostgreSQL SQL using a single sqlglot AST pass (parse PostgreSQL, rename identifier nodes, emit PostgreSQL). This step is intentionally separate from step 2: validation happens on unmodified identifiers, so agent fixes in step 3 work against recognisable original column names, not renamed ones. The output is obfuscated PostgreSQL SQL.

5. **Build `pg_rename` by cloning the `pg_base` Docker volume, then renaming in place**, not by reloading from SQLite a second time. `pg_base` and `pg_rename` are separate Postgres containers, each with its own named Docker volume (`pg_base_data`, `pg_rename_data`). Since `pg_base` is already verified byte-for-byte correct against SQLite (step 1's row-count/casing checks) and already has proper Postgres-native types (from pgloader's own type inference, not a second guess at it), the obfuscated instance is produced by:

   1. Stopping `pg_base` so its on-disk files are quiescent, and taking a raw filesystem copy of `pg_base_data` into `pg_rename_data` via a throwaway container that bind-mounts both volumes **read-only on the source side** (`docker run --rm -v pg_base_data:/from:ro -v pg_rename_data:/to alpine cp -a /from/. /to/`). The `:ro` mount is what makes this safe: the container that does the copy has no write path back to `pg_base_data` at all, so a bug in the copy command cannot touch the source.
   2. Restarting `pg_base` immediately after the copy (downtime is only as long as the `cp`, not the whole rename step) and starting `pg_rename` for the first time against its now-populated volume.
   3. Running `06_build_pg_rename.py` against **`pg_rename` only** (it never opens a connection to `pg_base`), issuing `ALTER TABLE ... RENAME TO ...` / `ALTER TABLE ... RENAME COLUMN ... TO ...` per `artifacts/schema_rename_map.json`. Renaming is a catalog-only metadata operation in PostgreSQL (no table rewrite, no data movement), so this is fast regardless of row count and cannot introduce a type or data mismatch versus `pg_base`: there is no second type-inference pass to disagree with the first.

   This replaces an earlier design where `06_build_pg_rename.py` re-read every SQLite file and re-inferred types independently via `_pg_helpers.py`'s data-sampling `infer_pg_type()` (NUMERIC/TEXT only, coarser than pgloader's own inference already verified in `pg_base`). That design risked `pg_base` and `pg_rename` silently diverging in column types for the same logical data, a real risk (not hypothetical), since the two inference paths used different logic. The volume-clone approach removes the second inference pass entirely: `pg_rename`'s types are `pg_base`'s types, by construction.

6. **Validate renaming (R1==R2).** Execute validated transpiled original SQL against `pg_base` → R1; execute obfuscated SQL against `pg_rename` → R2; assert equality. A mismatch here indicates a rename map gap (missed identifier in SQL rewrite), not a dialect issue.

Running both instances in PostgreSQL eliminates SQLite-to-PostgreSQL dialect mismatch as a variable in the R1==R2 rename integrity check. The R0==R1 step uses SQLite as the semantic ground truth oracle.

---

## 6. Artifact structure

### Obfuscated schema lake

`pg_base` and `pg_rename` are the two clean baselines, run locally via **Docker Compose**; two further **decoy-augmented** instances (`pg_decoy` at 5434 and `pg_rename_decoy` at 5435) carry the corrupted traps (see §8). `pg_base` is used for the R0==R1 transpilation check and the base eval conditions; `pg_rename` for the R1==R2 rename check and the rename conditions. The **published deliverable is all four instances** as PostgreSQL dumps on [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) plus the git-tracked [`eval_dataset/`](../../eval_dataset/) gold/mappings/manifests. Neither clean instance is reconstructed from scratch at eval time: both are built once and persisted as Docker volumes. `pg_rename`'s volume is a filesystem clone of `pg_base`'s, renamed in place (§5 step 5), and `pg_base` is only ever read during this clone (mounted read-only), never modified. The two `*_decoy` volumes are likewise clones of the clean ones with the traps injected (step 10).

### Files in this repo

All files are JSON or JSONL for machine readability.

```text
artifacts/
  retained_dbs.json             # step 1 output; db_id list, read by steps 0,2,3,4,6
  db_language_map.json          # db_id -> language
  schema_rename_map.json        # db_id -> {original_identifier: obfuscated_identifier} (git-tracked; regenerate via step 3)
  sqlite_identifier_audit.jsonl # step 0 diagnostic output
  translation_quality_flags.jsonl # step 3b advisory output
  train.jsonl                   # training questions (80% per DB, random split)
  test.jsonl                    # held-out test questions (20% per DB)

workdir/
  train_transpiled.jsonl        # step 5 output, read by step 7
  test_transpiled.jsonl
  transpilation_needs_fix.jsonl # step 5 repair queue
  transpilation_fixes.jsonl     # agent-written fixes
  transpilation_failures.jsonl  # terminal step-5 failures
  rename_failures.jsonl         # terminal step-7 failures
  fix_batches/                  # step 5c export batches for agents

eval_dataset/                   # git-tracked FINAL deliverable (snapshot of artifacts/)
  train_final.jsonl test_final.jsonl        # validated gold pairs (8,134 / 2,030)
  schema_rename_map.json db_language_map.json
  trap_manifest.json trap_table_manifest.json  # step 10 corrupted-trap ground truth
  decoy_map.json                            # step 08 structural decoys (superseded)
  question_paraphrases.jsonl                # step 09
  gold_star_expanded.jsonl order_sensitive_qids.json
```

`artifacts/` holds durable outputs consumed by name across pipeline steps (or read by a human as a diagnostic deliverable); the extension steps 08-10 also write there (`decoy_map.json`, `question_paraphrases.jsonl`, `trap_manifest.json`, `trap_table_manifest.json`, `gold_star_expanded.jsonl`, `order_sensitive_qids.json`). `eval_dataset/` is the git-tracked, frozen snapshot of the final deliverable (built by `eval_dataset/build_eval_dataset.py`); `workdir/` holds transient step-5/7 repair-queue scratch files with no consumer outside that repair loop.

Each line in `train_final.jsonl` / `test_final.jsonl` (the validated deliverable) is a JSON object with fields:

```json
{
  "question_id": "...",
  "db_id": "...",
  "question": "...",
  "evidence": "...",
  "evidence_rename": "...",
  "difficulty": "...",
  "sql_sqlite": "...",
  "sql_base": "...",
  "sql_rename": "..."
}
```

The three gold-SQL fields: `sql_sqlite` (raw **SQLite**, original BIRD identifiers, retained for traceability and the R0==R1 check), `sql_base` (**PostgreSQL**, original identifiers, used by the R1==R2 check and the base/decoy eval arms), and `sql_rename` (**PostgreSQL**, renamed identifiers, used for R1==R2, the rename/all arms, and downstream memory building against `pg_rename`). `difficulty` carries BIRD's label where available (dev questions only; train questions have none). Likewise, `evidence_rename` has column/table name occurrences substituted per the rename map (§4) and is the version downstream consumers should show the agent; `evidence` (original English) is retained for traceability only. The paraphrase dimension adds a separate `question_paraphrase` per test question (`eval_dataset/question_paraphrases.jsonl`).

---

## 7. Extended obfuscation dimensions (decoy + paraphrase)

Sections 1-6 cover the core validated pipeline (steps 0-7), which obfuscates **only schema identifiers** (the **rename** dimension) and leaves questions and database content untouched. This part specifies two **additional, independently-toggleable** obfuscation dimensions and the ablation that measures each. **Status: implemented and applied** — pipeline steps 08-10 and the ablation harness `pipeline/eval_ablation.py` all exist and have been run; results are in [evaluation.md §9.4](evaluation.md). The decoy dimension was **reworked** from the empty/structural design first sketched during planning into **corrupted "evil-twin" traps** (step 10); §8 reflects the as-built design, with full detail in [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md).

### 7.1 Why extend

Two independent lines of prior work indicate identifier renaming (the **rename** dimension) is the *weakest* contamination lever, and that BIRD is only weakly contaminated at that axis to begin with:

- **SPENCE** (*A Syntactic Probe for Detecting Contamination in NL2SQL Benchmarks*, arXiv 2604.17771): paraphrasing the **question** exposes memorisation far more than the schema axis. BIRD shows weak rank-sensitivity (Kendall's τ ≈ −0.35, CI spanning zero) versus Spider/SParC/CoSQL (τ ≈ −0.7 to −0.9). The **question form**, not the identifier, is the sensitive axis.
- **SQL2NL** (*Evaluating NL2SQL via SQL2NL*, arXiv 2509.04657, same authors): schema-aligned question paraphrase drops execution accuracy 10-20% on Spider, a large and real effect on the question axis that standard benchmarks hide.

The two new dimensions each attack a **different mechanism**; they are not three strengths of one thing:

| Dimension | Attacks | Mechanism |
| --- | --- | --- |
| **rename**: identifier rename (§4) | identifier recall | model recognises a memorised BIRD column name |
| **decoy**: decoy schema injection (§8) | schema linking | model must ground in the real schema, not pattern-match |
| **paraphrase**: question paraphrase (§9) | question-form recall | model can't lean on a memorised question→SQL template |

**Non-negotiable invariant (both dimensions):** every `(question, gold SQL)` pair must stay **solvable / execution-equivalent**, verified mechanically the same way the core pipeline verifies R1==R2.

---

## 8. The decoy dimension: corrupted decoy traps

### Goal
Turn decoys from inert schema-linking distractors into **traps**. Because the eval target is an **interactive execute-and-observe SQL agent**, a decoy that the agent queries must return *plausible-but-wrong* data. Empty decoy tables and NULL decoy columns, the original design, were rejected: `COUNT(*)=0` or an all-NULL column unmasks them for free. So decoys now hold **subtly corrupted copies of real data** (the confusable-name attack *plus* a data-level trap), while the model that only reads stripped DDL still just sees extra plausible identifiers.

### What is added (strictly additive)
Added only to **decoy-augmented clones** (`pg_decoy`, `pg_rename_decoy`), **never** into the clean `pg_base` / `pg_rename`. Two granularities (`pipeline/10_inject_traps.py`):
- **Evil-twin columns**: a NEW column on a real table whose values are a *corrupted copy* of a real **source** column, named as a near-synonym (e.g. real `annee_sortie` → decoy `date_sortie`). The real column is never modified. (`trap_manifest.json`, 1,486.)
- **Corrupted clone tables**: a whole real table cloned and renamed, with a subset of its columns corrupted and the rest copied exact for realism. Gold never references a decoy table, so these are R1==R2-safe by construction. (`trap_table_manifest.json`, 162.)

Both must not collide with a real table/column name or with the `db_id` itself (the `superhero`/`sales_in_weather`/`university` schema-qualifier caveat in AGENTS.md).

### Corruption (deterministic, additive)
The copied values are corrupted by hash-seeded operators (full spec: [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md)): join-key/FK columns are **permuted** (every value stays a real key → referential integrity preserved, still a stealthy join trap), numeric columns get sparse ±relative noise, text columns an in-domain category remap, temporal columns a bounded date offset. Corruption is a pure function of a per-row key + a **variant-independent** salt, so `pg_decoy` and `pg_rename_decoy` corrupt identical rows identically and a rebuild is reproducible. A cheap LLM (`gpt-5.4-mini`) supplies the synonym table/column names per DB per variant. The manifests (§10) are the ground truth; nothing is re-inferred at consumption time.

### Solvability invariant and the one breakage vector
Traps are **strictly additive**: real columns and tables stay byte-identical (verified by an order-independent fingerprint on both decoy instances), so gold SQL, which never references a decoy, executes unchanged and returns the real-column result → R1==R2 holds. **The one breakage vector is a gold `SELECT *` / `t.*` over a real table** with added decoy columns: at execution the star expands to include the decoys, widening the result and breaking equality.

**Measurement (2026-07-03, `sql_base` over the 10,164 validated questions):**

| Category | Count | % |
| --- | --- | --- |
| Real-table **top-level** star (definite breakage) | **3** | 0.03% |
| Real-table **any-level** star (upper bound) | 5 | 0.05% |
| VALUES-materialized (excluded; no real table) | 1,169 | n/a |
| DBs with **zero** star queries | 67 / 69 | n/a |

The 3 top-level cases are all in `mondial_geo`; the 2 subquery-level are in `professional_basketball`. `COUNT(*)` is correctly **not** counted (it is not a projection-list star). So `SELECT *` is effectively a rounding error.

**Resolution: `SELECT *` expansion.** In the gold SQL used against a decoy-augmented instance, expand `SELECT *` / `t.*` to the **explicit real-column list** (sqlglot + `information_schema` read from the instance *before* decoys are added). This is:
- **harmless** on a non-decoy instance (the star already equals the real columns), and
- **correct** on a decoy instance (decoys never enter the result, equality is exact).

Applying it uniformly to all gold keeps every ablation arm's gold answer identical and comparable. **Fallback** (if star expansion is inconvenient): exclude the 6-7 star-touched tables (`mondial_geo.{politics,river,mountain,geo_mountain,province,country}` + `professional_basketball.teams`) from column-decoys and give them decoy *tables* instead. That costs almost nothing at this count, though those tables then miss the confusable-column attack.

### Validation
Re-run step 7's R1==R2 against the decoy-augmented instance. Any residual star breakage is resolved by expansion. One residual class is **benign and expected**: the trap-population `UPDATE`s reorder the heap, so gold with a `LIMIT` and no total order (or a float aggregate) can return a *different-but-valid* row set on the decoy instance. These are enumerated in `order_sensitive_qids.json` (153 order-sensitive + 21 pre-existing exec-failed) and excluded from strict cross-variant EX, not treated as corruption (the real data is provably intact).

---

## 9. The paraphrase dimension: question paraphrase

### Goal
Break verbatim / near-verbatim question-string recall (the SPENCE-sensitive axis) while preserving the question's mapping to the gold SQL.

### Generation (cheap model)
A cheap LLM produces **one** paraphrase per question, conditioned on `(original question + gold SQL + obfuscated schema)` so intent is anchored (SQL2NL-style; SPENCE shows the signal does not depend on the generator choice). Constraints: stay **natural language**, and **do not inject schema identifiers** into the question (99.7% of BIRD questions contain none, so don't reintroduce the obfuscated ones).

### Drift and solvability
Because the model is given **both the question and the gold SQL**, semantic drift is expected to be small (project decision, 2026-07-03), so there is **no hard embedding gate**, only an optional cheap cosine sanity check. The gold SQL is unchanged, so **R1==R2 is untouched** by paraphrase. "Answerable" is measured by the ablation eval itself (a capable model still solves the paraphrased question), i.e. it is an **experimental measurement, not a pre-validated guarantee**. If a hard solvability guarantee is ever needed, add a solver round-trip gate: run a solver on `(paraphrase + obfuscated schema, no gold)` and require its result to match the gold R2, paired against the original question so hard questions aren't penalised.

The original `question` is retained for traceability.

---

## 10. Extended data and storage additions

Existing field/artifact names are **kept stable**; downstream consumers and `eval_contamination.py` depend on them.

### New per-question field
- `question_paraphrase`: the **paraphrase** dimension output (parallels `evidence_rename`; original `question` retained).

### New artifacts
Canonical copies are git-tracked in [`eval_dataset/`](../../eval_dataset/) (working copies in `artifacts/`), and are also listed in the tree in §6:
- `trap_manifest.json`: **evil-twin columns** ground truth. Per trap: `{db, table, source_column, source_type, operator, is_key, in_correlated_group, salt, names:{base, rename}}`.
- `trap_table_manifest.json`: **corrupted clone tables** ground truth. Per clone: `{db, source_table, columns:[{source_column, source_type, operator, is_key}], names:{base:{table, columns}, rename:{table, columns}}}`.
- `order_sensitive_qids.json`: qids excluded from strict cross-variant EX (153 order-sensitive + 21 exec-failed).
- `decoy_map.json`: the earlier step-08 *structural* decoy map (`db_id → {tables, columns}`); retained for provenance, superseded by the trap manifests above.
- `gold_star_expanded.jsonl`: `SELECT *`-expanded gold for the ~5 star queries.

### New PostgreSQL instances (docker-compose)
Two clean baselines stay untouched; two decoy-augmented instances are added, each built by **cloning the corresponding clean volume then injecting decoys** (same read-only clone pattern as §5 step 5):

| Instance | Port | Identifiers | Decoys | Used by arm |
| --- | --- | --- | --- | --- |
| `pg_base` | 5432 | original | no | base, paraphrase |
| `pg_rename` | 5433 | renamed | no | rename |
| `pg_decoy` | 5434 | original | yes (English) | decoy |
| `pg_rename_decoy` | 5435 | renamed | yes (translated) | combined |

### Eval results
- `eval/ablation_results.jsonl`: one record per `(question_id, arm)`, separate from the existing `eval/contamination_results.jsonl` (the contamination run).

### Field naming (resolved 2026-07-03)
Gold-SQL fields use a consistent scheme: `sql_sqlite` (raw SQLite), `sql_base` (PostgreSQL, original identifiers), `sql_rename` (PostgreSQL, renamed identifiers). These were formerly `sql_original` / `sql_pg` / `sql_obfuscated`; the `sql_pg`/`sql_obfuscated` pair was asymmetric (both were PostgreSQL). They were renamed repo-wide during the `base`/`rename`/`decoy`/`rename_decoy` consolidation, and the deliverable JSONL was migrated in place.

---

## 11. Extended pipeline steps (08-10)

Built in dependency order: decoy first (it is the part that touches the R1==R2 contract), then paraphrase, then the ablation harness.

| # | Script | Does |
| --- | --- | --- |
| 08 | `08_inject_decoys.py` | generate `decoy_map.json` (cheap LLM) → clone volumes into `pg_*_decoy` → inject *structural* decoys → expand `SELECT *` in affected gold → re-run R1==R2. **Superseded for the decoy payload by step 10.** |
| 09 | `09_paraphrase_questions.py` | generate `question_paraphrase` (cheap LLM), one per test question |
| 10 | `10_inject_traps.py` | **corrupted decoy traps**: evil-twin columns + corrupted clone tables (additive), injected into both `*_decoy` instances; emits `trap_manifest.json` + `trap_table_manifest.json`. See [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md). |
| n/a | `pipeline/eval_ablation.py` | standalone 5-arm ablation harness (base/rename/decoy/paraphrase/all); defaults to offline prepare/generate/grade; writes `eval/ablation_results.jsonl` |

See [evaluation.md §9](evaluation.md) for the ablation design that consumes these outputs, and [../reference/extension-implementation-plan.md](../reference/extension-implementation-plan.md) for the original step-by-step build spec (note: its decoy sections predate the step-10 corrupted-trap rework; see the banner there).
