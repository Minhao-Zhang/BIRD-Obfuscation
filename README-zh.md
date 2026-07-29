[English](README.md) · **中文**

# BIRD 混淆

> 对 [BIRD](https://bird-bench.github.io/) Text-to-SQL 基准的一次清洗、抗污染、对抗性重建,
> 作为**语义层**智能体评测的底料而构建,而非用于单轮 Text-to-SQL。

![status](https://img.shields.io/badge/status-active-brightgreen)
![python](https://img.shields.io/badge/python-3.13-blue)
![postgres](https://img.shields.io/badge/PostgreSQL-18-336791)
[![dataset](https://img.shields.io/badge/🤗%20dataset-BIRD__Obfuscation-orange)](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
[![agent eval](https://img.shields.io/badge/agent%20eval-governed--bi-8A2BE2)](https://github.com/Minhao-Zhang/governed-bi)
[![License: CC BY-SA 4.0](https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-sa/4.0/)

**58 个数据库 · 6,928 对题目/SQL · 5,539 train / 1,389 test · 4 种混淆变体**

## 本项目解决的三个问题

| 问题 | 做法 |
| --- | --- |
| **污染。** BIRD 把题目、gold SQL 和 schema 名称一并公开,前沿模型的分数可能来自*见过这个基准*。 | schema 标识符被重命名为五种语言之一;题目做保持 SQL 不变的改写。每个面都是可独立开关的维度。 |
| **gold SQL 错误。** BIRD 自身的标注存在大量且*系统性*的错误。已发表的审计给出 49-61% 的错误率,上游此后也已修正或撤回其中相当一部分。 | 通过与官方的 [`bird_sql_dev_20251106`](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106) 和 [`bird23-train-filtered`](https://huggingface.co/datasets/birdsql/bird23-train-filtered) release 对齐,移除了 2,739 个问题;随后剔除 11 个题量不足的数据库并重新划分。每个问题的溯源信息随 `gold_quality_flags.jsonl` 一并发布。 |
| **没有可供探测的对抗物。** 靠*执行查询*来探索数据库的智能体,在原版 BIRD 里遇不到任何陷阱。 | 1,486 个被污染的"邪恶双胞胎"列和 162 个克隆表,以看似合理的同义词命名,存放真实数据的细微错误副本。严格增量,因此基准真值任务可证明地保持完整。 |

问题过滤是这个数据集提供的能力之一,不是需要致歉的地方。gold 质量在这里比在逐题基准里更要紧:
语料中一对错误的(问题, SQL)不是浪费一行,而是教会模型一个会传播的错误映射。完整证据、方法与
引用见 [gold-quality-audit.md](docs/reference/gold-quality-audit-zh.md)。

## 用途

本数据集面向的是**语义层**任务,而不是单轮 Text-to-SQL。智能体读取 `train` 划分,也就是某个 schema 上的(问题, SQL)对,从中归纳出一个可复用的映射,
然后在同一个 schema 上回答未见过的 `test` 问题,
而不会被提供那份映射。评测框架、指标(`decoy_touch_rate`、`routing_recall`)与结果都在
[**governed-bi**](https://github.com/Minhao-Zhang/governed-bi)。

对数据本身有两点推论。每个 schema 的题量接近自变量:能归纳出多少语义层,取决于此前有多少*正确*
的题目。保留的 58 个 schema 语料规模跨 60 到 383,因此报告任何逐 schema 结果时请一并给出规模。另外,
划分边界不能泄漏:语料中一条与测试题近重复的问题,会让智能体用检索代替归纳。这一点目前尚未量化,
见 [gold-quality-audit.md §6](docs/reference/gold-quality-audit-zh.md)。

另有一项独立的小型五臂混淆评测(`base` / `rename` / `decoy` / `paraphrase` / `all`),在给全上下文
的条件下一次性运行。它的结论仅止于一个朴素的事实:重命名确实抹掉了前沿模型中的一部分记忆信息。
设计与数字见 [evaluation.md](docs/methodology/evaluation-zh.md) §8 与 §9。那些数字测于 gold 质量
清理之前,因此已过时。

## 获取数据集

两个存放位置:**数据库**在 Hugging Face(≈12 GB,过大无法入 git),**gold SQL 与清单**受 git
跟踪,位于 [`eval_dataset/`](eval_dataset/)。

```bash
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
docker compose --profile decoy up -d
docker compose cp bird_obf_dumps/pg_base.dump pg_base:/tmp/pg_base.dump
docker compose exec pg_base pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
```

对 `pg_rename`、`pg_decoy`、`pg_rename_decoy` 重复一次,在笔记本上**最多同时开两个**(否则
OOM)。完整的恢复与评测步骤见
[using-the-dataset.md](docs/reference/using-the-dataset-zh.md)。评测脚本优先读 `artifacts/`,
回退到 `eval_dataset/`,因此全新克隆无需重新生成即可运行;DSN 可通过 `PG_*_DSN` 环境变量指向
远端 Postgres。

## 文档

| 文档 | 内容 |
| --- | --- |
| [gold-quality-audit.md](docs/reference/gold-quality-audit-zh.md) | BIRD 的标注错误、清理、重新划分,以及仍待定的事项 |
| [dataset.md](docs/methodology/dataset-zh.md) | schema 数据湖、纳入标准、train/test 划分 |
| [obfuscation.md](docs/methodology/obfuscation-zh.md) | 混淆设计与物理实现 |
| [evaluation.md](docs/methodology/evaluation-zh.md) | 完整性检查、污染差值、消融 |
| [corrupted-decoys-design.md](docs/reference/corrupted-decoys-design-zh.md) | 陷阱设计、风险登记册、竣工参数 |
| [limitations.md](docs/reference/limitations-zh.md) | 范围注意事项;引用任何数字前请先读 |
| [using-the-dataset.md](docs/reference/using-the-dataset-zh.md) | 下载、恢复、运行 |
| [pipeline-invariants.md](docs/reference/pipeline-invariants-zh.md) | 每条流水线规则为何存在,附经验证据 |
| [development.md](docs/development.md) | 运行与扩展流水线:环境、约定、不变量(仅英文) |

## 许可

[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)。可自由共享与改编,需署名并以
相同许可分发。本项目是 [BIRD 基准](https://bird-bench.github.io/)的衍生作品;使用本数据集时请
注明 BIRD 为上游来源。
