# Methodology — Obfuscation Evaluation

## 1. Goal

The obfuscation evaluation answers one question: **does schema identifier obfuscation reduce the advantage that frontier language models may get from memorised BIRD identifiers?**

This project prepares data for an agentic Text-to-SQL setting where an agent builds a semantic memory layer from known true SQL paired with column names and dtypes only. One important contamination threat is identifier-level recall: a frontier language model that memorised BIRD column names (`movie_release_year`, `user_subscriber`) may exploit that recall rather than grounding its answer in the renamed schema. The evaluation probes whether the **rename** dimension (schema renaming) reduces this signal. It does not claim to remove every possible contamination path, such as memorised question wording, literal values, or SQL templates.

---

## 2. Scope Boundary

This project is a **data preparation project**. It does not evaluate whether a downstream agent system can navigate the schema lake — that is left to a downstream evaluation harness. The evaluation here answers only:

1. **Pipeline integrity** — is every obfuscated (question, SQL) pair internally consistent?
2. **Obfuscation effectiveness** — does a frontier language model lose some accuracy advantage when column names are renamed?

The correct database is given to the model upfront in all conditions — routing is not evaluated. How a downstream agent builds and uses memory is also out of scope for this repository; the B-condition (with-memory) design was dropped because the memory format is a downstream architectural decision, and any placeholder here would be premature without a fixed format to target.

---

## 3. Pipeline Integrity Check

The integrity check runs in two stages. See [obfuscation.md §5](obfuscation.md) for how the PostgreSQL instances are constructed.

### Stage 1 — Transpilation correctness (R0==R1)

1. Execute original gold SQL against the original SQLite database → result set R0 (ground truth)
2. Execute transpiled PostgreSQL SQL against `pg_base` → result set R1
3. Assert R0 == R1 (normalized multiset equality); if not, queue for agent repair (pass 2) and re-validate before merge. See [../reference/step5-transpilation.md](../reference/step5-transpilation.md).

### Stage 2 — Rename correctness (R1==R2)

1. Execute validated transpiled SQL against `pg_base` → R1
2. Execute obfuscated SQL against `pg_rename` (renamed DDL, same row data) → R2
3. Assert R1 == R2

R1==R2 checks that the rename map was applied consistently to executable SQL — no table or column references needed for execution were missed. This check covers all retained questions mechanically without requiring any model or human annotator. It does not, by itself, prove that natural-language evidence hints are semantically perfect; hint rewriting is a separate text transformation.

**Why this is sufficient for SQL solvability.** The gold SQL is the witness that the SQL task is solvable. If R1==R2, there exists a valid SQL query for the obfuscated schema that returns the same answer as the validated original PostgreSQL query. Difficulty preservation and downstream agent behaviour remain empirical questions for a downstream evaluation.

---

## 4. Obfuscation Effectiveness Check

### 4.1 Evaluation conditions

Each frontier language model is evaluated under four conditions across two dimensions:

- **Schema** — original BIRD identifiers vs. obfuscated (renamed) identifiers
- **Hints** — evidence hints included or excluded

| Condition | Schema | Hints |
| --------- | ------ | ----- |
| base_hint | Original | Yes |
| base_nohint | Original | No |
| rename_hint | Obfuscated | Yes |
| rename_nohint | Obfuscated | No |

In all conditions the model receives: question text + stripped DDL (column names and dtypes only — no PRIMARY KEY, FOREIGN KEY, CHECK constraints, and no column descriptions) + the correct DB label.

**Why two hint conditions?** 78% of BIRD hints follow the pattern `"X refers to column_name"` — explicit NL-to-column bridges. In the obfuscated condition with hints (rename_hint), the hint contains the renamed column name (e.g. `"released in 1945 refers to annee_sortie = 1945"`), partially guiding the model despite obfuscation. The no-hint condition (rename_nohint) is closest to the downstream test-time setting — the agent receives only the question during testing — and gives the clearest view of identifier obfuscation. Hint conditions are retained for comparability with the BIRD leaderboard, which tracks hint usage per submission.

### 4.2 Primary signal — Contamination delta

| Metric | Definition |
| --- | --- |
| **Delta (no hints)** | EX(base_nohint) - EX(rename_nohint) — primary signal |
| **Delta (hints)** | EX(base_hint) - EX(rename_hint) — BIRD-comparable |

A positive delta is evidence that some of the frontier language model's accuracy on the original schema may have depended on original BIRD identifiers, and that the **rename** dimension reduced that advantage. Delta (no hints) is the primary signal because it is not softened by renamed column names appearing in hint text. Delta (hints) is reported for comparability with standard BIRD leaderboard submissions.

### 4.3 Schema recall probe

The probe runs twice on the same question text (questions are identical in both conditions — they are never modified):

- **Original probe**: prompt the model with the question only, no schema context. Ask it to complete SQL. Measure how often it produces syntactically valid SQL containing correct original BIRD table/column names (e.g. `movie_release_year`). A high rate is evidence of possible identifier memorisation.
- **Obfuscated probe**: prompt the model with the question only and the obfuscated DB label, but still no schema DDL. Ask it to complete SQL using the obfuscated schema identifiers. Correct completions — producing `annee_sortie` or `guojia` without seeing schema DDL — should be much rarer if the renamed identifiers were not present in pretraining data.

The delta between the two probe rates is a direct probe of identifier recall, independent of execution accuracy.

---

## 5. Why a Human Annotator Condition Was Dropped

An earlier design included a human annotator checking obfuscated questions for difficulty preservation. This was dropped for two reasons:

1. **Language barrier.** The **rename** dimension assigns ~80% of databases to non-English schema languages (French, German, Spanish, Mandarin Pinyin). No single annotator could evaluate obfuscated SQL across all five languages. Limiting review to the English-controlled 20% would leave the strongest obfuscation conditions (Pinyin) unvalidated.

2. **The SQL solvability question is answered mechanically.** R1==R2 (§3) checks that a valid obfuscated SQL query exists for each retained example. Difficulty preservation is a downstream concern.

---

## 6. Why a Non-Contaminated Model Condition Was Dropped

An earlier design included a cross-model delta using a model with a pre-BIRD training cutoff as a non-contaminated baseline. This was dropped because no reliably non-contaminated model can be identified — any model with strong Text-to-SQL capability may have encountered BIRD data regardless of stated training cutoff. Contamination status cannot be verified externally.

The schema recall probe (§4.3) provides a more direct signal: it measures identifier recall without requiring a non-contaminated control.

---

## 7. Metrics Summary

| Metric | Measured on |
| --- | --- |
| Pipeline integrity (R0==R1, R1==R2) | 10,164 validated questions (of 10,541 candidates) |
| EX(base_hint) — original, hints | Test set (2,030 questions) |
| EX(base_nohint) — original, no hints | Test set |
| EX(rename_hint) — obfuscated, hints | Test set |
| EX(rename_nohint) — obfuscated, no hints | Test set |
| Contamination delta (base_nohint - rename_nohint) | Derived — primary signal |
| Contamination delta (base_hint - rename_hint) | Derived — BIRD-comparable |
| Schema recall rate (§4.3 probe) | Test set |

All effectiveness metrics broken down by:

- Obfuscation language (English control / French / German / Spanish / Pinyin)
- Collision status (table name shared across ≥2 databases vs. unique)

The per-language breakdown estimates whether Pinyin produces a larger contamination delta than French or Spanish, which informs how much obfuscation strength may be useful in practice. Difficulty breakdown is omitted — the BIRD train split carries no difficulty labels, and the dev difficulty labels are human-assigned on a different scale, making a combined breakdown misleading.

---

## 8. Results

> **Results pending — being re-run.** To ensure the reported numbers are robust, the
> full evaluation is being re-run on a stronger model. No result figures are quoted here
> in the interim; this section will be populated once that run completes.

**Setup for the run:** `pipeline/eval_contamination.py`, one-shot (no retry-on-error, no
feedback loop), over the full test set (2,030 questions × 4 conditions). Raw per-call
records are written to `eval/contamination_results.jsonl` (gitignored, reproducible via
the script). Each row includes an `eval_metadata` block with the model, reasoning effort,
prompt version, git commit, and input artifact hashes; resumability only reuses rows whose
metadata matches the current invocation. Metrics and the per-language / collision
breakdowns are defined in §7.

---

## 9. Ablation study — extended obfuscation layers

The contamination run (§8) measured only the **rename** dimension (identifier rename). This ablation measures the **independent contribution of each obfuscation dimension** to execution-accuracy drop, adding the two dimensions specified in [obfuscation-extensions.md](obfuscation-extensions.md). The harness (`pipeline/eval_ablation.py`) is **implemented**; the full run is **pending** (see [../../PROGRESS.md](../../PROGRESS.md)) and will be executed together with the §8 re-run on the stronger model. No interim numbers are reported.

### 9.1 Arms

All arms are **no-hint** (the cleanest signal — §4.2 makes rename_nohint/base_nohint the primary form; hints soften obfuscation by echoing renamed identifiers). Optionally repeat with hints later for BIRD comparability.

| Arm | Schema (instance) | Gold SQL field | Question | Reuse |
| --- | --- | --- | --- | --- |
| **base** | `pg_base` (5432) | `sql_base` | original | = existing base_nohint |
| **rename** | `pg_rename` (5433) | `sql_rename` | original | = existing rename_nohint |
| **decoy** | `pg_decoy` (5434) | `sql_base` (SELECT\*-expanded) | original | decoy instance |
| **paraphrase** | `pg_base` (5432) | `sql_base` | `question_paraphrase` | paraphrased questions |
| **all** (rename+decoy+paraphrase) | `pg_rename_decoy` (5435) | `sql_rename` (SELECT\*-expanded) | `question_paraphrase` | rename+decoy instance + paraphrases |

### 9.2 What each arm measures

- **rename − base** — identifier recall (as in base_nohint−rename_nohint). **Report this per-language, not pooled:** the 14 English DBs (~23% of rows) are assigned an identity rename map, so their `sql_rename == sql_base` and they contribute a guaranteed-zero rename delta by construction — English is the **noise-floor control**, and a single pooled rename number is structurally diluted by it. See [../reference/limitations.md §1](../reference/limitations.md).
- **decoy − base** — robustness to **corrupted decoy traps** (additive evil-twin columns + corrupted clone tables under synonym names): whether the agent grounds in the real columns/tables rather than grabbing a confusable decoy. Measures difficulty added, not memorisation per se.
- **paraphrase − base** — question-form recall (the SPENCE-sensitive axis).
- **all − base** — combined effect of the maximally-obfuscated dataset.

### 9.3 Reading the numbers

- **Paired, same test set, same model, same run.** Deltas are per-question paired against base; use a paired test (McNemar) and **bootstrap CIs**. Expect small effects, so read the CIs and McNemar p-values rather than the point deltas; use the English control as the empirical null (its measured noise floor), not zero.
- **Different mechanisms, not one strength scale** (§1 of the extensions doc): interpret rename/decoy/paraphrase separately.
- **Not a full factorial.** One-at-a-time + all does **not** isolate interactions: `all − (rename+decoy+paraphrase individual deltas)` is *not* a clean interaction term. A full 2³ = 8-cell factorial would be needed for that; deferred on cost.
- **Grading contract.** Multiset equality against the SELECT\*-expanded gold, so decoy columns never leak into any arm's answer and all arms are compared on the same well-defined result set. The summarizer reports **two EX columns**: *lenient* (`normalise_result`, BIRD-style — coerces types, so `1 == "1" == True`) and *strict* (`normalise_result_strict` — no cross-type collapse, case-sensitive; numeric equality preserved). The leniency is symmetric across arms so it cancels in the deltas, but **quote the strict column for any absolute-EX claim** (see [../reference/limitations.md §2](../reference/limitations.md)). Exclude the qids in `order_sensitive_qids.json` (153 order-sensitive + 21 exec-failed) from strict cross-variant EX: the trap-population `UPDATE`s reorder the heap, so gold with a `LIMIT` and no total order (or a float aggregate) can return a different-but-valid row set on the decoy instances — the real column values are provably intact (physical row order is not; see limitations §"precision notes").
