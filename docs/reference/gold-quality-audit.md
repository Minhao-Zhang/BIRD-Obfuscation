**English** · [中文](gold-quality-audit-zh.md)

# Gold-quality audit: BIRD annotation errors and the 2026-07-29 question purge

**Status:** APPLIED. 2,739 of 10,164 questions (27.0%) were removed on 2026-07-29, then 11
databases that fell below the `MIN_QUESTIONS = 60` floor were dropped and the remainder
re-split. Final dataset: **58 databases, 6,928 questions (5,539 train / 1,389 test).** The four
published PostgreSQL dumps are unaffected and were **not** rebuilt.

This document records why the dataset shrank, how the affected questions were identified, how the
splits were rebuilt, what was deliberately left alone, and which decisions are still open.

---

## 1. The finding

The gold SQL this dataset was built from — BIRD 2023 `train` + `dev` — is substantially
mis-annotated, and the annotation errors are *systematic* rather than random. Three
independent audits agree, and two official `birdsql` releases published after BIRD 2023
supersede or withdraw a large fraction of the original gold.

| Audit | Split | n | Error rate |
| --- | --- | --- | --- |
| Wretblad et al. 2024 (§5 [1]) | `dev`, `financial` domain | 106 | 49% any noise; 20.7% wrong gold SQL |
| Jin et al. 2026 ([2]) | Mini-Dev | 498 | **52.8%** |
| Jin et al. 2026 ([2]) | `dev` sample | 100 | 48% required correction |
| Zhu et al. 2026 ([4]) | **`train`** | 2,500 | **61.1%** |
| Pourreza & Rafiei 2023 (reported via [4]) | `train` subset | — | 18.2% |

Jin et al.'s taxonomy, as a share of the erroneous examples (multi-label): E2
schema/data misunderstanding 57.8%, E4 ambiguous question 29.7%, E1 question↔SQL
semantic mismatch 29.3%, E3 domain-knowledge error 10.7%. Recurring concrete patterns
include missing `DISTINCT`, wrong join conditions, `BETWEEN` used for strict
inequalities, and the `california_schools` gold omitting `rtype = 'S'` so that districts
are counted as schools.

The consequences for leaderboards are severe: re-evaluating 16 BIRD leaderboard agents on
100 corrected `dev` examples moved execution accuracy by −7% to +31% relative and rankings
by −9 to +9 positions, and rank correlation between the original leaderboard and the
corrected subset was rs = 0.32 (p = 0.23) — statistically indistinguishable from unrelated
([2]).

### Why this matters more here than for a per-question benchmark

This dataset measures whether an agent can build a **semantic layer** from prior
(question, SQL) pairs and apply it to unseen questions. Under that paradigm the train
split is not gradient-training data — it is the evidence corpus from which schema
semantics are induced. A wrong gold SQL there does not waste one row; it teaches a wrong
mapping that propagates into every later question touching the same concept. Because
BIRD's errors recur consistently within a database (the `rtype` omission, the `BETWEEN`
misuse), they are exactly the regularity an inductive learner will absorb as a rule.
Annotation noise is therefore *amplified by consistency*, not diluted by volume.

---

## 2. Upstream releases used

Both are official `birdsql` publications, CC-BY-SA-4.0 (the same licence as this repo).

| Release | Rows | Nature |
| --- | --- | --- |
| [`birdsql/bird_sql_dev_20251106`](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106) ([6]) | 1,534, 11 DBs | **Corrected** dev. `question_id` 0–1533 contiguous, so it joins to dev-origin rows directly. |
| [`birdsql/bird23-train-filtered`](https://huggingface.co/datasets/birdsql/bird23-train-filtered) ([7]) | 6,601 of 9,428 | **Filtered** train. No `question_id`; join on `(db_id, normalised question)`. |

The distinction is load-bearing. `dev_20251106` **corrects** gold SQL. `bird23-train-filtered`
**deletes** questions BIRD would not stand behind and corrects almost nothing — of the 6,292
rows that matched this dataset, only 6 (0.1%) had different SQL. For train-origin questions,
absence from the filtered release is the entire signal; no corrected train gold exists
upstream.

[`birdsql/bird_mini_dev`](https://huggingface.co/datasets/birdsql/bird_mini_dev) ([8]) was
also examined and **not** used: it is built on the *original* dev and so carries the same
annotation errors, and `dev_20251106` supersedes it. Its `mini_dev_pg` split did serve as an
independent check on this repo's SQLite→PostgreSQL transpilation (step 05): across the 499
questions overlapping this dataset, its PostgreSQL gold is semantically identical to our
`sql_base`, differing only in identifier quoting, schema qualification, alias labels, and
`substr` vs `substring`.

---

## 3. Method

`pipeline/build_gold_quality_flags.py` emits one row per `question_id` into
`eval_dataset/gold_quality_flags.jsonl`, joining as follows:

- **dev-origin** (the 11 BIRD dev databases): join on `question_id`. All 1,531 dev-origin
  rows matched. A row is flagged when the `dev_20251106` gold SQL differs from our
  `sql_sqlite` after whitespace/case/trailing-semicolon normalisation.
- **train-origin** (the other 58 databases): join on `(db_id, question)` with the question
  normalised to lowercase alphanumerics. A row is flagged when it is **absent** from
  `bird23-train-filtered`.

Drop ratios corroborate that absence means deletion rather than text edits: BIRD removed
30.0% of train globally (9,428 → 6,601), 27.1% of our train-origin rows are missing, and
only 0.1% of matched rows differ in SQL.

`pipeline/apply_gold_quality_filter.py` then rewrites every question-keyed artifact,
keeping `clean: true` rows only.

---

## 4. What was removed

| Split | Before | After | Removed |
| --- | --- | --- | --- |
| `train_final.jsonl` | 8,134 | **5,984** | 2,150 — 1,837 filter-dropped, 313 dev-changed |
| `test_final.jsonl` | 2,030 | **1,441** | 589 — 504 filter-dropped, 85 dev-changed |
| **Total** | **10,164** | **7,425** | **2,739 (27.0%)** |

Also filtered, all keyed on `question_id`: `question_paraphrases.jsonl` (10,164 → 7,425),
`gold_result_hashes_rename_decoy.jsonl` (10,164 → 7,425), `gold_star_expanded.jsonl`
(5 → 3), and the exclusion lists in `order_sensitive_qids.json` (`order_sensitive`
153 → 104, `exec_failed` 21 → 10).

`gold_quality_flags.jsonl` retains all 10,164 rows as the audit trail, including the
corrected `dev1106_gold_sql` for each of the 398 dev rows whose gold changed, so those
corrections can be adopted later without re-downloading anything.

### Per-database survival

7,425 questions remain across all 69 databases, but 11 now fall below step 01's
`MIN_QUESTIONS = 60` floor:

```
app_store                34/54    financial               38/106   retail_world         38/43
music_platform_2         40/69    college_completion      45/76    california_schools   46/89
debit_card_specializing  47/64    cookbook                49/69    bike_share_1         51/111
computer_student         53/72    software_company        56/75
```

`financial` (36% survive), `california_schools` (52%) and `bike_share_1` (46%) are exactly
the domains the literature flags as noisiest — `financial` losing 64% of its questions is
Wretblad et al.'s 49%-noise finding reproducing independently in our data. Note that
`app_store` (54) and `retail_world` (43) were **already** below 60 before this filter, from
step 05/07 validation attrition.

---

## 4b. Restoring the floor and re-splitting

The purge left two step-01 invariants broken: 11 databases had dropped below
`MIN_QUESTIONS = 60`, and because the purge hit train and test rows unevenly the per-database
test fraction had drifted to **12–26%** rather than a uniform 20% (`bike_share_1` was down to
6 test questions, `retail_world` to 6, `menu` to 10 — too few to estimate anything per schema).

`pipeline/resplit_after_purge.py` restores both. The decision taken was to **keep step 01's
original criterion unchanged** — a database needs ≥60 questions actually present — rather than
relax the floor or switch to a capped test size. The distribution argued both ways: bins 45–129
are continuously occupied with no natural gap, and the `<60` cut sheds 16% of schema diversity to
remove 6.7% of questions. The floor was kept anyway for consistency with the published
methodology, and because at ≥60 a proportional 20% still yields ≥12 test questions per schema.

The split mechanism is step 01's, reused verbatim rather than reimplemented: per-database
`Random(SEED ^ crc32(db_id))` with `SEED = 42` and `n_test = max(1, round(n * 0.20))`. The
per-database seed matters — a single shared `Random(42)` would apply one identical permutation
index-for-index across every database, correlating the split with any positional structure in
BIRD's source JSON.

| | Value |
| --- | --- |
| Pool after purge | 7,425 questions / 69 databases |
| Databases dropped (< 60 surviving) | 11 — 497 questions |
| **Databases retained** | **58** |
| **train_final.jsonl** | **5,539 (80.0%)** |
| **test_final.jsonl** | **1,389 (20.0%)** |
| Per-database test fraction | 0.195 – 0.206 (was 0.12 – 0.26) |
| Smallest test sets | `sales_in_weather` 12, `social_media` 12, `airline` 13 |

Dropped: `app_store` (34), `financial` (38), `retail_world` (38), `music_platform_2` (40),
`college_completion` (45), `california_schools` (46), `debit_card_specializing` (47),
`cookbook` (49), `bike_share_1` (51), `computer_student` (53), `software_company` (56).

Companions were filtered to the surviving qid set: `question_paraphrases.jsonl` and
`gold_result_hashes_rename_decoy.jsonl` 7,425 → 6,928, `order_sensitive` 104 → 98,
`exec_failed` 10 (unchanged), `gold_star_expanded.jsonl` 3 (unchanged).

Rows were **reassigned, never edited** — `sql_sqlite` / `sql_base` / `sql_rename` carry through
untouched, so R0==R1 and R1==R2 still hold and no re-transpilation ran.

`artifacts/retained_dbs.json` was deliberately **not** reduced: it describes the 69 schemas
physically present in the four published dumps, which are unchanged. The evaluated subset is a
new artifact, `evaluated_dbs.json` (58 databases). The 11 dropped databases therefore remain in
the dumps as **unreferenced schemas**. That is a deliberate open choice, not an oversight — to an
agent that explores the database they are either free distractors or wasted exploration budget.
Decide and document which before the next agent run.

Verified after the resplit: all 58 databases present in **both** splits, train/test disjoint,
minimum per-database count exactly 60, every row `clean: true`, and no dropped qid leaking into
any companion file.

---

## 5. What was deliberately not touched

- **The four PostgreSQL dumps.** `pg_base`, `pg_rename`, `pg_decoy` and `pg_rename_decoy`
  on [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) ([5]) are
  question-agnostic: the published repo contains only the dumps, `SHA256SUMS.txt` and a
  README — no question files. Nothing there needs re-uploading, and the dumps are shared
  unchanged across the pre- and post-purge question manifests.
- **All schema-level artifacts:** `schema_rename_map.json`, `trap_manifest.json`,
  `trap_table_manifest.json`, `db_language_map.json`. None depends on
  which questions are retained. The 1,486 evil-twin columns and 162 clone tables are unchanged.
- **No re-transpilation, no re-validation, no database rebuild.** Surviving rows keep their
  existing train/test assignment and their already-validated `sql_base` / `sql_rename`, so
  steps 04–08 and 10 did not re-run and the R0==R1 / R1==R2 guarantees still hold for every
  retained row.
- **Historical eval results.** The numbers in
  [docs/methodology/evaluation.md](../methodology/evaluation.md) §8–§9 are facts about runs
  over the old 2,030-question test set. They are marked superseded rather than rewritten.

---

## 6. Open decisions

1. **Resolve the 398 dev-changed rows by execution, not text diff.** The
   `dev1106_gold_sql_changed` flag is textual difference after normalisation; some of those
   398 are semantically equivalent rewrites (e.g. `question_id` 0 is a `WITH` restructure).
   Executing old vs new gold against the SQLite sources and comparing result hashes — the
   comparison step 07 already performs — would reclassify the equivalent ones as clean.
   `california_schools` (46) and `debit_card_specializing` (47) are close enough to any
   threshold that this check alone may decide whether they survive. **Do this before
   re-splitting.**
2. ~~**Re-split.**~~ **DONE** — see §4b. The floor was kept at ≥60 present questions and the
   80/20 per-database split was rebuilt with step 01's mechanism. The alternative considered and
   rejected was a capped test size (`test = min(25, 30% of n)`) with a lower corpus floor, which
   would have equalised test precision across schemas and returned ~50 test rows from
   `works_cycles` alone to the corpus. Worth revisiting if per-schema estimates turn out to be
   the primary unit of analysis, since test-set size still varies 12–78 across schemas.
3. **Report per-database corpus size as a covariate.** The purge shrank databases very
   unevenly (`financial` −64%, `retail_world` −12%) before the floor removed the worst-hit ones,
   and the retained 58 still span 60–383 questions — a 6× range. Without publishing corpus size,
   a per-database accuracy difference cannot be separated from a corpus-size difference.
4. **Measure cross-split near-duplicate leakage.** The split is random per database at seed
   42, and BIRD contains many templated near-identical questions within a database. A corpus
   question that near-duplicates a test question lets an agent retrieve instead of induce,
   inflating exactly the capability being measured. Worth quantifying before finalising a
   new split.

---

## 7. Reproducing

The upstream inputs are gitignored (`artifacts/upstream/`), as raw BIRD under `data/` is.
Re-download them with:

```bash
huggingface-cli download birdsql/bird_sql_dev_20251106 --repo-type dataset \
  --local-dir artifacts/upstream --include 'data/*'
huggingface-cli download birdsql/bird23-train-filtered --repo-type dataset \
  --local-dir artifacts/upstream --include 'data/*'
```

Then:

```bash
python pipeline/build_gold_quality_flags.py
python pipeline/apply_gold_quality_filter.py --dry-run
python pipeline/apply_gold_quality_filter.py
python pipeline/resplit_after_purge.py --dry-run
python pipeline/resplit_after_purge.py
python eval_dataset/build_eval_dataset.py
```

`resplit_after_purge.py` is idempotent: re-running it on the already-resplit tree drops no further
databases and reproduces the same assignment, because the per-database seed depends only on
`db_id`.

`apply_gold_quality_filter.py` is **not** idempotent against an already-filtered tree in the
sense of producing further changes — it is a no-op — but it is destructive on first run.
Recover pre-purge question files from git history (the commit tagged in §8) if needed.

---

## 8. Provenance

Purge applied 2026-07-29 against `gold_quality_flags.jsonl` derived from
`bird_sql_dev_20251106` and `bird23-train-filtered` as published at that date. The
pre-purge question manifest is recoverable from the parent of the commit that carries this
document.

---

## References

**Papers**

1. Niklas Wretblad, Fredrik Riseby, Rahul Biswas, Amin Ahmadi, Oskar Holmström.
   "Understanding the Effects of Noise in Text-to-SQL: An Examination of the BIRD-Bench
   Benchmark." *Proceedings of the 62nd Annual Meeting of the ACL (Volume 2: Short Papers)*,
   pp. 356–369, 2024. ACL ID `2024.acl-short.34`, DOI
   [10.18653/v1/2024.acl-short.34](https://doi.org/10.18653/v1/2024.acl-short.34) ·
   [arXiv:2402.12243](https://arxiv.org/abs/2402.12243) ·
   [code](https://github.com/niklaswretblad/the-effects-of-noise-in-text-to-SQL)
2. Tengjun Jin, Yoojin Choi, Yuxuan Zhu, Daniel Kang. "Pervasive Annotation Errors Break
   Text-to-SQL Benchmarks and Leaderboards." *PVLDB* 2026.
   [arXiv:2601.08778](https://arxiv.org/abs/2601.08778) · DOI
   [10.14778/3796195.3796206](https://doi.org/10.14778/3796195.3796206) ·
   [code and corrected Mini-Dev data](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks)
3. Jin et al. (same group). "Text-to-SQL Benchmarks are Broken: An In-Depth Analysis of
   Annotation Errors." *CIDR* 2026.
   [paper](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf) ·
   [listing](https://www.vldb.org/cidrdb/2026/text-to-sql-benchmarks-are-broken-an-in-depth-analysis-of-annotation-errors.html).
   Companion to [2]; cited separately because the titles differ and only [2] was read in full.
4. Yuxuan Zhu, Tengjun Jin, Yoojin Choi, Daniel Kang. "ReViSQL: Achieving Human-Level
   Text-to-SQL." March 2026. [arXiv:2603.20004](https://arxiv.org/abs/2603.20004) ·
   [BIRD-Platinum / BIRD-Verified data](https://github.com/uiuc-kang-lab/ReViSQL).
   Source of the 61.1% train-split error rate and the +8.2–13.9 point gain from training on
   corrected data. Also reports Pourreza & Rafiei (2023)'s earlier 18.2% train-subset figure,
   cited here at second hand.

**Datasets**

5. `minhaozhang/BIRD_Obfuscation` — this project's published PostgreSQL dumps.
   [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation). CC-BY-SA-4.0.
6. `birdsql/bird_sql_dev_20251106` — official corrected BIRD dev split, released 2025-11-06.
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106). CC-BY-SA-4.0.
7. `birdsql/bird23-train-filtered` — official filtered BIRD 2023 train split, 6,601 rows.
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird23-train-filtered). CC-BY-SA-4.0.
8. `birdsql/bird_mini_dev` — 500-question dev subset in SQLite / MySQL / PostgreSQL dialects.
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird_mini_dev). CC-BY-SA-4.0.
   Examined, not used for filtering.
9. `uiuc-kang-lab/ReViSQL` — BIRD-Platinum (formerly BIRD-Verified), 2,462 expert-corrected
   BIRD train instances. [GitHub](https://github.com/uiuc-kang-lab/ReViSQL). No licence
   stated upstream. Not used here — see §6.1 — but the only corrected train gold in existence.

**Upstream project**

10. BIRD-SQL benchmark. [bird-bench.github.io](https://bird-bench.github.io/) — carries the
    May 2025 acknowledgement of dev-set issues and the November 2025 `bird-sql-dev-1106`
    release announcement.
