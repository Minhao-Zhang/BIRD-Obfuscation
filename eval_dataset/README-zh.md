[English](README.md) · **中文**

# 最终评测数据集

这是 BIRD text-to-SQL **混淆基准** 的交付物,已冻结、**受 git 跟踪**:经过验证的 gold
问题/SQL 配对,外加评测所需的全部映射与清单。它是 **`artifacts/` 的快照**(即流水线的
工作目录,其中若干文件因体积过大或可由 LLM 重新生成而被 gitignore 忽略)。

> **数据库实例本身**(这些 gold 配对赖以运行的四个 PostgreSQL dump)体积太大,无法纳入
> git,已托管在 Hugging Face:
> [minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)。
> 若要下载、恢复并端到端运行评测,参见
> [../docs/reference/using-the-dataset.md](../docs/reference/using-the-dataset-zh.md)。

每次重建之后,用下面的命令刷新这份快照:

```bash
python eval_dataset/build_eval_dataset.py
```

`build_eval_dataset.py` 才是内容的权威定义;本 README 只是供人阅读的索引。

---

## 四个数据库实例

这个基准跑在四个 PostgreSQL 18 实例上(Docker,后两个用 `decoy` compose profile)。
**本地切勿同时运行超过 2 个热实例**(会导致 OOM,参见
[AGENTS.md](../AGENTS.md));在已配置好的服务器上则不受此限制。

| 实例 | 端口 | 标识符 | 诱饵/陷阱 | 混淆维度 |
| --- | --- | --- | --- | --- |
| `pg_base` | 5432 | 原始英文 | 无 | -(对照) |
| `pg_rename` | 5433 | 重命名(目标语言) | 无 | 重命名 |
| `pg_decoy` | 5434 | 原始英文 | 损坏陷阱 | 诱饵 |
| `pg_rename_decoy` | 5435 | 重命名 | 损坏陷阱 | 重命名 + 诱饵 |

真实数据在全部四个实例中逐字节相同(陷阱是严格增量式的)。不同的只有两点:标识符,以及是否存在诱饵列/表。

---

## 文件

### Gold 问题 / SQL 数据集(基准本身)
- **`train_final.jsonl`**(8,134):经过验证的训练集划分。
- **`test_final.jsonl`**(2,030):经过验证的测试集划分;**评测就基于这份数据运行**。

  字段(两者相同):`question_id`、`db_id`、`question`、`evidence`、`evidence_rename`、
  `difficulty`、`sql_sqlite`(原始 BIRD gold)、`sql_base`(为 `pg_base` 转译的 gold)、
  `sql_rename`(为 `pg_rename` 改写的 gold)。每一个保留的配对都经过 R1==R2 验证:
  `sql_base` 在 `pg_base` 上与 `sql_rename` 在 `pg_rename` 上返回相同的结果。

### 维度 1:标识符重命名
- **`schema_rename_map.json`**:按数据库组织、针对表和列的
  `{english_identifier: renamed_identifier}`。它是重命名维度的 ground truth;也是把清单里
  任何英文名解析成对应重命名形式的依据。
- **`db_language_map.json`**:按数据库组织的重命名目标语言
  (english / french / german / spanish / pinyin / …)。

### 维度 2:诱饵 / 陷阱(增量式;位于 `*_decoy` 实例上)
- **`trap_manifest.json`**:“邪恶双胞胎”诱饵**列**(某个真实列的损坏副本,用同义词命名)。每条记录:`db, table, source_column, source_type, operator, is_key,
  in_correlated_group, salt, names:{base, rename}`。添加到 `<db>.<table>` 的诱饵列即
  `names.<variant>`。
- **`trap_table_manifest.json`**:损坏的诱饵克隆**表**。每条记录:
  `db, source_table, columns:[{source_column, source_type, operator, is_key}],
  names:{base:{table, columns}, rename:{table, columns}}`。`operator: null` = 精确复制
  (未损坏)。在构造上即保证 R1==R2 安全(gold 从不引用诱饵表)。
- **`decoy_map.json`**:step-08 的**结构性**(空)诱饵表/列。对于交互式的“执行并观察”
  智能体,它已被上文的损坏陷阱*取代*(空诱饵会自我暴露);保留它是为了留存出处。

  `names.base` 是英文诱饵标识符,`names.rename` 则是目标语言里的对应形式。损坏用的 `salt` 与变体
  无关,因此 `pg_decoy` 和 `pg_rename_decoy` 会以相同方式损坏相同的行。设计与算子:
  [docs/reference/corrupted-decoys-design.md](../docs/reference/corrupted-decoys-design-zh.md)。

### 维度 3:问题改写
- **`question_paraphrases.jsonl`**:每个问题的 SQL 保持不变的改写
  (`question_id -> question_paraphrase`)。`eval_dataset/` 快照含 2,030 条测试改写;
  训练改写(再加 8,134 条)在 `artifacts/question_paraphrases.jsonl`,需步骤 09 加 `--include-train`。

### 评测支持
- **`gold_star_expanded.jsonl`**:为约 5 条 star 查询提供的 `SELECT *` 展开版 gold
  (`sql_base_expanded` / `sql_rename_expanded`),以确保诱饵列绝不会泄漏进 gold 答案。
- **`order_sensitive_qids.json`**:需要**从严格 EX 评分中排除**的 qid:`order_sensitive`
  (153 个:gold 带有 `LIMIT` 但没有全序,或含浮点聚合,因此陷阱 UPDATE 引起的堆重排会在
  诱饵实例上产生不同但仍有效的结果)+ `exec_failed`(21 个:本就存在的退化 BIRD gold,
  >200k 行 / 60s 超时)。真实数据已验证完好无损;这些属于比较层面的假象,而非损坏。
- **`gold_result_hashes_rename_decoy.jsonl`**：`pg_rename_decoy` 上全部 train/test gold
  SQL 结果的宽松与严格 SHA-256 哈希。对模型结果算同样哈希再比对即可，不必再跑 gold。
  字段与算法见
  [docs/reference/gold-result-hashes-zh.md](../docs/reference/gold-result-hashes-zh.md)。
  重建：`uv run python pipeline/precompute_gold_result_hashes.py`。

---

## 评测臂 → (实例、gold 字段、问题来源)

5 臂无提示消融实验(参见 [pipeline/eval_ablation.py](../pipeline/eval_ablation.py)):

| 臂 | 实例 | 端口 | gold SQL 字段 | 问题文本 |
| --- | --- | --- | --- | --- |
| `base` | `pg_base` | 5432 | `sql_base` | `question` |
| `rename` | `pg_rename` | 5433 | `sql_rename` | `question` |
| `decoy` | `pg_decoy` | 5434 | `sql_base`(star 展开) | `question` |
| `paraphrase` | `pg_base` | 5432 | `sql_base` | `question_paraphrase` |
| `all` | `pg_rename_decoy` | 5435 | `sql_rename`(star 展开) | `question_paraphrase` |

---

## 运行离线评测

```bash
# PostgreSQL 机器:一次一臂,最多热两个实例
uv run python pipeline/eval_ablation.py --arms base --prepare-only

# API 机器
uv run python pipeline/run_offline_generations.py \
  --bundle-dir eval/offline/ablation-base --model "Claude-Opus-4.8" --effort high

# PostgreSQL 机器,拷回 generations.jsonl 后
uv run python pipeline/eval_ablation.py --arms base \
  --generations eval/offline/ablation-base/generations.jsonl \
  --model "Claude-Opus-4.8" --effort high
```

评测脚本(`eval_ablation.py`、`eval_contamination.py`、`probe_schema_recall.py`)通过
`_eval_helpers.dataset_path(name)` 解析每个输入:**若存在 `artifacts/<name>` 则优先使用,
否则回退到 `eval_dataset/<name>`。** 因此,完整的本地检出会使用 `artifacts/` 中的工作副本;
而全新克隆若只有这个已入库文件夹(没有 `artifacts/`),就会自动针对快照运行:无需任何
flag,也无需改动。如果你保留了已填充的 `artifacts/`,请在重建后重新运行
`build_eval_dataset.py` 以刷新此快照。下游使用者应当以**这个**文件夹为准。
