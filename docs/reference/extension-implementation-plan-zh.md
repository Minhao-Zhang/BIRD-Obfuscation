[English](extension-implementation-plan.md) · **中文**

# 扩展实现方案:诱饵注入、问题改写、消融实验

**读者对象:** 首次实现扩展混淆层的工程师。这是**构建规范**。*设计与理由*见 [../methodology/obfuscation-extensions.md](../methodology/obfuscation-extensions-zh.md) 与 [../methodology/evaluation.md §9](../methodology/evaluation-zh.md);需要复现的*消融数据*在 evaluation.md §8/§9。请先读那些文档,再从头到尾跟着本文操作。

> **⚠️ 部分内容已被取代(2026-07-04)。请先读这一段。** 本方案中关于**诱饵**的部分
> (§0 的诱饵参数,以及 §5 的 "步骤 08:`08_inject_decoys.py`")描述的是*空的 /
> 结构性的*诱饵表和诱饵列。这套做法后来重新设计成了**被污染的诱饵陷阱**
> (`pipeline/10_inject_traps.py`,步骤 10):诱饵现在会被*填充*上经过细微
> **污染的真实数据副本**(附加的 "evil-twin" 列和被污染的克隆表),因为面对能执行并观察结果的交互式智能体,空诱饵会露馅。下文任何说诱饵"保持为空" /
> "在剥离后的 DDL 中不可见" / R1==R2 成立是"因为诱饵未被引用/为空"的说法,均**已被取代**;
> 当前的保证是陷阱是**严格附加的**(真实的行/列/表逐字节保持一致)。步骤 08 仍会运行
> (它会生成 `decoy_map.json` 和 `SELECT *` 展开),但诱饵的权威标准答案现在是
> `trap_manifest.json` + `trap_table_manifest.json`。共享辅助函数的重构(§2)、
> 四个实例(§3)、改写(§6)以及消融实验(§7)均已按此实现。
> **当前诱饵设计:[corrupted-decoys-design.md](corrupted-decoys-design-zh.md)。**

**前置条件(必须已经满足):**
- 核心流水线已完成:`artifacts/train_final.jsonl` 和 `artifacts/test_final.jsonl` 已存在(8,134 / 2,030 行)。
- `pg_base`(5432)和 `pg_rename`(5433)已构建且状态健康(`docker compose up -d`)。
- `.env` 中有 `OPENAI_API_KEY`(与 `eval_contamination.py` 使用的是同一个)。
- `uv` 环境可用(`uv run python -c "import sqlglot, psycopg2, openai"`)。

**黄金法则:** 绝不改动 `pg_base` / `pg_rename` 或已有的 `*_final.jsonl`。所有新工作都写入**新的实例和新的产物**。核心交付物保持不可变。

---

## 0. 方法学覆盖检查

方法学已在**设计层面完整记录**。下文所有内容都是*实现*细节,而非新的方法学。以下是**开放参数**(已给出默认值,按默认值推进是安全的):

| 参数 | 默认值 | 影响之处 |
| --- | --- | --- |
| 用于生成的廉价模型 | `gpt-5-mini`(回退到 `gpt-4o-mini`) | 步骤 08、09 |
| 每个数据库的诱饵表数量 | 真实表数量的 +30-50%,最少 2 个,上限 15 个 | 步骤 08 |
| 每张真实表的诱饵列数量 | 1-3 个易混淆的列 | 步骤 08 |
| 是否给诱饵表填充行数据? | ~~**否**(为空;在剥离后的 DDL 中不可见)~~ **已被取代 → 是**,填充被污染的数据(步骤 10 的陷阱) | 步骤 08 → 10 |
| 改写范围 | **仅测试集**(消融实验所需的全部);训练集为可选,供下游使用 | 步骤 09 |
| 两个实例的诱饵生成 | **按变体分别生成**(`pg_decoy` 用英语,`pg_rename_decoy` 用目标语言) | 步骤 08 |
| 可选的余弦漂移合理性检查 | 关闭(若启用则使用 OpenAI `text-embedding-3-small`,无需新依赖) | 步骤 09 |

---

## 1. 仓库改动一览

| 路径 | 操作 | 目的 |
| --- | --- | --- |
| `pipeline/_db.py` | **新增** | 共享的 `normalise_result`、`exec_pg`、`ResultSetTooLarge`、DSN 常量、`new_connection`(消除 7 个文件里的复制粘贴) |
| `pipeline/_eval_helpers.py` | **新增** | 从 `eval_contamination.py` 提取出的共享评测逻辑(LLM 调用、prompt 构建、DDL 缓存、可续跑) |
| `pipeline/eval_contamination.py` | **编辑** | 从上述两个辅助模块导入;行为不变(污染评测) |
| `pipeline/08_inject_decoys.py` | **新增** | 生成 `decoy_map.json`,注入到诱饵实例,展开 `SELECT *`,重新校验 R1==R2 |
| `pipeline/09_paraphrase_questions.py` | **新增** | 生成 `question_paraphrases.jsonl`(廉价 LLM) |
| `pipeline/eval_ablation.py` | **新增** | 5 组的消融实验运行;导入 `_eval_helpers` + `_db` |
| `docker-compose.yml` | **编辑** | 添加 `pg_decoy`(5434)+ `pg_rename_decoy`(5435)及其数据卷 |
| `artifacts/decoy_map.json` | **新增(生成)** | 每个数据库、每个变体的权威诱饵定义 |
| `artifacts/gold_star_expanded.jsonl` | **新增(生成)** | 对受影响的约 5 个问题做 `SELECT *` 展开后的标准答案 |
| `artifacts/question_paraphrases.jsonl` | **新增(生成)** | `question_id → question_paraphrase` |
| `eval/ablation_results.jsonl` | **新增(生成)** | 每个 `(question_id, arm)` 一条记录 |
| `AGENTS.md` | **编辑(代码落地之后)** | 在运行表和数据库启动说明中加入步骤 08/09 + `eval_ablation.py` |
| `PROGRESS.md` | **编辑(随进度)** | 勾掉任务,记录决策 |

**不重命名或删除任何已有文件。** 唯一的"移动"是 §2 中的两次重构(把共享代码从已有文件抽取到新的 `_db.py` / `_eval_helpers.py`)。已有的字段名(`sql_base`、`sql_rename`、…)保持不变。

---

## 2. 先重构(在写 08/09 之前做)

目前 `normalise_result` / `exec_pg` / DSN 常量被复制粘贴在 `07_rename_sql_and_validate.py`、`eval_contamination.py` 和 `_transpile_helpers.py` 三处(还带着一句 "keep these in sync" 的注释,这是坏味道)。步骤 08/09/消融实验都需要它们,所以**一次性**合并:

### 2a. `pipeline/_db.py`(新增)
将权威版本移到这里(不要重新造):
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
然后把 `07_rename_sql_and_validate.py` 和 `eval_contamination.py` 改为 `from _db import ...`。**不要改变行为**:`normalise_result` 必须逐字节保持一致(评分语义依赖它)。重构后把现有流水线跑一遍,确认没有任何东西发生偏移。遵守 AGENTS.md 中的不变量(用 127.0.0.1 而非 localhost;在 autocommit 下用普通的 `SET`;用 `fetchmany` 而非 `fetchall`)。

### 2b. `pipeline/_eval_helpers.py`(新增)
从 `eval_contamination.py` 中抽取 `eval_ablation.py` 将要复用的部分:`get_schema_ddl`、`build_prompt`、`extract_sql`、`usage_dict`、`SYSTEM_INSTRUCTIONS`、`load_done_keys`/`append_result`,以及 `run_one` 核心逻辑。让 `eval_contamination.py` 保持精简,作为污染评测入口,导入这些函数。这与现有的 `_transpile_helpers.py` / `_pg_helpers.py` 约定一致。

> 如果时间有限,2a 是必须的(08/09/消融实验都要执行 SQL);2b 是锦上添花(你也可以把 eval_contamination.py 复制成 eval_ablation.py 再扩展,接受这份重复)。

---

## 3. PostgreSQL 要求

### 3a. 实例

| 实例 | 端口 | 数据卷 | 标识符 | 诱饵 | 构建方式 |
| --- | --- | --- | --- | --- | --- |
| `pg_base` | 5432 | `pg_base_data` | 原始 | 无 | 步骤 4(已有) |
| `pg_rename` | 5433 | `pg_rename_data` | 重命名 | 无 | 步骤 6(已有) |
| **`pg_decoy`** | **5434** | `pg_decoy_data` | 原始 | 英语 | 克隆 `pg_base` + 步骤 08 |
| **`pg_rename_decoy`** | **5435** | `pg_rename_decoy_data` | 重命名 | 目标语言 | 克隆 `pg_rename` + 步骤 08 |

### 3b. `docker-compose.yml` 的新增内容:**已完成**
`pg_decoy`(5434)和 `pg_rename_decoy`(5435)已在 `docker-compose.yml` 中,各自是对应干净实例的副本(相同的 WAL 调优 `command`/`healthcheck`)外加 `profiles: ["decoy"]`。这个 profile 让核心流水线的启动保持不变:`docker compose up -d` 仍然只启动两个干净实例;这对诱饵实例只有加上 `--profile decoy` 才会启动。验证:`docker compose config --services` → 2,`docker compose --profile decoy config --services` → 4。

### 3c. 通过克隆构建诱饵实例(运行一次,在步骤 08 之前)
与步骤 6 相同的只读克隆模式(`:ro` 源挂载是安全保证,见 [pipeline-invariants.md](pipeline-invariants-zh.md))。在克隆诱饵目标的数据卷时,它们必须处于**停止**状态(运行中的 Postgres 会占用数据卷);profile 意味着它们不会被自动启动,但为保险起见请显式停止它们:
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
(用 `docker volume ls` 确认加了前缀的确切数据卷名。)完成后,`pg_*_decoy` 是逐字节一致的克隆;步骤 08 会就地添加诱饵。**重新运行克隆会重置诱饵数据卷 → 之后需重新运行步骤 08。**

---

## 4. 新产物(schema)

### `artifacts/decoy_map.json`
权威、可重新生成、带随机种子。每个数据库一条条目,**按变体划分**,这样每个实例都能拿到语言匹配的名称:
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
`mimics` 记录某个易混淆的诱饵列影射的是哪个真实列(供后续分析;注入时不使用)。

### `artifacts/gold_star_expanded.jsonl`
只包含标准答案中带有真实表 `SELECT *` 的那约 5 个问题(测量方法见 obfuscation-extensions.md §2)。由步骤 08 产生:
```json
{"question_id": "train_8505", "sql_base_expanded": "SELECT \"col1\", ... FROM ...",
 "sql_rename_expanded": "SELECT \"...\", ... FROM ..."}
```

### `artifacts/question_paraphrases.jsonl`
由步骤 09 产生(与 `*_final.jsonl` 分开存放,以保持交付物不可变;消融实验按 `question_id` 关联):
```json
{"question_id": "train_5093", "question_paraphrase": "..."}
```

### `eval/ablation_results.jsonl`
每个 `(question_id, arm)` 一条记录,可续跑,结构与 `eval/contamination_results.jsonl` 相同,外加一个 `arm` 字段(`base|rename|decoy|paraphrase|all`)。

---

## 5. 步骤 08:`08_inject_decoys.py`

> **就诱饵载荷而言已被取代(见顶部横幅)。** 这里所述的步骤 08 注入的是*空的*
> 诱饵表/列。它仍会运行,以产生 `decoy_map.json` 和
> `gold_star_expanded.jsonl`,但诱饵现在由**步骤 10**(`10_inject_traps.py`)
> *填充上被污染的数据*;见 [corrupted-decoys-design.md](corrupted-decoys-design-zh.md)。
> 下文的"诱饵表**为空**"和"预期:**零** …… 因为诱饵未被引用"
> 反映的是旧设计。R1==R2 现在成立是因为陷阱是严格附加的。

**目的:** 生成诱饵,把它们注入到两个 `*_decoy` 实例,展开少量 `SELECT *` 标准答案查询,并证明没有破坏任何东西(针对诱饵实例的 R1==R2)。

**输入:** `artifacts/retained_dbs.json`、`artifacts/db_language_map.json`、`artifacts/schema_rename_map.json`、`artifacts/{train,test}_final.jsonl`,运行中的 `pg_base`/`pg_rename`(读取真实列)+ `pg_decoy`/`pg_rename_decoy`(注入)。

**输出:** `artifacts/decoy_map.json`、`artifacts/gold_star_expanded.jsonl`,以及出现在两个 `*_decoy` 实例中的诱饵表/列。

**算法:**
1. **生成 `decoy_map.json`**(若已存在则跳过,重新生成需通过 `--regenerate` 显式开启)。对每个数据库、每个变体(`original`、`obfuscated`):
   - 从对应干净实例的 `information_schema` 读取该变体的真实 schema(original ← `pg_base`,obfuscated ← `pg_rename`)。
   - 用 `random.Random(zlib.crc32(f"{SEED}:{db_id}:{variant}".encode()))` 作为种子(每个数据库独立、可复现,与 `01_split.py` 一致)。
   - 用廉价模型(见下方模板)生成 N 个诱饵表 + 用该数据库语言表达的易混淆列。强制要求:任何名称都不与真实表/列冲突,**也不与 `db_id` 本身冲突**(即 `superhero`/`sales_in_weather`/`university` 这类限定符陷阱);名称 ≤ 63 字节(Postgres 标识符上限);`snake_case`。
2. **计算 `gold_star_expanded.jsonl`。** 用 sqlglot 解析每条标准答案;对那约 5 条带有真实表星号投影(顶层或子查询)的查询,用干净实例的 `information_schema` 把 `*`/`t.*` 展开为显式的真实列清单(在注入诱饵**之前**做这一步,这样展开时只会看到真实列)。产出 `sql_base_expanded` 和 `sql_rename_expanded`。
3. **注入**(幂等,先检查是否已存在):向 `pg_decoy` 应用 `original` 变体,向 `pg_rename_decoy` 应用 `obfuscated` 变体。发出 `CREATE TABLE "db_id"."decoy"( ... )` 和 `ALTER TABLE "db_id"."real" ADD COLUMN "decoy" <type>`,**所有标识符加引号**,**无 FK 约束**,诱饵表**为空**。
4. **重新校验 R1==R2**(验收关卡)。对每个问题,在干净实例上执行干净标准答案(R1),在诱饵实例上执行标准答案(R2),断言 `normalise_result` 相等,对星号问题使用**展开后的** SQL。复用步骤 7 的比较逻辑(现在位于 `_db.py`)。两趟:原始标识符标准答案对 `pg_decoy`;混淆后标准答案对 `pg_rename_decoy`。任何不匹配 → `workdir/decoy_failures.jsonl`(预期:**零**,因为诱饵未被引用且星号已展开)。

**Prompt 模板(诱饵生成):**
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

**参数(CLI):** `--model gpt-5-mini`、`--n-tables-frac 0.4`、`--min-tables 2`、`--max-tables 15`、`--regenerate`、`--limit N`(试运行)、`--validate-only`。

**可续跑性:** `decoy_map.json` 只写一次(只有加 `--regenerate` 才会重新生成);注入在创建前会检查 `information_schema`;校验是只读的关卡。

**验收标准:**
- `decoy_map.json` 存在;每个诱饵名称都通过冲突/长度/`db_id` 检查。
- 两个 `*_decoy` 实例都包含诱饵对象(抽查 `information_schema`)。
- R1==R2 重新校验:**0** 个失败(或只有已存在于 `rename_failures.jsonl` 中的 id)。
- 重新运行步骤 08 是空操作(幂等)。

---

## 6. 步骤 09:`09_paraphrase_questions.py`

**目的:** 为每个问题生成一条改写,以标准答案 SQL + schema 为条件,从而锚定意图。

**输入:** `artifacts/test_final.jsonl`(若加 `--include-train` 则还有 `train_final.jsonl`)。
**输出:** `artifacts/question_paraphrases.jsonl`(`question_id → question_paraphrase`),可续跑。

**算法:** 对每个尚未完成的问题(在 `question_id` 上调用 `load_done_keys`):
- 用 `(original question, gold SQL = sql_rename, obfuscated schema DDL for db_id)` 构建 prompt。
- 调用廉价模型一次(`--model gpt-5-mini`),temperature 约 0.7 以获得词汇多样性。
- system prompt 中的约束:精确保留原意;**只用自然语言**;**不要**提及任何表/列标识符;只返回改写后的问题。
- (可选,`--cosine-check`)用 `text-embedding-3-small` 对原文与改写做嵌入;若余弦 < 0.6,重试一次后保留并打标记。遵照方法学,默认关闭(提供标准答案 SQL 时漂移很低)。
- 用 fsync 追加 `{"question_id", "question_paraphrase"}`(复用 `eval_contamination.py` 中的追加/fsync 模式)。

**验收标准:**
- 每个测试 `question_id` 一条改写(数量与 2,030 个唯一值匹配)。
- 抽查 10 行:原意保留、无标识符泄漏、措辞有实质改动。
- 重新运行是空操作(可续跑)。

---

## 7. 消融评测:`eval_ablation.py`

**目的:** 运行 [evaluation.md §9](../methodology/evaluation-zh.md) 中的 5 个组,并报告带置信区间的配对差值。默认走**离线**准备 → API 生成 → DB 打分;`--local` 保留旧的同机路径。

**离线脚本(分机):**

| 脚本 | 机器 | 作用 |
| --- | --- | --- |
| `prepare_offline_eval.py` | PostgreSQL | 导出 `requests.jsonl` + 私有 `grading_manifest.private.jsonl` |
| `run_offline_generations.py` | API | 从冻结 prompt 调用模型 |
| `grade_offline_eval.py` | PostgreSQL | 执行 gold + 生成 SQL,写入 `eval/*_results.jsonl` |

API 机器可移植公开包:`eval/offline-public-bundles.zip`(git 跟踪)。用 `--split {test,train}` 准备训练包;训练 `paraphrase`/`all` 需 `09_paraphrase_questions.py --include-train`(截至 2026-07-10 共 10,164 行)。

**各组 → (实例、标准答案字段、问题来源):**

| 组 | DSN | 标准答案字段(适用时经 SELECT\* 展开) | 问题 |
| --- | --- | --- | --- |
| `base` | `PG_BASE_DSN` | `sql_base` | `question` |
| `rename` | `PG_RENAME_DSN` | `sql_rename` | `question` |
| `decoy` | `PG_DECOY_DSN` | `sql_base` → 若在 `gold_star_expanded` 中则为 `sql_base_expanded` | `question` |
| `paraphrase` | `PG_BASE_DSN` | `sql_base` | `question_paraphrase` |
| `all` | `PG_RENAME_DECOY_DSN` | `sql_rename` → 若已展开则为 `sql_rename_expanded` | `question_paraphrase` |

**实现:** 扩展 `CONDITION_SPEC` / `DSN_FOR_SCHEMA`(在 `eval_contamination.py` 中已是正确的形状),加入四个新实例和五个组。所有组都**不给提示**(不展示 evidence),这是 §4.2 所述的主信号。模型仍会通过 `get_schema_ddl` 读取诱饵实例的 `information_schema`,从而看到诱饵列(无需特殊处理:诱饵在那里是真实的目录对象)。评分标准是:模型 SQL 与该组标准答案在该组实例上执行后,`normalise_result` 精确相等。

**标准答案展开的关联:** 把 `gold_star_expanded.jsonl` 加载进一个字典;对 `decoy`/`all`,若 `question_id` 存在,则使用展开后的标准答案。这保证诱饵列绝不会泄漏进任何一组的标准答案里。

**输出与汇总:** `eval/ablation_results.jsonl`;一个 `--summarize`,打印各组的 EX、每组相对基线的差值、每个差值的**配对 McNemar 检验**和**自助法置信区间**(把英语对照报告为经验零假设 / 噪声本底,而非零),以及按语言的细分(关联 `db_language_map.json`)。要明确说明 `all − Σ(individual deltas)` **不是**一个干净的交互项(不是完整析因设计)。

**验收标准:**
- 全部 5 个实例可达;`gold_exec_failed` 计数 ≈ 0(标准答案已预先校验)。
- 每组覆盖全部 2,030 个测试问题;按 `(question_id, arm)` 可续跑。
- 汇总打印 EX、差值、置信区间、按语言的表格。

---

## 8. 端到端运行顺序 + 检查清单

> **⚠️ 资源安全:** 在本地 Docker Desktop / WSL 环境下,绝不要让四个 PostgreSQL 实例同时承受大量查询负载。这可能让 WSL 虚拟机 OOM,而在 `fsync=off` 下,一次 OOM 崩溃可能损坏数据卷。只启动某一步骤需要的实例(一次克隆涉及 2 个),运行消融实验时**一次一组**(每组恰好查询一个实例:组与组之间对其余实例执行 `docker compose stop`),把评测的 `--concurrency` 保持 ≤ 3,并且绝不要让步骤 08 的校验趟与消融实验重叠。在 `.wslconfig` 中限制 WSL 虚拟机的内存是兜底手段,并不意味着你就可以把所有实例都跑满负荷;在配置充足的服务器上这条限制不适用。

```bash
# 0. 健全性检查(可选)
uv run python pipeline/eval_contamination.py --summarize

# 1. DB: 添加 compose 服务(§3b),再克隆(§3c)
docker compose up -d
# ...§3c 中的克隆命令...

# 2. decoys
uv run python pipeline/08_inject_decoys.py --limit 20   # 试跑
uv run python pipeline/08_inject_decoys.py              # 全量: generate + inject + validate
#   关卡: workdir/decoy_failures.jsonl 为空

# 3. paraphrase(测试 + 训练,供离线训练臂)
uv run python pipeline/09_paraphrase_questions.py --limit 20
uv run python pipeline/09_paraphrase_questions.py --include-train
#   关卡: artifacts/question_paraphrases.jsonl 含 10,164 个唯一 question_id

# 4. 离线消融(一次一臂)
uv run python pipeline/eval_ablation.py --arms base --prepare-only
# API 机器: run_offline_generations.py --bundle-dir eval/offline/ablation-base
uv run python pipeline/eval_ablation.py --arms base \
  --generations eval/offline/ablation-base/generations.jsonl
uv run python pipeline/eval_ablation.py --summarize
```

**任务检查清单:**
- [ ] §2a `_db.py` 已抽取;`07` + `eval_contamination` 导入它;现有数字不变
- [ ] §2b `_eval_helpers.py` 已抽取(或接受重复)
- [ ] §3b compose 服务 + 数据卷已添加
- [ ] §3c 诱饵实例已克隆且健康(5434、5435)
- [ ] §5 `08_inject_decoys.py`:`decoy_map.json` + `gold_star_expanded.jsonl` + 注入 + **0** 个 R1==R2 失败
- [ ] §6 `09_paraphrase_questions.py`:2,030 条改写,已抽查
- [ ] §7 `eval_ablation.py`:5 个组已运行,`--summarize` 带置信区间
- [ ] AGENTS.md 已更新(运行表中加入步骤 08/09 + `eval_ablation.py`;数据库启动说明);PROGRESS.md 已勾选

---

## 9. 会坑到你的地方(提前预警)

- **以 `db_id` 命名的表**(`superhero`、`sales_in_weather`、`university`):诱饵名称不得等于 `db_id`,注入也不得触碰 schema 限定符。见 sqlglot 不变量。
- 在发出的 DDL 和展开的 SQL 中**给每个标识符加引号**;绝不要假设是小写。
- **`SELECT *` 展开必须在注入之前运行**(它必须只看到真实列)。
- 除非加 `--populate`,否则**诱饵表必须保持为空**。只有在下游智能体能够运行查询时,非空的行数才会暴露破绽;评测只展示剥离后的 DDL。
- **重新克隆会重置诱饵**:如果你曾经从干净源重新克隆某个 `*_decoy` 数据卷,就必须重新运行步骤 08(manifest 让这一步是确定性的)。
- 在 §2a 重构期间**`normalise_result` 不得改变**:与现有 R1==R2 和污染运行的评分等价性都依赖它。
- **保持 `pg_base`/`pg_rename` 干净**:诱饵只进入 `*_decoy` 实例。
