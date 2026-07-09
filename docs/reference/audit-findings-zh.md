[English](audit-findings.md) · **中文**

# SQLite 标识符/类型审计:发现(2026-06-30)

由 `pipeline/00_audit_sqlite_identifiers.py` 生成。重新运行即可再生成
`artifacts/sqlite_identifier_audit.jsonl`(逐库的完整明细,已加入 gitignore:
文件很大,且可机械复现)。签入版本库的就是这份汇总,这样即使没人重新运行扫描,这些发现也能保留下来。若需要每一个含空格/标点/连字符的问题标识符的
完整枚举清单(而不只是计数和示例),参见
[`identifier-audit-detail.md`](identifier-audit-detail-zh.md)。

## 为什么会有这次审计

第 4 步改用 pgloader 重写(`pipeline/04_load_pg_base.py`,参见
`AGENTS.md`),最初的理由是有人声称:某些 SQLite 列虽然声明为
`INTEGER`/`REAL`,实际却存放着非数值字符串(例如
`app_store.Price` = `"$4.99"`),从而破坏了 `pipeline/_pg_helpers.py` 里那套
手写的 `NUMERIC` 映射。本次审计对保留下来的每个数据库都核实了这一说法,同时
单独检查了不属于纯小写 `[a-z0-9_]` 的标识符(表名/列名)——只要某个流水线步骤假定 PostgreSQL 会保留 SQLite 的原始拼写,这类标识符就是实实在在的风险。

## 主要结果

| 检查项 | 结果 |
| --- | --- |
| 混合类型列(声明为数值,却存放非数值字符串) | 全部 69 个数据库中共 **0** 个 |
| 高风险标识符(非小写 / 含空格 / 含标点) | 共 **2,351** 个,分布在 **69 个数据库中的 48 个** |

**这个「0 个混合类型列」的结论有一处已知缺口(于 2026-07-01 第 4 步实际生产
运行时发现):** 上文的 `is_numeric_value()` 只检查一个值能否被解析为*某种*
数字(即 `float(str(v))` 是否成功),并不检查该值的数值*形态*是否与列声明的
类型相符。一个声明为 `INTEGER` 的列,若实际存放着像 `182.88` 这样的浮点数
(`european_football_2.Player.height`,看起来是由英寸换算成的厘米),也能顺利通过这项检查,因此本次审计给出的「0」并不能证明这一失败模式不存在。它只排除了数值列中的非数值*字符串*,而没有排除整数列中的浮点数。这导致
第 4 步出现了一次真实的 COPY 失败,而本次审计事先并未发现;修复方式参见
`../methodology/obfuscation.md` §4 以及 `04_load_pg_base.py` 中的 `EXTRA_CASTS["european_football_2"]`。
若日后重新审视这个脚本,`is_numeric_value` 还应针对声明为 `INTEGER` 的列标记出
`isinstance(v, float) and not v.is_integer()` 的情况。

**最初关于类型不匹配的理由站不住脚。** `app_store` 的
`playstore.Price` 列在 SQLite 里本身就声明为 `TEXT`,而非
`INTEGER`/`REAL`。`"$4.99"` 这样的值正是 `TEXT` 列本该存放的内容。在保留下来的这批数据库里,没有一个存在声明为数值、却实际存放非数值字符串的列。至于当初在那套手写加载器下导致 6 个数据库 COPY 失败的原因
(根据现已删除的 `CHECKPOINT.md`,分别是 `ice_hockey_draft`、`mondial_geo`、
`professional_basketball`、`talkingdata`、`works_cycles`、`european_football_2`),
极有可能是**未加引号的标识符破坏了 DDL/COPY 语句**,而不是数值强制转换:
这 6 个数据库恰恰都属于下表中标识符风险最高的那批。

标识符风险真实存在,规模也不小。除了简单的大小写差异之外,还发现了以下模式:

- **内嵌空格**:`app_store."Content Rating"`、`thrombosis_prediction."Examination Date"`、`synthea."POPULATION TYPE"`、`superstore."Customer ID"`
- **标识符内部含标点/符号**:`talkingdata."F23-"` / `"F43+"`、`hockey."R/P"` / `"+/-"`、`disney."Studio Entertainment[NI 1]"`、`retail_complains."Consumer consent provided?"`
- **带连字符的表名**,根本不是合法的未加引号 SQL 标识符:`legislator`(`current-terms`、`historical-terms`、`social-media`)、`disney`(`voice-actors`)
- **整个 schema 使用 PascalCase/camelCase**:`works_cycles`(65/65 张表受影响)、`hockey`、`mondial_geo`、`formula_1`

## 在大规模重跑第 4/5 步之前建议的代码修复

1. **已完成(2026-07-01)。** ~~通过实测核实 pgloader 实际的大小写处理行为,
   不要想当然。~~ 已解决,方法是直接阅读 pgloader 的 Lisp 源码,而不是加载一个
   测试数据库:`downcase identifiers` 被确认为 SQLite 源真正的默认行为,但它
   只对匹配 `^[A-Za-z_][A-Za-z0-9_$]*$` 的标识符生效。凡是含空格/标点的标识符,
   早就被强制进入加引号/保留大小写的分支了。完整的解决过程见下文
   「本次审计未能回答的悬而未决问题」一节。
2. **已完成(2026-07-01)。** ~~如果 pgloader 会把标识符转为小写,那就以
   `information_schema` 而非 SQLite 拼写为准来构建重命名映射。~~ 已被上面的
   修复 #1 取代:现在第 4 步的 WITH 子句里显式传入了 `quote identifiers`,因此
   `pg_base` 应当会原样保留 SQLite 的原始拼写,不再需要基于小写键的重映射。若
   日后某个 pgloader 版本让这一点回退,`pipeline/04_load_pg_base.py` 中的
   `verify_casing()` 会大声报错,而不会悄无声息地生成一份不匹配的重命名映射。
3. **仍未解决:需要 Docker/pgloader 来验证。** 如果 pgloader 即便正确加引号也无法加载像 `current-terms` 这样带连字符的表名,那么该数据库的
   第 4 步加载就需要一个显式的逐库覆盖配置,或者在 SQLite 一侧对表做一次有记录
   的预重命名。目前尚未针对真实的 pgloader 运行做过测试。
4. **已完成(2026-07-01)。** ~~增加一项自动化的加载后标识符审计。~~ 现在
   `pipeline/04_load_pg_base.py` 会在每个数据库完成 pgloader 加载之后、第 5 步开始之前,立即运行 `verify_casing()`(检查表/列是否存在)和
   `verify_foreign_keys()`(检查 FK 约束;这项检查是在同一天确认了 pgloader 曾
   有过即便基础表/列 DDL 正确、也不把引号传递到 `FOREIGN KEY` 子句的历史之后
   追加的)。
5. **一旦第 4 步真正开始运行,以下仍是需要重点关注的优先清单**:
   `works_cycles`、`hockey`、`mondial_geo`、`soccer_2016`、
   `european_football_2`、`professional_basketball`、`synthea`、
   `thrombosis_prediction` 和 `legislator` 集中了大部分标识符风险,其中有几个
   也在旧加载器下失败过。此外,`legislator` 和 `disney` 是仅有的两个带连字符
   表名的数据库(`current-terms`、`historical-terms`、`social-media`、
   `voice-actors`)。上面修复 #3 所要处理的正是这种情况,而它仍然悬而未决。

## 逐数据库完整结果

按高风险标识符数量降序排列。每一行的 `mixed_type_cols` 都是 0(已从表中省略;
参见上文的主要结果)。

| db_id | tables | risky_identifiers |
| --- | --- | --- |
| works_cycles | 65 | 491 |
| hockey | 22 | 262 |
| mondial_geo | 34 | 145 |
| soccer_2016 | 21 | 106 |
| european_football_2 | 7 | 102 |
| professional_basketball | 9 | 86 |
| california_schools | 3 | 83 |
| codebase_community | 8 | 73 |
| world_development_indicators | 6 | 73 |
| synthea | 11 | 68 |
| card_games | 6 | 67 |
| thrombosis_prediction | 3 | 67 |
| beer_factory | 7 | 61 |
| superstore | 6 | 61 |
| retail_world | 8 | 50 |
| regional_sales | 6 | 47 |
| car_retails | 8 | 44 |
| formula_1 | 13 | 43 |
| software_company | 5 | 41 |
| airline | 3 | 35 |
| codebase_comments | 4 | 29 |
| world | 3 | 27 |
| authors | 5 | 26 |
| image_and_language | 6 | 26 |
| retail_complains | 6 | 26 |
| debit_card_specializing | 5 | 21 |
| sales | 4 | 20 |
| social_media | 3 | 20 |
| ice_hockey_draft | 4 | 19 |
| app_store | 2 | 15 |
| financial | 8 | 15 |
| public_review_platform | 15 | 15 |
| shakespeare | 4 | 12 |
| talkingdata | 12 | 12 |
| disney | 5 | 10 |
| address | 9 | 8 |
| chicago_crime | 7 | 8 |
| simpson_episodes | 7 | 7 |
| computer_student | 4 | 6 |
| law_episode | 6 | 6 |
| cookbook | 4 | 4 |
| menu | 4 | 4 |
| cars | 4 | 3 |
| legislator | 5 | 3 |
| bike_share_1 | 4 | 1 |
| cs_semester | 5 | 1 |
| food_inspection_2 | 5 | 1 |
| movielens | 7 | 1 |
| book_publishing_company | 11 | 0 |
| books | 15 | 0 |
| college_completion | 4 | 0 |
| donor | 4 | 0 |
| food_inspection | 3 | 0 |
| language_corpus | 6 | 0 |
| movie_3 | 16 | 0 |
| movie_platform | 5 | 0 |
| movies_4 | 17 | 0 |
| music_platform_2 | 4 | 0 |
| olympics | 11 | 0 |
| restaurant | 3 | 0 |
| retails | 8 | 0 |
| sales_in_weather | 3 | 0 |
| shipping | 5 | 0 |
| student_club | 8 | 0 |
| student_loan | 10 | 0 |
| superhero | 10 | 0 |
| toxicology | 4 | 0 |
| university | 6 | 0 |
| video_games | 8 | 0 |

## 本次审计未能回答的悬而未决问题(已于 2026-07-01 解决)

问题在于:鉴于 `pipeline/04_load_pg_base.py` 中当前的
`WITH create tables, create indexes, reset sequences` 子句,pgloader 默认的
`downcase identifiers` 行为(文档中针对 SQLite 源有记载,且不像 MySQL/PostgreSQL
源那样有可覆盖它的选项)究竟是否真的会生效。本次审计只扫描 SQLite 源文件,
并不会把任何东西加载进 PostgreSQL。

**通过直接阅读 pgloader 源码得以解决**(`src/params.lisp`、
`src/utils/quoting.lisp`、`src/parsers/command-options.lisp`):答案是肯定的,
`downcase identifiers` 确实是默认行为,对 SQLite 源也生效,但仅限匹配
`^[A-Za-z_][A-Za-z0-9_$]*$` 的标识符。凡是含空格/标点的标识符(也正是本次
审计的重点),无论 WITH 子句如何写,一直都会被强制进入加引号、保留大小写的
分支。真正的缺口在于那些不含标点的纯 PascalCase/camelCase 名称(例如
`works_cycles` 里的每一张表),它们能通过那条正则,于是被悄悄转成了小写。现在
`pipeline/04_load_pg_base.py` 会在 WITH 子句里显式传入 `quote identifiers`
(已确认这对 SQLite 源是合法语法,并非本次审计最初猜测的那样为 MySQL 专有),
并在每次加载之后,通过把 SQLite 的 `PRAGMA table_info` 和
`PRAGMA foreign_key_list` 与 `pg_base` 的 `information_schema` 做比对来实测
核实结果,这样一来,日后某个 pgloader 版本的变化会被立即发现,而不会在之后
以 R0==R1 执行失败的形式浮现。完整的说明见 `../methodology/obfuscation.md`
§4「Identifier quoting invariant」,其中也解释了为什么外键需要与表/列标识符
分开单独检查。

**一个无关的后续处理(同一天):** `reset sequences` 被单独从 WITH 子句里移除
了(不是因为大小写问题,而是因为 pgloader v3 的另一个 bug:`quote identifiers`
与 `reset sequences` 同时使用时,会在大小写混合的 serial/PK 列上产生一个硬性的
`42703` 错误;参见 `../methodology/obfuscation.md` §5 第 1 步,以及
`pipeline/04_load_pg_base.py` 中 `pgloader_command_script()` 上的代码注释)。
上面引用的那个 WITH 子句(`create tables, create indexes, reset sequences`)
反映的是撰写本审计时该子句的样子,而非当前的样子。
