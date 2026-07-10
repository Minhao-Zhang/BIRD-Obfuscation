[English](obfuscation-extensions.md) · **中文**

# 方法论:扩展的混淆维度(诱饵 schema + 问题改写)

经过验证的核心流水线(步骤 0-7;参见 [obfuscation.md](obfuscation-zh.md)、[dataset.md](dataset-zh.md)、[evaluation.md](evaluation-zh.md))**只混淆 schema 标识符**(即 **rename** 维度),而问题和数据库内容保持原样。本文定义了两个**额外的、可独立开关的**混淆维度,以及分别衡量它们的消融实验。**状态:已实现并已应用。**流水线步骤 08-10 以及消融实验框架 `pipeline/eval_ablation.py` 均已存在并已运行。诱饵维度已从这里最初勾勒的空表/结构化设计**重做**为**被污染的"邪恶双胞胎"陷阱**(步骤 10);下文 §2 描述的是实际构建的设计,完整细节见 [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design-zh.md)。状态参见 [../../PROGRESS.md](../../PROGRESS-zh.md)。

## 1. 动机:为什么要扩展

两条独立的既有研究表明,标识符重命名(即 **rename** 维度)是*最弱*的污染杠杆,而且 BIRD 在该维度上本身也只受到轻微污染(本项目自己的污染测量正在用一个更强的模型重新运行,此处不作报告):

- **SPENCE**(*A Syntactic Probe for Detecting Contamination in NL2SQL Benchmarks*, arXiv 2604.17771):改写**问题**远比 schema 维度更能暴露记忆。BIRD 表现出较弱的排名敏感性(Kendall's τ ≈ −0.35,置信区间跨越零),而 Spider/SParC/CoSQL 则为(τ ≈ −0.7 至 −0.9)。敏感的维度是**问题形式**,而非标识符。
- **SQL2NL**(*Evaluating NL2SQL via SQL2NL*, arXiv 2509.04657,同一批作者):与 schema 对齐的问题改写使 Spider 上的执行准确率下降 10-20pp,这是问题维度上一个巨大而真实的效应,只是被标准基准掩盖了。

这两个新维度各自攻击一种**不同的机制**;它们不是同一件事的三种强度:

| 维度 | 攻击对象 | 机制 |
| --- | --- | --- |
| **rename**:标识符重命名(现有) | 标识符记忆调取 | 模型识别出记住的 BIRD 列名 |
| **decoy**:诱饵 schema 注入(新) | schema 链接 | 模型必须扎根于真实 schema,而不是靠模式匹配 |
| **paraphrase**:问题改写(新) | 问题形式记忆调取 | 模型无法依赖记住的 question→SQL 模板 |

**不可妥协的不变量(两个维度都适用):**每一个 `(question, gold SQL)` 对都必须保持**可解 / 执行等价**,并且用与核心流水线验证 R1==R2 相同的机械方式来验证。

---

## 2. 诱饵维度:诱饵 schema 注入

### 目标
把诱饵从惰性的 schema 链接干扰项变成**陷阱**。由于评测目标是一个**交互式的执行-观察 SQL 智能体**,智能体查询到的诱饵必须返回*貌似合理但错误*的数据。空的诱饵表和 NULL 诱饵列(即最初的设计)已被否决:`COUNT(*)=0` 或一整列全为 NULL,很容易就暴露了。因此诱饵现在保存的是**对真实数据的细微污染副本**(既是易混淆名称攻击,*又是*一个数据层面的陷阱),而只读取精简后 DDL 的模型仍然只会看到一些额外的、貌似合理的标识符。

### 添加了什么(严格增量)
只添加到**诱饵增强的克隆**(`pg_decoy`、`pg_rename_decoy`)中,**绝不**添加到干净的 `pg_base` / `pg_rename` 中。两种粒度(`pipeline/10_inject_traps.py`):
- **邪恶双胞胎列**:在真实表上新增一个列,其值是某个真实**源**列的*污染副本*,名字取一个近义词(例如真实的 `annee_sortie` → 诱饵 `date_sortie`)。真实列永远不会被修改。(`trap_manifest.json`,1,486 个。)
- **被污染的克隆表**:整张真实表克隆一份并重命名,其中一部分列被污染,其余列原样复制以保证真实感。gold 永远不会引用诱饵表,所以这些表在构造上就是 R1==R2 安全的。(`trap_table_manifest.json`,162 个。)

两者都不得与真实的表/列名冲突,也不得与 `db_id` 本身冲突(即 AGENTS.md 中关于 `superhero`/`sales_in_weather`/`university` 的 schema 限定符注意事项)。

### 污染(确定性、增量)
复制出来的值由一个哈希播种的算子污染(完整规范:[../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design-zh.md)):连接键/FK 列被**置换**(每个值仍是一个真实的键 → 引用完整性得以保持,同时仍是一个隐蔽的连接陷阱),数值列加入稀疏的 ±相对噪声,文本列做域内的类别重映射,时间列做有界的日期偏移。污染是一个纯函数,只取决于每行的键 + 一个**与变体无关**的盐值,因此 `pg_decoy` 和 `pg_rename_decoy` 会以相同方式污染相同的行,重建也可复现。一个廉价的 LLM(`gpt-5.4-mini`)为每个数据库的每个变体提供同义的表/列名。清单(§4)才是基准事实,使用时不会再推断任何内容。

### 可解性不变量与唯一的破坏途径
陷阱是**严格增量的**:真实的列和表保持逐字节一致(通过对两个诱饵实例做与顺序无关的指纹校验来验证),因此从不引用诱饵的 gold SQL 会原样执行,并返回真实列的结果 → R1==R2 成立。**唯一会造成破坏的情形,是 gold 在已添加诱饵列的真实表上用了 `SELECT *` / `t.*`**:执行时星号会展开,把诱饵一起纳入,从而扩宽结果、破坏等价性。

**测量结果(2026-07-03,`sql_base`,基于 10,164 个已验证的问题):**

| 类别 | 数量 | % |
| --- | --- | --- |
| 真实表**顶层**星号(确定会破坏) | **3** | 0.03% |
| 真实表**任意层级**星号(上界) | 5 | 0.05% |
| VALUES 物化(已排除;无真实表) | 1,169 | n/a |
| **零**星号查询的数据库 | 67 / 69 | n/a |

这 3 个顶层案例都在 `mondial_geo` 中;那 2 个子查询层级的在 `professional_basketball` 中。`COUNT(*)` 确实**没有**计入(它不是投影列表中的星号)。所以 `SELECT *` 其实只是个舍入误差。

**解决方案:`SELECT *` 展开。**在针对诱饵增强实例使用的 gold SQL 中,把 `SELECT *` / `t.*` 展开为**显式的真实列清单**(使用 sqlglot + 在添加诱饵*之前*从实例读取的 `information_schema`)。这样做:
- 在非诱饵实例上是**无害的**(星号本来就等于真实列),并且
- 在诱饵实例上是**正确的**(诱饵永远不会进入结果,等价关系是精确的)。

对所有 gold 统一应用这一处理,可以让每个消融分支的 gold 答案保持一致且可比。**退路**(如果星号展开不方便):把这 6-7 张涉及星号的表(`mondial_geo.{politics,river,mountain,geo_mountain,province,country}` + `professional_basketball.teams`)排除在列诱饵之外,改为给它们诱饵*表*。在这个数量下这几乎没有代价,只不过这些表会因此错过易混淆列的攻击。

### 验证
针对诱饵增强实例重新运行步骤 7 的 R1==R2。任何残留的星号破坏都由展开来解决。有一类残留情况是**良性且在预期之内的**:填充陷阱的 `UPDATE` 会重排堆(heap),所以带 `LIMIT` 且没有全序(或带浮点聚合)的 gold 在诱饵实例上可能返回一个*不同但有效*的行集合。这些情况在 `order_sensitive_qids.json` 中列出(153 个顺序敏感 + 21 个原本就执行失败的),并被排除在严格的跨变体 EX 之外,而不被当作污染(真实数据可被证明是完好无损的)。

---

## 3. 改写维度:问题改写

### 目标
打破对问题字符串的逐字/近逐字记忆调取(SPENCE 所敏感的维度),同时保留问题到 gold SQL 的映射。

### 生成(廉价模型)
一个廉价的 LLM 为每个问题生成**一条**改写,以 `(original question + gold SQL + obfuscated schema)` 为条件,从而锚定意图(SQL2NL 风格;SPENCE 表明该信号不依赖于生成器的选择)。约束条件:保持**自然语言**,并且**不要把 schema 标识符注入**到问题中(99.7% 的 BIRD 问题原本一个都不含,所以不要把混淆后的标识符重新引入进来)。

### 漂移与可解性
由于模型同时被给出了**问题和 gold SQL**,语义漂移预计会很小(项目决定,2026-07-03),因此**没有硬性的嵌入门控**,只有一个可选的、廉价的余弦相似度合理性检查。gold SQL 保持不变,所以改写**不会触及 R1==R2**。"是否可答"由消融评测本身来衡量(一个有能力的模型仍能解出改写后的问题),也就是说,它是一个**实验测量结果,而非事先验证过的保证**。如果哪天确实需要一个硬性的可解性保证,可以加入一个求解器往返门控:在 `(paraphrase + obfuscated schema, no gold)` 上运行一个求解器,并要求其结果与 gold R2 匹配,同时与原始问题配对,以免难题被不公平地扣分。

原始的 `question` 会被保留以便追溯。

---

## 4. 数据与存储方面的新增内容

现有的字段/产物名称**保持稳定**;下游消费者和 `eval_contamination.py` 依赖于它们。

### 新增的每问题字段
- `question_paraphrase`:**paraphrase** 维度的输出(与 `evidence_rename` 相对应;原始的 `question` 予以保留)。

### 新增的产物
规范副本由 git 跟踪在 [`eval_dataset/`](../../eval_dataset/) 中(工作副本在 `artifacts/` 中):
- `trap_manifest.json`:**邪恶双胞胎列**的基准事实。每个陷阱:`{db, table, source_column, source_type, operator, is_key, in_correlated_group, salt, names:{base, rename}}`。
- `trap_table_manifest.json`:**被污染的克隆表**的基准事实。每个克隆:`{db, source_table, columns:[{source_column, source_type, operator, is_key}], names:{base:{table, columns}, rename:{table, columns}}}`。
- `order_sensitive_qids.json`:被排除在严格跨变体 EX 之外的 qid(153 个顺序敏感 + 21 个执行失败)。
- `decoy_map.json`:较早的步骤 08 的*结构化*诱饵映射(`db_id → {tables, columns}`);为溯源而保留,已被上面的陷阱清单取代。
- `gold_star_expanded.jsonl`:针对那约 5 个星号查询、经过 `SELECT *` 展开的 gold。

### 新增的 PostgreSQL 实例(docker-compose)
两个干净的基线保持不动;新增两个诱饵增强实例,每个都通过**克隆对应的干净卷、然后注入诱饵**来构建(与步骤 6 相同的只读克隆模式):

| 实例 | 端口 | 标识符 | 诱饵 | 使用它的分支 |
| --- | --- | --- | --- | --- |
| `pg_base` | 5432 | 原始 | 无 | base、paraphrase |
| `pg_rename` | 5433 | 重命名 | 无 | rename |
| `pg_decoy` | 5434 | 原始 | 有(英文) | decoy |
| `pg_rename_decoy` | 5435 | 重命名 | 有(翻译后) | combined |

### 评测结果
- `eval/ablation_results.jsonl`:每个 `(question_id, arm)` 一条记录,与现有的 `eval/contamination_results.jsonl`(污染实验运行)相互独立。

### 字段命名(2026-07-03 已定)
Gold-SQL 字段采用一致的命名方案:`sql_sqlite`(原始 SQLite)、`sql_base`(PostgreSQL,原始标识符)、`sql_rename`(PostgreSQL,重命名后的标识符)。它们以前叫 `sql_original` / `sql_pg` / `sql_obfuscated`;其中 `sql_pg`/`sql_obfuscated` 这一对是不对称的(两者都是 PostgreSQL)。在 `base`/`rename`/`decoy`/`rename_decoy` 的整合过程中,它们在整个仓库范围内被重命名,交付用的 JSONL 也就地完成了迁移。

---

## 5. 流水线步骤(已实现)

按依赖顺序构建:先做 decoy(它是触及 R1==R2 契约的部分),然后是 paraphrase,最后是消融实验框架。

| # | 脚本 | 作用 |
| --- | --- | --- |
| 08 | `08_inject_decoys.py` | 生成 `decoy_map.json`(廉价 LLM)→ 把卷克隆为 `pg_*_decoy` → 注入*结构化*诱饵 → 在受影响的 gold 中展开 `SELECT *` → 重新运行 R1==R2。**就诱饵负载而言已被步骤 10 取代。** |
| 09 | `09_paraphrase_questions.py` | 生成 `question_paraphrase`(廉价 LLM),每个测试问题一条 |
| 10 | `10_inject_traps.py` | **被污染的诱饵陷阱**:邪恶双胞胎列 + 被污染的克隆表(增量),注入到两个 `*_decoy` 实例中;产出 `trap_manifest.json` + `trap_table_manifest.json`。参见 [../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design-zh.md)。 |
| n/a | `pipeline/eval_ablation.py` | 独立的 5 臂消融框架(base/rename/decoy/paraphrase/all);默认离线准备/生成/打分;写入 `eval/ablation_results.jsonl` |

消费这些输出的消融实验设计参见 [evaluation.md §9](evaluation-zh.md),最初的逐步构建规范参见 [../reference/extension-implementation-plan.md](../reference/extension-implementation-plan-zh.md)(注意:其中的诱饵章节早于步骤 10 的被污染陷阱重做;参见那里的横幅提示)。
