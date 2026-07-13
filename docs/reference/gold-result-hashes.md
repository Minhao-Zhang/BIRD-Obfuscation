**English** · [中文](gold-result-hashes-zh.md)

# Gold result hashes (`pg_rename_decoy`)

SHA-256 digests of each gold query's normalised result multiset on `pg_rename_decoy`.
A grader can run only the model's SQL, hash that result the same way, and compare
against this file instead of re-executing gold.

| | |
| --- | --- |
| Artifact | `eval_dataset/gold_result_hashes_rename_decoy.jsonl` (also under `artifacts/` after a local run) |
| Instance | `pg_rename_decoy` (`127.0.0.1:5435`, DSN key `rename_decoy`) |
| Coverage | Every row in `train_final.jsonl` and `test_final.jsonl` |
| Builder | `uv run python pipeline/precompute_gold_result_hashes.py` |

## Record fields

Each JSONL line:

| field | meaning |
| --- | --- |
| `question_id` | Stable question id |
| `db_id` | PostgreSQL schema |
| `split` | `train` or `test` (which `*_final.jsonl` the row came from) |
| `dsn_key` | Always `rename_decoy` |
| `sql_sha256` | SHA-256 of the exact gold SQL string that was executed (UTF-8) |
| `nrows` | Row count returned (`null` on failure) |
| `hash_lenient` | SHA-256 over `normalise_result` (BIRD-style EX) |
| `hash_strict` | SHA-256 over `normalise_result_strict` (no cross-type collapse) |
| `error` | `null` on success; otherwise e.g. `ResultSetTooLarge: ...` |
| `recorded_at_utc` | When this line was written |

Treat a cache hit as stale if `sql_sha256` does not match the gold SQL you would run
today (gold text or star-expansion changed).

## Which gold SQL is hashed

Same rule as ablation arm `all`:

1. If `question_id` is in `gold_star_expanded.jsonl` and `sql_rename_expanded` is set,
   use that.
2. Otherwise use `sql_rename` from `*_final.jsonl`.

See `resolve_gold_sql` in `pipeline/precompute_gold_result_hashes.py`.

## Hash algorithm (byte-for-byte replication)

Canonical code: `hash_normalised_result` / `hash_normalised_result_strict` in
[`pipeline/_db.py`](../../pipeline/_db.py).

1. Execute the gold SQL with `exec_pg` on `pg_rename_decoy` (read-only connection,
   `statement_timeout` 300s, hard cap `MAX_RESULT_ROWS = 200_000`). On overflow, write
   `error` and leave both hashes `null`. Do not hash a truncated result set.
2. Normalise the raw rows:
   - Lenient (`normalise_result`): each cell becomes null, float, or a lowercased
     stripped string; NaN/inf become string sentinels; rows sorted as a multiset.
   - Strict (`normalise_result_strict`): keep type distinctions (bool ≠ int ≠ numeric
     string); rows sorted as a multiset. Matches EX grading in `_eval_helpers.grade`.
3. Canonical JSON: turn the normalised list of tuples into a list of lists, then

   ```text
   json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
   ```

   Floats stay JSON numbers. After step 2, NaN/inf never appear as floats (only as
   string sentinels).
4. Digest: SHA-256 of that JSON string as UTF-8 bytes; store the lowercase hex digest
   as `hash_lenient` / `hash_strict`.

### Pseudocode

```python
import hashlib, json
from _db import normalise_result, normalise_result_strict

def canonical_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

def hash_lenient(rows) -> str:
    norm = normalise_result(rows)  # list[tuple]
    payload = canonical_json([list(r) for r in norm])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def hash_strict(rows) -> str:
    norm = normalise_result_strict(rows)
    payload = canonical_json([list(r) for r in norm])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

To grade without re-running gold: execute the generated SQL the same way, compute the
same hashes, and require `hash_lenient` (and optionally `hash_strict`) to match the
cached line, with `sql_sha256` equal to the gold SQL string you trust.

## Rebuild

Needs a live `pg_rename_decoy`. On a laptop, prefer keeping only that instance hot
(see [AGENTS.md](../../AGENTS.md)).

```bash
docker compose --profile decoy up -d pg_rename_decoy
uv run python pipeline/precompute_gold_result_hashes.py          # resumable
uv run python pipeline/precompute_gold_result_hashes.py --status
uv run python eval_dataset/build_eval_dataset.py                # refresh git snapshot
```
