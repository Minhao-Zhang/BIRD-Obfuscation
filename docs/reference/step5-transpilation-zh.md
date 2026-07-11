[English](step5-transpilation.md) · **中文**

# 步骤 5:转译产物与 R0==R1 校验

面向 `05_transpile_sql.py`、`05b_apply_sql_fixes.py` 以及 `workdir/` 下各 JSONL 文件的操作参考。方法论背景见 [obfuscation.md §5](../methodology/obfuscation-zh.md) 与 [evaluation.md §Stage 1](../methodology/evaluation-zh.md)。

## 「已校验」意味着什么

对于 `train_transpiled.jsonl` 或 `test_transpiled.jsonl` 中的每一道问题,流水线**在写入时**都运行了 `_transpile_helpers.py` 里的 `compare_r0_r1()`:

1. 对 **SQLite** 源文件(`data/{train,dev}/…/{db_id}.sqlite`)执行 `sql_sqlite`。
2. 对 **`pg_base`**(与 SQLite 行/类型相同,由步骤 4 加载)执行 `sql_base`。
3. **规范化**两个结果集,并要求**多重集相等**(与顺序无关)。

规范化(`normalise_result()`):

- `NULL` 保持为 `NULL`。
- 看起来像数值的值用 `float()` 强制转换;`NaN` / `±Inf` 规范化成哨兵字符串(`__nan__`、`__inf__`、`__neg_inf__`),以保证比较结果稳定。
- 其他值转换为去除首尾空白的小写字符串。
- 比较前会对行排序。**行顺序不属于契约的一部分**。

两条写入路径都强制执行这道关卡:

| 路径 | 脚本 | R0==R1 的检查时机 |
| --- | --- | --- |
| 第 1 轮(sqlglot) | `05_transpile_sql.py` | 追加到 `*_transpiled.jsonl` 之前 |
| 第 2 轮(agent 修复) | `05b_apply_sql_fixes.py` | 合并进 `*_transpiled.jsonl` 之前 |

始终无法通过 R0==R1 的问题最终会进入 `transpilation_failures.jsonl`(或者在修复之前一直留在 `transpilation_needs_fix.jsonl` 中)。它们**不会**被视为已校验的转译结果。

### 执行超时

`exec_sqlite()` 和 `exec_pg()` 采用**每条查询 60 秒的超时**(`_transpile_helpers.py` 中的 `QUERY_TIMEOUT_SEC`)。SQLite 超时表现为硬失败(`sqlite_exec_failed: sqlite query exceeded 60s`);PostgreSQL 超时会变成 `pg_exec_error`,通常落入待修复队列。

### 前提假设:冻结的 `pg_base`

只有在 **`pg_base` 与步骤 4 加载的 SQLite 语料保持一致**时,R0==R1 才有意义。重建或改动 `pg_base` 却不重新运行步骤 5,会使先前的转译行失效,直到重新校验为止。

---

## 第 1 轮与第 2 轮工作流

**第 1 轮**:`uv run python pipeline/05_transpile_sql.py`

- sqlglot 转译 + 模式限定 + 标识符加引号。
- **匹配** → 追加到 `workdir/{train,test}_transpiled.jsonl`。
- **不匹配**(PG 执行错误或 R0≠R1) → 追加到 `workdir/transpilation_needs_fix.jsonl`,并带上 `error`、`pg_error`、失败的 `sql_base` 以及 `split`。
- **SQLite 执行错误 / 超时** → 追加到 `workdir/transpilation_failures.jsonl`。

**第 2 轮**:人工 agent 修复 + `05b`

1. 导出批次:`uv run python pipeline/05c_export_fix_batch.py --offset N --limit 50 --out workdir/fix_batches/batch_XXX.jsonl`
2. agent 追加提议的 SQL:写入 `workdir/transpilation_fixes.jsonl`,每行一个对象:`{"question_id", "sql_base"}`。
3. 应用:`uv run python pipeline/05b_apply_sql_fixes.py`。它会重新运行 R0==R1;成功则合并进对应的 `*_transpiled.jsonl`,失败则追加到 `transpilation_failures.jsonl`。

进度:`uv run python pipeline/05_transpile_sql.py --status`(`needs_fix_pending` 统计队列中 `question_id` 尚未出现在已转译成功**或**失败结果中的行数)。

---

## 产物文件(`workdir/`)

### 输入(步骤 1-2,来自 `artifacts/`)

| 文件 | 字段 |
| --- | --- |
| `train.jsonl`, `test.jsonl` | `question_id`, `db_id`, `question`, `evidence`, `difficulty`, `sql_sqlite` |

`question_id` 的取值形如 `train_5093` 或数字型的 dev 风格 id。前缀反映的是 BIRD 的来源,而不是 train/test 划分的文件名。

### 输出(写入 `workdir/`)

| 文件 | 含义 | 关键字段 |
| --- | --- | --- |
| `{train,test}_transpiled.jsonl` | **通过 R0==R1 校验的** PostgreSQL 金标准 SQL | 输入字段 + `sql_base` |
| `transpilation_needs_fix.jsonl` | 第 1 轮未命中的项;**历史队列**(可能含重复) | 输入 + `sql_base`、`error`、`pg_error`、`split` |
| `transpilation_fixes.jsonl` | agent 提议的修复(同一 id 可能有重复行) | `question_id`、`sql_base` |
| `transpilation_failures.jsonl` | **未校验**:超时、SQLite 错误、被拒绝的修复 | 输入 + `error`(通常没有可靠的 `sql_base`) |
| `fix_batches/batch_*.jsonl` | 导出的 agent 工作单元 | 待修复行 + `pg_schema_ddl` |

### 问题去向(按唯一 `question_id`)

保留下来的 10,541 道问题,每一道都应当**恰好出现在**下列之一中:

- `{train,test}_transpiled.jsonl`:通过校验的匹配,或
- `transpilation_failures.jsonl`:被排除在已校验转译集合之外。

待修复文件和修复文件属于**工作流产物**;即便某道问题已合并进转译输出,仍可能列在其中。

---

## 重要注意事项

### 1. VALUES 物化(~12% 的已校验行)

许多 agent 修复无法用可移植的 PostgreSQL 重写来匹配 SQLite 语义(float4/pgloader 漂移、NaN 行、SQLite 的平局决胜 / 宽松的 `GROUP BY` 等)。这些修复用来通过 R0==R1 的 SQL 形如:

```sql
SELECT * FROM (VALUES (...), (...)) AS t("col1", "col2", ...)
```

该 PostgreSQL 查询**内嵌了 SQLite 的结果行**,而不是重新计算它们。这满足了评测预言机(在已加载数据上得到相同的结果集),但**不是**持久的方言翻译。若不在步骤 7 中加以替换,它无法推广到新的行或 `pg_rename`。

启发式判断:`sql_base` 中包含 `VALUES`(大小写不敏感)。截至第一次完整的第 2 轮运行,大约有 **~1,100 / ~10,200** 道唯一转译问题使用了该模式。

**这些并非全都是「循环的」。** 含有 `VALUES` 子句并不等同于内嵌固定常量:绝大多数都是合理地使用 `VALUES`(作为 `IN` 列表,或作为与真实模式表*并列*的派生表)。在实际交付的 `sql_base` 上测量,只有 **~0.5%**(≈46 行)**完全不引用任何真实表**。那些才是真正循环的情形,其中 `R0==R1`/`R1==R2` 平凡地自我满足。因此「~12% 循环校验」的说法会夸大其词;真正的常量集合约为 ~0.5%。这些常量金标准里有少数是极大的字面量转储(多达 ~4.4M 个字符),它们是忠实的转译,但并非自然的 SQL;拿来给下游的记忆学习 agent 当「已知为真的 SQL」用,效果也很差。见 [limitations.md §5](limitations-zh.md)。

### 2. 重复的 JSONL 行

续跑和被中断的追加操作可能会写入**多行相同 `question_id` 的记录**。做覆盖率统计时应使用唯一 `question_id` 的计数,而不是原始行数。目前观察到的重复行都共享相同的 `sql_base`(同一 id 没有相互冲突的重写)。在下游使用之前,最好先去重(每个 `question_id` 保留最后一行)。

`append_jsonl()` 会在每行写入后执行 fsync;`load_done_ids()` 会跳过格式错误的行。如果进程在写入中途被杀掉,仍可能出现不完整的行。修复方法是丢弃非 JSON 行,并对受影响的 id 重新运行第 1 轮。

### 3. 失败项不算匹配

`transpilation_failures.jsonl` 保存的是满足以下情况的问题:

- SQLite 执行失败或超时,
- 没有任何 agent 修复通过 R0==R1,或
- 某个修复引用了未知的 `question_id`。

这些行**绝不能**被当作用于评测的已校验金标准 SQL 来使用。

### 4. R0==R1 *不*检查什么

- `sql_base` 超出当前 SQLite/pg_base 快照的**语义可移植性**(见 VALUES)。
- **行顺序**(结果在比较前已排序)。
- **浮点近似相等**:除非适用 NaN/Inf 哨兵,否则值在经过 `float()` 强制转换后必须相等;微小的浮点漂移曾使许多修复受阻,直到改用 VALUES 或显式转换。
- **混淆后的模式**:步骤 7 的 R1==R2 检查是独立的。

---

## 重新校验

要针对活动数据库抽查或审计语料:

```python
import psycopg2, sys
sys.path.insert(0, "pipeline")
from _transpile_helpers import compare_r0_r1, PG_BASE_DSN

pg = psycopg2.connect(PG_BASE_DSN)
# for each unique row in *_transpiled.jsonl:
match, err = compare_r0_r1(db_id, sql_sqlite, sql_base, pg)
```

目前还没有专门的审计脚本;如果你在发布前需要一道覆盖全部 10,541 行的关卡,请自行添加。

---

## 相关代码

| 符号 / 文件 | 作用 |
| --- | --- |
| `compare_r0_r1()` | R0==R1 关卡 |
| `normalise_result()` | 结果集规范化 |
| `exec_sqlite()` / `exec_pg()` | 带超时的执行 |
| `transpile_status()` | `--status` 计数 |
| `05_transpile_sql.py` | 第 1 轮 |
| `05b_apply_sql_fixes.py` | 第 2 轮合并 |
| `05c_export_fix_batch.py` | agent 批次导出 |
