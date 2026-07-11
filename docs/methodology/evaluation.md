**English** · [中文](evaluation-zh.md)

# Methodology: obfuscation evaluation

## 1. Goal

The obfuscation evaluation answers one question: **does schema identifier obfuscation reduce the advantage that frontier language models may get from memorised BIRD identifiers?**

This project prepares data for an agentic Text-to-SQL setting where an agent builds a semantic memory layer from known true SQL paired with column names and dtypes only. One important contamination threat is identifier-level recall: a frontier language model that memorised BIRD column names (`movie_release_year`, `user_subscriber`) may exploit that recall rather than grounding its answer in the renamed schema. The evaluation probes whether the **rename** dimension (schema renaming) reduces this signal. It does not claim to remove every possible contamination path, such as memorised question wording, literal values, or SQL templates.

---

## 2. Scope boundary

This project is a **data preparation project**. It does not evaluate whether a downstream agent system can navigate the schema lake; that is left to a downstream evaluation harness. The evaluation here answers only:

1. **Pipeline integrity**: is every obfuscated (question, SQL) pair internally consistent?
2. **Obfuscation effectiveness**: does a frontier language model lose some accuracy advantage when column names are renamed?

The correct database is given to the model upfront in all conditions; routing is not evaluated. How a downstream agent builds and uses memory is also out of scope for this repository. The B-condition (with-memory) design was dropped because the memory format is a downstream architectural decision, and any placeholder here would be premature without a fixed format to target.

---

## 3. Pipeline integrity check

The integrity check runs in two stages. See [obfuscation.md §5](obfuscation.md) for how the PostgreSQL instances are constructed.

### Stage 1: transpilation correctness (R0==R1)

1. Execute original gold SQL against the original SQLite database → result set R0 (ground truth)
2. Execute transpiled PostgreSQL SQL against `pg_base` → result set R1
3. Assert R0 == R1 (normalized multiset equality); if not, queue for agent repair (pass 2) and re-validate before merge. See [../reference/step5-transpilation.md](../reference/step5-transpilation.md).

### Stage 2: rename correctness (R1==R2)

1. Execute validated transpiled SQL against `pg_base` → R1
2. Execute obfuscated SQL against `pg_rename` (renamed DDL, same row data) → R2
3. Assert R1 == R2

R1==R2 checks that the rename map was applied consistently to executable SQL: no table or column references needed for execution were missed. This check covers all retained questions mechanically without requiring any model or human annotator. It does not, by itself, prove that natural-language evidence hints are semantically perfect; hint rewriting is a separate text transformation.

**Why this is sufficient for SQL solvability.** The gold SQL is the witness that the SQL task is solvable. If R1==R2, there exists a valid SQL query for the obfuscated schema that returns the same answer as the validated original PostgreSQL query. Difficulty preservation and downstream agent behaviour remain empirical questions for a downstream evaluation.

---

## 4. Obfuscation effectiveness check

### 4.1 Evaluation conditions

Each frontier language model is evaluated under four conditions across two dimensions:

- **Schema**: original BIRD identifiers vs. obfuscated (renamed) identifiers
- **Hints**: evidence hints included or excluded

| Condition | Schema | Hints |
| --------- | ------ | ----- |
| base_hint | Original | Yes |
| base_nohint | Original | No |
| rename_hint | Obfuscated | Yes |
| rename_nohint | Obfuscated | No |

In all conditions the model receives: question text + stripped DDL (column names and dtypes only, with no PRIMARY KEY, FOREIGN KEY, or CHECK constraints, and no column descriptions) + the correct DB label.

**Why two hint conditions?** 78% of BIRD hints follow the pattern `"X refers to column_name"`, an explicit NL-to-column bridge. In the obfuscated condition with hints (rename_hint), the hint contains the renamed column name (e.g. `"released in 1945 refers to annee_sortie = 1945"`), partially guiding the model despite obfuscation. The no-hint condition (rename_nohint) is closest to the downstream test-time setting (the agent receives only the question during testing) and gives the clearest view of identifier obfuscation. Hint conditions are retained for comparability with the BIRD leaderboard, which tracks hint usage per submission.

### 4.2 Primary signal: contamination delta

| Metric | Definition |
| --- | --- |
| **Delta (no hints)** | EX(base_nohint) - EX(rename_nohint); primary signal |
| **Delta (hints)** | EX(base_hint) - EX(rename_hint); BIRD-comparable |

A positive delta is evidence that some of the frontier language model's accuracy on the original schema may have depended on original BIRD identifiers, and that the **rename** dimension reduced that advantage. Delta (no hints) is the primary signal because it is not softened by renamed column names appearing in hint text. Delta (hints) is reported for comparability with standard BIRD leaderboard submissions.

### 4.3 Schema recall probe

The probe runs twice on the same question text (questions are identical in both conditions; they are never modified):

- **Original probe**: prompt the model with the question only, no schema context. Ask it to complete SQL. Measure how often it produces syntactically valid SQL containing correct original BIRD table/column names (e.g. `movie_release_year`). A high rate is evidence of possible identifier memorisation.
- **Obfuscated probe**: prompt the model with the question only and the obfuscated DB label, but still no schema DDL. Ask it to complete SQL using the obfuscated schema identifiers. Correct completions (producing `annee_sortie` or `guojia` without seeing schema DDL) should be much rarer if the renamed identifiers were not present in pretraining data.

The delta between the two probe rates is a direct probe of identifier recall, independent of execution accuracy.

---

## 5. Why a human annotator condition was dropped

An earlier design included a human annotator checking obfuscated questions for difficulty preservation. This was dropped for two reasons:

1. **Language barrier.** The **rename** dimension assigns ~80% of databases to non-English schema languages (French, German, Spanish, Mandarin Pinyin). No single annotator could evaluate obfuscated SQL across all five languages. Limiting review to the English-controlled 20% would leave the strongest obfuscation conditions (Pinyin) unvalidated.

2. **The SQL solvability question is answered mechanically.** R1==R2 (§3) checks that a valid obfuscated SQL query exists for each retained example. Difficulty preservation is a downstream concern.

---

## 6. Why a non-contaminated model condition was dropped

An earlier design included a cross-model delta using a model with a pre-BIRD training cutoff as a non-contaminated baseline. This was dropped because no reliably non-contaminated model can be identified: any model with strong Text-to-SQL capability may have encountered BIRD data regardless of stated training cutoff. Contamination status cannot be verified externally.

The schema recall probe (§4.3) provides a more direct signal: it measures identifier recall without requiring a non-contaminated control.

---

## 7. Metrics summary

| Metric | Measured on |
| --- | --- |
| Pipeline integrity (R0==R1, R1==R2) | 10,164 validated questions (of 10,541 candidates) |
| EX(base_hint): original, hints | Test set (2,030 questions) |
| EX(base_nohint): original, no hints | Test set |
| EX(rename_hint): obfuscated, hints | Test set |
| EX(rename_nohint): obfuscated, no hints | Test set |
| Contamination delta (base_nohint - rename_nohint) | Derived; primary signal |
| Contamination delta (base_hint - rename_hint) | Derived; BIRD-comparable |
| Schema recall rate (§4.3 probe) | Test set |

All effectiveness metrics broken down by:

- Obfuscation language (English control / French / German / Spanish / Pinyin)
- Collision status (table name shared across ≥2 databases vs. unique)

The per-language breakdown estimates whether Pinyin produces a larger contamination delta than French or Spanish, which informs how much obfuscation strength may be useful in practice. Difficulty breakdown is omitted: the BIRD train split carries no difficulty labels, and the dev difficulty labels are human-assigned on a different scale, which would make a combined breakdown misleading.

---

## 8. Results

### Run: **claude opus 4.8 high**

| Field | Value |
| --- | --- |
| Model | `Claude-Opus-4.8` |
| Reasoning effort | `high` |
| Prompt version | `contamination-v1` |
| Split | test (2,030 questions × 4 conditions = 8,120 generations) |
| Recorded | 2026-07-10 (UTC) |
| Git commit | `6b5d9a1` |
| Bundle hash | `requests_sha256 7d38d28c…` |

One-shot generation (no retry-on-error, no feedback loop), graded once against the frozen
PostgreSQL snapshots. All 8,120 generations graded; none skipped.

> **Units.** EX is a fraction in [0, 1] (0.5163 = 51.63% of questions correct). A delta is the
> difference between two EX values, written as a percentage of the test set: +0.0478 = 4.8% (4.8 more questions correct per 100), not
> 0.048%. The same convention holds in §9.4.

#### 8.1 Execution accuracy by condition

| Condition | Lenient EX | Strict EX |
| --- | --- | --- |
| base_hint | 0.5882 (1194/2030) | 0.5655 (1148/2030) |
| base_nohint | 0.5163 (1048/2030) | 0.4956 (1006/2030) |
| rename_hint | 0.5704 (1158/2030) | 0.5488 (1114/2030) |
| rename_nohint | 0.4685 (951/2030) | 0.4507 (915/2030) |

Lenient is BIRD-style type-collapsing equality; strict forbids cross-type matches. Quote
the strict column for any absolute-EX claim (see [../reference/limitations.md §2](../reference/limitations.md)).

#### 8.2 Contamination delta

| Delta | Lenient | Strict |
| --- | --- | --- |
| **No hints** (base_nohint − rename_nohint), primary signal | +0.0478 | +0.0449 |
| Hints (base_hint − rename_hint), BIRD-comparable | +0.0177 | +0.0167 |

Renaming schema identifiers costs Opus 4.8 about 4.8 lenient EX points without hints and
1.8 with hints. Both deltas are positive but small: some of the model's accuracy on the
original schema did lean on memorised BIRD identifiers, but not much of it. This is
consistent with the prior-literature reading that BIRD is only weakly contaminated on the
identifier axis (§1). Hints roughly halve the delta because the obfuscated hint text
echoes the renamed column name and partly re-bridges the gap (§4.1).

#### 8.3 By obfuscation language

The pooled no-hint delta hides a clear gradient. English databases carry an identity
rename map (their `sql_rename == sql_base`), so they are the noise-floor control, not an
obfuscation arm (see [../reference/limitations.md §1](../reference/limitations.md)).

| Language | DBs | n | base_nohint (L / S) | rename_nohint (L / S) | Δ no-hint (L / S) |
| --- | --- | --- | --- | --- | --- |
| english (control) | 14 | 467 | 0.495 / 0.484 | 0.490 / 0.480 | +0.004 / +0.004 |
| french | 14 | 382 | 0.463 / 0.448 | 0.435 / 0.419 | +0.029 / +0.029 |
| spanish | 14 | 438 | 0.573 / 0.555 | 0.523 / 0.507 | +0.050 / +0.048 |
| german | 14 | 351 | 0.524 / 0.487 | 0.464 / 0.430 | +0.060 / +0.057 |
| pinyin | 13 | 392 | 0.523 / 0.497 | 0.418 / 0.403 | +0.105 / +0.094 |

The English control sits at its expected ~0 floor (+0.004), which validates the
measurement: an identity rename produces no delta. The delta then grows monotonically as
the renamed identifiers move away from English: French +0.029, Spanish +0.050, German
+0.060, Pinyin +0.105. Pinyin, the most distant from English orthography, removes the most
identifier advantage (about 10 EX points), roughly 25× the English floor. This supports
using stronger (further-from-English) rename languages when the goal is to suppress
identifier recall.

#### 8.4 Run health

- **Grading outcomes:** of 8,120 records, 3,769 were incorrect: 3,602 result mismatches
  and 167 generated-SQL execution failures; the remaining 4,351 are correct (lenient).
- **Latency:** mean 2.83 s, p50 2.44 s, p95 5.41 s, max 26.44 s (n = 8,120).
- **Tokens:** input 11,567,297; output 1,375,866; total 12,943,163 (no prompt-cache hits
  in this offline run).

#### 8.5 Setup

`pipeline/eval_contamination.py`, one-shot, over the full test set (2,030 questions × 4
conditions), using the default offline workflow: prompts and private gold are frozen on
the PostgreSQL machine, model generations run on an API-only machine, and returned SQL is
graded on the original PostgreSQL snapshot. Raw graded records are written to
`eval/contamination_results.jsonl`. Each row carries an `eval_metadata` block with the
model, reasoning effort, prompt version, git commit, and input artifact hashes;
resumability only reuses rows whose metadata matches the current invocation. Reproduce the
figures above with:

```bash
uv run python pipeline/eval_contamination.py --summarize \
  --model "Claude-Opus-4.8" --effort high --bundle-dir eval/offline/contamination
```

Metrics and the per-language / collision breakdowns are defined in §7. Bootstrap CIs and
McNemar p-values on the paired deltas are not yet computed (planned; see
[../PROGRESS.md](../PROGRESS.md)).

---

## 9. Ablation study: extended obfuscation layers

The contamination run (§8) measured only the **rename** dimension (identifier rename). This ablation measures the **independent contribution of each obfuscation dimension** to execution-accuracy drop, adding the two dimensions specified in [obfuscation.md §7-§11](obfuscation.md). The harness (`pipeline/eval_ablation.py`) uses the same default offline workflow as §8. First results (run `claude opus 4.8 high`) are in §9.4.

### 9.1 Arms

All arms are **no-hint** (the cleanest signal; §4.2 makes rename_nohint/base_nohint the primary form, and hints soften obfuscation by echoing renamed identifiers). Optionally repeat with hints later for BIRD comparability.

| Arm | Schema (instance) | Gold SQL field | Question | Reuse |
| --- | --- | --- | --- | --- |
| **base** | `pg_base` (5432) | `sql_base` | original | = existing base_nohint |
| **rename** | `pg_rename` (5433) | `sql_rename` | original | = existing rename_nohint |
| **decoy** | `pg_decoy` (5434) | `sql_base` (SELECT\*-expanded) | original | decoy instance |
| **paraphrase** | `pg_base` (5432) | `sql_base` | `question_paraphrase` | paraphrased questions |
| **all** (rename+decoy+paraphrase) | `pg_rename_decoy` (5435) | `sql_rename` (SELECT\*-expanded) | `question_paraphrase` | rename+decoy instance + paraphrases |

### 9.2 What each arm measures

- **rename − base**: identifier recall (as in base_nohint−rename_nohint). **Report this per-language, not pooled:** the 14 English DBs (~23% of rows) are assigned an identity rename map, so their `sql_rename == sql_base` and they contribute a guaranteed-zero rename delta by construction. English is the **noise-floor control**, and a single pooled rename number is structurally diluted by it. See [../reference/limitations.md §1](../reference/limitations.md).
- **decoy − base**: robustness to **corrupted decoy traps** (additive evil-twin columns plus corrupted clone tables under synonym names). Does the agent ground in the real columns and tables rather than grab a confusable decoy? This measures difficulty added, not memorisation per se.
- **paraphrase − base**: question-form recall (the SPENCE-sensitive axis).
- **all − base**: combined effect of the maximally-obfuscated dataset.

### 9.3 Reading the numbers

- **Paired, same test set, same model, same run.** Deltas are per-question paired against base; use a paired test (McNemar) and **bootstrap CIs**. Expect small effects, so read the CIs and McNemar p-values rather than the point deltas; use the English control as the empirical null (its measured noise floor), not zero.
- **Different mechanisms, not one strength scale** (§1 of the extensions doc): interpret rename/decoy/paraphrase separately.
- **Not a full factorial.** One-at-a-time + all does **not** isolate interactions: `all − (rename+decoy+paraphrase individual deltas)` is *not* a clean interaction term. A full 2³ = 8-cell factorial would be needed for that; deferred on cost.
- **Grading contract.** Multiset equality against the SELECT\*-expanded gold, so decoy columns never leak into any arm's answer and all arms are compared on the same well-defined result set. The summarizer reports **two EX columns**: *lenient* (`normalise_result`, BIRD-style, which coerces types so `1 == "1" == True`) and *strict* (`normalise_result_strict`: no cross-type collapse, case-sensitive; numeric equality preserved). The leniency is symmetric across arms so it cancels in the deltas, but **quote the strict column for any absolute-EX claim** (see [../reference/limitations.md §2](../reference/limitations.md)). Exclude the qids in `order_sensitive_qids.json` (153 order-sensitive + 21 exec-failed) from strict cross-variant EX: the trap-population `UPDATE`s reorder the heap, so gold with a `LIMIT` and no total order (or a float aggregate) can return a different-but-valid row set on the decoy instances. The real column values are provably intact (physical row order is not; see limitations §"precision notes").

### 9.4 Results

#### Run: **claude opus 4.8 high**

Same model, effort, and test set as §8 (`Claude-Opus-4.8`, effort `high`, 2,030
questions), prompt version `ablation-v1`, git commit `674d6a7`, recorded 2026-07-11 (UTC).
Each arm was prepared as its own offline bundle, generated one-shot on an API machine, and
graded here against the arm's PostgreSQL instance. All 5 × 2,030 = 10,150 generations
graded; none skipped. Before grading the two decoy-based arms, gold-on-decoy was
spot-checked against the clean instance (40/40 identical per arm), confirming the additive
traps do not hide the correct answer.

**Execution accuracy by arm:**

| Arm | Lenient EX | Strict EX |
| --- | --- | --- |
| base | 0.5113 (1038/2030) | 0.4916 (998/2030) |
| rename | 0.4700 (954/2030) | 0.4527 (919/2030) |
| decoy | 0.4892 (993/2030) | 0.4690 (952/2030) |
| paraphrase | 0.5463 (1109/2030) | 0.5256 (1067/2030) |
| all | 0.4532 (920/2030) | 0.4389 (891/2030) |

**Paired deltas vs base** (per-question paired, n = 2,030; lenient point delta with
bootstrap 95% CI and McNemar p; discordant pairs b/c = base-right→arm-wrong /
base-wrong→arm-right; strict delta for reference):

| Arm | Δ lenient | 95% CI | McNemar p | disc b/c | Δ strict |
| --- | --- | --- | --- | --- | --- |
| rename | −0.0414 | [−0.0557, −0.0276] | <0.001 | 154/70 | −0.0389 |
| decoy | −0.0222 | [−0.0350, −0.0094] | 0.0010 | 112/67 | −0.0227 |
| paraphrase | **+0.0350** | [+0.0182, +0.0512] | <0.001 | 116/187 | +0.0340 |
| all | −0.0581 | [−0.0768, −0.0384] | <0.001 | 264/146 | −0.0527 |

**Per-language EX by arm** (lenient; n per language: english 467, french 382, german 351,
pinyin 392, spanish 438):

| Language | base | rename | decoy | paraphrase | all |
| --- | --- | --- | --- | --- | --- |
| english (control) | 0.497 | 0.495 | 0.469 | 0.544 | 0.512 |
| french | 0.474 | 0.435 | 0.461 | 0.487 | 0.414 |
| german | 0.507 | 0.464 | 0.510 | 0.547 | 0.442 |
| pinyin | 0.520 | 0.429 | 0.477 | 0.538 | 0.378 |
| spanish | 0.555 | 0.516 | 0.530 | 0.607 | 0.502 |

**Reading (each mechanism separately, per §9.3):**

- **rename −4.1%** (McNemar p < 0.001). Consistent with the §8 contamination no-hint
  delta (+4.8%) measured on the same model: the small identifier-recall effect
  replicates. The English control is flat (0.497 → 0.495, its noise floor), and the delta
  grows away from English, largest for pinyin (0.520 → 0.429).
- **decoy −2.2%** (p = 0.001). Corrupted decoy traps cost about two points: the model
  mostly grounds in the real columns and tables but sometimes grabs a confusable decoy.
  Gold still resolves correctly on the decoy instance (verified above), so this is added
  difficulty, not a broken task.
- **paraphrase +3.5%** (p < 0.001): **positive**, an honest negative result for the
  question-form-recall hypothesis on this model. The cheap-model, SQL-conditioned
  paraphrases slightly *help* rather than hurt, most likely because they clean up ambiguous
  BIRD phrasing (116 questions went right→wrong but 187 went wrong→right). So paraphrasing
  as implemented does not expose memorised question wording here; if anything the original
  phrasing was marginally harder. Caveat: this also means the paraphrase dimension is not a
  clean obfuscation lever on this data: it changes difficulty in the easier direction.
- **all −5.8%** (p < 0.001), the largest drop. rename and decoy compound while
  paraphrase's positive contribution partly offsets them; the net is still clearly
  negative, and pinyin-all is lowest overall (0.378). Per §9.3 this is not a clean
  interaction term.

**Reproduce:**

```bash
uv run python pipeline/eval_ablation.py --summarize \
  --model "Claude-Opus-4.8" --effort high --bundle-dir eval/offline/ablation-base
```

(Any one arm's freshly-prepared bundle works as the metadata reference; `metadata_matches`
keys on model/effort/prompt-version/commit/dataset hashes, not the per-arm request hash.)
Raw graded records are in `eval/ablation_results.jsonl`.
