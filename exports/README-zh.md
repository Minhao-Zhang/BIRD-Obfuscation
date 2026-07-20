[English](README.md) · **中文**

# 评测导出:问题 + gold + 生成的 SQL

这是一组扁平、自包含的表:把每个基准问题、它的 gold SQL、模型生成的 SQL,以及执行准确率判定放在一起。设计目标是脱离 PostgreSQL 机器和评测框架也能直接复用——加载 JSONL(或 CSV)即可用。

这里的全部数据来自 **test 划分**上的 **`claude opus 4.8 high`** 运行(2,030 个问题)。这些行汇总后的聚合数字见 [../docs/methodology/evaluation.md](../docs/methodology/evaluation-zh.md) §8(污染)与 §9.4(消融)。

> 运行名里的 `high` 是记录下来的努力强度标签;本次运行实际并未对 `Claude-Opus-4.8` 生效(端点默认设置)。见 [evaluation.md §8](../docs/methodology/evaluation-zh.md) 的说明。

## 文件

数据以一个压缩包发布;松散的 `.jsonl`/`.csv` 已被 git 忽略,可按下文重新生成。

**`Claude-Opus-4.8_high_qa_sql.zip`** —— 解压后得到四个文件:

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `contamination_qsql.jsonl` / `.csv` | 8,120 | 4 条件 × 2,030:schema(base/rename)× 提示(hint/nohint) |
| `ablation_qsql.jsonl` / `.csv` | 10,150 | 5 臂 × 2,030:base、rename、decoy、paraphrase、all |

JSONL 与 CSV 的行和列完全相同,按工具习惯任选其一。UTF-8 编码;CSV 按 RFC 4180 加引号(含逗号/换行的 SQL 文本也安全)。

## 列

| 列 | 含义 |
| --- | --- |
| `eval` | `contamination` 或 `ablation` |
| `condition` | 污染条件(`base_hint`、`base_nohint`、`rename_hint`、`rename_nohint`)或消融臂(`base`、`rename`、`decoy`、`paraphrase`、`all`) |
| `question_id` | BIRD 源问题 id(稳定的连接键) |
| `db_id` | 数据库名 |
| `obfuscation_language` | `english`(恒等/对照)、`french`、`german`、`spanish`、`pinyin` |
| `difficulty` | 有则为 BIRD 难度标签(train 源问题为空) |
| `schema_instance` | SQL 运行在哪个 PostgreSQL 实例上:`base`、`rename`、`decoy`、`rename_decoy` |
| `hints` | 是否展示了 evidence 提示(仅污染的 `*_hint` 为 `true`) |
| `question` | 展示给模型的确切自然语言问题(`paraphrase`/`all` 臂为改写版,其余为原始版) |
| `evidence` | 展示的 evidence 提示文本(无提示时为空) |
| `gold_sql` | 用于打分的确切 gold SQL(decoy 臂为 `SELECT *` 展开版) |
| `generated_sql` | 模型输出的 SQL |
| `correct` | 执行准确率,宽松(BIRD 风格,类型折叠) |
| `correct_strict` | 执行准确率,严格(不做跨类型折叠) |
| `error` | 打分失败原因(`result_mismatch`、`generated_exec_failed` 等),正确时为空 |
| `model` / `effort` | 生成所用模型与推理强度(`Claude-Opus-4.8` / `high`) |

## 如何生成

```bash
uv run python pipeline/export_qa_sql.py --model "Claude-Opus-4.8" --effort high
```

它会写出松散的 `.jsonl`/`.csv`,并打包成入库的 `.zip`。脚本把打分结果(`eval/*_results.jsonl`)与各条件的 gold SQL(离线打分清单)、问题与难度(`test_final.jsonl`)、改写文本(`question_paraphrases.jsonl`)、混淆语言(`db_language_map.json`)连接起来。跑完新评测后重新执行即可刷新这些文件。

## 复用说明

- **每个问题在每个条件/臂里各出现一次**,所以同一个 `question_id` 会以不同的 schema、gold 和生成 SQL 反复出现。按 `condition` 过滤即可得到单一视图。
- `generated_sql` 只反映某一次一次性(one-shot)运行,不是标准答案。需要正确的参考查询时用 `gold_sql`。
- `english` 行是噪声下限对照(恒等重命名),不是一个混淆臂——见 [../docs/reference/limitations.md](../docs/reference/limitations-zh.md) §1。
- 若要自己执行这些 SQL,请按 [../docs/reference/using-the-dataset.md](../docs/reference/using-the-dataset-zh.md) 恢复 PostgreSQL 实例。
