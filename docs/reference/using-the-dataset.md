**English** · [中文](using-the-dataset-zh.md)

# Getting and using the obfuscation dataset

The benchmark ships in **two parts**, and you need both:

1. **The databases**. Four PostgreSQL dumps hosted on Hugging Face:
   [`minhaozhang/BIRD_Obfuscation`](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation)
   (repo type: **dataset**). Each dump is one instance = all 69 obfuscated BIRD
   databases for that arm. These hold the schemas + data the model queries.
2. **The gold + mappings**. Questions, gold SQL, the rename map, and the trap
   manifests: the git-tracked [`eval_dataset/`](../../eval_dataset/) folder in this
   repo (see [eval_dataset/README.md](../../eval_dataset/README.md)). These hold the
   gold answers and the obfuscation ground truth.

The databases live off-git (≈12 GB) on Hugging Face; the small gold/mapping files
are versioned in this repo.

---

## 1. Download the database dumps from Hugging Face

Expected files (zstd-compressed custom-format `pg_dump`, ≈12 GB total):

| file | instance | port | identifiers | decoys/traps |
| --- | --- | --- | --- | --- |
| `pg_base.dump` | base | 5432 | original English | none |
| `pg_rename.dump` | rename | 5433 | renamed (target language) | none |
| `pg_decoy.dump` | decoy | 5434 | original English | corrupted traps |
| `pg_rename_decoy.dump` | rename+decoy | 5435 | renamed | corrupted traps |

plus `SHA256SUMS.txt` (checksums) and `README.md`. Confirm the exact names on the
dataset page in case they change.

> The dataset may be **private**. Authenticate first: run `hf auth login` and paste a
> [Hugging Face access token](https://huggingface.co/settings/tokens), or export
> `HF_TOKEN=hf_...` in your environment.

### Option A: Hugging Face CLI (recommended)

```bash
pip install -U "huggingface_hub[cli]"
hf auth login                       # only if the repo is private
hf download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
```

Older `huggingface_hub` uses the legacy command:

```bash
huggingface-cli download minhaozhang/BIRD_Obfuscation --repo-type dataset --local-dir bird_obf_dumps
```

### Option B: Python

```python
from huggingface_hub import snapshot_download
snapshot_download(
    "minhaozhang/BIRD_Obfuscation", repo_type="dataset",
    local_dir="bird_obf_dumps",        # token="hf_..." if the repo is private
)
```

### Option C: git + LFS (large files are stored via LFS on the Hub)

```bash
git lfs install
git clone https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation bird_obf_dumps
```

### Verify integrity

```bash
cd bird_obf_dumps
sha256sum -c SHA256SUMS.txt          # Linux/macOS
# PowerShell: Get-FileHash pg_base.dump -Algorithm SHA256   (compare to SHA256SUMS.txt)
```

---

## 2. Restore into PostgreSQL

These are **logical** custom-format dumps. Restore with `pg_restore` into
**PostgreSQL ≥ 18**. `--no-owner` drops the dependency on the original `bird` role
(restore as any superuser); `-j 4` restores in parallel. Each dump restores the whole
`bird` database (69 schemas + `public`); the dumps carry no indexes/PKs/FKs (loaded
without them by design), so restore is fast.

### Into this repo's Docker instances (for the local eval)

[`docker-compose.yml`](../../docker-compose.yml) defines four empty PostgreSQL 18
instances on ports 5432/5433/5434/5435 (the last two behind the `decoy` profile).
Bring them up and restore each dump into its matching instance by **service name**:

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

> **OOM note (local only):** on a laptop/desktop, do **not** run all four instances
> under load at once. Bring up and restore two at a time (`pg_base`+`pg_decoy`, then
> `pg_rename`+`pg_rename_decoy`), stopping the others in between. See the warning in
> [AGENTS.md](../../AGENTS.md). On a well-provisioned server this limit does not apply.

### Into any PostgreSQL server

```bash
createdb bird_base
pg_restore -d bird_base --no-owner --no-privileges -j 4 pg_base.dump
# repeat into a separate database per instance you need
```

---

## 3. Run the offline eval

Offline split-machine evaluation is the default. The PostgreSQL machine prepares
the frozen prompts and later grades returned SQL; the API machine only receives
the public prompt bundle.

```bash
# 1. PostgreSQL machine: prepare one arm at a time (local OOM safety)
uv run python pipeline/eval_ablation.py --arms base --prepare-only

# 2. API machine: requests.jsonl + manifest.json only
uv run python pipeline/run_offline_generations.py \
  --bundle-dir eval/offline/ablation-base --model <model>

# 3. PostgreSQL machine: copy generations.jsonl back, grade, summarize
uv run python pipeline/eval_ablation.py --arms base \
  --generations eval/offline/ablation-base/generations.jsonl --model <model>
```

The eval resolves its gold/mapping inputs from `artifacts/` if present, else falls
back to `eval_dataset/`, so a fresh clone (no `artifacts/`) runs against the tracked
snapshot with no extra steps. The arm → (instance, gold field, question) mapping and
per-file details are in [eval_dataset/README.md](../../eval_dataset/README.md).

Each arm queries exactly one instance (`base`/`paraphrase` → `pg_base`, `rename` →
`pg_rename`, `decoy` → `pg_decoy`, `all` → `pg_rename_decoy`), so run arms whose
instances are up. Only the API machine needs `OPENAI_API_KEY`.

The equivalent explicit preparation commands are:

```bash
uv run python pipeline/prepare_offline_eval.py --eval contamination
uv run python pipeline/prepare_offline_eval.py --eval ablation --arms rename
uv run python pipeline/prepare_offline_eval.py --eval contamination --split train
uv run python pipeline/prepare_offline_eval.py --eval ablation --split train --arms base,rename
```

The exporter reads stripped DDL from the selected live instance and writes under
`eval/offline/<eval>/`:

- `requests.jsonl` and `manifest.json`: copy these to the API machine.
- `grading_manifest.private.jsonl`: keep this on the PostgreSQL machine because it
  contains gold SQL and local database routing.
- `README.txt`: the generation-result JSONL contract and handoff instructions.

Each public request contains the exact system instructions and rendered prompt,
plus a deterministic `request_sha256`. Process requests in file order to retain
the existing per-database prompt-cache locality. The API runner returns
`request_id`, `request_sha256`, and `generated_sql`; the grader rejects mismatched
or modified requests. Use `--conditions`, `--arms`, `--split`, and `--limit` to
export subsets; an existing bundle is not replaced unless `--overwrite` is
supplied.

Train contamination and the `base`/`rename`/`decoy` ablation arms are immediately
available. Train `paraphrase`/`all` first require:

```bash
uv run python pipeline/09_paraphrase_questions.py --include-train
```

The former same-machine behavior remains available explicitly with `--local`.

---

## Which file is which (summary)

| you have | it is | where |
| --- | --- | --- |
| `pg_base.dump` | control: original English identifiers, no decoys | Hugging Face |
| `pg_rename.dump` | dim 1: renamed identifiers | Hugging Face |
| `pg_decoy.dump` | dim 2: corrupted decoy columns + tables | Hugging Face |
| `pg_rename_decoy.dump` | dims 1+2 combined | Hugging Face |
| `train_final.jsonl` / `test_final.jsonl` | gold questions + SQL (`sql_base`, `sql_rename`) | `eval_dataset/` (git) |
| `schema_rename_map.json`, `db_language_map.json` | rename mappings | `eval_dataset/` (git) |
| `trap_manifest.json`, `trap_table_manifest.json` | decoy/trap ground truth | `eval_dataset/` (git) |
| `question_paraphrases.jsonl` | dim 3: paraphrased questions | `eval_dataset/` (git) |

Design of the decoys/traps: [corrupted-decoys-design.md](corrupted-decoys-design.md).
