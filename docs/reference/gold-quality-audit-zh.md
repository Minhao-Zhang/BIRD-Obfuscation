[English](gold-quality-audit.md) · **中文**

# Gold 质量审计:BIRD 标注错误与 2026-07-29 问题清理

**状态:** 已执行。2026-07-29,10,164 个问题里的 2,739 个(27.0%)被移除;随后又剔除了跌破
`MIN_QUESTIONS = 60` 下限的 11 个数据库,并对剩余部分重新划分。最终数据集:**58 个数据库、
6,928 个问题(5,539 train / 1,389 test)**。四个已发布的 PostgreSQL dump **未受影响**,也没有重建。

本文记录数据集为何缩减、受影响的问题如何被识别、划分如何重建、哪些内容被有意保留,以及哪些
决策仍待定。

---

## 1. 发现

本数据集所依赖的 gold SQL —— BIRD 2023 的 `train` + `dev` —— 存在大量标注错误,而且这些错误
是**系统性**的,而非随机的。三项独立审计的结论一致;另有两个在 BIRD 2023 之后发布的官方
`birdsql` release,取代或撤回了原始 gold 中相当大的一部分。

| 审计 | 划分 | n | 错误率 |
| --- | --- | --- | --- |
| Wretblad 等 2024(§5 [1]) | `dev`,`financial` 领域 | 106 | 49% 含噪声;20.7% gold SQL 错误 |
| Jin 等 2026([2]) | Mini-Dev | 498 | **52.8%** |
| Jin 等 2026([2]) | `dev` 抽样 | 100 | 48% 需要修正 |
| Zhu 等 2026([4]) | **`train`** | 2,500 | **61.1%** |
| Pourreza & Rafiei 2023(经 [4] 转述) | `train` 子集 | — | 18.2% |

Jin 等给出的错误分类(占出错样本的比例,可多标):E2 schema/数据理解错误 57.8%,E4 问题歧义
29.7%,E1 问题与 SQL 语义不匹配 29.3%,E3 领域知识错误 10.7%。反复出现的具体模式包括:漏掉
`DISTINCT`、join 条件错误、用 `BETWEEN` 表达严格不等式,以及 `california_schools` 的 gold 漏掉
`rtype = 'S'`,导致学区被当成学校计数。

对榜单的影响很严重:把 BIRD 榜单上 16 个 agent 在 100 个修正后的 `dev` 样本上重新评测,执行准确率
的相对变化区间为 −7% 到 +31%,排名变动 −9 到 +9 位;原始榜单与修正子集之间的排名相关性为
rs = 0.32(p = 0.23)—— 在统计上与"无关"无法区分([2])。

### 为什么这对本数据集比对逐题基准更重要

本数据集衡量的是:agent 能否从此前的(问题, SQL)对中构建**语义层**,并将其应用到未见过的问题上。
在这一范式下,train 划分不是用于梯度训练的数据,而是用来归纳 schema 语义的证据语料。其中一条
错误的 gold SQL 不只是浪费一行,而是教会模型一个错误映射,并会传播到之后每一个涉及同一概念的
问题。由于 BIRD 的错误在同一个数据库内反复出现(`rtype` 漏条件、`BETWEEN` 误用),它们恰好是
归纳型学习者会当作规则吸收的规律。因此标注噪声在这里是**因一致性而被放大**,而不是因数量而被稀释。

---

## 2. 使用的上游 release

两者都是官方 `birdsql` 发布,许可为 CC-BY-SA-4.0(与本仓库相同)。

| Release | 行数 | 性质 |
| --- | --- | --- |
| [`birdsql/bird_sql_dev_20251106`](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106)([6]) | 1,534,11 个库 | **已修正**的 dev。`question_id` 0–1533 连续,可直接与 dev 来源的行对齐。 |
| [`birdsql/bird23-train-filtered`](https://huggingface.co/datasets/birdsql/bird23-train-filtered)([7]) | 9,428 中的 6,601 | **已过滤**的 train。无 `question_id`,需按 `(db_id, 规范化问题文本)` 对齐。 |

这个区别很关键。`dev_20251106` **修正** gold SQL;`bird23-train-filtered` 则是**删除**了 BIRD 不愿
背书的问题,几乎不做修正 —— 在与本数据集匹配上的 6,292 行中,只有 6 行(0.1%)SQL 不同。对
train 来源的问题而言,"不在过滤后的 release 里"就是全部信号;上游不存在修正后的 train gold。

[`birdsql/bird_mini_dev`](https://huggingface.co/datasets/birdsql/bird_mini_dev)([8])也做了检查,
但**未**采用:它基于*原始* dev 构建,因此带有同样的标注错误,且已被 `dev_20251106` 取代。不过它的
`mini_dev_pg` 划分起到了一个作用 —— 独立校验本仓库的 SQLite→PostgreSQL 转译(步骤 05):在与本
数据集重叠的 499 个问题上,它的 PostgreSQL gold 与我们的 `sql_base` 语义完全一致,差异仅在标识符
引号、schema 限定、别名标签,以及 `substr` 与 `substring` 的写法。

---

## 3. 方法

`pipeline/build_gold_quality_flags.py` 为每个 `question_id` 输出一行到
`eval_dataset/gold_quality_flags.jsonl`,对齐方式如下:

- **dev 来源**(BIRD 的 11 个 dev 数据库):按 `question_id` 对齐。全部 1,531 行 dev 来源的问题
  都匹配上了。当 `dev_20251106` 的 gold SQL 在规范化(空白、大小写、结尾分号)后与我们的
  `sql_sqlite` 不同时,该行被标记。
- **train 来源**(其余 58 个数据库):按 `(db_id, 问题)` 对齐,问题文本规范化为小写字母数字。当该
  行**不存在于** `bird23-train-filtered` 时被标记。

删除比例佐证了"缺失=删除"而非"文本改写":BIRD 在全局删掉了 train 的 30.0%(9,428 → 6,601),
我们 train 来源的行有 27.1% 缺失,而匹配上的行里只有 0.1% SQL 不同。

随后 `pipeline/apply_gold_quality_filter.py` 重写所有以问题为键的产物,只保留 `clean: true` 的行。

---

## 4. 移除了什么

| 划分 | 之前 | 之后 | 移除 |
| --- | --- | --- | --- |
| `train_final.jsonl` | 8,134 | **5,984** | 2,150 —— 1,837 被 train 过滤删除,313 dev gold 变更 |
| `test_final.jsonl` | 2,030 | **1,441** | 589 —— 504 被 train 过滤删除,85 dev gold 变更 |
| **合计** | **10,164** | **7,425** | **2,739(27.0%)** |

同样按 `question_id` 过滤的还有:`question_paraphrases.jsonl`(10,164 → 7,425)、
`gold_result_hashes_rename_decoy.jsonl`(10,164 → 7,425)、`gold_star_expanded.jsonl`(5 → 3),
以及 `order_sensitive_qids.json` 中的排除名单(`order_sensitive` 153 → 104,`exec_failed` 21 → 10)。

`gold_quality_flags.jsonl` 保留全部 10,164 行作为审计轨迹,其中包含 398 行 dev gold 变更所对应的
修正后 `dev1106_gold_sql`,以便日后采纳这些修正时无需重新下载任何数据。

### 各数据库的存活情况

7,425 个问题分布在全部 69 个数据库中,但有 11 个已低于步骤 01 的 `MIN_QUESTIONS = 60` 下限:

```
app_store                34/54    financial               38/106   retail_world         38/43
music_platform_2         40/69    college_completion      45/76    california_schools   46/89
debit_card_specializing  47/64    cookbook                49/69    bike_share_1         51/111
computer_student         53/72    software_company        56/75
```

`financial`(存活 36%)、`california_schools`(52%)和 `bike_share_1`(46%)恰好是文献指出噪声最
严重的领域 —— `financial` 损失 64% 的问题,正是 Wretblad 等 49% 噪声率的结论在我们数据上的独立复现。
注意 `app_store`(54)和 `retail_world`(43)在本次过滤**之前**就已低于 60,原因是步骤 05/07 的校验损耗。

---

## 4b. 恢复下限并重新划分

清理破坏了步骤 01 的两条不变量:11 个数据库跌破 `MIN_QUESTIONS = 60`;而且由于清理对 train 和
test 的命中并不均匀,各库的 test 占比漂移到 **12–26%**,而非统一的 20%(`bike_share_1` 只剩 6 个
测试题,`retail_world` 6 个,`menu` 10 个 —— 少到无法对该 schema 做任何估计)。

`pipeline/resplit_after_purge.py` 恢复了这两条。所采取的决策是**保持步骤 01 的原始判据不变** ——
一个数据库需要实际存在 ≥60 个问题 —— 而不是放宽下限或改用固定测试集规模。分布对两种做法都有话
说:45–129 区间的桶连续有值,不存在自然的间隙,而 `<60` 这一刀为移除 6.7% 的问题付出了 16% 的
schema 多样性。最终仍保留该下限,理由是与已发布的方法论保持一致,且在 ≥60 时按比例取 20% 仍能为
每个 schema 留下 ≥12 个测试题。

划分机制沿用步骤 01 的实现(直接复用而非重写):按库 `Random(SEED ^ crc32(db_id))`,`SEED = 42`,
`n_test = max(1, round(n * 0.20))`。按库取种子很关键 —— 若共用一个 `Random(42)`,会对每个数据库
逐下标施加完全相同的置换,从而使划分与 BIRD 源 JSON 中可能存在的位置结构产生相关。

| | 数值 |
| --- | --- |
| 清理后的候选池 | 7,425 个问题 / 69 个数据库 |
| 剔除的数据库(存活 < 60) | 11 个 —— 497 个问题 |
| **保留的数据库** | **58** |
| **train_final.jsonl** | **5,539(80.0%)** |
| **test_final.jsonl** | **1,389(20.0%)** |
| 各库 test 占比 | 0.195 – 0.206(此前为 0.12 – 0.26) |
| 最小的测试集 | `sales_in_weather` 12、`social_media` 12、`airline` 13 |

被剔除:`app_store`(34)、`financial`(38)、`retail_world`(38)、`music_platform_2`(40)、
`college_completion`(45)、`california_schools`(46)、`debit_card_specializing`(47)、
`cookbook`(49)、`bike_share_1`(51)、`computer_student`(53)、`software_company`(56)。

配套文件已过滤到存活 qid 集合:`question_paraphrases.jsonl` 与
`gold_result_hashes_rename_decoy.jsonl` 7,425 → 6,928,`order_sensitive` 104 → 98,
`exec_failed` 10(不变),`gold_star_expanded.jsonl` 3(不变)。

行只被**重新分配,从未被修改** —— `sql_sqlite` / `sql_base` / `sql_rename` 原样传递,因此
R0==R1 与 R1==R2 依然成立,没有重跑任何转译。

`artifacts/retained_dbs.json` **有意未**缩减:它描述的是四个已发布 dump 中物理存在的 69 个 schema,
这些未发生变化。被评测的子集写入新产物 `evaluated_dbs.json`(58 个数据库)。因此被剔除的 11 个
数据库仍以**未被引用的 schema** 形式留在 dump 中。这是一个有意保留的开放选择,而非疏漏 —— 对一个
靠执行查询探索数据库的 agent 而言,它们既可能是免费的干扰项,也可能是白耗的探索预算。在下一次
agent 运行之前请先决定并记录。

重新划分后已验证:58 个数据库在**两个**划分中都出现;train/test 无交集;各库最小计数恰为 60;
每一行 `clean: true`;没有任何被剔除的 qid 泄漏进配套文件。

---

## 5. 有意未改动的部分

- **四个 PostgreSQL dump。** [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
  上的 `pg_base`、`pg_rename`、`pg_decoy`、`pg_rename_decoy`([5])与问题无关:该仓库只包含 dump、
  `SHA256SUMS.txt` 和一个 README,没有任何问题文件。无需重新上传,并且清理前后的问题清单共用同一
  批未改动的 dump。
- **所有 schema 级产物:** `schema_rename_map.json`、`trap_manifest.json`、
  `trap_table_manifest.json`、`db_language_map.json`。它们都不依赖于保留了哪些问题。1,486 个
  evil-twin 列和 162 个克隆表均未改动。
- **没有重新转译、没有重新校验、没有重建数据库。** 存活的行保留原有的 train/test 归属,以及已校验的
  `sql_base` / `sql_rename`,因此步骤 04–08 和 10 都没有重跑,R0==R1 / R1==R2 保证对每一条保留的行
  依然成立。
- **历史评测结果。** [docs/methodology/evaluation.md](../methodology/evaluation.md) §8–§9 中的数字是
  在旧的 2,030 题测试集上运行的事实记录。它们被标注为已过时,而不是被改写。

---

## 6. 待定决策

1. **用执行而非文本 diff 来判定那 398 行 dev 变更。** `dev1106_gold_sql_changed` 标记依据的是规范化
   后的文本差异;这 398 行中有一部分是语义等价的改写(例如 `question_id` 0 是改成了 `WITH` 结构)。
   把新旧 gold 都在 SQLite 源库上执行并比较结果哈希 —— 也就是步骤 07 已经在做的比较 —— 可以把等价
   的那些重新归为 clean。`california_schools`(46)和 `debit_card_specializing`(47)距离任何阈值都
   足够近,单靠这项检查就可能决定它们是否存活。**请在重新划分之前完成这一步。**
2. ~~**重新划分。**~~ **已完成** —— 见 §4b。下限保持为"实际存在 ≥60 题",并用步骤 01 的机制重建了
   按库 80/20 划分。被考虑但未采用的替代方案是固定测试集规模(`test = min(25, n 的 30%)`)配合更低
   的语料下限,那样能让各 schema 的测试精度趋于一致,并且仅 `works_cycles` 一个库就能把约 50 个
   测试行归还给语料。如果日后以"逐 schema 估计"为主要分析单位,值得重新考虑,因为目前各 schema
   的测试集规模仍在 12–78 之间浮动。
3. **把每个数据库的语料规模作为协变量报告。** 清理对各库的削减极不均匀(`financial` −64%,
   `retail_world` −12%),而在下限剔除掉受损最重的那些之后,保留的 58 个库仍横跨 60–383 题 ——
   6 倍的差距。若不公布语料规模,就无法把跨库准确率差异与语料规模差异区分开。
4. **测量跨划分的近重复泄漏。** 划分是按库随机、seed 42,而 BIRD 在同一个库内包含大量模板化的近似
   重复问题。语料中一条与测试题近重复的问题,会让 agent 用检索代替归纳,从而虚高的恰恰是被声称在
   测量的那项能力。在定稿新划分之前值得先量化这一点。

---

## 7. 复现

上游输入被 gitignore(`artifacts/upstream/`),与 `data/` 下的原始 BIRD 一致。重新下载:

```bash
huggingface-cli download birdsql/bird_sql_dev_20251106 --repo-type dataset \
  --local-dir artifacts/upstream --include 'data/*'
huggingface-cli download birdsql/bird23-train-filtered --repo-type dataset \
  --local-dir artifacts/upstream --include 'data/*'
```

然后:

```bash
python pipeline/build_gold_quality_flags.py
python pipeline/apply_gold_quality_filter.py --dry-run
python pipeline/apply_gold_quality_filter.py
python pipeline/resplit_after_purge.py --dry-run
python pipeline/resplit_after_purge.py
python eval_dataset/build_eval_dataset.py
```

`resplit_after_purge.py` 是幂等的:在已重新划分的目录树上再次运行不会再剔除数据库,并且会复现出
完全相同的归属 —— 因为按库的种子只取决于 `db_id`,且划分前会先把输入顺序规范化。

`apply_gold_quality_filter.py` 对已过滤的目录树再次运行不会产生进一步变化(是个 no-op),但首次
运行是破坏性的。如需恢复清理前的问题文件,请从 git 历史(§8 中标注的提交)中取回。

---

## 8. 溯源

清理于 2026-07-29 执行,依据的 `gold_quality_flags.jsonl` 由当日发布的 `bird_sql_dev_20251106` 与
`bird23-train-filtered` 推导得出。清理前的问题清单可从携带本文档的那个提交的父提交中恢复。

---

## 参考资料

**论文**

1. Niklas Wretblad, Fredrik Riseby, Rahul Biswas, Amin Ahmadi, Oskar Holmström.
   "Understanding the Effects of Noise in Text-to-SQL: An Examination of the BIRD-Bench
   Benchmark." *Proceedings of the 62nd Annual Meeting of the ACL (Volume 2: Short Papers)*,
   pp. 356–369, 2024. ACL ID `2024.acl-short.34`,DOI
   [10.18653/v1/2024.acl-short.34](https://doi.org/10.18653/v1/2024.acl-short.34) ·
   [arXiv:2402.12243](https://arxiv.org/abs/2402.12243) ·
   [代码](https://github.com/niklaswretblad/the-effects-of-noise-in-text-to-SQL)
2. Tengjun Jin, Yoojin Choi, Yuxuan Zhu, Daniel Kang. "Pervasive Annotation Errors Break
   Text-to-SQL Benchmarks and Leaderboards." *PVLDB* 2026.
   [arXiv:2601.08778](https://arxiv.org/abs/2601.08778) · DOI
   [10.14778/3796195.3796206](https://doi.org/10.14778/3796195.3796206) ·
   [代码与修正后的 Mini-Dev 数据](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks)
3. Jin 等(同一团队)。"Text-to-SQL Benchmarks are Broken: An In-Depth Analysis of Annotation
   Errors." *CIDR* 2026。[论文](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf) ·
   [条目](https://www.vldb.org/cidrdb/2026/text-to-sql-benchmarks-are-broken-an-in-depth-analysis-of-annotation-errors.html)。
   与 [2] 为姊妹论文;因标题不同且只完整读过 [2],故单独列出。
4. Yuxuan Zhu, Tengjun Jin, Yoojin Choi, Daniel Kang. "ReViSQL: Achieving Human-Level
   Text-to-SQL." 2026 年 3 月。[arXiv:2603.20004](https://arxiv.org/abs/2603.20004) ·
   [BIRD-Platinum / BIRD-Verified 数据](https://github.com/uiuc-kang-lab/ReViSQL)。
   61.1% 的 train 错误率,以及"在修正数据上训练带来 +8.2–13.9 个点提升"的来源。该文亦转述了
   Pourreza & Rafiei(2023)更早的 18.2% train 子集数字,本文为二手引用。

**数据集**

5. `minhaozhang/BIRD_Obfuscation` —— 本项目已发布的 PostgreSQL dump。
   [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)。CC-BY-SA-4.0。
6. `birdsql/bird_sql_dev_20251106` —— 官方修正后的 BIRD dev 划分,2025-11-06 发布。
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106)。CC-BY-SA-4.0。
7. `birdsql/bird23-train-filtered` —— 官方过滤后的 BIRD 2023 train 划分,6,601 行。
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird23-train-filtered)。CC-BY-SA-4.0。
8. `birdsql/bird_mini_dev` —— 500 题 dev 子集,含 SQLite / MySQL / PostgreSQL 三种方言。
   [Hugging Face](https://huggingface.co/datasets/birdsql/bird_mini_dev)。CC-BY-SA-4.0。
   已检查,未用于过滤。
9. `uiuc-kang-lab/ReViSQL` —— BIRD-Platinum(原名 BIRD-Verified),2,462 条专家修正的 BIRD train
   实例。[GitHub](https://github.com/uiuc-kang-lab/ReViSQL)。上游未声明许可。本次未使用(见 §6.1),
   但它是目前唯一存在的修正后 train gold。

**上游项目**

10. BIRD-SQL 基准。[bird-bench.github.io](https://bird-bench.github.io/) —— 载有 2025 年 5 月对 dev
    集问题的承认,以及 2025 年 11 月 `bird-sql-dev-1106` 的发布公告。
