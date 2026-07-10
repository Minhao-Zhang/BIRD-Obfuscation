[English](PROGRESS.md) · **中文**

# 进度日志

项目历史与状态。`AGENTS.md` 保持指导性质(讲如何运行/扩展流水线);本文件记录的是 **做了什么、何时做的、为什么这么做**:也就是 `AGENTS.md` 有意省略的叙事部分。方法论细节放在 [docs/methodology/](docs/methodology/) 下;本文件只做指引,不重复其中内容。

文中日期均为绝对日期。

---

## 状态快照:2026-07-10

- **离线分机评测已是默认路径。** `eval_contamination.py` 和 `eval_ablation.py` 在 PostgreSQL 机器上冻结 prompt 并准备公开请求包,在纯 API 机器上调用模型(`run_offline_generations.py`),再在 PostgreSQL 机器上执行返回的 SQL 并打分(`grade_offline_eval.py`)。`--local` 保留旧的同机路径;`--split {test,train}` 选择数据集。
- **训练集改写已完成。** `09_paraphrase_questions.py --include-train` 已跑完;`artifacts/question_paraphrases.jsonl` 现有 10,164 行(2,030 测试 + 8,134 训练)。
- **可移植公开包已入库。** `eval/offline-public-bundles.zip`(约 11 MiB)包含全部测试/训练公开包(`requests.jsonl` + `manifest.json`),供 API 机器使用。私有的 `grading_manifest.private.jsonl` 留在 DB 机器,本地通过 `prepare_offline_eval.py` 重新生成。
- **下一步:** 从 zip(或本地 `eval/offline/` 包)跑 API 生成,把 `generations.jsonl` 拷回 DB 机器,再打分并汇总。

## 状态快照:2026-07-05

- **核心流水线(步骤 0-7):已完成并通过验证。** 10,541 个候选问题中有 10,164 个通过了端到端验证(8,134 个训练 / 2,030 个测试;训练、测试两边都覆盖了全部 69 个数据库)。见 [docs/methodology/dataset.md §7](docs/methodology/dataset-zh.md)。
- **扩展混淆(步骤 08-10):已构建并应用。** 问题改写(步骤 09)和最初的诱饵 schema 注入(步骤 08)均已完成;随后诱饵这一维度重做成了 **损坏诱饵陷阱**(步骤 10,`10_inject_traps.py`)——在意识到空诱饵会在交互式"执行并观察"的 agent 面前自我暴露之后,改为附加式的"邪恶双胞胎"列(1,486 个)+ 克隆表(162 个),里面存的是真实数据被微妙*损坏*后的副本。这些内容注入进两个诱饵实例(`pg_decoy`、`pg_rename_decoy`)的两种变体中;真实数据已验证逐字节相同,因此 R1==R2 依然成立。见 [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design-zh.md)。
- **四个 PostgreSQL 实例**(`pg_base` / `pg_rename` / `pg_decoy` / `pg_rename_decoy`)已构建,并作为压缩的 `pg_dump` **发布**在 [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation) 上。Gold SQL + 映射 + 陷阱清单以 git 跟踪的形式放在 [`eval_dataset/`](eval_dataset/) 中;下载/恢复/运行说明见 [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset-zh.md)。
- **混淆有效性评测(四种条件):已实现;正在重跑。** 早前的数据已作废。为确保结果稳健,完整评测正在一个更强的模型上重跑。设置见 [docs/methodology/evaluation.md §8](docs/methodology/evaluation-zh.md);在该次运行完成前不报告任何结果。
- **五臂消融实验(`eval_ablation.py`:base/rename/decoy/paraphrase/all):已实现;完整运行有待** 上述同一次更强模型的运行。目前尚无结果可报告。

---

## 已完成

### 核心流水线(截至 2026-07-02)
- 已实现步骤 0-7:切分 → 语言分配 → 重命名映射(Bedrock)→ 加载 `pg_base`(pgloader)→ 转译 + R0==R1 → 克隆/重命名 `pg_rename` → 重命名 SQL + R1==R2。交付物:`artifacts/{train,test}_final.jsonl`。
- 双 oracle 完整性:R0==R1(SQLite 基准真值 vs 转译后的 PG)与 R1==R2(原始 PG vs 混淆后的 PG)。约 12% 通过验证的行使用了 VALUES 物化(见 [docs/reference/step5-transpilation.md](docs/reference/step5-transpilation-zh.md))。
- 四条件混淆有效性评测已通过 `pipeline/eval_contamination.py` 实现;设置见 [evaluation.md §8](docs/methodology/evaluation-zh.md)(结果正在一个更强的模型上重跑,完成前不予报告)。

### 方向确定(2026-07-03)
- **文献综述**(SPENCE arXiv 2604.17771;SQL2NL arXiv 2509.04657;Termite/ATD arXiv 2402.08100;ConStat;Min-K%/Time Travel 综述)。核心结论:敏感的污染信号在于 **问题/句法轴**,而非标识符轴;BIRD 在标识符轴上只有微弱污染(τ ≈ −0.35,置信区间跨越零),这是来自已有文献的信号,与我们自己(尚待进行)的测量相互独立。定位结论:该数据集的持久价值在于它是一个 **经过验证的多语言 Postgres Text-to-SQL 资产 + 稳健性测试平台**,污染只是一个次要的(诚实、偏负面的)结果。
- **决策:扩展混淆**,新增两个可独立开关的维度,用一次消融实验分别测量:
  - **诱饵 schema 注入**:干扰表 + 易混淆列(攻击 schema linking)。
  - **问题改写**:使用廉价模型、以 SQL 为条件生成(攻击对问题表述的记忆)。
- **`SELECT *` 测量**(子 agent,2026-07-03):在 gold 查询中,只有 **3 / 10,164** 个在顶层含有真实表的星号(全部来自 `mondial_geo`),任意层级则有 5 个;已排除 1,169 个 VALUES 物化的查询;67/69 个数据库不含星号。→ 诱饵列导致 `SELECT *` 出错的情况可以忽略;对受影响的 gold 做星号展开即可解决。
- **先整理文档再写代码**(本轮):撰写了 `obfuscation-extensions.md`,新增 `evaluation.md §9`(消融设计),从 `obfuscation.md` 建立交叉链接,并创建了本日志。

---

### 构建进度(2026-07-03)

按照 [docs/reference/extension-implementation-plan.md](docs/reference/extension-implementation-plan-zh.md) 实施:

- ✅ §2a `pipeline/_db.py`:抽取了共享的 PG 辅助函数(保持行为不变;污染评测数据不变)。
- ✅ §2b `pipeline/_eval_helpers.py`:抽取了共享的评测机制;`eval_contamination.py` 现在只是一个精简的污染评测入口。
- ✅ §3b `docker-compose.yml`:`pg_decoy`(5434)+ `pg_rename_decoy`(5435),受 profile 控制(`--profile decoy`);默认启动不变。
- ✅ §3c 克隆了诱饵数据卷;✅ 运行步骤 08(`decoy_map.json`,注入结构性诱饵 + 重新验证 R1==R2);✅ §6 运行 `09_paraphrase_questions.py`(`question_paraphrases.jsonl`);✅ §7 编写了 `eval_ablation.py`。

### 扩展混淆:构建 + 转向损坏诱饵(2026-07-04 → 07-05)

- **转向损坏诱饵。** 评测对象是一个 **交互式"执行并观察"的 SQL agent**,而空的诱饵表 / NULL 诱饵列会轻易自我暴露(`COUNT(*)=0` 等)。因此诱饵维度从*空的*结构性诱饵(步骤 08)重做成 **损坏陷阱**(`pipeline/10_inject_traps.py`),且严格 **附加**,使真实数据保持逐字节相同、R1==R2 依旧成立。设计 + 风险登记:[docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design-zh.md)。
  - **阶段 1:邪恶双胞胎列**(`trap_manifest.json`,1,486 个):一个新列,以 LLM 给出的同义名命名,存放某真实列被损坏后的副本;仅限 ≤500k 行的表;连接键 → 置换(保持 RI),其余则混合使用 sparse perturb / cat-remap / date-offset / null。
  - **阶段 2:损坏克隆表**(`trap_table_manifest.json`,横跨 66 个数据库共 162 个):将整张真实表克隆 + 重命名,并损坏其中一部分列;源表限 ≤50k 行;构造上就保证 R1==R2 安全(gold 从不引用诱饵表)。
  - 注入到 `pg_decoy` + `pg_rename_decoy` 的两种变体中;**真实数据已被证明与干净实例逐字节相同**(每侧 532 张表)。命名通过 `gpt-5.4-mini` 完成;所有 `_alt`/`_archive` 兜底名称均已清除。修复了一个 `sparse_perturb` 整数溢出问题(将其钳制到目标整数类型的取值范围内)。
  - R1==R2 遗留下 **153 个顺序敏感 + 21 个原本就执行失败** 的 qid,排除在严格的跨变体 EX 之外(`artifacts/order_sensitive_qids.json`);这些都是良性的(陷阱 UPDATE 引起堆重排 → 得到不同但仍有效的 LIMIT/浮点聚合结果)。
- **打包 + 发布。** 四个实例全部导出(`pg_dump -Fc`,zstd;每个约 3 GB,约 10:1 压缩比),并 **发布到 Hugging Face**:[minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)。Gold + 映射 + 清单已整合进 git 跟踪的 [`eval_dataset/`](eval_dataset/);使用者指南见 [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset-zh.md)。
- **评测可移植性。** 评测脚本按 `artifacts/` → 回退到 `eval_dataset/` 的顺序解析输入(全新克隆无需重新生成即可运行)。Postgres 的 DSN 现在可通过 `PG_*_DSN` 用环境变量配置(默认 = 本地 docker),因此评测无需改代码即可指向远程 Postgres / AWS RDS。

## 下一步(计划,按顺序)

1. **端到端跑完离线评测:** 从 `eval/offline-public-bundles.zip`(或 `eval/offline/` 各臂包)在 API 机器生成,再在 DB 机器用 `eval_contamination.py` / `eval_ablation.py --generations ...` 打分。结果行会写入 eval 元数据(模型、推理强度、prompt 版本、git commit、输入产物哈希);续跑只复用元数据匹配的行。报告配对差值 + bootstrap 置信区间;严格评分排除 `order_sensitive_qids.json`。
2. **(可选)AWS 部署**:配置现已可移植(环境变量 DSN、Hugging Face 上的 dump、被跟踪的 `eval_dataset/`)。推荐形态:单台 EC2,运行仓库中由 HF dump 恢复的 docker-compose 实例,OpenAI key 来自 Secrets Manager,结果写入 S3。
3. **(下游,独立仓库)** 交互式 agent harness + 真正触发陷阱的"诱饵一致性回答" / 陷阱命中率指标。

## 决策日志

- **2026-07-03**:将问题改写作为一个*可选*维度重新引入(它曾因漂移风险在核心流水线中被舍弃;以 SQL 为条件的生成缓解了该风险,而且针对的是更敏感的那条轴)。
- **2026-07-03**:诱饵实例是独立的 PG 容器(`pg_*_decoy`);`pg_base`/`pg_rename` 保持为干净的基线。诱饵表默认为空(在精简后的 DDL 中不可见)。**已于 2026-07-04 被取代:** 空诱饵在交互式"执行并观察"的 agent 面前会自我暴露,因此现在往诱饵里*填入*附加的损坏数据(步骤 10 的损坏陷阱);见下文。
- **2026-07-03**:所有消融臂一律以对 `SELECT *` 展开后的 gold 做精确多重集相等来评分,绝不使用宽松的包含判定(包含判定会让偷懒的 `SELECT *` 蒙混过关,从而虚高 EX)。
- **2026-07-03**:在整个仓库统一命名为 `base`/`rename`/`decoy`/`rename_decoy`(数据库实例、评测臂/条件、数据字段、文件)。这解决了旧有的 `sql_pg`/`sql_obfuscated` 不对称问题(改为 `sql_base`/`sql_rename`);"obfuscation" 仍作为统称。交付的 JSONL 已就地迁移。
- **2026-07-03**:在本地 Docker Desktop / WSL 环境下,切勿同时让四个 PostgreSQL 实例都处于重负载。这可能让 WSL 虚拟机 OOM,并(在 `fsync=off` 时)损坏数据卷。只启动某个步骤/臂所需的实例;顺序运行各消融臂;评测 `--concurrency` ≤ 3;在 `.wslconfig` 中限制 WSL 虚拟机的内存,也是个有用的兜底。(这是*本地* Docker-Desktop/WSL 的限制;配置充足的服务器可以同时跑全部四个。)
- **2026-07-04**:转向损坏诱饵(见状态快照)。诱饵携带真实数据的 **附加式** 损坏副本,从不修改真实的行/列/表,因此 R1==R2 得以保持。相关联的列 *可以* 作为陷阱来源(附加式 ⇒ 不会破坏任何跨列不变量);连接键/外键列 **只通过置换** 来损坏(值仍是真实的键 ⇒ 参照完整性得以保持)。设置行数上限以约束成本:邪恶双胞胎列 ≤500k 行,克隆表源 ≤50k 行。损坏是确定性的(以哈希作种子、与变体无关的 salt),因此重新构建就能复现。
- **2026-07-04**:对良性的 R1!=R2 采取接受 + 标记而非追查的做法:153 个顺序敏感(无全序的 LIMIT / 浮点聚合的 gold 在陷阱 UPDATE 重排堆之后会返回不同但仍有效的结果)+ 21 个原本就执行失败 → `artifacts/order_sensitive_qids.json`,排除在严格的跨变体 EX 之外。与顺序无关的指纹已证明真实数据完好无损。
- **2026-07-05**:交付物分两处存放:四个 PostgreSQL **数据库** 以 `pg_dump` 归档形式放在 Hugging Face(git 放不下),而 **gold SQL + 映射 + 陷阱清单** 以 git 跟踪放在 `eval_dataset/` 中。评测脚本先读 `artifacts/`,再回退到 `eval_dataset/`,因此全新克隆无需重新生成即可运行。
- **2026-07-05**:Postgres 的 DSN 可通过 `PG_*_DSN` 用环境变量配置(默认 = 本地 docker-compose 的端口);让评测无需改代码即可指向远程 Postgres / AWS RDS。
