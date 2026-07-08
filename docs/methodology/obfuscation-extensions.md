# Methodology — Extended Obfuscation Dimensions (Decoy Schema + Question Paraphrase)

The core validated pipeline (steps 0–7; see [obfuscation.md](obfuscation.md), [dataset.md](dataset.md), [evaluation.md](evaluation.md)) obfuscates **only schema identifiers** (the **rename** dimension) and leaves questions and database content untouched. This document specifies two **additional, independently-toggleable** obfuscation dimensions and an ablation to measure each. **Status: implemented and applied** — pipeline steps 08–10 and the ablation harness `pipeline/eval_ablation.py` all exist and have been run. The decoy dimension was **reworked** from the empty/structural design first sketched here into **corrupted "evil-twin" traps** (step 10); §2 below reflects the as-built design, with full detail in [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md). See [../../PROGRESS.md](../../PROGRESS.md) for status.

## 1. Motivation — why extend

Two independent lines of prior work indicate identifier renaming (the **rename** dimension) is the *weakest* contamination lever, and that BIRD is only weakly contaminated at that axis to begin with (this project's own contamination measurement is being re-run on a stronger model and is not reported here):

- **SPENCE** (*A Syntactic Probe for Detecting Contamination in NL2SQL Benchmarks*, arXiv 2604.17771): paraphrasing the **question** exposes memorisation far more than the schema axis. BIRD shows weak rank-sensitivity (Kendall's τ ≈ −0.35, CI spanning zero) versus Spider/SParC/CoSQL (τ ≈ −0.7 to −0.9). The **question form**, not the identifier, is the sensitive axis.
- **SQL2NL** (*Evaluating NL2SQL via SQL2NL*, arXiv 2509.04657, same authors): schema-aligned question paraphrase drops execution accuracy 10–20pp on Spider — a large, real effect on the question axis that standard benchmarks hide.

The two new dimensions each attack a **different mechanism** — they are not three strengths of one thing:

| Dimension | Attacks | Mechanism |
| --- | --- | --- |
| **rename** — identifier rename (existing) | identifier recall | model recognises a memorised BIRD column name |
| **decoy** — decoy schema injection (new) | schema linking | model must ground in the real schema, not pattern-match |
| **paraphrase** — question paraphrase (new) | question-form recall | model can't lean on a memorised question→SQL template |

**Non-negotiable invariant (both dimensions):** every `(question, gold SQL)` pair must stay **solvable / execution-equivalent**, verified mechanically the same way the core pipeline verifies R1==R2.

---

## 2. The decoy dimension — Decoy schema injection

### Goal
Turn decoys from inert schema-linking distractors into **traps**. Because the eval target is an **interactive execute-and-observe SQL agent**, a decoy that the agent queries must return *plausible-but-wrong* data. Empty decoy tables / NULL decoy columns — the original design — were rejected: `COUNT(*)=0` or an all-NULL column unmasks them for free. So decoys now hold **subtly corrupted copies of real data** (the confusable-name attack *plus* a data-level trap), while the model that only reads stripped DDL still just sees extra plausible identifiers.

### What is added (strictly additive)
Added only to **decoy-augmented clones** (`pg_decoy`, `pg_rename_decoy`) — **never** into the clean `pg_base` / `pg_rename`. Two granularities (`pipeline/10_inject_traps.py`):
- **Evil-twin columns** — a NEW column on a real table whose values are a *corrupted copy* of a real **source** column, named as a near-synonym (e.g. real `annee_sortie` → decoy `date_sortie`). The real column is never modified. (`trap_manifest.json`, 1,486.)
- **Corrupted clone tables** — a whole real table cloned and renamed, with a subset of its columns corrupted and the rest copied exact for realism. Gold never references a decoy table, so these are R1==R2-safe by construction. (`trap_table_manifest.json`, 162.)

Both must not collide with a real table/column name or with the `db_id` itself (the `superhero`/`sales_in_weather`/`university` schema-qualifier caveat in AGENTS.md).

### Corruption (deterministic, additive)
The copied values are corrupted by hash-seeded operators (full spec: [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md)): join-key/FK columns are **permuted** (every value stays a real key → referential integrity preserved, still a stealthy join trap), numeric columns get sparse ±relative noise, text columns an in-domain category remap, temporal columns a bounded date offset. Corruption is a pure function of a per-row key + a **variant-independent** salt, so `pg_decoy` and `pg_rename_decoy` corrupt identical rows identically and a rebuild is reproducible. A cheap LLM (`gpt-5.4-mini`) supplies the synonym table/column names per DB per variant. The manifests (§4) are the ground truth; nothing is re-inferred at consumption time.

### Solvability invariant — and the one breakage vector
Traps are **strictly additive** — real columns/tables stay byte-identical (verified by an order-independent fingerprint on both decoy instances) — so gold SQL, which never references a decoy, executes unchanged and returns the real-column result → R1==R2 holds. **The one breakage vector is a gold `SELECT *` / `t.*` over a real table** with added decoy columns: at execution the star expands to include the decoys, widening the result and breaking equality.

**Measurement (2026-07-03, `sql_base` over the 10,164 validated questions):**

| Category | Count | % |
| --- | --- | --- |
| Real-table **top-level** star (definite breakage) | **3** | 0.03% |
| Real-table **any-level** star (upper bound) | 5 | 0.05% |
| VALUES-materialized (excluded — no real table) | 1,169 | — |
| DBs with **zero** star queries | 67 / 69 | — |

The 3 top-level cases are all in `mondial_geo`; the 2 subquery-level are in `professional_basketball`. `COUNT(*)` is correctly **not** counted (it is not a projection-list star). So `SELECT *` is effectively a rounding error.

**Resolution — `SELECT *` expansion.** In the gold SQL used against a decoy-augmented instance, expand `SELECT *` / `t.*` to the **explicit real-column list** (sqlglot + `information_schema` read from the instance *before* decoys are added). This is:
- **harmless** on a non-decoy instance (the star already equals the real columns), and
- **correct** on a decoy instance (decoys never enter the result, equality is exact).

Applying it uniformly to all gold keeps every ablation arm's gold answer identical and comparable. **Fallback** (if star expansion is inconvenient): exclude the 6–7 star-touched tables (`mondial_geo.{politics,river,mountain,geo_mountain,province,country}` + `professional_basketball.teams`) from column-decoys and give them decoy *tables* instead — near-zero cost at this count, but those tables then miss the confusable-column attack.

### Validation
Re-run step 7's R1==R2 against the decoy-augmented instance. Any residual star breakage is resolved by expansion. One residual class is **benign and expected**: the trap-population `UPDATE`s reorder the heap, so gold with a `LIMIT` and no total order (or a float aggregate) can return a *different-but-valid* row set on the decoy instance. These are enumerated in `order_sensitive_qids.json` (153 order-sensitive + 21 pre-existing exec-failed) and excluded from strict cross-variant EX — not treated as corruption (the real data is provably intact).

---

## 3. The paraphrase dimension — Question paraphrase

### Goal
Break verbatim / near-verbatim question-string recall (the SPENCE-sensitive axis) while preserving the question's mapping to the gold SQL.

### Generation (cheap model)
A cheap LLM produces **one** paraphrase per question, conditioned on `(original question + gold SQL + obfuscated schema)` so intent is anchored (SQL2NL-style; SPENCE shows the signal does not depend on the generator choice). Constraints: stay **natural language**, and **do not inject schema identifiers** into the question (99.7% of BIRD questions contain none — don't reintroduce the obfuscated ones).

### Drift and solvability
Because the model is given **both the question and the gold SQL**, semantic drift is expected to be small (project decision, 2026-07-03) — so there is **no hard embedding gate**, only an optional cheap cosine sanity check. The gold SQL is unchanged, so **R1==R2 is untouched** by paraphrase. "Answerable" is measured by the ablation eval itself (a capable model still solves the paraphrased question), i.e. it is an **experimental measurement, not a pre-validated guarantee**. If a hard solvability guarantee is ever needed, add a solver round-trip gate: run a solver on `(paraphrase + obfuscated schema, no gold)` and require its result to match the gold R2, paired against the original question so hard questions aren't penalised.

The original `question` is retained for traceability.

---

## 4. Data & storage additions

Existing field/artifact names are **kept stable** — downstream consumers and `eval_contamination.py` depend on them.

### New per-question field
- `question_paraphrase` — **paraphrase** dimension output (parallels `evidence_rename`; original `question` retained).

### New artifacts
Canonical copies are git-tracked in [`eval_dataset/`](../../eval_dataset/) (working copies in `artifacts/`):
- `trap_manifest.json` — **evil-twin columns** ground truth: per trap `{db, table, source_column, source_type, operator, is_key, in_correlated_group, salt, names:{base, rename}}`.
- `trap_table_manifest.json` — **corrupted clone tables** ground truth: per clone `{db, source_table, columns:[{source_column, source_type, operator, is_key}], names:{base:{table, columns}, rename:{table, columns}}}`.
- `order_sensitive_qids.json` — qids excluded from strict cross-variant EX (153 order-sensitive + 21 exec-failed).
- `decoy_map.json` — the earlier step-08 *structural* decoy map (`db_id → {tables, columns}`); retained for provenance, superseded by the trap manifests above.
- `gold_star_expanded.jsonl` — `SELECT *`-expanded gold for the ~5 star queries.

### New PostgreSQL instances (docker-compose)
Two clean baselines stay untouched; two decoy-augmented instances are added, each built by **cloning the corresponding clean volume then injecting decoys** (same read-only clone pattern as step 6):

| Instance | Port | Identifiers | Decoys | Used by arm |
| --- | --- | --- | --- | --- |
| `pg_base` | 5432 | original | no | base, paraphrase |
| `pg_rename` | 5433 | renamed | no | rename |
| `pg_decoy` | 5434 | original | yes (English) | decoy |
| `pg_rename_decoy` | 5435 | renamed | yes (translated) | combined |

### Eval results
- `eval/ablation_results.jsonl` — one record per `(question_id, arm)`, separate from the existing `eval/contamination_results.jsonl` (the contamination run).

### Field naming (resolved 2026-07-03)
Gold-SQL fields use a consistent scheme: `sql_sqlite` (raw SQLite), `sql_base` (PostgreSQL, original identifiers), `sql_rename` (PostgreSQL, renamed identifiers). These were formerly `sql_original` / `sql_pg` / `sql_obfuscated` — the `sql_pg`/`sql_obfuscated` pair was asymmetric (both were PostgreSQL). They were renamed repo-wide during the `base`/`rename`/`decoy`/`rename_decoy` consolidation, and the deliverable JSONL was migrated in place.

---

## 5. Pipeline steps (implemented)

Built in dependency order: decoy first (it is the part that touches the R1==R2 contract), then paraphrase, then the ablation harness.

| # | Script | Does |
| --- | --- | --- |
| 08 | `08_inject_decoys.py` | generate `decoy_map.json` (cheap LLM) → clone volumes into `pg_*_decoy` → inject *structural* decoys → expand `SELECT *` in affected gold → re-run R1==R2. **Superseded for the decoy payload by step 10.** |
| 09 | `09_paraphrase_questions.py` | generate `question_paraphrase` (cheap LLM), one per test question |
| 10 | `10_inject_traps.py` | **corrupted decoy traps** — evil-twin columns + corrupted clone tables (additive), injected into both `*_decoy` instances; emits `trap_manifest.json` + `trap_table_manifest.json`. See [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design.md). |
| — | `pipeline/eval_ablation.py` | standalone 5-arm ablation harness (base/rename/decoy/paraphrase/all); writes `eval/ablation_results.jsonl` |

See [evaluation.md §9](evaluation.md) for the ablation design that consumes these outputs, and [../reference/extension-implementation-plan.md](../reference/extension-implementation-plan.md) for the original step-by-step build spec (note: its decoy sections predate the step-10 corrupted-trap rework — see the banner there).
