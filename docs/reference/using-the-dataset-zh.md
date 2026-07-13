[English](using-the-dataset.md) · **中文**

# 获取与使用混淆数据集

本基准由**两部分**组成,两者缺一不可:

1. **数据库**。托管在 Hugging Face 上的四个 PostgreSQL 转储文件:
   [`minhaozhang/BIRD_Obfuscation`](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
   (仓库类型:**dataset**)。每个转储文件对应一个实例 = 该实验组的全部 69 个混淆后的
   BIRD 数据库。它们保存着模型要查询的 schema 与数据。
2. **标准答案 + 映射**。问题、标准 SQL、重命名映射以及陷阱清单:本仓库中受 git 跟踪的
   [`eval_dataset/`](../../eval_dataset/) 目录(见 [eval_dataset/README.md](../../eval_dataset/README-zh.md))。里面是标准答案与混淆的 ground truth。

数据库不纳入 git(≈12 GB),托管在 Hugging Face 上;体积较小的标准答案/映射文件
则纳入本仓库版本管理。

---

## 1. 从 Hugging Face 下载数据库转储文件

应包含以下文件(经 zstd 压缩的自定义格式 `pg_dump`,总计 ≈12 GB):

| 文件 | 实例 | 端口 | 标识符 | 诱饵/陷阱 |
| --- | --- | --- | --- | --- |
| `pg_base.dump` | base | 5432 | 原始英文 | 无 |
| `pg_rename.dump` | rename | 5433 | 已重命名(目标语言) | 无 |
| `pg_decoy.dump` | decoy | 5434 | 原始英文 | 损坏的陷阱 |
| `pg_rename_decoy.dump` | rename+decoy | 5435 | 已重命名 | 损坏的陷阱 |

此外还有 `SHA256SUMS.txt`(校验和)和 `README.md`。请在数据集页面核对确切文件名,以防有变动。

> 该数据集可能是**私有**的。请先完成身份认证:运行 `hf auth login` 并粘贴
> [Hugging Face access token](https://huggingface.co/settings/tokens),或在环境中导出
> `HF_TOKEN=hf_...`。

### 方案 A:Hugging Face CLI(推荐)

```bash
pip install -U "huggingface_hub[cli]"
hf auth login                       # only if the repo is private
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
```

较旧版本的 `huggingface_hub` 使用旧版命令:

```bash
huggingface-cli download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
```

### 方案 B:Python

```python
from huggingface_hub import snapshot_download
snapshot_download(
    "minhaozhang/BIRD_Obfuscation", repo_type="dataset",
    local_dir="bird_obf_dumps",        # token="hf_..." if the repo is private
)
```

### 方案 C:git + LFS(大文件通过 LFS 存储在 Hub 上)

```bash
git lfs install
git clone https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation bird_obf_dumps
```

### 校验完整性

```bash
cd bird_obf_dumps
sha256sum -c SHA256SUMS.txt          # Linux/macOS
# PowerShell: Get-FileHash pg_base.dump -Algorithm SHA256   (compare to SHA256SUMS.txt)
```

---

## 2. 恢复到 PostgreSQL

这些是**逻辑**层面的自定义格式转储。请使用 `pg_restore` 恢复到
**PostgreSQL ≥ 18**。`--no-owner` 会去除对原始 `bird` 角色的依赖(可用任意超级用户身份
恢复);`-j 4` 表示并行恢复。每个转储都会恢复整个 `bird` 数据库(69 个 schema +
`public`);这些转储不包含索引/主键/外键(有意在加载时不带),因此恢复很快。

### 恢复到本仓库的 Docker 实例(用于本地评测)

[`docker-compose.yml`](../../docker-compose.yml) 定义了四个空的 PostgreSQL 18 实例,
分别位于端口 5432/5433/5434/5435(后两个在 `decoy` profile 下)。启动这些实例,
并按**服务名**把每个转储恢复到对应的实例:

```bash
docker compose --profile decoy up -d          # 4 empty instances (see OOM note below)

docker compose cp   pg_base.dump          pg_base:/tmp/pg_base.dump
docker compose exec pg_base          pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
docker compose cp   pg_rename.dump        pg_rename:/tmp/pg_rename.dump
docker compose exec pg_rename        pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_rename.dump
docker compose cp   pg_decoy.dump         pg_decoy:/tmp/pg_decoy.dump
docker compose exec pg_decoy         pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_decoy.dump
docker compose cp   pg_rename_decoy.dump  pg_rename_decoy:/tmp/pg_rename_decoy.dump
docker compose exec pg_rename_decoy  pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_rename_decoy.dump
```

> **OOM 提示(仅限本地):** 在笔记本/台式机上,**不要**让四个实例同时满负荷运行。每次只启动并恢复两个(`pg_base`+`pg_decoy`,然后 `pg_rename`+`pg_rename_decoy`),
> 中间把其余实例停掉。参见 [AGENTS.md](../../AGENTS.md) 中的警告。在配置充足的服务器上
> 则不受此限制。

### 恢复到任意 PostgreSQL 服务器

```bash
createdb bird_base
pg_restore -d bird_base --no-owner --no-privileges -j 4 pg_base.dump
# repeat into a separate database per instance you need
```

---

## 3. 运行离线评测

离线分机评测是默认路径。PostgreSQL 机器冻结 prompt 并稍后打分;API 机器只收到公开请求包。

```bash
# 1. PostgreSQL 机器:一次一臂(本地 OOM 安全)
uv run python pipeline/eval_ablation.py --arms base --prepare-only

# 2. API 机器:仅 requests.jsonl + manifest.json
uv run python pipeline/run_offline_generations.py \
  --bundle-dir eval/offline/ablation-base --model <model>

# 3. PostgreSQL 机器:拷回 generations.jsonl,打分,汇总
uv run python pipeline/eval_ablation.py --arms base \
  --generations eval/offline/ablation-base/generations.jsonl --model <model>
```

评测会优先从 `artifacts/`(若存在)解析 gold/映射输入,否则回退到 `eval_dataset/`,
因此全新克隆(没有 `artifacts/`)无需额外步骤即可针对受跟踪快照运行。臂 → (实例、gold 字段、问题) 映射及各文件说明见 [eval_dataset/README.md](../../eval_dataset/README-zh.md)。

每臂只查询一个实例(`base`/`paraphrase` → `pg_base`,`rename` → `pg_rename`,`decoy` → `pg_decoy`,`all` → `pg_rename_decoy`),因此只运行实例已启动的臂。只有 API 机器需要 `OPENAI_API_KEY`。

等价的显式准备命令:

```bash
uv run python pipeline/prepare_offline_eval.py --eval contamination
uv run python pipeline/prepare_offline_eval.py --eval ablation --arms rename
uv run python pipeline/prepare_offline_eval.py --eval contamination --split train
uv run python pipeline/prepare_offline_eval.py --eval ablation --split train --arms base,rename
```

导出器从所选在线实例读取精简 DDL,写入 `eval/offline/<eval>/`:

- `requests.jsonl` 和 `manifest.json`: 拷到 API 机器。
- `grading_manifest.private.jsonl`: 留在 PostgreSQL 机器(含 gold SQL 和本地库路由)。
- `README.txt`: 生成结果 JSONL 约定和交接说明。

每个公开请求包含完整的 system 指令和渲染后的 prompt,以及确定性的 `request_sha256`。按文件顺序处理请求,以保留现有的按库 prompt-cache 局部性。API 运行器返回 `request_id`、`request_sha256`、`generated_sql`;打分器会拒绝不匹配或被改动的请求。用 `--conditions`、`--arms`、`--split`、`--limit` 导出子集;已有包除非加 `--overwrite` 否则不覆盖。

训练污染评测和 `base`/`rename`/`decoy` 消融臂立即可用。训练 `paraphrase`/`all` 需先:

```bash
uv run python pipeline/09_paraphrase_questions.py --include-train
```

旧的同机行为仍可用,显式加 `--local`。

### 可移植公开包(API 机器无需数据库)

仓库还提供 `eval/offline-public-bundles.zip`(约 11 MiB),内含全部测试/训练公开包(`requests.jsonl` + `manifest.json` + `README.txt`),**不含**私有打分清单。

```bash
# API 机器(git clone 后)
Expand-Archive eval/offline-public-bundles.zip -DestinationPath eval
uv run python pipeline/run_offline_generations.py \
  --bundle-dir eval/offline/contamination --model <model>
```

把各 `generations.jsonl` 拷回 PostgreSQL 机器打分。

---

## 各文件对照(汇总)

| 你拥有的 | 它是什么 | 位置 |
| --- | --- | --- |
| `pg_base.dump` | 对照组:原始英文标识符,无诱饵 | Hugging Face |
| `pg_rename.dump` | 维度 1:已重命名标识符 | Hugging Face |
| `pg_decoy.dump` | 维度 2:损坏的诱饵列 + 表 | Hugging Face |
| `pg_rename_decoy.dump` | 维度 1+2 组合 | Hugging Face |
| `train_final.jsonl` / `test_final.jsonl` | 标准问题 + SQL(`sql_base`、`sql_rename`) | `eval_dataset/`(git) |
| `schema_rename_map.json`、`db_language_map.json` | 重命名映射 | `eval_dataset/`(git) |
| `trap_manifest.json`、`trap_table_manifest.json` | 诱饵/陷阱 ground truth | `eval_dataset/`(git) |
| `question_paraphrases.jsonl` | 维度 3:改写后的问题 | `eval_dataset/`(git) |
| `gold_result_hashes_rename_decoy.jsonl` | `pg_rename_decoy` 上的 gold 结果哈希 | `eval_dataset/`（git）；算法见 [gold-result-hashes-zh.md](gold-result-hashes-zh.md) |

诱饵/陷阱的设计:[corrupted-decoys-design.md](corrupted-decoys-design-zh.md)。
