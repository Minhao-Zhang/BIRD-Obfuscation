[English](README.md) · **中文**

# BIRD 混淆

> 对 [BIRD](https://bird-bench.github.io/) Text-to-SQL 基准的抗污染、对抗性重建——
> 作为一个面向"执行并观察"型 SQL 智能体的评测数据集精心构建。schema 标识符被重命名,
> 注入损坏的"诱饵"陷阱,题目被改写,而 gold 答案在四个并行的数据库版本间自动校验一致。

![status](https://img.shields.io/badge/status-active-brightgreen)
![python](https://img.shields.io/badge/python-3.13-blue)
![postgres](https://img.shields.io/badge/PostgreSQL-18-336791)
[![dataset](https://img.shields.io/badge/🤗%20dataset-BIRD__Obfuscation-orange)](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
[![agent eval](https://img.shields.io/badge/agent%20eval-governed--bi-8A2BE2)](https://github.com/Minhao-Zhang/governed-bi)
[![License: CC BY-SA 4.0](https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-sa/4.0/)

像 BIRD 这样的公开基准会把题目、gold SQL 和 schema 名称一并公开,前沿模型的分数因此
可能有一部分来自*见过这个基准*,而不是来自对眼前 schema 的推理——而一个靠执行查询来
探索数据库的智能体,更是没有任何对抗性的东西需要应对。本项目把 BIRD 重建成这样一个
评测数据集:**(a)** 剥离可记忆的表层信息(重命名标识符、改写题目),并且 **(b)** 主动
反击探测 schema 的智能体(损坏的"诱饵"列和克隆表),同时**可证明地**保留 SQL 任务本身。
该数据集是一个独立下游智能体 [**governed-bi**](https://github.com/Minhao-Zhang/governed-bi)
的底料——后者会被考核:它有多少时候落在真实 schema 上,而不是咬钩上当。

```mermaid
flowchart LR
    subgraph THISREPO["本仓库 — 数据集构建"]
        BIRD["原始 BIRD SQLite"] --> PIPE["10 步流水线:<br/>重命名 · 诱饵陷阱 · 改写"]
        PIPE --> DB["4 个 Postgres 实例:<br/>base · rename · decoy · rename+decoy"]
        PIPE --> GOLD["Gold SQL + 陷阱清单<br/>(eval_dataset/)"]
    end
    subgraph GOV["governed-bi — 下游智能体评测"]
        AGENT["执行并观察型 SQL 智能体:<br/>inspect_schema · sample_rows · run_query"]
        AGENT --> METRICS["EX · decoy_touch_rate · routing_recall"]
    end
    DB --> AGENT
    GOLD --> AGENT
```

## 一览

| | |
| --- | --- |
| **问题** | 前沿模型可能靠记住 BIRD 标识符来虚高 Text-to-SQL 分数,而探测 schema 的智能体又没有任何对抗性障碍需要应对。 |
| **交付物** | 一个多语言 PostgreSQL Text-to-SQL 语料库,覆盖 69 个数据库(10,164 对经执行验证的题目/SQL),提供四种混淆变体,发布在 Hugging Face 上,专为智能体评测底料而构建。 |
| **下游评测** | 由 [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 消费——一个"执行并观察"型 SQL 智能体,按执行准确率和 `decoy_touch_rate`(躲开陷阱的程度)打分。 |
| **完整性** | gold 答案在四个数据库版本间保持执行等价(R0==R1、R1==R2);每个陷阱都严格*增量*,真实的行、列、表从不改动。 |
| **状态** | 数据集已完成并发布;数据集验证运行已出(Claude Opus 4.8,test 划分);下游智能体规模化运行正在 governed-bi 中进行。 |

## 下游评测:[governed-bi](https://github.com/Minhao-Zhang/governed-bi)

本仓库*构建数据集*;它想要考验的那个智能体在一个独立仓库里,
[**governed-bi**](https://github.com/Minhao-Zhang/governed-bi)。governed-bi 运行一个真正的
*执行并观察*型 SQL 智能体(LangGraph + LangChain):它检视 schema、抽样行、执行查询,
并根据观察到的结果不断修正——这正是诱饵陷阱所针对的威胁模型。它直接消费本数据集:
[`eval_dataset/`](eval_dataset/) 里的 gold、陷阱清单,以及 `pg_rename_decoy` 实例。
它报告的指标包括:

- **`decoy_touch_rate`** ——智能体的 SQL 有多少时候引用了损坏的诱饵列,而不是真实的那一列。
  这正是诱饵存在的意义所要产生的"陷阱触发"信号,在关闭 schema 层护栏的情况下测量,
  因此它反映的是智能体自身的落地(grounding),而非某个过滤器。
- **执行准确率(EX)** 与 **routing recall** ——任务成功率,以及在一个汇集了 69 个 schema 的
  数据湖里,智能体是否找对了表。

这三个仓库是一个整体系统:

> **在这里构建对抗性评测数据集** → **用它评测智能体([governed-bi](https://github.com/Minhao-Zhang/governed-bi))** → **通过前端提供服务([governed-bi-ui](https://github.com/Minhao-Zhang/governed-bi-ui))**

下游的 69 数据库规模化运行正在进行中;当前的智能体结果见
[governed-bi](https://github.com/Minhao-Zhang/governed-bi)。以下内容记录的是数据集本身,
以及那次确认混淆行为符合设计的验证运行。

---

## 问题所在:基准污染

在公开的 BIRD 语料上评测时,模型可能因为在训练中见过它的 schema
标识符(`movie_release_year`、`user_subscriber`)、题目措辞或 SQL 片段而占便宜。
于是,一个亮眼的总分就把两件截然不同的事情混为一谈:**schema 推理**
和**基准记忆**。本项目专攻其中的记忆通道,同时保留一个语义等价的 SQL 任务,
再测量二者的差距。

该设计针对三个相互独立的污染面:

- **Schema 标识符。** 表名和列名被重命名为五种语言之一(英语、
  法语、德语、西班牙语、普通话拼音)。
- **Schema 探测。** *损坏诱饵陷阱*:增量添加的"邪恶双胞胎"列和克隆表,
  用看似合理的同义词名称存放真实数据的细微损坏副本,意在
  误导那些靠*执行*查询来探索 schema 的智能体。
- **题目措辞。** 对每个自然语言题目做改写,同时保持 SQL 不变。

每个污染面都是一个独立、可单独开关的维度,因此评测可以把准确率下降归因于某个
*机制*,而不是笼统地归给一个"混淆"旋钮。

## 产出成果

- **一个经过验证的多语言 Postgres Text-to-SQL 语料库。** 69 个数据库;
  **10,541 个候选题目中有 10,164 个**通过了端到端执行验证(8,134 个训练 /
  2,030 个测试,每个数据库在两者中都有出现)。见 [docs/methodology/dataset.md §7](docs/methodology/dataset-zh.md)。
- **混淆后的 gold SQL 和证据提示(evidence hints)**,已改写为使用重命名后的标识符。
- **四个 PostgreSQL 实例**,覆盖各种混淆组合:`pg_base`(原始)、
  `pg_rename`(重命名)、`pg_decoy`(陷阱)和 `pg_rename_decoy`(重命名加陷阱),
  以压缩转储(dump)的形式发布在 [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) 上。
- **损坏诱饵陷阱**:1,486 个邪恶双胞胎列,外加 162 个装有损坏数据的克隆表
  ([设计与风险登记册](docs/reference/corrupted-decoys-design-zh.md))。
- **双预言(two-oracle)完整性保证。** 混淆后的 SQL 与验证过的原始
  SQL 保持执行等价(相对 SQLite 基准真值 R0==R1,跨实例 R1==R2)。之所以成立,
  是因为每个陷阱都严格*增量*:真实的行、列和表从不改动。
- **评测框架**:一项四条件的污染增量研究和一个五臂
  消融实验(`base` / `rename` / `decoy` / `paraphrase` / `all`)。

## 评测设计

这项评测只问一个问题:**去掉可记忆的表层信息后,模型的 BIRD
准确率还能保留多少?** 它的设计目标是可信地回答这个问题,而不只是
给出一个数字:

- **配对条件。** 每个实验臂都在同一次运行中,用同一个模型跑同一套测试集;
  增量是逐题与 `base` 配对计算的。**消融**的增量用 **McNemar 检验和自助法置信区间
  (bootstrap CIs)**解读([§9.4](docs/methodology/evaluation-zh.md));污染部分的增量
  目前以点估计报告(配对置信区间待补——见 [PROGRESS.md](docs/PROGRESS-zh.md))。
- **一个经验性的零假设,而非绝对的零。** 有 14 个数据库保留了恒等(英语→英语)重命名,
  因此按构造,它们的重命名增量必然 ≈0,充当**噪声下限对照**;
  重命名效果*按语言分别*报告,而不是汇总成一个被对照组稀释的数字
  ([limitations §1](docs/reference/limitations-zh.md))。
- **严格*和*宽松两种评分。** EX 同时在 BIRD 风格的类型宽松比较器*和*一个严格比较器
  (不做跨类型折叠、区分大小写)下报告。宽松性在增量中会相互抵消,
  任何关于绝对准确率的说法都引用严格那一列([limitations §2](docs/reference/limitations-zh.md))。
- **按机制消融。** `rename−base` 探测标识符记忆;`decoy−base` 探测
  对 schema 探测陷阱的鲁棒性;`paraphrase−base` 探测题目形式记忆;`all−base`
  衡量综合效果。设计见:[evaluation.md §9](docs/methodology/evaluation-zh.md)。

### 数据集验证——混淆真的改变了模型行为吗?

在把数据集交给智能体之前,先用一个前沿模型对 2,030 个测试题做**一次性(one-shot)**运行,
以确认混淆确实可测量地改变了行为,并且每个维度的表现都符合设计。这是对数据集的一次验证检查,
而非最终结论——最终结论是 [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 里的智能体评测。

运行:**Claude Opus 4.8,一次性,test 划分。** **EX** 是执行准确率(答对题目的百分比);
**差值(Δ)是两个 EX 之差**——例如 51.6% → 46.9% 是下降 4.8%。下表为宽松 EX;
完整表格(严格 EX、按语言拆分、bootstrap 置信区间)见 [evaluation.md §8](docs/methodology/evaluation-zh.md)(污染)与 [§9.4](docs/methodology/evaluation-zh.md)(消融)。

**污染——重命名 schema 标识符的代价是多少?**(四种条件)

| Schema | 无提示 | 有提示 |
| --- | --- | --- |
| 原始(base) | 51.6% | 58.8% |
| 重命名 | 46.9% | 57.0% |
| **Δ(重命名代价)** | **4.8%** | 1.8% |

**消融——每个混淆机制单独看**(无提示,相对 EX 为 51.1% 的 `base` 臂)

| 臂 | EX | 相对 base 的 Δ |
| --- | --- | --- |
| base | 51.1% | — |
| rename | 47.0% | −4.1%(p<0.001) |
| decoy | 48.9% | −2.2%(p=0.001) |
| paraphrase | 54.6% | **+3.5%**(p<0.001) |
| all | 45.3% | −5.8%(p<0.001) |

- **重命名**去掉了一小块但真实的标识符记忆优势(无提示 4.8%),消融也复现了这一点(−4.1%)。它在英文对照(恒等重命名)上接近零,在拼音上最大(无提示 +10.5%),即效应随着离英文越远而增大。需要注意,这条按语言的梯度,对一个以英文为中心的模型而言,部分与任务本身的原始难度相混淆;要把二者分开,需要一个英语→英语的同义词对照([limitations §1](docs/reference/limitations-zh.md))。
- **诱饵陷阱**在这次一次性设置下只花掉 2.2%——但这一臂只把诱饵当作 DDL 里多出来的列*名*来看;它们真正被设计出来要触发的那种交互式"咬钩",是在下游的 [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 里测量的(`decoy_touch_rate`)。
- **改写为正(+3.5%)**——这是「问题措辞记忆」假设的一个诚实负面结果:保持 SQL 的改写理顺了含糊措辞,而非暴露被记住的措辞。
- **全部叠加**下降最大(−5.8%),拼音最低。

覆盖 10,164 个题目的流水线完整性(R0==R1、R1==R2)成立。本次运行的逐条(问题、gold SQL、生成 SQL、正确性)记录见 [`exports/`](exports/)。

## 项目状态

**数据集已完成并发布;数据集验证运行(Claude Opus 4.8,test 划分)已打分并报告在此,下游智能体评测已在 [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 中构建。剩余的测量工作是 train 划分与更多模型覆盖。**

| 组件 | 状态 |
| --- | --- |
| 核心流水线(步骤 0-7):切分 → 重命名映射 → 加载 → 转译 → 重命名 → 验证 | ✅ 已完成并验证 |
| 扩展混淆(诱饵陷阱、改写) | ✅ 已构建并应用 |
| 四个 PostgreSQL 实例 + 受 git 跟踪的评测产物 | ✅ 已发布(HF 和 [`eval_dataset/`](eval_dataset/)) |
| 污染增量评测框架 | ✅ 已实现;✅ 首批结果(Claude Opus 4.8,test 划分) |
| 五臂消融框架 | ✅ 已实现;✅ 首批结果(同一次运行) |
| 触发这些陷阱的交互式"执行并观察"智能体 | ✅ 已在 [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 中构建(下游仓库) |

完整的历史、决策和后续计划:[PROGRESS.md](docs/PROGRESS-zh.md)。

### 范围边界

- 本仓库**准备并验证**数据集;下游的*智能体*评测(执行并观察、schema 路由)在
  [governed-bi](https://github.com/Minhao-Zhang/governed-bi) 里。在这里的验证运行中,
  正确的数据库在所有条件下都是预先提供的。
- 它**不修改真实数据**。干净实例保持原样,诱饵实例只是*添加*损坏的列和表,
  因此 R1==R2 成立。
- 它**并不**声称移除了所有污染路径(被记住的字面量或高层 SQL 模板依然存在);
  它针对的是标识符、schema 探测和题目措辞这几个面。

## 本项目展示了什么

如果你把它当作一份工程样例来审阅,其中可迁移的部分包括:

- **面向智能体评测的数据集构建。** 一个从"它将要考验的那个智能体"倒推设计出来的基准:
  损坏的诱饵之所以存在,是为了在 [governed-bi](https://github.com/Minhao-Zhang/governed-bi)
  中产生一个可测量的 `decoy_touch_rate`,而不是装点门面。
- **污染条件下的评测设计。** 受控条件、经验性零假设、逐机制
  消融,以及配对显著性检验,而不是原始的排行榜数字。
- **对抗性数据设计。** 专门针对"执行并观察"型智能体构建的诱饵陷阱,
  同时可证明地保留了基准真值任务([设计文档](docs/reference/corrupted-decoys-design-zh.md))。
- **正确的数据基础设施。** 从 SQLite 到 PostgreSQL 的迁移,带执行等价
  保证,以及一套记录在案的[流水线不变量](docs/reference/pipeline-invariants-zh.md)
  (pgloader 的 DDL bug、一处 AST 变异导致的死循环、无界结果集、连接延迟陷阱)。
- **诚实的范围界定。** 一份独立的[局限性文档](docs/reference/limitations-zh.md),
  在发布任何有效性结论之前就已写好。

## 工作原理

一条 10 步的流水线把原始的 BIRD SQLite 转化为四个经过验证的 PostgreSQL 实例。
每一步都读取上一步的输出;操作细节和不变量记录在 [AGENTS.md](AGENTS.md) 中。

### 流水线步骤

| # | 步骤 | 输出 |
| --- | --- | --- |
| 1 | 切分(每库 80/20,带种子) | `artifacts/{train,test}.jsonl` |
| 2 | 为每个数据库分配一种 schema 语言 | `artifacts/db_language_map.json` |
| 3 | 生成重命名映射(LLM 翻译) | `artifacts/schema_rename_map.json` |
| 4 | 通过 pgloader 加载 `pg_base` | `pg_base` (5432) |
| 5 | 把 gold SQL 转译为 Postgres 并验证 R0==R1 | `workdir/*_transpiled.jsonl` |
| 6 | 克隆 `pg_base` 卷,就地重命名标识符 | `pg_rename` (5433) |
| 7 | 重命名 SQL 并验证 R1==R2 → **交付物** | `artifacts/{train,test}_final.jsonl` |
| 8-9 | 结构性诱饵(已被取代)+ 题目改写 | `artifacts/question_paraphrases.jsonl` |
| 10 | 注入损坏诱饵陷阱 | `pg_decoy` (5434), `pg_rename_decoy` (5435) |

在仓库根目录下,先执行 `docker compose up -d`,再用 `uv run python pipeline/<script>.py` 运行。
两个评测入口 `pipeline/eval_contamination.py` 和 `pipeline/eval_ablation.py` 位于编号步骤下游,
默认走离线准备 → 纯 API 生成 → DB 打分;只有需要旧的同机路径时才加 `--local`。

### 仓库结构

| 路径 | 内容 |
| --- | --- |
| [`pipeline/`](pipeline/) | 编号流水线(`00`-`10`)、评测框架(`eval_contamination.py`、`eval_ablation.py`、`probe_schema_recall.py`),以及共享辅助模块(`_db.py`、`_traps.py`、`_corruption.py`,……) |
| [`eval_dataset/`](eval_dataset/) | 受 git 跟踪的交付物:经验证的 gold 题目/SQL 对、重命名映射、陷阱清单、改写 |
| [`exports/`](exports/) | 每次运行的(问题、gold SQL、生成 SQL、正确性)表,以压缩包形式发布 |
| [`artifacts/`](artifacts/) | 流水线的工作输出(受 git 跟踪的子集:重命名映射、保留的数据库、陷阱计划/清单) |
| [`docs/methodology/`](docs/methodology/) | 每个设计决策背后的原因(数据集、混淆、评测) |
| [`docs/reference/`](docs/reference/) | 操作细节:流水线不变量、诱饵陷阱设计、局限性、数据集用法 |
| [`data/`](data/README-zh.md) | 原始 BIRD 源数据(不受跟踪;下载说明见 `data/README.md`) |

## 获取数据集

交付物存放在两个地方:

- **数据库。** Hugging Face 上的四个 PostgreSQL 转储(base / rename / decoy / rename+decoy):
  [minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)(太大,放不进 git)。
- **Gold SQL、重命名映射和陷阱清单。** 受 git 跟踪,位于 [`eval_dataset/`](eval_dataset/)。

```bash
# 1. get the database dumps (~12 GB, four PostgreSQL instances)
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps

# 2. bring up the empty instances and restore each dump into its match
docker compose --profile decoy up -d
docker compose cp   bird_obf_dumps/pg_base.dump pg_base:/tmp/pg_base.dump
docker compose exec pg_base pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
#   ...repeat for pg_rename / pg_decoy / pg_rename_decoy (two at a time on a laptop; see OOM note)

# 3. 准备某一臂的公开 API 请求包与私有打分清单
uv run python pipeline/eval_ablation.py --arms base --prepare-only
```

完整的下载、恢复和本地评测说明:[docs/reference/using-the-dataset.md](docs/reference/using-the-dataset-zh.md)。
评测脚本会读取 `artifacts/`,并在缺失时回退到 `eval_dataset/`,因此全新克隆无需
重新生成即可运行;Postgres 的 DSN 可通过环境变量(`PG_*_DSN`)配置,指向远程 Postgres / RDS。

## 文档

| 文档 | 涵盖内容 |
| --- | --- |
| [docs/methodology/dataset.md](docs/methodology/dataset-zh.md) | schema 湖的构建、纳入标准、训练/测试切分 |
| [docs/methodology/obfuscation.md](docs/methodology/obfuscation-zh.md) | 混淆设计、决策、物理实现;诱饵陷阱 + 改写维度(§7-§11) |
| [docs/methodology/evaluation.md](docs/methodology/evaluation-zh.md) | 完整性检查、污染增量、消融(§9) |
| [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design-zh.md) | 诱饵陷阱设计、风险登记册、竣工参数 |
| [docs/reference/limitations.md](docs/reference/limitations-zh.md) | 已知局限性和范围注意事项;引用任何数字前请先阅读 |
| [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset-zh.md) | 下载、恢复并运行评测 |
| [docs/reference/pipeline-invariants.md](docs/reference/pipeline-invariants-zh.md) | 编辑流水线时需要保持的规则,附带理由 |
| [docs/eda-report.md](docs/eda-report-zh.md) | 对 BIRD 语料的探索性分析 |
| [AGENTS.md](AGENTS.md) | 如何运行和扩展流水线(操作层面) |
| [PROGRESS.md](docs/PROGRESS-zh.md) | 历史、状态快照和后续计划 |

## 语料事实

- **合并语料**:80 个 SQLite 数据库,10,962 个题目(BIRD 训练集 + 开发集合并)。
- **排除之后**:69 个数据库,10,541 个题目(排除了 11 个题目数 < 60 的数据库)。
- **切分**:在每个数据库内部做随机 80/20 留出,带种子;不做难度分层
  (BIRD 训练集题目不带难度标签)。

`data/` 目录存放原始 BIRD 数据集(不纳入版本控制)。下载说明见
[data/README.md](data/README-zh.md)。

## Python

始终使用 `uv`:

```bash
uv run python pipeline/<script>.py
uv pip install <package>
```

依赖声明在 [`pyproject.toml`](pyproject.toml) 中(并在 [`requirements.txt`](requirements.txt)
里提供了一份钉死版本的 pip 备用清单);`.venv` 目录由 `uv` 管理——不要手动激活它,
也不要直接使用裸的 `python`/`pip`。

## 许可证

本作品采用
[知识共享署名-相同方式共享 4.0 国际许可协议(CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/)进行许可。

你可以出于任何目的自由地共享和改编本材料,前提是给出适当的署名,
并在相同的许可下发布你的贡献。

本项目是 [BIRD 基准](https://bird-bench.github.io/)的衍生作品;使用本数据集时,
请将 BIRD 标注为上游来源。
