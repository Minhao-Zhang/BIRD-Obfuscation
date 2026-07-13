[English](gold-result-hashes.md) · **中文**

# Gold 结果哈希（`pg_rename_decoy`）

在 `pg_rename_decoy` 上，对每条 gold 查询的规范化结果多重集预先算好 SHA-256。打分时只要跑模型 SQL、按同一规则哈希，就能和本文件比对，不必再执行 gold。

| | |
| --- | --- |
| 产物 | `eval_dataset/gold_result_hashes_rename_decoy.jsonl`（本地跑过后也会落在 `artifacts/`） |
| 实例 | `pg_rename_decoy`（`127.0.0.1:5435`，DSN key `rename_decoy`） |
| 覆盖 | `train_final.jsonl` 与 `test_final.jsonl` 的全部行 |
| 生成脚本 | `uv run python pipeline/precompute_gold_result_hashes.py` |

## 记录字段

每行一条 JSONL：

| 字段 | 含义 |
| --- | --- |
| `question_id` | 稳定问题 id |
| `db_id` | PostgreSQL schema |
| `split` | `train` 或 `test` |
| `dsn_key` | 固定为 `rename_decoy` |
| `sql_sha256` | 实际执行的那条 gold SQL（UTF-8）的 SHA-256 |
| `nrows` | 返回行数（失败时为 null） |
| `hash_lenient` | 对 `normalise_result`（BIRD 风格 EX）的 SHA-256 |
| `hash_strict` | 对 `normalise_result_strict`（不做跨类型折叠）的 SHA-256 |
| `error` | 成功为 null；失败时如 `ResultSetTooLarge: …` |
| `recorded_at_utc` | 本行写入时间 |

如果 `sql_sha256` 对不上你现在要用的 gold SQL（原文或 star 展开改过），这条缓存就算作废。

## 哈希用哪条 gold SQL

规则与消融臂 `all` 相同：

1. `gold_star_expanded.jsonl` 里有该 `question_id`，且带了 `sql_rename_expanded`，就用展开版。
2. 否则用 `*_final.jsonl` 里的 `sql_rename`。

具体见 `pipeline/precompute_gold_result_hashes.py` 里的 `resolve_gold_sql`。

## 哈希算法（按字节复现）

实现以 [`pipeline/_db.py`](../../pipeline/_db.py) 的 `hash_normalised_result` / `hash_normalised_result_strict` 为准。

1. **执行**：在 `pg_rename_decoy` 上用 `exec_pg` 跑 gold（只读连接，`statement_timeout` 300s，硬上限 `MAX_RESULT_ROWS = 200_000`）。超限就写 `error`，两个哈希都留 null。截断结果集不要拿去哈希。
2. **规范化**：
   - **宽松**（`normalise_result`）：单元格收成 null / float / 去空白小写字符串；NaN、inf 改成字符串哨兵；行按多重集排序。
   - **严格**（`normalise_result_strict`）：保留类型差别（bool ≠ int ≠ 数字字符串）；同样按多重集排序。语义与 `_eval_helpers.grade` 的 EX 打分一致。
3. **规范 JSON**：把规范化后的 tuple 列表转成 list-of-lists，再：

   ```text
   json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
   ```

   float 仍是 JSON 数字。第二步之后不会再出现 NaN/inf 浮点（只剩字符串哨兵）。
4. **摘要**：对上述 JSON 的 UTF-8 字节做 SHA-256，小写十六进制写入 `hash_lenient` / `hash_strict`。

### 伪代码

```python
import hashlib, json
from _db import normalise_result, normalise_result_strict

def canonical_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

def hash_lenient(rows) -> str:
    norm = normalise_result(rows)
    payload = canonical_json([list(r) for r in norm])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def hash_strict(rows) -> str:
    norm = normalise_result_strict(rows)
    payload = canonical_json([list(r) for r in norm])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

打分时：模型 SQL 用同样方式执行、算同样哈希；`hash_lenient`（可选再比 `hash_strict`）要对上缓存行，并且 `sql_sha256` 要等于你采信的那条 gold SQL。

## 重建

需要活着的 `pg_rename_decoy`。笔记本上尽量只热这一个实例（见 [AGENTS.md](../../AGENTS.md)）。

```bash
docker compose --profile decoy up -d pg_rename_decoy
uv run python pipeline/precompute_gold_result_hashes.py
uv run python eval_dataset/build_eval_dataset.py
```
