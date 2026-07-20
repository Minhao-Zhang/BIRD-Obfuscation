[English](evaluation.md) · **中文**

# 方法论:混淆评估

## 1. 目标

混淆评估要回答一个问题:**对 schema 标识符做混淆,能否削弱前沿语言模型从记忆下来的 BIRD 标识符中可能获得的优势?**

本项目为一种基于智能体(agentic)的 Text-to-SQL 场景准备数据:在该场景中,智能体只依据已知的真实 SQL 以及配套的列名和数据类型(dtypes)来构建语义记忆层。标识符层面的记忆是一种重要的污染威胁:某个记住了 BIRD 列名(`movie_release_year`、`user_subscriber`)的前沿语言模型,可能会利用这份记忆,而不是把答案落到重命名后的 schema 上。本评估探究 **rename** 维度(schema 重命名)能否削弱这一信号,但并不声称能消除所有污染路径,比如模型记住的问题措辞、字面量取值或 SQL 模板。

---

## 2. 范围边界

本项目是一个**数据准备项目**。它不评估下游智能体系统能否在 schema lake 中导航;那留给下游的评估框架(harness)。这里的评估只回答:

1. **流水线完整性**:每一对混淆后的(问题,SQL)在内部是否自洽?
2. **混淆有效性**:当列名被重命名后,前沿语言模型是否会损失一部分准确率优势?

在所有条件下,正确的数据库都会预先提供给模型;路由(routing)不在评估之列。下游智能体如何构建和使用记忆同样超出本仓库的范围。带记忆的 B 条件(with-memory)设计被舍弃了,因为记忆格式属于下游的架构决策,在没有一个确定的格式可对齐之前,这里放任何占位设计都为时过早。

---

## 3. 流水线完整性检查

完整性检查分两个阶段运行。PostgreSQL 实例的构建方式参见 [obfuscation.md §5](obfuscation-zh.md)。

### 阶段 1:转译正确性(R0==R1)

1. 对原始 SQLite 数据库执行原始的 gold SQL → 结果集 R0(ground truth)
2. 对 `pg_base` 执行转译后的 PostgreSQL SQL → 结果集 R1
3. 断言 R0 == R1(归一化后的多重集相等);若不相等,则排入智能体修复队列(第 2 轮),并在合并前重新校验。参见 [../reference/step5-transpilation.md](../reference/step5-transpilation-zh.md)。

### 阶段 2:重命名正确性(R1==R2)

1. 对 `pg_base` 执行已校验的转译后 SQL → R1
2. 对 `pg_rename`(重命名后的 DDL,行数据不变)执行混淆后的 SQL → R2
3. 断言 R1 == R2

R1==R2 检查的是:重命名映射(rename map)是否被一致地应用到了可执行的 SQL 上——执行所需的表引用或列引用没有被遗漏。这项检查以机械的方式覆盖了所有保留下来的问题,无需任何模型或人工标注者。但它本身并不能证明自然语言的证据提示(evidence hints)在语义上完美无缺;提示改写是一项独立的文本变换。

**为什么这足以说明 SQL 可解。** gold SQL 就是 SQL 任务可解的见证(witness)。如果 R1==R2,那么对于混淆后的 schema,必定存在一条有效的 SQL 查询,其返回结果与已校验的原始 PostgreSQL 查询相同。难度是否得到保留,以及下游智能体的行为,仍是留给下游评估的经验性问题。

---

## 4. 混淆有效性检查

### 4.1 评估条件

每个前沿语言模型都在跨两个维度的四种条件下接受评估:

- **Schema**:原始 BIRD 标识符 vs. 混淆(重命名)后的标识符
- **Hints**:包含或不包含证据提示

| 条件 | Schema | Hints |
| --------- | ------ | ----- |
| base_hint | 原始 | 是 |
| base_nohint | 原始 | 否 |
| rename_hint | 混淆 | 是 |
| rename_nohint | 混淆 | 否 |

在所有条件下,模型收到的是:问题文本 + 精简后的 DDL(仅含列名和数据类型,不含 PRIMARY KEY、FOREIGN KEY 或 CHECK 约束,也不含列描述)+ 正确的 DB 标签。

**确切的 prompt。** 每个条件和消融臂都用同一个两段式 prompt(`pipeline/_eval_helpers.py`);只有 schema、问题文本和可选的提示行会变。系统指令(原文):

```text
You are a PostgreSQL expert. You will be given a database schema and a question
about the data. Write a single PostgreSQL SQL query that answers the question.
Quote all identifiers with double quotes. Output ONLY the SQL query, no
explanation, no markdown code fences.
```

用户消息:

```text
Database: <db_id>

Schema:
<精简后的 DDL>

Question: <question>
Hint: <evidence>          # 仅在带提示的条件下出现
```

**精简后的 DDL** 只有表名、列名和 PostgreSQL 数据类型——不含主键/外键、不含 CHECK 约束、不含描述——在运行时从目标实例的 `information_schema` 实时读取,每张表一段:

```sql
CREATE TABLE "<db_id>"."<table>" (
    "<column>" <dtype>,
    ...
)
```

各条件/臂之间会变的部分(其余完全一致):

| 元素 | base(_hint) | rename(_hint) | decoy | paraphrase | all |
| --- | --- | --- | --- | --- | --- |
| Schema / DDL 来源 | `pg_base`(原始名) | `pg_rename`(重命名) | `pg_decoy`(真实名 + 诱饵名) | `pg_base` | `pg_rename_decoy` |
| 问题 | 原始 | 原始 | 原始 | 改写 | 改写 |
| `Hint:` 行 | 仅带提示条件 | 仅带提示条件 | — | — | — |

本次运行是**一次性(one-shot)**的(每个问题 × 条件调用一次,出错不重试、无反馈回路)。`prompt_version`(`contamination-v1` / `ablation-v1`)会写入每一行结果,因此任何 prompt 改动都会使续跑失效,而不会悄悄把不同的运行混在一起。一个具体示例(base_nohint,schema 缩略为一张表):

```text
Database: address

Schema:
CREATE TABLE "address"."zip_data" (
    "zip_code" bigint,
    "male_population" bigint,
    ...
)

Question: How many males are there in New Haven County's residential areas?
```

**为什么要设两种提示条件?** 78% 的 BIRD 提示遵循 `"X refers to column_name"` 的模式,是一座从自然语言到列名的显式桥梁。在带提示的混淆条件(rename_hint)下,提示里含有重命名后的列名(例如 `"released in 1945 refers to annee_sortie = 1945"`),即便做了混淆,也会对模型形成部分引导。无提示条件(rename_nohint)最接近下游的测试时场景(测试时智能体只收到问题),也最能清楚地反映标识符混淆的效果。保留提示条件是为了与 BIRD 排行榜可比,后者会按每次提交追踪提示的使用情况。

### 4.2 主要信号:污染差值(contamination delta)

| 指标 | 定义 |
| --- | --- |
| **Delta(无提示)** | EX(base_nohint) - EX(rename_nohint);主要信号 |
| **Delta(有提示)** | EX(base_hint) - EX(rename_hint);可与 BIRD 对比 |

delta 为正,就是一个证据,表明前沿语言模型在原始 schema 上的准确率有一部分可能依赖于原始 BIRD 标识符,而 **rename** 维度削弱了这一优势。Delta(无提示)之所以是主要信号,是因为它不会因提示文本中出现重命名后的列名而被冲淡。报告 Delta(有提示)是为了与标准的 BIRD 排行榜提交可比。

### 4.3 Schema 记忆探测(schema recall probe)

该探测在同一份问题文本上运行两次(两种条件下的问题完全相同,从不改动):

- **原始探测(Original probe)**:只用问题给模型提示,不提供任何 schema 上下文。要求它补全 SQL。测量它以多大的比例产出语法有效、且包含正确的原始 BIRD 表名/列名(例如 `movie_release_year`)的 SQL。比例高,就是可能存在标识符记忆的证据。
- **混淆探测(Obfuscated probe)**:只用问题外加混淆后的 DB 标签给模型提示,但仍不提供 schema DDL。要求它使用混淆后的 schema 标识符来补全 SQL。如果重命名后的标识符未曾出现在预训练数据中,那么正确的补全(在没看到 schema DDL 的情况下产出 `annee_sortie` 或 `guojia`)理应罕见得多。

两个探测比例之间的差值,是对标识符记忆的直接探测,与执行准确率无关。

---

## 5. 为什么舍弃了人工标注者条件

早先的设计里包含一位人工标注者,负责检查混淆后的问题是否保持了难度。它因两个原因被舍弃:

1. **语言障碍。** **rename** 维度把约 80% 的数据库分配到了非英语的 schema 语言(法语、德语、西班牙语、普通话拼音)。没有哪一位标注者能评估横跨全部五种语言的混淆 SQL。若把审查限制在受英语控制的那 20%,就会让最强的混淆条件(拼音)得不到验证。

2. **SQL 可解性问题已由机械方式回答。** R1==R2(§3)检查的是:对每个保留下来的样例,都存在一条有效的混淆 SQL 查询。难度是否保留,属于下游关注的事。

---

## 6. 为什么舍弃了未污染模型条件

早先的设计里包含一个跨模型差值:用一个训练数据截止时间早于 BIRD 的模型作为未污染基线。它被舍弃了,因为无法确认出任何一个可靠地未受污染的模型:任何具备强 Text-to-SQL 能力的模型,无论其声称的训练截止时间为何,都可能接触过 BIRD 数据。污染状态无法从外部核实。

Schema 记忆探测(§4.3)提供了一个更直接的信号:它测量标识符记忆,而无需一个未污染的对照。

---

## 7. 指标汇总

| 指标 | 测量对象 |
| --- | --- |
| 流水线完整性(R0==R1、R1==R2) | 10,164 个已校验问题(共 10,541 个候选) |
| EX(base_hint):原始、有提示 | 测试集(2,030 个问题) |
| EX(base_nohint):原始、无提示 | 测试集 |
| EX(rename_hint):混淆、有提示 | 测试集 |
| EX(rename_nohint):混淆、无提示 | 测试集 |
| 污染差值(base_nohint - rename_nohint) | 派生;主要信号 |
| 污染差值(base_hint - rename_hint) | 派生;可与 BIRD 对比 |
| Schema 记忆率(§4.3 探测) | 测试集 |

所有有效性指标都按以下维度拆分:

- 混淆语言(英语对照 / 法语 / 德语 / 西班牙语 / 拼音)
- 碰撞状态(表名在 ≥2 个数据库间共享 vs. 唯一)

按语言的拆分用于估计拼音是否比法语或西班牙语产生更大的污染差值,这能为"实践中多大的混淆强度可能有用"提供参考。难度维度的拆分被略去:BIRD 的 train 划分没有难度标签,而 dev 的难度标签由人工按不同的标度赋值,把两者合并拆分会造成误导。

---

## 8. 结果

### 运行:**claude opus 4.8 high**

| 字段 | 取值 |
| --- | --- |
| 模型 | `Claude-Opus-4.8` |
| 推理强度 | `high`(已请求;但未生效——见下方说明) |
| Prompt 版本 | `contamination-v1` |
| 数据划分 | test(2,030 个问题 × 4 种条件 = 8,120 条生成) |
| 记录时间 | 2026-07-10(UTC) |
| Git commit | `6b5d9a1` |
| Bundle 哈希 | `requests_sha256 7d38d28c…` |

一次性生成(出错不重试、无反馈回路),对冻结的 PostgreSQL 快照打分一次。8,120 条生成全部完成打分,无一跳过。确切的 prompt 见 §4.1。

> **关于推理强度的说明。** 本次运行以 `--effort high` 发起,`high` 也被写进了每条结果的元数据;但离线生成客户端只会为 OpenAI 的推理型模型 ID(`gpt-5*` / `o*`)转发推理强度参数,对 `Claude-Opus-4.8` 并不发送。因此这次运行实际处于端点默认设置(未请求扩展推理),这与约 2.4 秒的中位延迟(§8.4)以及未记录到任何推理 token 相吻合。复现命令里保留 `--effort high` 是因为它能复现这次完全相同的运行;真正启用推理强度将是另一次单独、明确标注的运行。

> **单位。** EX 是答对题目的百分比(0.5163 = 51.63%)。差值(Δ)是两个 EX 之差,按占测试集的百分比来写:+0.0478 = 4.8%(每 100 题多答对 4.8 题),而不是 0.048% 的相对变化。§9.4 沿用同一约定。

#### 8.1 各条件的执行准确率

| 条件 | 宽松 EX | 严格 EX |
| --- | --- | --- |
| base_hint | 0.5882 (1194/2030) | 0.5655 (1148/2030) |
| base_nohint | 0.5163 (1048/2030) | 0.4956 (1006/2030) |
| rename_hint | 0.5704 (1158/2030) | 0.5488 (1114/2030) |
| rename_nohint | 0.4685 (951/2030) | 0.4507 (915/2030) |

宽松是 BIRD 风格的类型折叠相等;严格禁止跨类型匹配。任何关于绝对 EX 的结论请引用严格那一列(参见 [../reference/limitations.md §2](../reference/limitations-zh.md))。

#### 8.2 污染差值

| 差值 | 宽松 | 严格 |
| --- | --- | --- |
| **无提示**(base_nohint − rename_nohint),主信号 | +0.0478 | +0.0449 |
| 有提示(base_hint − rename_hint),可与 BIRD 对齐 | +0.0177 | +0.0167 |

重命名 schema 标识符让 Opus 4.8 在无提示时损失约 4.8 个宽松 EX 点,有提示时约 1.8 点。两个差值都是正的,但都很小:模型在原始 schema 上的准确率确有一部分依赖记住的 BIRD 标识符,但占比不高。这与先验文献的判断一致——BIRD 在标识符轴上只有弱污染(§1)。有提示会把差值大致减半,因为混淆后的提示文本里回显了重命名后的列名,部分重新搭起了桥(§4.1)。

#### 8.3 按混淆语言拆分

汇总后的无提示差值掩盖了一个清晰的梯度。英文库使用恒等重命名映射(其 `sql_rename == sql_base`),因此它们是噪声下限对照,而非一个混淆实验臂(参见 [../reference/limitations.md §1](../reference/limitations-zh.md))。

| 语言 | 库数 | n | base_nohint(宽/严) | rename_nohint(宽/严) | Δ 无提示(宽/严) |
| --- | --- | --- | --- | --- | --- |
| english(对照) | 14 | 467 | 0.495 / 0.484 | 0.490 / 0.480 | +0.004 / +0.004 |
| french | 14 | 382 | 0.463 / 0.448 | 0.435 / 0.419 | +0.029 / +0.029 |
| spanish | 14 | 438 | 0.573 / 0.555 | 0.523 / 0.507 | +0.050 / +0.048 |
| german | 14 | 351 | 0.524 / 0.487 | 0.464 / 0.430 | +0.060 / +0.057 |
| pinyin | 13 | 392 | 0.523 / 0.497 | 0.418 / 0.403 | +0.105 / +0.094 |

英文对照落在预期的约 0 下限(+0.004),这验证了测量本身:恒等重命名不产生差值。随着重命名标识符离英文越远,差值单调增大——法语 +0.029、西班牙语 +0.050、德语 +0.060、拼音 +0.105。拼音在拼写形态上离英文最远,抹掉的标识符优势也最多(约 10 个 EX 点),约为英文下限的 25 倍。当目标是压制标识符记忆时,这支持选用更强(离英文更远)的重命名语言。

#### 8.4 运行健康度

- **打分结果:** 8,120 条记录中有 3,769 条判错——3,602 条结果不匹配,167 条生成的 SQL 执行失败;其余 4,351 条正确(宽松口径)。
- **延迟:** 均值 2.83 秒,p50 2.44 秒,p95 5.41 秒,最大 26.44 秒(n = 8,120)。
- **Token:** 输入 11,567,297;输出 1,375,866;合计 12,943,163(本次离线运行无 prompt 缓存命中)。

#### 8.5 运行设置

`pipeline/eval_contamination.py`,一次性,覆盖整个测试集(2,030 个问题 × 4 种条件),默认走离线流程:在 PostgreSQL 机器冻结 prompt 和私有 gold,在纯 API 机器跑模型生成,在原始 PostgreSQL 快照上打分。原始记录写入 `eval/contamination_results.jsonl`。每一行包含 `eval_metadata` 块,记录模型、推理强度、prompt 版本、git commit 和输入产物哈希;续跑只复用元数据匹配的行。复现上述数字:

```bash
uv run python pipeline/eval_contamination.py --summarize \
  --model "Claude-Opus-4.8" --effort high --bundle-dir eval/offline/contamination
```

指标以及按语言/碰撞的拆分定义见 §7。配对差值上的 bootstrap 置信区间与 McNemar p 值尚未计算(计划中;参见 [../PROGRESS.md](../PROGRESS-zh.md))。

---

## 9. 消融研究:扩展的混淆层

污染运行(§8)只测量了 **rename** 维度(标识符重命名)。本消融研究测量**每个混淆维度对执行准确率下降的独立贡献**,并加入 [obfuscation.md §7-§11](obfuscation-zh.md) 中指定的两个维度。评估框架(`pipeline/eval_ablation.py`)默认走与 §8 相同的离线流程。首批结果(运行 `claude opus 4.8 high`)见 §9.4。

### 9.1 实验臂(arms)

所有实验臂都是**无提示**(no-hint,信号最干净;§4.2 把 rename_nohint/base_nohint 定为主要形式,而提示会因回显重命名后的标识符而冲淡混淆)。之后可选地用有提示再跑一遍,以便与 BIRD 可比。

| Arm | Schema(实例) | Gold SQL 字段 | 问题 | 复用 |
| --- | --- | --- | --- | --- |
| **base** | `pg_base` (5432) | `sql_base` | 原始 | = 已有的 base_nohint |
| **rename** | `pg_rename` (5433) | `sql_rename` | 原始 | = 已有的 rename_nohint |
| **decoy** | `pg_decoy` (5434) | `sql_base`(SELECT\* 展开) | 原始 | decoy 实例 |
| **paraphrase** | `pg_base` (5432) | `sql_base` | `question_paraphrase` | 改写后的问题 |
| **all**(rename+decoy+paraphrase) | `pg_rename_decoy` (5435) | `sql_rename`(SELECT\* 展开) | `question_paraphrase` | rename+decoy 实例 + 改写问题 |

### 9.2 每个实验臂测量什么

- **rename − base**:标识符记忆(同 base_nohint−rename_nohint)。**这一项要按语言报告,不要合并汇总:** 那 14 个英语数据库(约占 23% 的行)被赋予了恒等重命名映射,因此它们的 `sql_rename == sql_base`,从构造上就贡献了一个必定为零的 rename 差值。英语是**噪声底线对照(noise-floor control)**,单一的合并 rename 数字会被它在结构上冲淡。参见 [../reference/limitations.md §1](../reference/limitations-zh.md)。
- **decoy − base**:对**受损诱饵陷阱(corrupted decoy traps)**的鲁棒性(以附加方式加入的"邪恶双胞胎"列,外加以同义词命名的受损克隆表)。智能体是否会落到真实的列和表上,而不是抓取一个易混淆的诱饵?这测量的是增加的难度,而非记忆本身。
- **paraphrase − base**:问题形式的记忆(对 SPENCE 敏感的那个轴)。
- **all − base**:最大化混淆后数据集的综合效应。

### 9.3 如何解读数字

- **配对,同一测试集、同一模型、同一次运行。** 各差值都是逐问题地与 base 配对;使用配对检验(McNemar)以及 **bootstrap 置信区间(CIs)**。预期效应很小,因此要看置信区间和 McNemar 的 p 值,而不是看点差值;把英语对照当作经验性的零假设(它实测的噪声底线),而不是取零。
- **是不同的机制,而非单一的强度标度**(扩展文档的 §1):对 rename/decoy/paraphrase 分别解读。
- **不是完整的析因设计。** 逐个变动 + all 并**不能**分离出交互作用:`all − (rename+decoy+paraphrase individual deltas)` *并非*一个干净的交互项。要做到那一点,需要一个完整的 2³ = 8 单元格析因设计;出于成本考虑而推迟。
- **评分约定(Grading contract)。** 与 SELECT\* 展开后的 gold 做多重集相等比较,因此诱饵列绝不会渗入任何实验臂的答案,所有实验臂都在同一个定义明确的结果集上比较。汇总器报告**两列 EX**:*宽松(lenient)*(`normalise_result`,BIRD 风格,会强制类型转换,使得 `1 == "1" == True`)以及*严格(strict)*(`normalise_result_strict`:不做跨类型折叠,大小写敏感;保留数值相等性)。宽松度在各实验臂间是对称的,因此在差值里会相互抵消,但**任何关于绝对 EX 的结论都请引用严格那一列**(参见 [../reference/limitations.md §2](../reference/limitations-zh.md))。在严格的跨变体 EX 中排除 `order_sensitive_qids.json` 里的 qid(153 个顺序敏感 + 21 个执行失败):填充陷阱的那些 `UPDATE` 会重排堆(heap),因此带 `LIMIT` 且没有全序(或含浮点聚合)的 gold,在 decoy 实例上可能返回一个不同但仍然有效的行集。真实的列取值可被证明完好无损(物理行顺序则不然;参见 limitations 的 §"precision notes")。

### 9.4 结果

#### 运行:**claude opus 4.8 high**

模型与测试集与 §8 相同(`Claude-Opus-4.8`,`--effort high` 已请求但未生效——见 §8 的说明;2,030 个问题),prompt 版本 `ablation-v1`,git commit `674d6a7`,记录于 2026-07-11(UTC)。每个实验臂各自打包成独立的离线 bundle,在 API 机器一次性生成,再在此对该臂对应的 PostgreSQL 实例打分。5 × 2,030 = 10,150 条生成全部完成打分,无一跳过。在给两个基于 decoy 的实验臂打分前,先抽查了 decoy 上的 gold 与干净实例的结果(每臂 40/40 完全一致),确认加性陷阱不会掩盖正确答案。

**各实验臂的执行准确率:**

| 实验臂 | 宽松 EX | 严格 EX |
| --- | --- | --- |
| base | 0.5113 (1038/2030) | 0.4916 (998/2030) |
| rename | 0.4700 (954/2030) | 0.4527 (919/2030) |
| decoy | 0.4892 (993/2030) | 0.4690 (952/2030) |
| paraphrase | 0.5463 (1109/2030) | 0.5256 (1067/2030) |
| all | 0.4532 (920/2030) | 0.4389 (891/2030) |

**相对 base 的配对差值**(逐问题配对,n = 2,030;宽松点差值附 bootstrap 95% 置信区间与 McNemar p;不一致对 b/c = base 对→本臂错 / base 错→本臂对;严格差值供参考):

| 实验臂 | Δ 宽松 | 95% 置信区间 | McNemar p | 不一致 b/c | Δ 严格 |
| --- | --- | --- | --- | --- | --- |
| rename | −0.0414 | [−0.0557, −0.0276] | <0.001 | 154/70 | −0.0389 |
| decoy | −0.0222 | [−0.0350, −0.0094] | 0.0010 | 112/67 | −0.0227 |
| paraphrase | **+0.0350** | [+0.0182, +0.0512] | <0.001 | 116/187 | +0.0340 |
| all | −0.0581 | [−0.0768, −0.0384] | <0.001 | 264/146 | −0.0527 |

**按混淆语言的各臂 EX**(宽松;各语言 n:english 467、french 382、german 351、pinyin 392、spanish 438):

| 语言 | base | rename | decoy | paraphrase | all |
| --- | --- | --- | --- | --- | --- |
| english(对照) | 0.497 | 0.495 | 0.469 | 0.544 | 0.512 |
| french | 0.474 | 0.435 | 0.461 | 0.487 | 0.414 |
| german | 0.507 | 0.464 | 0.510 | 0.547 | 0.442 |
| pinyin | 0.520 | 0.429 | 0.477 | 0.538 | 0.378 |
| spanish | 0.555 | 0.516 | 0.530 | 0.607 | 0.502 |

**解读(每个机制分开看,依 §9.3):**

- **rename −4.1%**(McNemar p < 0.001)。与同一模型在 §8 污染评测里的无提示差值(+4.8%)一致——那个小的标识符记忆效应复现了。英文对照基本持平(0.497 → 0.495,其噪声下限),差值随离英文越远而增大,拼音最大(0.520 → 0.429)。
- **decoy −2.2%**(p = 0.001)。腐化诱饵陷阱代价约两个点:模型多数时候落在真实的列和表上,但偶尔会抓到一个易混淆的诱饵。gold 在 decoy 实例上仍能正确求解(上文已验证),所以这是增加了难度,而非任务被破坏。
- **paraphrase +3.5%**(p < 0.001)——**为正**,对本模型而言,这是「问题措辞记忆」假设的一个诚实的负面结果。用便宜模型、以 SQL 为条件生成的改写略微**帮助**而非损害,最可能是因为它把 BIRD 里含糊的措辞理顺了(116 个问题由对变错,但 187 个由错变对)。所以就当前实现而言,改写并未在此暴露被记住的问题措辞;若说有影响,原始措辞反而略难一些。注意:这也意味着改写维度在本数据上不是一个干净的混淆杠杆——它把难度往更容易的方向改动了。
- **all −5.8%**(p < 0.001),下降最大。rename 与 decoy 叠加,paraphrase 的正贡献部分抵消;净值仍明显为负,pinyin-all 整体最低(0.378)。依 §9.3,这不是一个干净的交互项。

**复现:**

```bash
uv run python pipeline/eval_ablation.py --summarize \
  --model "Claude-Opus-4.8" --effort high --bundle-dir eval/offline/ablation-base
```

(任取一个新准备的实验臂 bundle 作为元数据参照即可;`metadata_matches` 依据的是模型/强度/prompt 版本/commit/数据集哈希,而非各臂的请求哈希。)原始打分记录在 `eval/ablation_results.jsonl`。
