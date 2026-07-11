[English](obfuscation.md) · **中文**

# 方法论:数据集混淆

## 1. 动机

前沿语言模型的训练数据可能已经包含了 BIRD 基准:它的问题、gold SQL 和 schema 名称都是公开的。在原始语料上评测模型时,模型可能会借助记住的问题模式、SQL 片段、表名或列名,而不是只依赖评测时提供的 schema。

本项目为一种 **agentic Text-to-SQL 场景** 准备数据:在该场景中,agent 会从已知的真实 SQL 及其配套的 schema 元数据(仅包含列名和数据类型,没有列描述)中构建一个语义记忆层。在这种场景下,一种重要的记忆(recall)威胁发生在标识符层面:被污染的模型可能会认出某个 BIRD 列名(`movie_release_year`、`user_subscriber`),进而利用记住的 SQL 结构,而不是把答案落到所提供的 schema 上。混淆的目标,是在保持任务语义可用的前提下,尽可能地削弱这种列名识别信号。

**约束:** 列名和表名在重命名之后必须仍然具有语义含义。不透明的别名(`COL_1`、`T2`)会破坏下游 agent 所依赖的大量自然语言到 schema 的落地关联。目标是一种受控的同义词替换或语言切换,而不是彻底的匿名化。

---

## 2. 哪些会被混淆,哪些不会

### 会被混淆的

- **表名**:重命名为目标语言(见 **rename** 维度)
- **列名**:重命名为目标语言
- **Evidence 提示(hints)**:提示中出现的列名引用会依据 rename map 进行替换(机械式字符串替换,不做改写)
- **Gold SQL**:每一处 `FROM <table>`、`JOIN <table>` 和 `<table>.<column>` 引用都会依据 rename map 进行替换

> **扩展(已实现):** 另外还构建了两个可以独立开关的维度(流水线步骤 08-10 + `09`),并由 [evaluation.md §9](evaluation-zh.md) 中的消融实验进行度量:**损坏诱饵陷阱(corrupted decoy traps)**(新增的"邪恶双胞胎"列 + 损坏的克隆表,后者保存着真实数据的、被悄悄改错的副本;[../reference/corrupted-decoys-design.md](../reference/corrupted-decoys-design-zh.md))和 **问题改写(question paraphrase)**。它们存在于两个 `*_decoy` 实例上 / 存在于 paraphrase 字段中,是已发布交付物的一部分(见 [../reference/using-the-dataset.md](../reference/using-the-dataset-zh.md));下文描述的 *核心 rename 流水线* 不受它们影响。

### 不会被混淆的

- **问题(Questions)**:在核心流水线中保持不变(见 §3.1);一个可选的改写层在 [obfuscation-extensions.md](obfuscation-extensions-zh.md) 中有说明
- **数据库内容**:行和值保持不动(见 §3.2)
- **SQL 逻辑结构**:相同的 join、聚合、过滤和排序
- **难度标签**
- 由数据本身所隐含的 **表关系与基数(cardinality)**

### 有意从 schema lake 中省略的

- **外键约束**:`pg_base` 和 `pg_rename` 都不声明外键(FK)约束,尽管 BIRD 的 SQLite 源里是有的(见 §4 和 §5 步骤 1)。这是一个方法论上的选择,而不是疏忽或对某个 bug 的绕行:下游的 agentic Text-to-SQL 任务,意在评测 agent 能否从列名、值以及它在构建记忆过程中看到的问题/SQL 中推断出表关系,而不是从一份显式的 FK 目录里直接读出来——一个探索陌生 schema 的真实分析师同样不会被递上这样一份目录。`pg_rename` 现在是作为 `pg_base` 的一次精确卷克隆(volume clone)构建的(见 §5 步骤 5),因此它自动继承了这一特性,而不是靠第二个加载器独立地选择省略 FK。

---

## 3. 决策与理由

### 3.1 问题不做改写

早期设计中包含了基于 LLM 的问题改写(**paraphrase** 维度),用来打破对问题字符串的精确记忆。后来出于两个原因放弃了它:

1. **本仓库的主要记忆(recall)向量是 schema 标识符,而不是问题文本。** 在目标场景中,agent 是从 SQL 结构和列名构建记忆的,而不是从问题字符串。被污染的模型或许仍然记得问题的措辞或 SQL 模板,但 schema 重命名会直接削弱下游 agent 同样依赖的那部分标识符信号。问题改写解决的是一个更宽泛的威胁模型,却带来了更高的语义漂移风险。

2. **抽样表明问题绝大多数本来就是自然语言。** BIRD 训练集中只有 0.3% 的问题在问题文本里直接嵌入了 schema 标识符(snake_case 列名)。其余 99.7% 都是自然语言英文句子,几乎没有直接的 schema 泄露。对它们做改写只会增加成本、带来可能的含义漂移,却只能应对污染风险中很有限的一部分。

去掉问题改写省去了约 10,000 次 LLM 调用,消除了含义漂移导致 gold SQL 失效的风险,并产生了一条更简单、更可审计的流水线。

**重新评估(2026-07-03,现已实现):** 改写被作为一个 *可选* 维度重新引入,现在已经 **构建完成**(`pipeline/09_paraphrase_questions.py`)并随交付物一起发布(`eval_dataset/question_paraphrases.jsonl`,每个测试问题一条);见 [obfuscation-extensions.md §3](obfuscation-extensions-zh.md)。它保持 *与核心 rename gold 相互独立*(`question_paraphrase` 字段与保留下来的原始 `question` 字段并列存在)。动机发生了变化:SPENCE(arXiv 2604.17771)和 SQL2NL(arXiv 2509.04657)表明,相比本流水线主要针对的标识符轴,**问题轴才是更敏感的污染信号**。把改写以 gold SQL 为条件生成(SQL2NL 的做法)缓解了当初促使放弃它的那种含义漂移风险。

### 3.2 数据库内容不做修改

早期设计中包含了字面值替换,即在问题和 gold SQL 的 WHERE 子句中把命名实体的值替换掉,例如 `'France'` → `'Brazil'`。它被放弃的原因是:

为了让 R1==R2 成立(见 §4),被替换后的值必须真实存在于数据库中。要在不修改数据库行的前提下保证这一点,就得在每次替换之前先查询数据库找一个有效的替换值:这对横跨 10,541 个问题中每一个由 NER 识别出的实体来说,是一种脆弱的、依赖数据库的变换。另一种做法——修改数据库内容——则与"保留 schema 结构和数据"这一核心约束相冲突。

值层面的记忆(模型凭记忆补全 `WHERE pays = 'France'`)是一种被接受的残余风险。本项目主要针对的污染威胁是 schema 层面的记忆(把 `movie_release_year` 认出来是一个 BIRD 列),而这正是 **rename** 维度意在削弱的。值层面的记忆不在本数据准备项目的范围之内。

---

## 4. 混淆维度

### rename 维度:schema 语言分配

69 个数据库中的每一个都会被分配一种 schema 语言。该数据库内的所有表名和列名都会被翻译成所分配的语言。分配是随机的,但固定(带随机种子),因此实验可复现。

**分配是按数据库进行的,而不是按表。** 在单个 DB 内部,所有标识符使用同一种语言。这既保持了 DB 内部的一致性,又让原始的英文 BIRD 标识符名称变得不那么直接有用。

#### 语言池

| 语言 | 槽位占比 | 示例:`country` | 示例:`first_name` |
| --- | --- | --- | --- |
| 英语(不变) | ~20% | `country` | `first_name` |
| 法语 | ~20% | `pays` | `prenom` |
| 德语 | ~20% | `land` | `vorname` |
| 西班牙语 | ~20% | `pais` | `nombre` |
| 普通话拼音 | ~20% | `guojia` | `mingzi` |

有 69 个数据库和 5 种语言,每种语言大约覆盖 14 个数据库。英语被作为五个槽位之一纳入进来,好让一部分 DB 充当实验内部的对照组。被分配到英语槽位的数据库保持其原始 BIRD 标识符不变;不做任何翻译。

拼音预计是最强的混淆变体:它使用拉丁字符(对 SQL 安全,没有编码问题),但产生的标识符与记住的英文 BIRD SQL 重叠的可能性要小得多。它应该会让直接的标识符记忆变得困难得多,不过并不能排除其他形式的记忆式推理或模板复用。

#### 规则

- 语言分配映射存储为 `artifacts/db_language_map.json`(`db_id → language`)
- 翻译由一个 LLM 生成,并在 **单个 prompt 中提供完整的数据库上下文**:数据库名、所有表名,以及所有列名和类型一并给出。模型被要求产出一位母语数据库设计者会自然使用的术语,而不是逐词查字典。在给任何东西命名之前先看到完整的 schema,可以确保领域一致性(例如足球数据库里的 `detailed_date` 列会得到一个契合足球领域的翻译,而不是泛泛的翻译)。
- 当某个语言槽位里的所有数据库都翻译完成后,会运行一次 **一致性遍(consistency pass)**:第二个 LLM prompt 审查那些跨数据库通用概念(`id`、`name`、`created_at`、`status` 等)的翻译,并按每种语言把它们规整到一个规范形式。这样可以减少同一语言槽位下各数据库之间在通用概念上可以避免的差异。当某个规范形式与某个特定数据库已经选定的、契合领域的术语相冲突时,契合领域的术语优先;DB 内部一致性有更高的优先级。
- **翻译质量的建议性检查(`03b_check_translation_quality.py`,非阻塞):** BIRD 为每张表附带一个 `database_description/<table>.csv`(`original_column_name, column_name, column_description, data_format, value_description`),这是一份由人工撰写的、独立的说明,描述每个列实际的含义,与本流水线生成的任何东西都无关。步骤 3b 把每个 DB 翻译后的列名连同其 BIRD 撰写的描述一起交给一个 LLM,请它标记出那些相对描述而言语义错误的翻译,而不仅仅是风格上泛泛的翻译(例如,当描述说某列存放的是完整地址时,却把 `StreetAddress` 翻译成一个只表示"街道"的词)。标记会写入 `artifacts/translation_quality_flags.jsonl` 以供人工审查;这一步绝不会修改 `schema_rename_map.json` 本身。曾考虑把它作为 schema *迁移* 的基础(读取每一份描述,为每个 DB 手写一个迁移脚本),但为此目的被否决了:pgloader 的实际 bug(索引/PK 加引号、FK-DDL 崩溃、`CURRENT_TIMESTAMP` 加引号;见 §5 步骤 1)没有一个是因为不了解 schema 而造成的,而且 BIRD 的 `data_format` 字段比 `_pg_helpers.py` 已经在做的数据驱动型类型推断更粗糙。它特别适合专门用于翻译质量审查——在这里,BIRD 的描述是步骤 3 中的翻译 LLM 没有看到过的、真正独立的信号。
- 翻译使用 `snake_case` 以匹配 PostgreSQL 的标识符约定(例如 `date_of_birth` → `date_de_naissance`、`fecha_de_nacimiento`、`geburtsdatum`、`chushengriqi`)
- **已知风险:** PostgreSQL 会在 63 字节处静默截断标识符。较长的拼音转写在实际中不太可能触及这一点,但如果在 DDL 加载期间发生冲突,受影响的标识符会被手动解决,并更新 rename map。
- rename map 存储为 `artifacts/schema_rename_map.json`,其结构为 `db_id → {bare_name: obfuscated_name}`。键是不带 schema 限定的裸标识符(例如 `"country"`,而不是 `"world.country"`)。在每个 `db_id` 内部,表名和列名共享同一个键空间;流水线依靠 SQL AST 的节点类型以及 R1==R2 验证步骤来捕捉遗漏的或有歧义的标识符替换。
- Gold SQL 通过 **sqlglot** 的 AST 遍来重写:SQLite SQL 先被转译(transpile)向 PostgreSQL,然后在确认其 PostgreSQL 形式能等价执行之后,再单独进行验证和重命名。标识符节点(表名、列引用)会依据 rename map 被替换,而字符串字面量节点保持不动。其结果意在验证之后全程都是 PostgreSQL SQL;残余的方言差异由 R0==R1 检查处理,必要时辅以 LLM 协助的纠正。
- **标识符加引号不变量:** PostgreSQL 会把未加引号的标识符转为小写,而 BIRD 的 SQLite schema 里可能包含大写名称、空格或标点。`04_load_pg_base.py` 会显式地向 pgloader 传入 `quote identifiers`(已确认对 SQLite 源是合法语法),因此 `pg_base` 的标识符拼写应当与原始 SQLite 的拼写完全一致。转译步骤会给表引用加上 schema 限定,并对输出的标识符一致地加引号(例如 `"app_store"."AppleStore"."Price"`)。给所有标识符加引号是有意为之:它保留了大小写混合的名称,而对全小写名称也无害。步骤 4 还会运行一次基于实测的加载后检查:它针对每个 DB,把 SQLite 的 `PRAGMA table_info` 标识符与 `pg_base` 中的 `information_schema` 做 diff,一旦有任何不匹配就大声报错,而不是把这个检查推迟到很久之后、代价高得多的 R0==R1 SQL 执行失败时。

  这并不是一个小小的边缘情况:对全部 69 个保留下来的 SQLite 数据库的一次完整审计(`pipeline/00_audit_sqlite_identifiers.py`,结论见 [`docs/reference/audit-findings.md`](../reference/audit-findings-zh.md))在 69 个数据库中的 48 个里发现了 2,351 个有风险的标识符(大写、内嵌空格、标点,甚至带连字符、根本不是合法的未加引号 SQL 的表名),而发现零个列存在最初促成重写 pgloader 的那种 numeric/string 类型不匹配。经由 pgloader → sqlglot 转译 → rename-map 的标识符加引号保真度,而非类型推断,才是这一流水线阶段中风险最高的部分。

  **已解决(此前曾是一个悬而未决的问题):** pgloader 的 SQLite 加载器默认确实会把标识符转为小写,这已对照 pgloader 源码得到确认(`src/params.lisp` 里的 `*identifier-case*` 默认为 `:downcase`,统一应用于包括 SQLite 在内的每一个源加载器),但该默认值只对匹配 `^[A-Za-z_][A-Za-z0-9_$]*$` 的标识符生效;任何带空格/标点的东西无论如何都已经被强制进入了加引号/保留大小写的分支。这意味着像 `works_cycles` 那样纯 PascalCase 的表(65/65 张表受影响)即便不含有风险标点,也在被静默地转为小写:这正是旧的 `WITH create tables, create indexes, reset sequences` 子句(没有大小写指令)会踩中的失败模式。`quote identifiers` 已确认对 SQLite 源是合法的 WITH 子句语法(与 MySQL 源共用同一条规则,而不是像最初怀疑的那样为 MySQL 独有),现在已出现在步骤 4 的 WITH 子句中。

  **给标识符加引号会与 pgloader 自己自动生成的 index/PK/FK DDL 冲突(经实测验证,而不仅仅是从源码推断)。** 当 `quote identifiers` 生效时,pgloader 的 `CREATE UNIQUE INDEX`/`ALTER TABLE ... ADD PRIMARY KEY`/`ADD FOREIGN KEY` 语句会让 *这些语句内部的列名* 不加引号(只有 `CREATE TABLE` 中的表/列定义被正确加引号),于是 PostgreSQL 会把像 `Id` 这样的大小写混合列名折叠成 `id`,找不到它,导致约束/索引创建失败。这一点已针对加载进一个真实 Postgres 的 `works_cycles` 直接复现:51 个硬错误,每张表一个,每一次 index/PK 创建都失败。没有办法在保留索引创建的同时只修复其中的列加引号问题:pgloader 的语法把 PK 创建和索引创建捆绑在同一个 `create indexes`/`create no indexes` 开关下,对所生成 DDL 内部的加引号没有独立的覆盖手段。因此步骤 4 传入 `create no indexes`:`pg_base` 拥有拼写正确的表和列(已逐行与 SQLite 核对),但没有索引或 PK 约束。本流水线只会从 `pg_base` 读取(步骤 5 和 7 的 R0==R1/R1==R2 检查),所以代价是查询速度,而不是正确性。

  **外键约束根本就不创建:这是一个有意的方法论决策,而不是绕行手段。** 见 §2 中的"有意从 schema lake 中省略的"。它恰好也绕开了一个另外的、已确认的 pgloader 崩溃:SQLite 的简写形式 `FOREIGN KEY (col) REFERENCES OtherTable`(省略被引用的列,这是合法的 SQLite,意思是"OtherTable 的主键")会让 `PRAGMA foreign_key_list` 返回一个为空的 `to` 列,而这在某些 pgloader 构建中会导致 FK-DDL 生成崩溃。69 个保留数据库中的 15 个里,共有 176 个 FK 使用了这种简写,所以这并不是一个可以指望 pgloader 悄无声息地处理好的边角情况。在 WITH 子句中传入 `no foreign keys` 同时也让 `pg_base` 与 `pg_rename` 保持一致——后者从一开始就从未创建过 FK 约束(`_pg_helpers.py` 的混淆 schema 加载器只发出 `CREATE TABLE`)。

  **即便遇到硬性的、丢数据的失败,pgloader 也会返回退出码 0(这是直接确认的,不是假设的)。** 用 `--on-error-stop` 运行 pgloader v3 去应对一个 `FATAL` 级的 schema 创建错误,它仍然退出 0。另一个单独的 bug(pgloader 把 SQLite 的 `DEFAULT CURRENT_TIMESTAMP` 加引号成字面字符串 `'current_timestamp'`,而 `timestamptz` 列随后会拒绝它;影响 `works_cycles` 和 `movie_3` 里的 80 张表)通过在 WITH 子句中加入一条显式的 `CAST` 规则得到了修复,但即便 *没有* 这个修复,被中止的加载也是在零张表被创建的情况下退出 0 的。因此,在 pgloader 调用外面包一个 subprocess 的 `check=True` 是必要的,但并不充分。步骤 4 的 `verify_row_counts()`(逐表比较 SQLite 与 `pg_base` 之间的 `SELECT COUNT(*)`)才是真正能捕捉到静默的部分加载的那个检查;BIRD 自己的 `works_cycles.sqlite` 中两处真实的、早已存在的数据质量缺陷正是通过这种方式才被发现的(一处是被烤进 `CountryRegion` 数据里的字面表头行,另一处是以十六进制字符串 `TEXT` 存储的 BLOB 列,被 pgloader 的类型推断误判为 base64 而无法解码)。
- Evidence 提示中出现的列名/表名会用词边界正则(`\bcol_name\b`)对照 rename map 进行替换。提示是自然语言,不是 SQL,因此字符串字面量的歧义问题在这里不适用。
- **已知局限:** 78% 的 BIRD 提示使用结构化模式 `"X refers to column_name = value"`。较短的单词列名(例如 `critic`、`date`、`city`)也会以自然语言的形式出现在提示的散文里;抽样显示这影响约 37% 的提示。由于 `\b` 尊重词边界,像 `critic_likes` 这样的复合标识符不会被 `critic` → `critique` 的替换所破坏。散文层面的替换(例如 `"the critic made by"` → `"la critique faite par"`)可能会让某些提示读起来不那么自然,即便其中结构化的 `refers to` 部分仍然可用。这被作为一个已知的残余局限而接受。

---

## 5. 物理落地

整条流水线全程产出 PostgreSQL SQL。在 sqlglot 重写步骤之后,不存在任何 SQLite SQL。有序的步骤如下:

1. **加载 `pg_base`。** 使用 pgloader 把全部 69 个 SQLite 数据库加载进 PostgreSQL。每个 BIRD 数据库映射到一个 PostgreSQL schema(`db_id.table_name`),如 [dataset.md](dataset-zh.md) 中所述。加载完成后,如有需要可查看 `information_schema.tables` 和 `information_schema.columns`,以确认 pgloader 是如何表示原始 SQLite 标识符的大小写和标点的。

   **pgloader 以容器方式运行(`dimitri/pgloader:v3.6.7`),而不是安装在宿主机上。** 并没有打包良好的原生 Windows 构建,而且 pgloader 是一个 Common Lisp 二进制文件,在各种环境下都很难可靠地安装;既然 Docker 本就是本流水线的硬依赖(两个 PostgreSQL 实例都在 Compose 中运行),那么用同样的方式运行 pgloader 就把环境前置条件收敛成了"Docker 在运行"这一条,别无其他。`04_load_pg_base.py` 把每个 SQLite 文件以只读方式 bind-mount 进容器,并通过 stdin 传入 `.load` 命令脚本(`pgloader /dev/stdin`;pgloader 不接受用 `-` 表示 stdin,那必须是字面路径)。容器通过 `host.docker.internal` 而不是 Compose 的网络/服务名来访问 `pg_base`,因为 pgloader 的 DSN 主机名语法拒绝下划线,而两个服务名(`pg_base`、`pg_rename`)都各含一个下划线。

   **最终的 WITH 子句是 `create tables, create no indexes, quote identifiers, no foreign keys` 再加上一条 `CAST` 规则。** 每一个子句选择都是针对一个真实的 Postgres 和真正最坏情况的 DB(`works_cycles`)通过实测验证的,而不是从文档里假设出来的;关于为什么 `create no indexes` 和 `no foreign keys` 两者都是必要的(而不仅仅是风格问题)以及 CAST 规则为何存在的完整说明,见 §4 的"标识符加引号不变量"。`reset sequences` 被完全省略:pgloader v3(仍在积极维护的标签谱系;Docker Hub 上的 `:latest` 标签自 2022-08 起就已陈旧)有一个另外的、尚未修复的 bug,涉及把 `quote identifiers` 与序列重置组合使用:调用 `pg_get_serial_sequence()` 时,列名已经被 `quote_ident()` 包裹过,从而把字面的双引号字符嵌进了一个本应是纯文本的参数里(dimitri/pgloader#1651;PR #1701 中提出过一个修复,但未合并即被关闭,转而支持 v4 的 Clojure 重写 PR #1705,且从未回移植到 v3)。既然本流水线在加载之后只会从 `pg_base` 读取,序列的起始值就无关紧要;去掉这个子句是在规避这个 bug,而不是绕行它。

2. **转译 gold SQL(SQLite → PostgreSQL)。** 对每个问题,把原始的 SQLite gold SQL 送入 sqlglot(`read='sqlite', write='postgres'`)以产出转译后的原始 SQL。这会自动处理常见模式:`STRFTIME` → `DATE_PART`、`IIF` → `CASE WHEN` 等。schema 限定(`FROM t` → `FROM "db_id"."t"`)会应用到 AST 中除 CTE 别名之外的每一个表引用:一个 `WITH x AS (...)` 别名在语句后面被引用时会被解析为 `exp.Table`,而 sqlglot 的 AST 在结构上没有办法把 CTE 引用与真正的表引用区分开。硬给它加限定会产生 `"db_id"."x"`,而它并不存在,会导致执行失败。CTE 别名会先被收集起来(`{cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}`)并从限定中排除。这不是假想:保留语料中有 9 条 gold 查询使用了 `WITH`(`card_games`、`formula_1` ×6、`toxicology`),在修复之前正好踩中了这个失败。

3. **验证转译(R0==R1)。** 对原始 SQLite 数据库执行原始 gold SQL → R0(基准真值)。对 `pg_base` 执行转译后的 SQL → R1。要求归一化后的多重集(multiset)相等。第 1 遍(sqlglot)把直接匹配的写入 `workdir/*_transpiled.jsonl`;不匹配的进入 `transpilation_needs_fix.jsonl`。第 2 遍借助编码 agent(而不是流水线内的 LLM)提出修复,在合并之前再由 `05b_apply_sql_fixes.py` 验证一次。始终无法通过 R0==R1 的问题会被记录在 `transpilation_failures.jsonl` 中。一些 agent 修复使用了 **VALUES 物化(VALUES materialization)**(把 SQLite 结果行嵌入进去的 PostgreSQL SQL);它们在已加载的数据上能通过 R0==R1,但并不是可移植的方言翻译。关于产物布局、超时、重复以及注意事项,见 [../reference/step5-transpilation.md](../reference/step5-transpilation-zh.md)。

   **给 agent 的 schema 上下文必须描述 `pg_base` 实际加载后的样子,而不是 SQLite 源。** `get_pg_schema_ddl()` 在实时的 `pg_base` 连接上查询 `information_schema`,因为 pgloader 会进行自己的类型推断,标识符拼写也可能与 SQLite 的 `CREATE TABLE` 文本不同。

4. **重命名 gold SQL。** 用单趟 sqlglot AST 遍(解析 PostgreSQL、重命名标识符节点、输出 PostgreSQL)把 rename map 应用到已验证的 PostgreSQL SQL 上。这一步有意与步骤 2 分开:验证是在未修改的标识符上进行的,所以步骤 3 中的 agent 修复面对的是可辨认的原始列名,而不是重命名后的列名。输出是混淆后的 PostgreSQL SQL。

5. **通过克隆 `pg_base` 的 Docker 卷、然后原地重命名来构建 `pg_rename`**,而不是第二次从 SQLite 重新加载。`pg_base` 和 `pg_rename` 是各自独立的 Postgres 容器,各有自己的命名 Docker 卷(`pg_base_data`、`pg_rename_data`)。既然 `pg_base` 已经被验证与 SQLite 逐字节一致(步骤 1 的行数/大小写检查),并且已经拥有恰当的 Postgres 原生类型(来自 pgloader 自己的类型推断,而不是对它的第二次猜测),那么混淆实例就通过以下方式产生:

   1. 停止 `pg_base`,让它的磁盘文件处于静止状态,并通过一个用完即弃的容器把 `pg_base_data` 原样地做一次文件系统拷贝到 `pg_rename_data`,该容器把两个卷都挂载为 **源侧只读**(`docker run --rm -v pg_base_data:/from:ro -v pg_rename_data:/to alpine cp -a /from/. /to/`)。`:ro` 挂载正是让这一步安全的关键:执行拷贝的容器根本没有通往 `pg_base_data` 的写路径,因此拷贝命令中的 bug 也无法触碰到源。
   2. 拷贝完成后立即重启 `pg_base`(停机时间只有 `cp` 那么长,而不是整个重命名步骤),并首次针对现已填充好的卷启动 `pg_rename`。
   3. **只针对 `pg_rename`** 运行 `06_build_pg_rename.py`(它从不打开到 `pg_base` 的连接),按照 `artifacts/schema_rename_map.json` 发出 `ALTER TABLE ... RENAME TO ...` / `ALTER TABLE ... RENAME COLUMN ... TO ...`。在 PostgreSQL 中,重命名是一个仅涉及目录(catalog)的元数据操作(不重写表、不移动数据),所以无论行数多少都很快,而且不会引入相对 `pg_base` 的类型或数据不匹配:不存在第二趟类型推断来与第一趟发生分歧。

   这取代了一个早期设计:在那个设计里,`06_build_pg_rename.py` 会重新读取每个 SQLite 文件,并通过 `_pg_helpers.py` 那个基于数据抽样的 `infer_pg_type()`(只区分 NUMERIC/TEXT,比 `pg_base` 中已经验证过的 pgloader 自己的推断更粗糙)独立地重新推断类型。那种设计有让 `pg_base` 和 `pg_rename` 在相同逻辑数据上的列类型悄悄发生分歧的风险,这是一个真实的(而非假想的)风险,因为两条推断路径用的是不同的逻辑。卷克隆方法彻底去掉了第二趟推断:从构造上讲,`pg_rename` 的类型就是 `pg_base` 的类型。

6. **验证重命名(R1==R2)。** 对 `pg_base` 执行已验证的转译后原始 SQL → R1;对 `pg_rename` 执行混淆后的 SQL → R2;断言两者相等。这里出现不匹配意味着 rename map 有缺口(SQL 重写中遗漏了某个标识符),而不是方言问题。

让两个实例都运行在 PostgreSQL 上,SQLite 到 PostgreSQL 的方言不匹配就不再是 R1==R2 重命名完整性检查里的一个变量。R0==R1 步骤则用 SQLite 作为语义上的基准真值参照(oracle)。

---

## 6. 产物结构

### 混淆后的 schema lake

`pg_base` 和 `pg_rename` 是两个干净的基线,通过 **Docker Compose** 在本地运行;此外还有两个 **带诱饵增强的** 实例(5434 端口上的 `pg_decoy` 和 5435 端口上的 `pg_rename_decoy`)承载着损坏陷阱(见扩展文档)。`pg_base` 用于 R0==R1 转译检查以及 base 评测条件;`pg_rename` 用于 R1==R2 重命名检查以及 rename 条件。**已发布的交付物是全部四个实例**,以 PostgreSQL dump 形式发布在 [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) 上,外加受 git 跟踪的 [`eval_dataset/`](../../eval_dataset/) 中的 gold/映射/清单。两个干净实例在评测时都不会从零重建:两者都只构建一次,并作为 Docker 卷持久化。`pg_rename` 的卷是 `pg_base` 卷的文件系统克隆,并原地重命名(§5 步骤 5),而 `pg_base` 在这次克隆过程中只会被读取(以只读方式挂载),从不被修改。两个 `*_decoy` 卷同样是干净卷的克隆,并注入了陷阱(步骤 10)。

### 本仓库中的文件

所有文件都是 JSON 或 JSONL,以便机器读取。

```text
artifacts/
  retained_dbs.json             # step 1 output; db_id list, read by steps 0,2,3,4,6
  db_language_map.json          # db_id -> language
  schema_rename_map.json        # db_id -> {original_identifier: obfuscated_identifier} (git-tracked; regenerate via step 3)
  sqlite_identifier_audit.jsonl # step 0 diagnostic output
  translation_quality_flags.jsonl # step 3b advisory output
  train.jsonl                   # training questions (80% per DB, random split)
  test.jsonl                    # held-out test questions (20% per DB)

workdir/
  train_transpiled.jsonl        # step 5 output, read by step 7
  test_transpiled.jsonl
  transpilation_needs_fix.jsonl # step 5 repair queue
  transpilation_fixes.jsonl     # agent-written fixes
  transpilation_failures.jsonl  # terminal step-5 failures
  rename_failures.jsonl         # terminal step-7 failures
  fix_batches/                  # step 5c export batches for agents

eval_dataset/                   # git-tracked FINAL deliverable (snapshot of artifacts/)
  train_final.jsonl test_final.jsonl        # validated gold pairs (8,134 / 2,030)
  schema_rename_map.json db_language_map.json
  trap_manifest.json trap_table_manifest.json  # step 10 corrupted-trap ground truth
  decoy_map.json                            # step 08 structural decoys (superseded)
  question_paraphrases.jsonl                # step 09
  gold_star_expanded.jsonl order_sensitive_qids.json
```

`artifacts/` 保存的是跨流水线步骤按名称被消费的持久化产物(或作为诊断交付物由人来阅读);扩展步骤 08-10 也会往这里写(`decoy_map.json`、`question_paraphrases.jsonl`、`trap_manifest.json`、`trap_table_manifest.json`、`gold_star_expanded.jsonl`、`order_sensitive_qids.json`)。`eval_dataset/` 是受 git 跟踪的、冻结的最终交付物快照(由 `eval_dataset/build_eval_dataset.py` 构建);`workdir/` 保存的是步骤 5/7 修复队列的临时草稿文件,在那个修复循环之外没有任何消费者。

`train_final.jsonl` / `test_final.jsonl`(已验证的交付物)中的每一行都是一个带如下字段的 JSON 对象:

```json
{
  "question_id": "...",
  "db_id": "...",
  "question": "...",
  "evidence": "...",
  "evidence_rename": "...",
  "difficulty": "...",
  "sql_sqlite": "...",
  "sql_base": "...",
  "sql_rename": "..."
}
```

三个 gold-SQL 字段:`sql_sqlite`(原始 **SQLite**,原始 BIRD 标识符,为可追溯性和 R0==R1 检查而保留)、`sql_base`(**PostgreSQL**,原始标识符,由 R1==R2 检查和 base/decoy 评测臂使用),以及 `sql_rename`(**PostgreSQL**,重命名后的标识符,用于 R1==R2、rename/all 评测臂,以及针对 `pg_rename` 的下游记忆构建)。`difficulty` 在可用时携带 BIRD 的标签(仅 dev 问题有;train 问题没有)。同样地,`evidence_rename` 中出现的列名/表名已按 rename map(§4)进行了替换,是下游消费者应当展示给 agent 的版本;`evidence`(原始英文)仅为可追溯性而保留。paraphrase 维度为每个测试问题额外增加一个独立的 `question_paraphrase`(`eval_dataset/question_paraphrases.jsonl`)。
