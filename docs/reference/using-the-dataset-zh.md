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
则在本仓库中进行版本管理。

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
分别位于端口 5432/5433/5434/5435(后两个在 `decoy` profile 下)。将它们启动起来,
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
> 中间把其余实例停掉。参见 [AGENTS.md](../../AGENTS-zh.md) 中的警告。在配置充足的服务器上
> 则不受此限制。

### 恢复到任意 PostgreSQL 服务器

```bash
createdb bird_base
pg_restore -d bird_base --no-owner --no-privileges -j 4 pg_base.dump
# repeat into a separate database per instance you need
```

---

## 3. 使用:运行本地评测

实例恢复好、本仓库也检出后(`eval_dataset/` 随之就位),运行五组消融实验:

```bash
uv run python pipeline/eval_ablation.py --arms base   --model <model>   # one arm at a time (local OOM safety)
uv run python pipeline/eval_ablation.py --arms decoy  --model <model>
uv run python pipeline/eval_ablation.py --summarize                     # EX / deltas / McNemar / CIs
```

评测会优先从 `artifacts/`(若存在)解析标准答案/映射输入,否则回退到
`eval_dataset/`,因此全新克隆的仓库(没有 `artifacts/`)不用额外步骤,就能基于受跟踪的快照运行。实验组 → (实例、标准答案字段、问题) 的映射以及各文件的详细说明见
[eval_dataset/README.md](../../eval_dataset/README-zh.md)。

每组实验只查询一个实例(`base`/`paraphrase` → `pg_base`,`rename` →
`pg_rename`,`decoy` → `pg_decoy`,`all` → `pg_rename_decoy`),因此只运行实例已启动的实验组。把 `.env.example` 复制为 `.env`,并设置 `OPENAI_API_KEY` 供模型调用。

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

诱饵/陷阱的设计:[corrupted-decoys-design.md](corrupted-decoys-design-zh.md)。
