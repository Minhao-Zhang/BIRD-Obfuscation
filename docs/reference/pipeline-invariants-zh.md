[English](pipeline-invariants.md) · **中文**

# 流水线不变量:详细依据

`AGENTS.md` 把这些不变量精简成了编辑流水线时要保留的规则。本文件则是**取证记录**:讲清每条规则为什么存在,以及背后的经验证据。每一条都在真实运行的 PostgreSQL 和实际最坏情况的数据库上验证过,不是照着文档凭空推断出来的。改动某条规则所保护的代码之前,请先读一读对应章节。

---

## 步骤 4:用 pgloader 加载进 `pg_base`

### pgloader 以容器方式运行,而非安装在宿主机上
步骤 4 使用 `dimitri/pgloader:v3.6.7`。它没有像样的原生 Windows 构建版本,加上 pgloader 是个 Common Lisp 二进制文件,很难在不同环境里都装稳妥;既然 Docker 本来就是硬依赖(PostgreSQL 实例都跑在 Compose 里),干脆用同样的方式跑 pgloader,环境前提就简化成一句"Docker 正在运行"。`load_db()` 会把每个 SQLite 文件以**只读**方式绑定挂载进容器,再通过 stdin 以 `pgloader /dev/stdin` 的形式传入 LOAD DATABASE 脚本:pgloader **不**接受用 `-` 代表 stdin,必须写字面路径 `/dev/stdin`。它会把每个 SQLite 数据库的一份**未重命名的精确副本**加载进 `pg_base`,各自放在独立的 schema 里(`db_id.table_name`)。

### 通过 `host.docker.internal` 访问 `pg_base`
而不是走 Compose 网络 / 服务名。pgloader 的 DSN 主机名语法不接受下划线,而两个 Compose 服务名(`pg_base`、`pg_rename`)都带下划线。加入 Compose 网络、用服务名做 DNS 解析,会解析失败。为了在 Linux 宿主机上也能用,即便 Docker Desktop 会自动解析,还是传了 `--add-host=host.docker.internal:host-gateway`。

### 不要把 `reset sequences` 加回 WITH 子句
pgloader v3 有一个没修的 bug(dimitri/pgloader#1651;PR #1701 提过修复,但没合并就被关掉了,只在 v4 重写版 PR #1705 里落地,从未向后移植):`quote identifiers` 加序列重置,会生成把字面双引号嵌进列名的 `pg_get_serial_sequence()` 调用,对任何大小写混合的 serial/PK 列都会触发硬性 `42703` 错误,而这些恰恰是 `quote identifiers` 本来就要处理的数据库(`works_cycles` 等)。本流水线加载后从不往 `pg_base` 插数据,序列起始值无所谓;所以这个子句是直接删掉,不是想办法绕过。

### 不要把 `create indexes`(去掉 `no`)加回 WITH 子句
`quote identifiers` 生效时,pgloader 自动生成的 `CREATE UNIQUE INDEX` / `ADD PRIMARY KEY` DDL 会**在索引定义内部把列名写成不带引号**,哪怕这列本身在 `CREATE TABLE` 里创建得没问题、也加了引号。Postgres 会把未加引号的名字做大小写折叠,于是找不到这一列。实测过:光 `works_cycles` 一个库就有 51 个硬性错误,每张表一个。没有哪个 WITH 子句选项能既保住索引、又只修掉它内部的引用 bug(在 pgloader 的语法里,PK 和索引创建共用一个开关)。`pg_base` 没有索引是不得已,不是主动选的。反正流水线只会对它做 `SELECT`,损失的只是查询速度。

### 不要加 `foreign keys`(也不要移除 `no foreign keys`):这是方法论决策,而非权宜之计
下游的智能体式 Text-to-SQL 任务不该白白拿到 FK 关系(参见 [../methodology/obfuscation.md](../methodology/obfuscation-zh.md) §2"故意不放进 schema lake 中")。这同时还避开了一个真实存在的 pgloader 崩溃:SQLite 的 `FOREIGN KEY (col) REFERENCES OtherTable` 简写形式(省略了被引用的列)会让 `PRAGMA foreign_key_list` 返回空的 `to` 列,某些 pgloader 版本给它生成 DDL 时会崩溃:保留下来的 69 个数据库里,15 个总共有 176 个这样的 FK,不算少见。这也让 `pg_base` 跟从不创建 FK 约束的 `pg_rename` 保持一致。

### 针对 `DEFAULT CURRENT_TIMESTAMP` 列的 CAST 规则
`CAST type datetime when default 'current_timestamp' to timestamptz drop default`。没有它,pgloader 会在生成的 DDL 中把 SQLite 的 `DEFAULT CURRENT_TIMESTAMP` 引用成字面字符串 `'current_timestamp'`,而 `timestamptz` 列会拒绝它:这是一个硬性的 `CREATE TABLE` 失败,会中止**整个**含此类列的数据库的加载(已确认:`works_cycles`、`movie_3`,共 80 张表)。

### 针对 MySQL 风格零日期哨兵值的 CAST 规则
`type date when default '0000-00-00' to date drop default`。SQLite 的 `0000-00-00` 哨兵值(`formula_1.races`、`thrombosis_prediction.Laboratory`)超出了 PostgreSQL 的 `date` 范围,同样会中止整个数据库的 `CREATE TABLE`。

### CAST `blob to text`
有些 SQLite 列声明为 `BLOB`,但每一行的实际存储类别都是 `TEXT`(例如以字符串形式存储的十六进制编码图片):`book_publishing_company.pub_info.logo`、`works_cycles.ProductPhoto`/`Document`、`movie_3.staff.picture`。pgloader 默认的 `BLOB→bytea` 转换会尝试对文本值做 base64 解码,失败后**悄无声息地丢掉整行**(退出码仍是 0)。改把目标类型定为 `text` 就完全绕开了解码路径(pgloader 关于 base64 的判断取决于 CAST 的*目标*类型,而不是源端的声明)。

### FIXNUM 溢出挂起:最危险的失败模式
当一个 SQLite `INTEGER` 列保存的值超出 SBCL 的 FIXNUM 范围(约 ±4.6×10^18)时,会让 pgloader 的 `integer-to-string` 转换崩溃,而 pgloader **不会退出,而是在 0% CPU 下无限期挂起**。它不理会 `--on-error-stop`,也不报错退出,会让一次无人值守的运行永远悄悄卡住(只能 `docker kill` 掉容器)。已在 `talkingdata` 的 `app_id`/`device_id`(合法的 64 位哈希)以及 `events_relevant.timestamp`(声明为 `DATETIME`,却保存着同样的大整数)上确认过。修复方式是 `04_load_pg_base.py` 中的 `EXTRA_CASTS`:一条列级作用域、**不带 `using` 子句**的 `CAST column tbl.col to bigint` 规则可以绕开会崩溃的转换(不带 `using` 时会退回到一个通用的、对 fixnum 安全的字符串化器)。**只检查了 `talkingdata` 中的 `app_id`/`device_id`,并未排除语料库中其他地方存在此问题的可能。**如果步骤 4 在一张不算庞大的表上卡在 0% CPU、且超过 30s 没有新日志输出,请首先怀疑这个问题。

### `EXTRA_CASTS` 必须列在全局的按类型 CAST 规则*之前*
pgloader 的 CAST 匹配会按列表顺序在第一条匹配的规则处停止。如果某个类型的列级作用域覆盖规则(例如一个 `DATETIME DEFAULT current_timestamp` 列)同时也被某条全局规则覆盖,那么当它排在后面时会悄无声息地输给全局规则,这一点已在 `works_cycles.CountryRegion.ModifiedDate` 上确切遇到过——那里的覆盖规则直到被移到全局规则之前才生效。

### WITH 子句必须包含 `quote identifiers`
pgloader 默认会把 SQLite 标识符转为小写(`src/params.lisp` 中的 `*identifier-case*` 默认为 `:downcase`,应用于包括 SQLite 在内的每一种源加载器)。这个默认行为只对匹配 `^[A-Za-z_][A-Za-z0-9_$]*$` 的标识符生效;带空格/标点的名字本就已经被强制走了加引号/保留大小写的分支。因此,即便没有危险的标点,普通的 PascalCase 表(例如 `works_cycles` 的全部 65 张表)也在被悄悄转为小写:这正是旧的、不含大小写指令的 WITH 子句会踩到的失败。`quote identifiers` 是 SQLite 源合法的 WITH 子句语法(与 MySQL 源共用同一条规则,并非 MySQL 独有)。最终的 WITH 子句是 `create tables, create no indexes, quote identifiers, no foreign keys` 再加上上面那些 CAST 规则。

### `check=True` 是必要但不充分的:即便发生数据丢失的失败,pgloader 也会返回 0
已直接确认过,包括在带 `--on-error-stop` 的情况下遇到 `FATAL` 级的 schema 创建错误:pgloader 依然以 0 退出,且一张表都没创建。因此,步骤 4 在每次加载后都会独立于退出码验证两件事:
- `verify_casing()`:把 SQLite 的 `PRAGMA table_info` 与 `pg_base` 的 `information_schema.tables/columns` 做差异比对。
- `verify_row_counts()`:逐表比对 `SELECT COUNT(*)`;正是这项检查能抓出表虽正确存在、却悄悄缺少行的情况(例如某次 COPY 出现了被拒绝的行错误)。BIRD 的 `works_cycles.sqlite` 中两个真实的、原本就存在的数据质量缺陷,就是靠这种方式才发现的(一行表头被混进了 `CountryRegion` 的数据里;以及上面提到的以十六进制 TEXT 形式存储的 BLOB 列)。

如果这两项检查中任何一项开始失败,不要只是重试。请去读 pgloader 日志,找出真正的错误。

---

## 步骤 5 与 7:sqlglot 转译、重命名与 SQL 生成

### 所有生成的标识符都要加引号
`05_transpile_sql.py` 和 `07_rename_sql_and_validate.py` 会给每个标识符加双引号。给一个全小写的名字加引号没有坏处;而不加引号的大小写混合/带标点的名字会被 PostgreSQL 误解。不要为"看起来是小写,就跳过加引号"做特殊处理。

### schema 限定必须跳过 CTE 别名
`WITH x AS (...)` 别名在后续被引用时会解析成 `exp.Table`:sqlglot 的 AST 无法区分 CTE 引用和真正的表引用。盲目地给每个 `exp.Table` 加 schema 限定,会把 `FROM x` 变成并不存在的 `FROM "db_id"."x"`。要收集 CTE 别名(`{cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}`)并将其排除。这并非假设:有 9 条 gold 查询使用了 `WITH`(`card_games`、`formula_1` ×6、`toxicology`),在修复前正好踩到了这个问题。

### 切勿在遍历正在进行的 `stmt.walk()` 时修改 sqlglot AST 节点
`node.set(...)` 会创建一棵新的子树,遍历器随后会下降进去并重复访问它,已确认即便是一次平凡的恒等重命名也会导致挂起和内存无限增长(每次重命名都会触发,而非边缘情况)。要先收集出需要修改的节点列表,等遍历完成后再做修改。参见 `07_rename_sql_and_validate.py` 中的 `rename_sql()`。

### 切勿重命名作为 Table 的 `db`/`catalog` 参数的 `exp.Identifier`
有三个数据库(`superhero`、`sales_in_weather`、`university`)有一张表,其名字恰好等于 `db_id` 本身。若天真地重命名每一个与重命名映射键匹配的 `Identifier`,就会破坏 schema 限定符(`"superhero"."superheld"` → `"superheld"."superheld"`,而后者并不存在)。要检查 `node.parent.args.get("db") is node`(以及 `"catalog"`)并跳过这些节点。

### 修复批次导出给智能体的 schema 上下文来自 `pg_base`,而非 SQLite
`_transpile_helpers.py` 中的 `get_pg_schema_ddl()`(由 `05c_export_fix_batch.py` 使用)读取的是真实运行的 `pg_base` 的 `information_schema`,因为 pgloader 会做自己的类型推断(SQLite 的动态类型无法与 PostgreSQL 一一对应),而且如果某个大小写边缘情况漏了过去,它拼写标识符的方式可能会不同。把 SQLite 的 `CREATE TABLE` 文本交给智能体,所描述的 schema 可能与它实际查询的对象并不一致。

### 证据提示与 SQL 使用相同的重命名映射替换
采用词边界正则,先匹配最长的标识符。参见 `07_rename_sql_and_validate.py` 中的 `rename_evidence()`。输出记录同时带有 `evidence`(原文)和 `evidence_rename`;下游消费方应使用经过混淆的那一个。

---

## 步骤 6:克隆并重命名 `pg_rename`

### 在已克隆好的卷内就地重命名:不从 SQLite 重新加载,也不连接 `pg_base`
早先的版本会重新读取每个 SQLite 文件,并通过 `_pg_helpers.py` 中基于数据采样的 `infer_pg_type()`(仅 NUMERIC/TEXT)独立地重新推断列类型:这是第二遍更粗糙的推断,可能悄悄地与 pgloader 已经在 `pg_base` 中选定并验证过的类型不一致。在运行步骤 6 之前先克隆 `pg_base` 的 Docker 卷,就完全消除了这个风险:`pg_rename` 的类型在构造上就是 `pg_base` 的类型,而 `06_build_pg_rename.py` 只按 `schema_rename_map.json` 执行 `ALTER TABLE ... RENAME TO` / `RENAME COLUMN`:无论行数多少,这都是一个只涉及目录(catalog)的快速操作。`_pg_helpers.py` 的 `infer_pg_type()`/`copy_data()`/`get_sqlite_schema()` 已不再被任何脚本使用;`find_sqlite_path()` 仍被步骤 0 使用。

---

## 执行与连接

### 步骤 7 中的 `exec_pg()` 必须使用带硬性行数上限的 `fetchmany()`,绝不能用 `fetchall()`
至少有一条 gold 查询(`bike_share_1`,一个缺了日期条件的连接)在没有 `LIMIT` 的情况下返回 19.4M 行:`fetchall()` 会把数千万个 Python 元组全部实例化出来,让进程在数 GB 内存下挂起。超过 `MAX_RESULT_ROWS` 的溢出会抛出 `ResultSetTooLarge`,并像其他任何失败一样被送往 `rename_failures.jsonl`。

### 在 `autocommit=True` 下 `SET LOCAL` 会悄无声息地什么都不做;要用普通的 `SET`
`SET LOCAL` 的作用域限于事务,而 autocommit 会给每条语句各自一个隐式事务,因此它对下一条查询没有任何影响(通过随后立即执行 `SHOW statement_timeout` 返回 `'0'` 得到确认)。`_transpile_helpers.py` 的 `exec_pg()` 正确地使用了 `SET LOCAL`,因为它的连接运行在 `autocommit=False` 下;而 `07_rename_sql_and_validate.py` 的连接是 `autocommit=True`,必须使用普通的 `SET`。

### Postgres DSN 默认使用 `host=127.0.0.1`,而非 `host=localhost`
在本项目的 Windows/Docker Desktop 环境下,`localhost` 会优先解析为 IPv6,而 IPv6 的连接尝试要花 20 多秒才会回退到 IPv4(通过裸用 `/dev/tcp/localhost/5432` 耗时 21s 与 `/dev/tcp/127.0.0.1/5432` 瞬间完成的对比得到确认)。每一次新建连接都要付出这份代价;不要在本地默认值中重新引入 `localhost`。这些 DSN 可以通过 `PG_*_DSN` 环境变量按实例覆盖(`_db.py`,参见 `.env.example`),这样评测就能指向远程 Postgres / AWS RDS(远程覆盖自然会使用主机名),但本地 docker 的默认值必须保持为 `127.0.0.1`。

---

## 横切关注点

### 所有涉及 `schema_rename_map.json` 或问题/证据文本的读取都要显式指定 `encoding="utf-8"`
没有它,Python 会默认使用平台代码页(Windows 上是 `cp1252`),一遇到第一个非 ASCII 的法语/德语/西班牙语/拼音标识符就会崩溃。写入端已经这样做了(`json.dumps(..., ensure_ascii=False)` 加上显式的 UTF-8 文件句柄);读取端必须与之匹配。

### `01_split.py` 的切分逻辑要保持按数据库独立且可复现
用 `(SEED, db_id)` 的稳定哈希(例如 `zlib.crc32`,**而不是** Python 的 `hash()`,后者按进程加盐)为每个数据库的 `random.Random` 播种。切勿跨数据库复用同一个 `Random` 实例的状态。

### Docker Compose 的 WAL 调优是有意为之
`wal_level=minimal`、`fsync=off`、`max_parallel_workers=0`、`shm_size: 256mb` 等等。四个实例始终都是可重建的(干净的 `pg_base`/`pg_rename` 从 SQLite 重建,`*_decoy` 这一对则通过克隆干净卷 + 重新注入陷阱来重建),因此持久性无关紧要;这些调优是为了批量加载的速度。请保留它。
