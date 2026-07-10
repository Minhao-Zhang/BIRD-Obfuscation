[English](AGENTS.md) · **中文**

# BIRDBench

本仓库将 [BIRD benchmark](https://bird-bench.github.io/) 数据集转换为**混淆后的 Text-to-SQL 数据集**,用于衡量基准测试的准确率有多少依赖于记忆下来的 schema 标识符,并在多数据库的 schema lake 上对"执行并观察"(execute-and-observe)型 SQL agent 进行压力测试。

## 目标

标准的 BIRD benchmark 是公开的,问题、gold SQL、schema 标识符都在其中。前沿语言模型在训练时可能已经见过其中一部分内容。本项目旨在通过重命名表名和列名,削弱记忆下来的 BIRD schema 标识符的作用,同时保留语义上有意义的 SQL 任务。

下游任务模拟了一个 agentic 的 Text-to-SQL 场景:agent 从已知的真实 SQL 和剥离后的 schema 元数据中构建记忆,然后针对混淆后的 schema lake 回答留出的(held-out)自然语言问题。本仓库负责准备并验证该数据集,本身并不评测 schema routing。在本文描述的评测中,会预先给定正确的数据库。

要了解完整的方法论以及每一项设计决策背后的理由,见 [docs/methodology/](docs/methodology/)(混淆、数据集、评测以及混淆扩展)。本文件**仅为操作指南**:如何运行和扩展该 pipeline,以及在改动它时需要保持的不变量(invariants)。项目历史、状态和决策记录在 [PROGRESS.md](PROGRESS-zh.md);下面这些不变量的详细经验性理由见 [docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants-zh.md)。

## 数据

`data/` 目录存放 BIRD 数据集(不纳入版本控制)。下载说明、目录结构和文件格式见 [data/README.md](data/README-zh.md)。

- **Dev split**:11 个 SQLite 数据库,1,534 个问题
- **Train split**:73 个 SQLite 数据库,9,428 个问题
- 每个问题都包含一个自然语言问题、可选的 evidence 提示、gold SQL,以及一个难度标签(`simple` / `moderate` / `challenging`)

## 运行 pipeline

在仓库根目录下用 `uv run python pipeline/<script>.py` 按顺序运行各脚本。每一步都会读取上一步的输出;不要跳过或重新排序。

| # | 脚本 | 需先运行的前置步骤 |
| --- | --- | --- |
| 0 | `00_audit_sqlite_identifiers.py` | 步骤 1(`retained_dbs.json`) |
| 1 | `01_split.py` | - |
| 2 | `02_assign_languages.py` | 步骤 1 |
| 3 | `03_generate_rename_map.py` | 步骤 1-2 |

`artifacts/schema_rename_map.json` 是 **git 追踪** 的(可通过步骤 3 用 Bedrock 重新生成,但已签入仓库,这样步骤 6-7 及下游就不必重新运行 LLM 翻译)。`artifacts/db_language_map.json` 仍然被 gitignore(可由步骤 2 确定性地生成)。
| 3b | `03b_check_translation_quality.py` | 步骤 3(仅供参考,见下文) |
| 4 | `04_load_pg_base.py` | 步骤 1,`pg_base` 正在运行 |
| 5 | `05_transpile_sql.py` | 步骤 1、4 |
| 5b | `05b_apply_sql_fixes.py` | 步骤 5 的 pass 1 + 由 agent 编写的 `transpilation_fixes.jsonl` |
| 5c | `05c_export_fix_batch.py` | 步骤 5 的 pass 1(仅供参考,为 agent 导出批次) |
| 6 | `06_build_pg_rename.py` | 步骤 1、3,`pg_base_data` volume 已克隆到 `pg_rename_data`(见下文),`pg_rename` 正在运行 |
| 7 | `07_rename_sql_and_validate.py` | 步骤 3、5、6,两个 PG 实例都在运行 |
| 8 | `08_inject_decoys.py` | 步骤 3、7,两个 `*_decoy` 实例都已克隆并运行(扩展混淆,见下文) |
| 9 | `09_paraphrase_questions.py` | 步骤 7,`pg_rename` 正在运行(扩展混淆) |

先启动两个 PostgreSQL 实例:`docker compose up -d`。`pg_base` 是 `127.0.0.1:5432`,`pg_rename` 是 `127.0.0.1:5433`(两者的 DSN 都是:`dbname=bird user=bird password=bird`)。步骤 4 只需要 Docker 处于运行状态:pgloader 本身作为容器(`dimitri/pgloader:v3.6.7`)运行,不需要在宿主机上安装。

步骤 0 是诊断性的(不在关键路径上)。当你新增一个源数据库或改动标识符处理逻辑时,在步骤 4 之前运行它,以便在有风险的标识符进入 loader 之前就把它们揪出来。见 [docs/reference/audit-findings.md](docs/reference/audit-findings-zh.md)。

步骤 3b 仅供参考,不是一道关卡(gate)。它会将 `schema_rename_map.json` 与 BIRD 的 `database_description/*.csv` 交叉核对,并把有疑问的翻译写入 `artifacts/translation_quality_flags.jsonl` 供人工复核;它绝不会修改 rename map。请在步骤 3 之后、步骤 6-7 消费该 map 之前运行它。详情:[docs/methodology/obfuscation.md §4](docs/methodology/obfuscation-zh.md)。

步骤 5 的 pass 1(`05_transpile_sql.py`)只用 sqlglot(不涉及 LLM):它转译 gold SQL,校验 R0==R1,把匹配的写入 `workdir/*_transpiled.jsonl`,并把不匹配的排入 `workdir/transpilation_needs_fix.jsonl`。pass 2 是由 agent 手动修复,将 `{"question_id", "sql_base"}` 追加到 `workdir/transpilation_fixes.jsonl`,再由 `05b_apply_sql_fixes.py` 合并;`05c_export_fix_batch.py` 导出批次;`--status` 显示进度。

**Artifact 语义、R0==R1 的定义、VALUES 物化,以及失败分桶(failure buckets):** 见 [docs/reference/step5-transpilation.md](docs/reference/step5-transpilation-zh.md)。

**步骤 6 要求在运行脚本之前,`pg_rename` 的 Docker volume 必须是 `pg_base` 的一份克隆。** `pg_base` 和 `pg_rename` 是两个独立的容器,各有独立的命名 volume(`pg_base_data`、`pg_rename_data`);`06_build_pg_rename.py` 已经完全不再读取 SQLite。它只是在已经填充好数据的 `pg_rename` 里重命名表/列。请先克隆 volume:

```bash
docker compose stop pg_base
docker run --rm -v pg_base_data:/from:ro -v pg_rename_data:/to alpine sh -c "rm -rf /to/* && cp -a /from/. /to/"
docker compose start pg_base
docker compose up -d pg_rename
uv run python pipeline/06_build_pg_rename.py
```

这次克隆之所以安全,靠的正是 `:ro` 只读源挂载。这些 volume 名由 Compose 自动生成。运行 `docker volume ls` 确认带前缀的确切名称(例如 `bird-data-obfuscation_pg_base_data`)。

步骤 7(`07_rename_sql_and_validate.py`)应用 rename map 并检查 R1==R2(`pg_base` 上的 `sql_base` 与 `pg_rename` 上的 `sql_rename` 结果相等),把匹配的写入 `artifacts/{train,test}_final.jsonl`(即最终交付物),把失败的写入 `workdir/rename_failures.jsonl`。可通过 `question_id` 断点续跑;用 `wc -l artifacts/*_final.jsonl workdir/rename_failures.jsonl` 查看进度。已验证的数量:[docs/methodology/dataset.md §7](docs/methodology/dataset-zh.md)。

`pipeline/eval_contamination.py` 是下游的四条件混淆有效性评测,不属于带编号的 pipeline 步骤(编号范围到步骤 7 为止)。默认走离线分机流程:在 PostgreSQL 机器准备公开请求包,在 API 机器运行 `run_offline_generations.py`,再把生成结果拿回 DB 机器打分。`--split {test,train}` 选择数据集;`--local` 显式启用旧的同机路径。详情:[docs/methodology/evaluation.md §4](docs/methodology/evaluation-zh.md)和 [docs/reference/using-the-dataset.md §3](docs/reference/using-the-dataset-zh.md)。

## 扩展混淆(decoy + paraphrase)

可选的 decoy/paraphrase 维度步骤及其消融实验(ablation):设计见 [docs/methodology/obfuscation-extensions.md](docs/methodology/obfuscation-extensions-zh.md),完整的构建规范见 [docs/reference/extension-implementation-plan.md](docs/reference/extension-implementation-plan-zh.md)。

另有两个 PostgreSQL 实例存放增加了 decoy 的克隆,受 `decoy` 这个 compose profile 控制(默认的 `docker compose up -d` 不受影响):`pg_decoy`(5434)、`pg_rename_decoy`(5435)。构建方式是先克隆干净的 volume,再注入:克隆命令见 [extension-implementation-plan.md §3c](docs/reference/extension-implementation-plan-zh.md)。

**⚠️ 不要在本地的 Docker Desktop / WSL 环境下让四个实例同时高负载运行。这可能导致 WSL VM 发生 OOM,而在 `fsync=off` 的情况下,一次 OOM 崩溃可能损坏这些 volume。** 只启动当前步骤/实验臂(arm)所需要的实例(一次克隆涉及 2 个;每个消融实验臂恰好只查询 1 个:`base`/`paraphrase`→`pg_base`、`rename`→`pg_rename`、`decoy`→`pg_decoy`、`all`→`pg_rename_decoy`,因此请顺序运行 `eval_ablation.py --arms <one>`,并用 `docker compose stop` 停掉其余实例)。把评测的 `--concurrency` 保持在较低水平(≤3),并且绝不要让步骤 08 的校验(validate)过程与消融评测重叠。在 `.wslconfig` 中限制 WSL VM 的内存,是个有用的兜底。在配置充足的服务器上,这条限制并不适用。

- `08_inject_decoys.py`:生成 `artifacts/decoy_map.json`(廉价 LLM,带随机种子),向两个 `*_decoy` 实例注入 decoy 表 + 易混淆的列,展开少数几个 `SELECT *` 的 gold 查询(`artifacts/gold_star_expanded.jsonl`),并重新校验 R1==R2(验收关卡 → `workdir/decoy_failures.jsonl`,预期为 0)。`--phase {generate,inject,validate,all}`、`--regenerate`、`--validate-only`。
- `09_paraphrase_questions.py`:为每个问题生成一条以 SQL 为条件的 paraphrase → `artifacts/question_paraphrases.jsonl`(可断点续跑;`--model` 在运行时选择,`--concurrency`)。
- `10_inject_traps.py`:面向交互式"执行并观察"型 agent 范式的被破坏 decoy **陷阱(traps)**(空的/NULL 的 decoy 会自行暴露、毫无代价)。在 `*_decoy` 实例上严格采用**追加式(additive)**的被破坏副本,因此真实的列/表保持逐字节一致,R1==R2 依然成立。阶段 1 = 邪恶双胞胎列(`--phase rowcounts,plan,name,inject`,≤500k 行的表);阶段 2 = 被破坏的克隆表(`--phase plan-tables,name-tables,inject-tables`,≤50k 行的源表,由于 gold 从不引用 decoy 表,所以对 R1==R2 没有影响)。基于哈希种子的确定性破坏,使用**与变体无关(variant-independent)**的 salt(对连接键做置换 → 保持引用完整性(RI);其余情况做稀疏扰动/类别重映射/日期偏移/置空);每个变体由 LLM 命名(`--model`、`--effort`)。产出 `artifacts/trap_manifest.json` + `artifacts/trap_table_manifest.json`。用 `--variants base|rename` **一次只注入一个变体**(OOM);`--regenerate` 会先删除再重建。设计 + 风险登记表:[docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design-zh.md)。
- `pipeline/eval_ablation.py`:5 臂无提示消融(base/rename/decoy/paraphrase/all),默认与污染评测相同的离线准备/生成/打分流程;依赖步骤 08 产出 + 步骤 09 改写。训练 `paraphrase`/`all` 臂还需步骤 09 加 `--include-train`。`--summarize` 打印 EX/差值/置信区间。设计:[docs/methodology/evaluation.md §9](docs/methodology/evaluation-zh.md)。

## 需要保持的不变量

在编辑 pipeline 时需要遵守的规则。**在改动某条规则所保护的代码之前,请先阅读 [docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants-zh.md) 中的详细理由和经验证据**。每一条都是在真实运行的 Postgres 和真实的最坏情况数据库上确认过的,而非凭空假设。

**步骤 4(pgloader 加载):**

- 以容器方式运行(`dimitri/pgloader:v3.6.7`),而非宿主机安装;`load_db()` 以只读方式 bind-mount SQLite 文件,并通过 `/dev/stdin`(不是 `-`)把 LOAD 脚本管道传入。它会把一份未重命名的精确副本加载到 `db_id.table_name`。
- 通过 `host.docker.internal` 访问 `pg_base`(pgloader 的 DSN 语法不接受 Compose 服务名中的下划线)。
- WITH 子句是 `create tables, create no indexes, quote identifiers, no foreign keys` + CAST 规则。**不要**加回 `reset sequences`、`create indexes` 或 `foreign keys`。每一项都会触发一个已确认的 pgloader bug,而且 `no foreign keys` 同时也是一项方法论上的选择(§2 "Deliberately absent")。
- CAST 规则对于 `DEFAULT CURRENT_TIMESTAMP`→`timestamptz`、`'0000-00-00'`→`date` 以及 `blob`→`text` 是必需的;没有它们,加载会中止或悄无声息地丢弃行。
- `EXTRA_CASTS`(列级的 `to bigint`,**不带 `using`**)用于防范 FIXNUM 溢出导致的**挂起**:pgloader 会停在 0% CPU 上卡住,而不报错。要把 `EXTRA_CASTS` 列在全局 CAST 规则**之前**(先匹配者胜)。
- 每次加载都要用 `verify_casing()` + `verify_row_counts()` 来验证:仅靠 `check=True` 是不够的:即便发生丢数据的失败,pgloader 也会以 0 退出。

**sqlglot 转译 / 重命名(步骤 5、7):**

- 对每一个输出的标识符都加引号:不要搞"看起来是小写就跳过加引号"的特例。
- schema 限定会跳过 CTE 别名(一个 `WITH x` 引用会被解析为 `exp.Table`)。
- 绝不要在 `walk()` 过程中修改 sqlglot 节点:先收集节点,再修改(否则会陷入无限循环 + 内存无上限增长)。
- 绝不要重命名作为某个 Table 的 `db`/`catalog` 参数的 `Identifier`(有 3 个数据库(`superhero`、`sales_in_weather`、`university`)的某张表与其 `db_id` 同名)。
- fix-batch 的 schema 上下文来自 `pg_base` 的 `information_schema`,而不是 SQLite。
- evidence 提示会和 SQL 套用同一份 rename-map 替换(`evidence` 和 `evidence_rename` 都会被输出;下游消费混淆后的那个)。

**步骤 6:** 在预先克隆好的 `pg_rename` volume 内部原地重命名。它**不会**重新加载 SQLite,也不会连接 `pg_base`(以免第二次类型推断跑出不一致的结果)。`_pg_helpers.py` 中的 `infer_pg_type()`/`copy_data()`/`get_sqlite_schema()` 没有用到;`find_sqlite_path()` 步骤 0 仍在用。

**执行与连接:**

- 步骤 7 的 `exec_pg()` 使用 `fetchmany(MAX_RESULT_ROWS)`,绝不用 `fetchall()`(有一条 gold 查询会返回 19.4M 行)。
- 在 `autocommit=True` 下使用普通的 `SET`,而不是 `SET LOCAL`(后者会静默地什么都不做)。
- Postgres 的 DSN **默认** 使用 `host=127.0.0.1`,绝不用 `localhost`(在此环境下会带来 20s+ 的 IPv6 开销)。可以通过 `PG_*_DSN` 环境变量(`_db.py`,见 `.env.example`)按实例覆盖它们,以指向远程 Postgres / AWS RDS;本地默认值请保持在 `127.0.0.1`。

**横切事项:**

- 每次读取 `schema_rename_map.json` / 问题 / evidence 文本时都要显式传入 `encoding="utf-8"`(Windows 默认使用 `cp1252`,在遇到非 ASCII 标识符时会崩溃)。
- `01_split.py` 保持逐数据库独立且可复现:以 `zlib.crc32((SEED, db_id))` 作为种子,绝不用带 salt 的 `hash()`,也绝不在多个数据库之间共享同一个 `Random`。
- 保留 Docker Compose 的 WAL 调优(`fsync=off`、`wal_level=minimal`、…):这是为了批量加载速度;两个数据库都是可重建的。

## Python

凡是涉及 Python 的操作,一律用 `uv` 运行:

```bash
uv run python script.py
uv run pytest
uv pip install <package>
```

`.venv` 目录由 `uv` 管理。不要手动激活 venv,也不要使用裸的 `python`/`pip` 命令。
