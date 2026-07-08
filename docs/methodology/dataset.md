# Methodology: dataset construction and the schema lake

## 1. Task definition

This project prepares an obfuscated evaluation dataset for an agentic Text-to-SQL setting. The dataset is designed to reduce the advantage that frontier language models may get from memorised public BIRD schema identifiers while preserving a semantically usable text-to-SQL task. In the motivating scenario, an agent builds a semantic memory layer from known true SQL paired with schema metadata (column names and dtypes only), then answers new natural-language questions by generating SQL against the correct schema.

The evaluation gives the model the correct database upfront. Routing is not evaluated here; it is left to a downstream evaluation harness. This project's job is to produce a schema lake where column-name memorisation is made less directly useful, not to prove that all forms of benchmark memorisation have been removed.

---

## 2. Downstream task

The motivating downstream task is an agentic Text-to-SQL workflow over a multi-database schema lake. During memory building, the agent receives:

- stripped schema metadata for every retained database (table names, column names, and dtypes only);
- training questions paired with known correct SQL;
- the obfuscated PostgreSQL schema produced by this repository.

At test time, the agent receives a new natural-language question and must generate SQL that executes correctly against the obfuscated schema. A downstream evaluation harness may additionally evaluate schema routing, retrieval, memory construction, or agent planning. Those system-level choices are outside this repository; this repository only prepares and validates the obfuscated data artifacts used by that task.

---

## 3. Schema lake construction

The BIRD train and dev splits are combined before any filtering. Train and dev have no overlapping databases (0 shared db_ids), so the combined corpus is 80 distinct databases with 10,962 questions total.

All retained databases are loaded into the two clean **PostgreSQL instances** (`pg_base` and `pg_rename`) as described in [obfuscation.md §5](obfuscation.md); two further decoy-augmented instances (`pg_decoy`, `pg_rename_decoy`) are clones carrying the corrupted traps (four instances total). Each BIRD database maps to a PostgreSQL schema in every instance:

```text
world.country
movies_4.country
works_cycles.employee
...
```

This fully-qualified naming resolves all table-name collisions without renaming or modifying any schema. Across the 69 retained databases, 45 table names are shared by more than one database (e.g. `country` appears in 11 databases). PostgreSQL schemas make these unambiguous at the query level.

Gold SQL from BIRD is transpiled from SQLite to PostgreSQL dialect and rewritten to use schema-qualified table references (`db_id.table_name`). See [obfuscation.md §5](obfuscation.md) for the pipeline overview and [../reference/step5-transpilation.md](../reference/step5-transpilation.md) for what R0==R1 validation guarantees (including VALUES materialization and the failures bucket).

---

## 4. Database inclusion criteria

Each database in the lake contributes both its schema (available to the agent during memory building) and held-out test questions (used to measure evaluation accuracy). A database that cannot contribute a meaningful test set provides no evaluation signal.

**Minimum threshold: 60 questions per database**, applied to the combined train+dev count.

Databases below this threshold lack sufficient questions to support a stratified holdout with reliable per-difficulty coverage. The threshold is evaluated on the combined corpus, since combining train and dev gives smaller train-only databases the best chance of passing before exclusion is applied.

All 11 excluded databases fall below 60 questions in the train split and have no dev counterpart:

| Database | Combined questions | Reason for exclusion |
| --- | --- | --- |
| `craftbeer` | 6 | Effectively unevaluable |
| `citeseer` | 19 | Min difficulty cell = 2 |
| `genes` | 23 | Zero challenging questions |
| `shooting` | 28 | Too few for meaningful holdout |
| `trains` | 40 | Borderline; no per-difficulty split possible |
| `music_tracker` | 45 | Min cell = 3 |
| `movie` | 46 | Zero challenging questions |
| `coinmarketcap` | 48 | Min cell = 3 |
| `mental_health_survey` | 50 | Borderline |
| `european_football_1` | 57 | Borderline |
| `human_resources` | 59 | Borderline |

These 11 databases are excluded from both the schema lake and the evaluation. The remaining **69 databases** (10,541 questions) form the dataset.

---

## 5. Train / test split

The original BIRD train/dev split is discarded. A new random split is applied within each of the 69 retained databases across the combined question pool.

### Split strategy

A **random 80/20 holdout** is applied per database with a fixed seed for reproducibility:

- 80% of each database's questions → train (used for agent memory building)
- 20% of each database's questions → test (held out for evaluation)

### Rationale

- **Combined pool before splitting** prevents the original BIRD dev questions from being wasted. The original train/dev boundary was arbitrary for this project's purposes.
- **Within-DB split** is appropriate for the downstream memory-building setting because the agent is expected to build memory over seen schemas. This evaluates performance on held-out questions within retained databases, not generalisation to unseen databases.
- **Random rather than stratified** because the BIRD train split carries no difficulty labels; they were absent from the JSON. The BIRD dev difficulty labels were human-assigned and not comparable to SQL-complexity-derived labels. Stratifying on inconsistent labels would be false precision. A random split is honest and reproducible.

---

## 6. Evaluation metrics

Each test question is evaluated on execution accuracy only. The correct database is given to the model upfront; routing accuracy is out of scope for this project.

| Metric | Definition |
| --- | --- |
| **Execution accuracy (EX)** | Does the generated SQL, when run against the schema lake, return the correct result set? |

Reported overall and broken down by:

- Obfuscation language (English control / French / German / Spanish / Pinyin)
- Collision status (table name appears in ≥2 databases vs. unique)

---

## 7. Summary

| | Count |
| --- | --- |
| BIRD train databases | 69 |
| BIRD dev databases | 11 |
| Combined databases | 80 |
| Databases excluded (< 60 questions) | 11 |
| Databases in lake | 69 |
| Total questions in lake (pre-validation split) | 10,541 |
| Train questions (80% per DB) | 8,428 |
| Test questions (20% per DB) | 2,113 |
| Table-name collisions across lake | 45 |

### Final validated deliverable

Every one of the 69 databases in the lake is represented in both `train_final.jsonl` and `test_final.jsonl` (schema-symmetric split, §5). Not every question from the pre-validation split survives; see [step5-transpilation.md](../reference/step5-transpilation.md) for why a small number are excluded (transpilation failures, and gold SQL with genuine defects like a missing join condition caught by the R1==R2 check). The canonical, git-tracked copies of these files (plus the rename map, trap manifests, paraphrases, and other mappings) live in [`eval_dataset/`](../../eval_dataset/); the `artifacts/` copies are the pipeline's working versions.

| | Count |
| --- | --- |
| Databases represented | 69 / 69 |
| `artifacts/train_final.jsonl` (validated train questions) | 8,134 |
| `artifacts/test_final.jsonl` (validated test questions) | 2,030 |
| **Total validated questions in final deliverable** | **10,164** |
| Excluded (transpilation failures + R1==R2 rename failures) | 377 (10,541 − 10,164) |
