**English** · [中文](extension-implementation-plan-zh.md)

# Extension implementation plan: decoy injection, question paraphrase, ablation

**Audience:** an engineer implementing the extended obfuscation layers for the first time. This is the **build spec**. The *design and rationale* live in [../methodology/obfuscation-extensions.md](../methodology/obfuscation-extensions.md) and [../methodology/evaluation.md §9](../methodology/evaluation.md); the *ablation numbers* to reproduce are in evaluation.md §8/§9. Read those first, then follow this doc top to bottom.

> **⚠️ Partially superseded (2026-07-04). Read this first.** The **decoy** parts of this
> plan (§0 decoy parameters and §5 "Step 08: `08_inject_decoys.py`") describe *empty /
> structural* decoy tables + columns. That approach was reworked into **corrupted decoy
> traps** (`pipeline/10_inject_traps.py`, step 10): decoys are now *populated* with subtly
> **corrupted copies of real data** (additive "evil-twin" columns and corrupted clone
> tables) because empty decoys unmask themselves under an interactive execute-and-observe
> agent. Anywhere below that says decoys "stay empty" / are "invisible in stripped DDL" /
> that R1==R2 holds "because decoys are unreferenced/empty" is **superseded**; the current
> guarantee is that traps are **strictly additive** (real rows/columns/tables byte-identical).
> Step 08 still runs (it seeds `decoy_map.json` + the `SELECT *` expansion), but the canonical
> decoy ground truth is now `trap_manifest.json` + `trap_table_manifest.json`. The
> shared-helper refactors (§2), the four instances (§3), paraphrase (§6), and the ablation
> (§7) are as-built. **Current decoy design: [corrupted-decoys-design.md](corrupted-decoys-design.md).**

**Prerequisites (must already be true):**
- Core pipeline complete: `artifacts/train_final.jsonl` and `artifacts/test_final.jsonl` exist (8,134 / 2,030 rows).
- `pg_base` (5432) and `pg_rename` (5433) are built and healthy (`docker compose up -d`).
- `.env` has `OPENAI_API_KEY` (same one `eval_contamination.py` uses).
- `uv` environment works (`uv run python -c "import sqlglot, psycopg2, openai"`).

**Golden rule:** never mutate `pg_base` / `pg_rename` or the existing `*_final.jsonl`. All new work goes to **new instances and new artifacts**. The core deliverable stays immutable.

---

## 0. Methodology coverage check

The methodology is **fully documented at the design level**. Everything below is *implementation* detail, not new methodology. These are the **open parameters** (defaults given, safe to proceed with them):

| Parameter | Default | Where it bites |
| --- | --- | --- |
| Cheap model for generation | `gpt-5-mini` (fallback `gpt-4o-mini`) | steps 08, 09 |
| Decoy tables per DB | +30-50% of real table count, min 2, cap 15 | step 08 |
| Decoy columns per real table | 1-3 confusable columns | step 08 |
| Populate decoy tables with rows? | ~~**No** (empty; invisible in stripped DDL)~~ **SUPERSEDED → Yes**, with corrupted data (step 10 traps) | step 08 → 10 |
| Paraphrase scope | **test set only** (all the ablation needs); train optional for downstream | step 09 |
| Decoy generation for the two instances | generate **per-variant** (English on `pg_decoy`, target-lang on `pg_rename_decoy`) | step 08 |
| Optional cosine drift sanity check | off (use OpenAI `text-embedding-3-small` if enabled, no new dependency) | step 09 |

---

## 1. Repo changes at a glance

| Path | Action | Purpose |
| --- | --- | --- |
| `pipeline/_db.py` | **new** | shared `normalise_result`, `exec_pg`, `ResultSetTooLarge`, DSNs, `new_connection` (kills the 7-file copy-paste) |
| `pipeline/_eval_helpers.py` | **new** | shared eval logic extracted from `eval_contamination.py` (LLM call, prompt build, DDL cache, resumability) |
| `pipeline/eval_contamination.py` | **edit** | import from the two helpers above; unchanged behaviour (contamination eval) |
| `pipeline/08_inject_decoys.py` | **new** | generate `decoy_map.json`, inject into decoy instances, expand `SELECT *`, re-validate R1==R2 |
| `pipeline/09_paraphrase_questions.py` | **new** | generate `question_paraphrases.jsonl` (cheap LLM) |
| `pipeline/eval_ablation.py` | **new** | the 5-arm ablation run; imports `_eval_helpers` + `_db` |
| `docker-compose.yml` | **edit** | add `pg_decoy` (5434) + `pg_rename_decoy` (5435) + their volumes |
| `artifacts/decoy_map.json` | **new (generated)** | canonical decoy definitions per DB per variant |
| `artifacts/gold_star_expanded.jsonl` | **new (generated)** | `SELECT *`-expanded gold for the ~5 affected questions |
| `artifacts/question_paraphrases.jsonl` | **new (generated)** | `question_id → question_paraphrase` |
| `eval/ablation_results.jsonl` | **new (generated)** | one record per `(question_id, arm)` |
| `AGENTS.md` | **edit (after code lands)** | add steps 08/09 + `eval_ablation.py` to the run table and DB bring-up |
| `PROGRESS.md` | **edit (as you go)** | tick off tasks, log decisions |

**No existing files are renamed or deleted.** The only "moves" are the two refactors in §2 (pulling shared code out of existing files into new `_db.py` / `_eval_helpers.py`). Existing field names (`sql_base`, `sql_rename`, …) stay as-is.

---

## 2. Refactor first (do this before writing 08/09)

`normalise_result` / `exec_pg` / the DSN constants are currently copy-pasted across `07_rename_sql_and_validate.py`, `eval_contamination.py`, and `_transpile_helpers.py` (with a "keep these in sync" comment, a smell). Steps 08/09/ablation all need them, so consolidate **once**:

### 2a. `pipeline/_db.py` (new)
Move (don't re-invent) the canonical versions here:
```python
PG_BASE_DSN         = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"
PG_RENAME_DSN       = "host=127.0.0.1 port=5433 dbname=bird user=bird password=bird"
PG_DECOY_DSN        = "host=127.0.0.1 port=5434 dbname=bird user=bird password=bird"
PG_RENAME_DECOY_DSN = "host=127.0.0.1 port=5435 dbname=bird user=bird password=bird"
MAX_RESULT_ROWS = 200_000
QUERY_TIMEOUT_SEC = 60

class ResultSetTooLarge(Exception): ...
def normalise_result(rows) -> list: ...      # verbatim from 07_rename_sql_and_validate.py
def exec_pg(conn, sql): ...                   # fetchmany(MAX_RESULT_ROWS+1) + cap, autocommit-safe
def new_connection(dsn, autocommit=True):     # SET statement_timeout (plain SET, not SET LOCAL)
```
Then update `07_rename_sql_and_validate.py` and `eval_contamination.py` to `from _db import ...`. **Do not change behaviour**: `normalise_result` must stay byte-for-byte identical (grading semantics depend on it). Run the existing pipeline once after the refactor to confirm nothing moved. Respect the AGENTS.md invariants (127.0.0.1 not localhost; plain `SET` under autocommit; `fetchmany` not `fetchall`).

### 2b. `pipeline/_eval_helpers.py` (new)
Extract from `eval_contamination.py` the pieces `eval_ablation.py` will reuse: `get_schema_ddl`, `build_prompt`, `extract_sql`, `usage_dict`, `SYSTEM_INSTRUCTIONS`, `load_done_keys`/`append_result`, and the `run_one` core. Leave `eval_contamination.py` as a thin contamination-eval entrypoint importing them. This mirrors the existing `_transpile_helpers.py` / `_pg_helpers.py` convention.

> If time-boxed, 2a is mandatory (08/09/ablation all execute SQL); 2b is a nice-to-have (you could instead copy eval_contamination.py to eval_ablation.py and extend it, accepting the duplication).

---

## 3. PostgreSQL requirements

### 3a. Instances

| Instance | Port | Volume | Identifiers | Decoys | Built by |
| --- | --- | --- | --- | --- | --- |
| `pg_base` | 5432 | `pg_base_data` | original | no | steps 4 (existing) |
| `pg_rename` | 5433 | `pg_rename_data` | renamed | no | step 6 (existing) |
| **`pg_decoy`** | **5434** | `pg_decoy_data` | original | English | clone of `pg_base` + step 08 |
| **`pg_rename_decoy`** | **5435** | `pg_rename_decoy_data` | renamed | target-lang | clone of `pg_rename` + step 08 |

### 3b. `docker-compose.yml` additions: **DONE**
`pg_decoy` (5434) and `pg_rename_decoy` (5435) are in `docker-compose.yml`, each a copy of its clean counterpart (same WAL-tuning `command`/`healthcheck`) plus `profiles: ["decoy"]`. The profile keeps the core-pipeline bring-up unchanged: `docker compose up -d` still starts only the two clean instances; the decoy pair starts only with `--profile decoy`. Verify: `docker compose config --services` → 2, `docker compose --profile decoy config --services` → 4.

### 3c. Build the decoy instances by cloning (run once, before step 08)
Same read-only clone pattern as step 6 (the `:ro` source mount is the safety guarantee, see [pipeline-invariants.md](pipeline-invariants.md)). The decoy targets must be **stopped** while their volume is cloned (a running Postgres holds the volume open); the profile means they aren't auto-started, but stop them explicitly to be safe:
```bash
docker compose up -d pg_base pg_rename                 # ensure sources exist
docker compose --profile decoy stop pg_decoy pg_rename_decoy  # ensure targets are down
# clone pg_base -> pg_decoy (source must be quiescent)
docker compose stop pg_base
docker run --rm -v pg_base_data:/from:ro -v pg_decoy_data:/to alpine \
  sh -c "rm -rf /to/* && cp -a /from/. /to/"
docker compose start pg_base
# clone pg_rename -> pg_rename_decoy
docker compose stop pg_rename
docker run --rm -v pg_rename_data:/from:ro -v pg_rename_decoy_data:/to alpine \
  sh -c "rm -rf /to/* && cp -a /from/. /to/"
docker compose start pg_rename
docker compose --profile decoy up -d pg_decoy pg_rename_decoy
```
(Confirm the exact prefixed volume names with `docker volume ls`.) After this, `pg_*_decoy` are byte-identical clones; step 08 adds the decoys in place. **Re-running the clone resets a decoy volume → re-run step 08 afterward.**

---

## 4. New artifacts (schemas)

### `artifacts/decoy_map.json`
Canonical, regeneratable, seeded. One entry per DB, **per variant** so each instance gets language-matched names:
```json
{
  "movies_4": {
    "base": {
      "decoy_tables": [
        {"name": "distributor", "columns": [{"name": "distributor_id", "type": "integer"},
                                             {"name": "region", "type": "text"}]}
      ],
      "decoy_columns": {
        "movie": [{"name": "release_year_est", "type": "integer", "mimics": "movie_release_year"}]
      }
    },
    "rename": {
      "decoy_tables": [ ... target-language names ... ],
      "decoy_columns": { "<renamed_table>": [{"name": "...", "type": "...", "mimics": "<renamed_col>"}] }
    }
  }
}
```
`mimics` records which real column a confusable decoy shadows (for later analysis; not used at inject time).

### `artifacts/gold_star_expanded.jsonl`
Only the ~5 questions whose gold has a real-table `SELECT *` (see the measurement in obfuscation-extensions.md §2). Produced by step 08:
```json
{"question_id": "train_8505", "sql_base_expanded": "SELECT \"col1\", ... FROM ...",
 "sql_rename_expanded": "SELECT \"...\", ... FROM ..."}
```

### `artifacts/question_paraphrases.jsonl`
Produced by step 09 (kept separate from `*_final.jsonl` so the deliverable stays immutable; the ablation joins on `question_id`):
```json
{"question_id": "train_5093", "question_paraphrase": "..."}
```

### `eval/ablation_results.jsonl`
One record per `(question_id, arm)`, resumable, same shape as `eval/contamination_results.jsonl` plus an `arm` field (`base|rename|decoy|paraphrase|all`).

---

## 5. Step 08: `08_inject_decoys.py`

> **Superseded for the decoy payload (see top banner).** Step 08 as described here injects
> *empty* decoy tables/columns. It still runs to produce `decoy_map.json` and
> `gold_star_expanded.jsonl`, but the decoys are now *populated with corrupted data* by
> **step 10** (`10_inject_traps.py`); see [corrupted-decoys-design.md](corrupted-decoys-design.md).
> "decoy tables **empty**" and "expected: **zero** … because decoys are unreferenced" below
> reflect the old design. R1==R2 now holds because the traps are strictly additive.

**Purpose:** generate decoys, inject them into the two `*_decoy` instances, expand the handful of `SELECT *` gold queries, and prove nothing broke (R1==R2 against the decoy instances).

**Inputs:** `artifacts/retained_dbs.json`, `artifacts/db_language_map.json`, `artifacts/schema_rename_map.json`, `artifacts/{train,test}_final.jsonl`, live `pg_base`/`pg_rename` (read real columns) + `pg_decoy`/`pg_rename_decoy` (inject).

**Outputs:** `artifacts/decoy_map.json`, `artifacts/gold_star_expanded.jsonl`, decoy tables/columns present in both `*_decoy` instances.

**Algorithm:**
1. **Generate `decoy_map.json`** (skip if it already exists, regeneration is opt-in via `--regenerate`). For each DB and each variant (`original`, `obfuscated`):
   - Read the real schema of that variant from the corresponding clean instance's `information_schema` (original ← `pg_base`, obfuscated ← `pg_rename`).
   - Seed `random.Random(zlib.crc32(f"{SEED}:{db_id}:{variant}".encode()))` (per-DB-independent, reproducible, mirrors `01_split.py`).
   - Prompt the cheap model (see template below) for N decoy tables + confusable columns in the DB's language. Enforce: no name collides with a real table/column **or with the `db_id` itself** (the `superhero`/`sales_in_weather`/`university` qualifier trap); names ≤ 63 bytes (Postgres identifier limit); `snake_case`.
2. **Compute `gold_star_expanded.jsonl`.** Parse each gold with sqlglot; for the ~5 queries with a real-table star projection (top-level or subquery), expand `*`/`t.*` to the explicit real-column list using the clean instance's `information_schema` (do this **before** injecting decoys, so the expansion sees only real columns). Emit `sql_base_expanded` and `sql_rename_expanded`.
3. **Inject** (idempotent, check existence first): into `pg_decoy` apply the `original` variant, into `pg_rename_decoy` the `obfuscated` variant. Emit `CREATE TABLE "db_id"."decoy"( ... )` and `ALTER TABLE "db_id"."real" ADD COLUMN "decoy" <type>`, **all identifiers quoted**, **no FK constraints**, decoy tables **empty**.
4. **Re-validate R1==R2** (the acceptance gate). For every question, execute the clean gold on the clean instance (R1) and the gold on the decoy instance (R2), asserting `normalise_result` equality, using the **expanded** SQL for the star questions. Reuse step 7's comparison (now in `_db.py`). Two passes: original-identifier gold vs `pg_decoy`; obfuscated gold vs `pg_rename_decoy`. Any mismatch → `workdir/decoy_failures.jsonl` (expected: **zero**, given decoys are unreferenced and stars are expanded).

**Prompt template (decoy generation):**
```
You are extending a {language} database schema with plausible but FAKE distractor
objects for a schema-linking robustness test. Given the real schema below, produce:
- {n_tables} decoy TABLE(s): plausible for this domain, {language} snake_case names,
  each with 2-5 columns (name + Postgres type).
- For {k} of the real tables, 1-3 decoy COLUMN(s) that are CONFUSABLE near-synonyms
  or siblings of an existing real column (e.g. real "release_year" -> "release_year_est").
Rules: never reuse a real table/column name or the database id "{db_id}"; snake_case;
each name <= 60 chars. Output JSON only: {schema of decoy_map entry}.

Real schema:
{ddl}
```

**Params (CLI):** `--model gpt-5-mini`, `--n-tables-frac 0.4`, `--min-tables 2`, `--max-tables 15`, `--regenerate`, `--limit N` (dry run), `--validate-only`.

**Resumability:** `decoy_map.json` is write-once (regen only with `--regenerate`); injection checks `information_schema` before creating; validation is a read-only gate.

**Acceptance criteria:**
- `decoy_map.json` present; every decoy name passes the collision/length/`db_id` checks.
- Both `*_decoy` instances contain the decoy objects (spot-check `information_schema`).
- R1==R2 re-validation: **0** failures (or only pre-existing `rename_failures.jsonl` ids).
- Re-running step 08 is a no-op (idempotent).

---

## 6. Step 09: `09_paraphrase_questions.py`

**Purpose:** produce one paraphrase per question, conditioned on the gold SQL + schema so intent is anchored.

**Inputs:** `artifacts/test_final.jsonl` (and `train_final.jsonl` if `--include-train`).
**Output:** `artifacts/question_paraphrases.jsonl` (`question_id → question_paraphrase`), resumable.

**Algorithm:** for each question not already done (`load_done_keys` on `question_id`):
- Build a prompt with `(original question, gold SQL = sql_rename, obfuscated schema DDL for db_id)`.
- Call the cheap model once (`--model gpt-5-mini`), temperature ~0.7 for lexical diversity.
- Constraints in the system prompt: preserve meaning exactly; **natural language only**; **do not** mention any table/column identifier; return only the rephrased question.
- (Optional, `--cosine-check`) embed original vs paraphrase with `text-embedding-3-small`; if cosine < 0.6, retry once then keep-with-flag. Off by default per the methodology decision (drift is low when the gold SQL is provided).
- Append `{"question_id", "question_paraphrase"}` with fsync (reuse the append/fsync pattern from `eval_contamination.py`).

**Acceptance criteria:**
- One paraphrase per test `question_id` (count matches 2,030 unique).
- Spot-check 10 rows: meaning preserved, no identifiers leaked, materially reworded.
- Re-run is a no-op (resumable).

---

## 7. Ablation eval: `eval_ablation.py`

**Purpose:** run the 5 arms from [evaluation.md §9](../methodology/evaluation.md) and report paired deltas with CIs.

**Arms → (instance, gold field, question source):**

| Arm | DSN | Gold field (SELECT\*-expanded where applicable) | Question |
| --- | --- | --- | --- |
| `base` | `PG_BASE_DSN` | `sql_base` | `question` |
| `rename` | `PG_RENAME_DSN` | `sql_rename` | `question` |
| `decoy` | `PG_DECOY_DSN` | `sql_base` → `sql_base_expanded` if in `gold_star_expanded` | `question` |
| `paraphrase` | `PG_BASE_DSN` | `sql_base` | `question_paraphrase` |
| `all` | `PG_RENAME_DECOY_DSN` | `sql_rename` → `sql_rename_expanded` if expanded | `question_paraphrase` |

**Implementation:** extend `CONDITION_SPEC` / `DSN_FOR_SCHEMA` (already the right shape in `eval_contamination.py`) with the four new instances and five arms. All arms **no-hint** (evidence not shown), the primary signal per §4.2. The model still sees decoy columns via `get_schema_ddl` reading the decoy instance's `information_schema` (no special-casing needed: decoys are real catalog objects there). Grade the model's SQL by exact `normalise_result` equality against the arm's gold, executed on the arm's instance.

**Gold-expansion join:** load `gold_star_expanded.jsonl` into a dict; for `decoy`/`all`, if `question_id` is present, use the expanded gold. This guarantees decoy columns never leak into any arm's gold answer.

**Output & summary:** `eval/ablation_results.jsonl`; a `--summarize` that prints per-arm EX, each arm's delta vs baseline, a **paired McNemar test** and **bootstrap CI** per delta (report the English control as the empirical null / noise floor, not zero), and the per-language breakdown (join `db_language_map.json`). State explicitly that `all − Σ(individual deltas)` is **not** a clean interaction term (not a full factorial).

**Acceptance criteria:**
- All 5 instances reachable; `gold_exec_failed` count ≈ 0 (gold is pre-validated).
- Each arm covers all 2,030 test questions; resumable by `(question_id, arm)`.
- Summary prints EX, deltas, CIs, per-language table.

---

## 8. End-to-end run order + checklist

> **⚠️ Resource safety:** on a local Docker Desktop / WSL setup, never run all four PostgreSQL instances under heavy query load at once. It can OOM the WSL VM, and with `fsync=off` an OOM crash can corrupt the volumes. Bring up only the instances a step needs (a clone touches 2), run the ablation **one arm at a time** (each arm queries exactly one instance: `docker compose stop` the others between arms), keep eval `--concurrency` ≤ 3, and never overlap step-08's validate pass with the ablation. Capping the WSL VM's memory in `.wslconfig` is the backstop, not a licence to run everything hot; on a well-provisioned server this limit does not apply.

```bash
# 0. refactor (§2) and confirm the existing pipeline still passes
uv run python pipeline/eval_contamination.py --summarize     # sanity: unchanged contamination-eval numbers

# 1. DB: add compose services (§3b), then clone (§3c)
docker compose up -d
# ...clone commands from §3c...

# 2. decoys
uv run python pipeline/08_inject_decoys.py --limit 20   # dry run
uv run python pipeline/08_inject_decoys.py              # full: generate + inject + validate
#   GATE: workdir/decoy_failures.jsonl is empty

# 3. paraphrase
uv run python pipeline/09_paraphrase_questions.py --limit 20
uv run python pipeline/09_paraphrase_questions.py
#   GATE: 2,030 unique question_ids in artifacts/question_paraphrases.jsonl

# 4. ablation
uv run python pipeline/eval_ablation.py --model gpt-5.5 --limit 20   # dry run
uv run python pipeline/eval_ablation.py --model gpt-5.5             # full 5 arms
uv run python pipeline/eval_ablation.py --summarize
```

**Task checklist:**
- [ ] §2a `_db.py` extracted; `07` + `eval_contamination` import it; existing numbers unchanged
- [ ] §2b `_eval_helpers.py` extracted (or accepted duplication)
- [ ] §3b compose services + volumes added
- [ ] §3c decoy instances cloned and healthy (5434, 5435)
- [ ] §5 `08_inject_decoys.py`: `decoy_map.json` + `gold_star_expanded.jsonl` + injection + **0** R1==R2 failures
- [ ] §6 `09_paraphrase_questions.py`: 2,030 paraphrases, spot-checked
- [ ] §7 `eval_ablation.py`: 5 arms run, `--summarize` with CIs
- [ ] AGENTS.md updated (steps 08/09 + `eval_ablation.py` in the run table; DB bring-up note); PROGRESS.md ticked

---

## 9. Things that will bite you (pre-warned)

- **`db_id`-named tables** (`superhero`, `sales_in_weather`, `university`): decoy names must not equal the `db_id`, and injection must not touch the schema qualifier. See the sqlglot invariant.
- **Quote every identifier** in emitted DDL and expanded SQL; never assume lowercase.
- **`SELECT *` expansion must run before injection** (it must see only real columns).
- **Decoy tables must stay empty** unless `--populate`. A non-empty count is only a giveaway if a downstream agent can run queries; the eval only shows stripped DDL.
- **Re-cloning resets decoys**: if you ever re-clone a `*_decoy` volume from its clean source, you must re-run step 08 (the manifest makes this deterministic).
- **`normalise_result` must not change** during the §2a refactor: grading equivalence with the existing R1==R2 and contamination runs depends on it.
- **Keep `pg_base`/`pg_rename` clean**: decoys go only into the `*_decoy` instances.
