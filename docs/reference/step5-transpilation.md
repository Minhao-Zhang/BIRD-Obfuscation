# Step 5: Transpilation artifacts and R0==R1 validation

Operational reference for `05_transpile_sql.py`, `05b_apply_sql_fixes.py`, and the JSONL files under `workdir/`. Methodology context lives in [obfuscation.md §5](../methodology/obfuscation.md) and [evaluation.md §Stage 1](../methodology/evaluation.md).

## What "validated" means

For every question in `train_transpiled.jsonl` or `test_transpiled.jsonl`, the pipeline **at write time** ran `compare_r0_r1()` in `_transpile_helpers.py`:

1. Execute `sql_sqlite` against the **SQLite** source file (`data/{train,dev}/…/{db_id}.sqlite`).
2. Execute `sql_base` against **`pg_base`** (same rows/types as SQLite, loaded by step 4).
3. **Normalize** both result sets and require **multiset equality** (order-independent).

Normalization (`normalise_result()`):

- `NULL` stays `NULL`.
- Numeric-looking values are coerced with `float()`; `NaN` / `±Inf` are canonicalized to sentinel strings (`__nan__`, `__inf__`, `__neg_inf__`) so comparisons are stable.
- Other values become lowercased stripped strings.
- Rows are sorted before comparison. **Row order is not part of the contract**.

Both write paths enforce this gate:

| Path | Script | When R0==R1 is checked |
| --- | --- | --- |
| Pass 1 (sqlglot) | `05_transpile_sql.py` | Before append to `*_transpiled.jsonl` |
| Pass 2 (agent fixes) | `05b_apply_sql_fixes.py` | Before merge into `*_transpiled.jsonl` |

Questions that never pass R0==R1 end up in `transpilation_failures.jsonl` (or remain in `transpilation_needs_fix.jsonl` until fixed). They are **not** treated as validated transpilations.

### Execution timeouts

`exec_sqlite()` and `exec_pg()` use a **60-second per-query timeout** (`QUERY_TIMEOUT_SEC` in `_transpile_helpers.py`). SQLite timeouts surface as hard failures (`sqlite_exec_failed: sqlite query exceeded 60s`); PostgreSQL timeouts become `pg_exec_error` and usually land in the needs-fix queue.

### Assumption: frozen `pg_base`

R0==R1 is only meaningful while **`pg_base` matches the SQLite corpus step 4 loaded**. Rebuilding or mutating `pg_base` without re-running step 5 invalidates prior transpiled rows until re-validated.

---

## Pass 1 and pass 2 workflow

**Pass 1**: `uv run python pipeline/05_transpile_sql.py`

- sqlglot transpile + schema qualification + identifier quoting.
- **Match** → append to `workdir/{train,test}_transpiled.jsonl`.
- **Mismatch** (PG exec error or R0≠R1) → append to `workdir/transpilation_needs_fix.jsonl` with `error`, `pg_error`, failed `sql_base`, and `split`.
- **SQLite exec error / timeout** → append to `workdir/transpilation_failures.jsonl`.

**Pass 2**: manual agent repair + `05b`

1. Export batches: `uv run python pipeline/05c_export_fix_batch.py --offset N --limit 50 --out workdir/fix_batches/batch_XXX.jsonl`
2. Agents append proposed SQL: `workdir/transpilation_fixes.jsonl`, one object per line: `{"question_id", "sql_base"}`.
3. Apply: `uv run python pipeline/05b_apply_sql_fixes.py`. Re-runs R0==R1; on success merges into the correct `*_transpiled.jsonl`; on failure appends to `transpilation_failures.jsonl`.

Progress: `uv run python pipeline/05_transpile_sql.py --status` (`needs_fix_pending` counts queue rows whose `question_id` is not yet in transpiled ok **or** failures).

---

## Artifact files (`workdir/`)

### Inputs (steps 1-2, from `artifacts/`)

| File | Fields |
| --- | --- |
| `train.jsonl`, `test.jsonl` | `question_id`, `db_id`, `question`, `evidence`, `difficulty`, `sql_sqlite` |

`question_id` values look like `train_5093` or numeric dev-style ids. The prefix reflects BIRD provenance, not the train/test split filename.

### Outputs (to `workdir/`)

| File | Meaning | Key fields |
| --- | --- | --- |
| `{train,test}_transpiled.jsonl` | **R0==R1 validated** PostgreSQL gold SQL | input fields + `sql_base` |
| `transpilation_needs_fix.jsonl` | Pass-1 misses; **historical queue** (may contain duplicates) | input + `sql_base`, `error`, `pg_error`, `split` |
| `transpilation_fixes.jsonl` | Agent-proposed fixes (may contain duplicate lines per id) | `question_id`, `sql_base` |
| `transpilation_failures.jsonl` | **Not validated**: timeout, SQLite error, rejected fix | input + `error` (usually no reliable `sql_base`) |
| `fix_batches/batch_*.jsonl` | Exported agent work units | needs-fix row + `pg_schema_ddl` |

### Question disposition (unique `question_id`)

Each of the 10,541 retained questions should appear in **exactly one** of:

- `{train,test}_transpiled.jsonl`: validated match, or
- `transpilation_failures.jsonl`: excluded from validated transpiled set.

The needs-fix and fixes files are **workflow artifacts**; a question can remain listed there even after it has been merged into transpiled output.

---

## Important caveats

### 1. VALUES materialization (~12% of validated rows)

Many agent fixes could not match SQLite semantics with a portable PostgreSQL rewrite (float4/pgloader drift, NaN rows, SQLite tie-break / loose `GROUP BY`, etc.). Those fixes pass R0==R1 using SQL of the form:

```sql
SELECT * FROM (VALUES (...), (...)) AS t("col1", "col2", ...)
```

The PostgreSQL query **embeds SQLite result rows** rather than recomputing them. That satisfies the evaluation oracle (same result set on the loaded data) but is **not** a durable dialect translation. It will not generalize to new rows or to `pg_rename` without replacement in step 7.

Heuristic: `sql_base` containing `VALUES` (case-insensitive). As of the first full pass-2 run, on the order of **~1,100 / ~10,200** unique transpiled questions used this pattern.

**Not all of these are "circular."** Containing a `VALUES` clause is not the same as being a baked-in constant: the large majority use `VALUES` legitimately (as an `IN`-list or a derived table *alongside* real schema tables). Measured on the shipped `sql_base`, only **~0.5%** (≈46 rows) reference **no real table at all**. Those are the genuinely-circular cases where `R0==R1`/`R1==R2` are trivially self-satisfied. So a "~12% circular validation" reading would be an overstatement; the truly-constant set is ~0.5%. A handful of those constant golds are very large literal dumps (up to ~4.4M characters), faithful transpilations, but not natural SQL and poor as "known-true SQL" for a downstream memory-learning agent. See [limitations.md §5](limitations.md).

### 2. Duplicate JSONL lines

Resume runs and interrupted appends can write **multiple lines with the same `question_id`**. Use unique `question_id` counts for coverage stats, not raw line counts. Duplicates observed so far share the same `sql_base` (no conflicting rewrites per id). Prefer deduping (keep last line per `question_id`) before downstream consumption.

`append_jsonl()` fsyncs after each line; `load_done_ids()` skips malformed lines. Partial lines can still appear if a process is killed mid-write. Repair by dropping non-JSON lines and re-running pass 1 for affected ids.

### 3. Failures are not matched

`transpilation_failures.jsonl` holds questions where:

- SQLite execution failed or timed out,
- no agent fix passed R0==R1, or
- a fix referenced an unknown `question_id`.

These rows must **not** be consumed as validated gold SQL for eval.

### 4. What R0==R1 does *not* check

- **Semantic portability** of `sql_base` beyond the current SQLite/pg_base snapshot (see VALUES).
- **Row order** (results are sorted before compare).
- **Approximate float equality**: values must match after `float()` coercion unless NaN/Inf sentinels apply; tiny float drift blocked many fixes until VALUES or explicit casts.
- **Obfuscated schema**: step 7's R1==R2 check is separate.

---

## Re-validation

To spot-check or audit the corpus against live databases:

```python
import psycopg2, sys
sys.path.insert(0, "pipeline")
from _transpile_helpers import compare_r0_r1, PG_BASE_DSN

pg = psycopg2.connect(PG_BASE_DSN)
# for each unique row in *_transpiled.jsonl:
match, err = compare_r0_r1(db_id, sql_sqlite, sql_base, pg)
```

There is no dedicated audit script yet; add one if you need a full 10,541-row gate before release.

---

## Related code

| Symbol / file | Role |
| --- | --- |
| `compare_r0_r1()` | R0==R1 gate |
| `normalise_result()` | Result-set normalization |
| `exec_sqlite()` / `exec_pg()` | Timed execution |
| `transpile_status()` | `--status` counts |
| `05_transpile_sql.py` | Pass 1 |
| `05b_apply_sql_fixes.py` | Pass 2 merge |
| `05c_export_fix_batch.py` | Agent batch export |
