**English** · [中文](PROGRESS-zh.md)

# Progress Log

Project history and status. `AGENTS.md` stays instructional (how to run/extend the pipeline); this file records **what was done, when, and why**: the narrative `AGENTS.md` deliberately omits. Methodology detail lives under [docs/methodology/](docs/methodology/); this file points at it rather than duplicating it.

Dates are absolute.

---

## Status snapshot: 2026-07-05

- **Core pipeline (steps 0-7): complete and validated.** 10,164 / 10,541 candidate questions pass end-to-end validation (8,134 train / 2,030 test; all 69 databases represented in both). See [docs/methodology/dataset.md §7](docs/methodology/dataset.md).
- **Extended obfuscation (steps 08-10): built and applied.** Question paraphrases (step 09) and the original decoy-schema injection (step 08) are done; the decoy dimension was then reworked into **corrupted decoy traps** (step 10, `10_inject_traps.py`), additive "evil-twin" columns (1,486) + cloned tables (162) holding subtly *corrupted* copies of real data, after recognising that empty decoys unmask themselves under an interactive execute-and-observe agent. Injected into both decoy instances (`pg_decoy`, `pg_rename_decoy`), both variants; real data proven byte-identical, so R1==R2 still holds. See [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design.md).
- **Four PostgreSQL instances** (`pg_base` / `pg_rename` / `pg_decoy` / `pg_rename_decoy`) built and **published** as compressed `pg_dump`s on [Hugging Face](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation). Gold SQL + mappings + trap manifests are git-tracked in [`eval_dataset/`](eval_dataset/); download/restore/run instructions in [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset.md).
- **Obfuscation effectiveness eval (four conditions): implemented; being re-run.** Earlier numbers have been discarded. The full evaluation is being re-run on a stronger model to ensure the results are robust. Setup in [docs/methodology/evaluation.md §8](docs/methodology/evaluation.md); no results reported until that run completes.
- **Five-arm ablation (`eval_ablation.py`: base/rename/decoy/paraphrase/all): implemented; full run pending** the same stronger-model run. No results reported yet.

---

## Done

### Core pipeline (through 2026-07-02)
- Steps 0-7 implemented: split → language assignment → rename map (Bedrock) → load `pg_base` (pgloader) → transpile + R0==R1 → clone/rename `pg_rename` → rename SQL + R1==R2. Deliverable: `artifacts/{train,test}_final.jsonl`.
- Two-oracle integrity: R0==R1 (SQLite ground truth vs transpiled PG) and R1==R2 (original PG vs obfuscated PG). ~12% of validated rows use VALUES-materialization (see [docs/reference/step5-transpilation.md](docs/reference/step5-transpilation.md)).
- Four-condition obfuscation-effectiveness eval implemented via `pipeline/eval_contamination.py`; setup in [evaluation.md §8](docs/methodology/evaluation.md) (results being re-run on a stronger model, not reported until complete).

### Direction-setting (2026-07-03)
- **Literature review** (SPENCE arXiv 2604.17771; SQL2NL arXiv 2509.04657; Termite/ATD arXiv 2402.08100; ConStat; Min-K%/Time Travel surveys). Key takeaway: the **question/syntactic axis** is the sensitive contamination signal, not the identifier axis; BIRD is only weakly contaminated at the identifier axis (τ ≈ −0.35, CI spans zero), a prior-literature signal, independent of our own (pending) measurement. Framing conclusion: the dataset's durable value is as a **validated multilingual Postgres Text-to-SQL asset + robustness testbed**, with contamination as a secondary (honest, negative-ish) result.
- **Decision: extend obfuscation** with two new independently-toggleable dimensions and an ablation to measure each:
  - **decoy schema injection**: distractor tables + confusable columns (attacks schema linking).
  - **question paraphrase**: cheap-model, SQL-conditioned (attacks question-form recall).
- **`SELECT *` measurement** (subagent, 2026-07-03): only **3 / 10,164** gold queries have a top-level real-table star (all `mondial_geo`), 5 at any level; 1,169 VALUES-materialized excluded; 67/69 DBs star-free. → `SELECT *` breakage under decoy columns is negligible; resolved by star-expansion of the affected gold.
- **Docs organized before code** (this pass): wrote `obfuscation-extensions.md`, added `evaluation.md §9` (ablation design), cross-linked from `obfuscation.md`, created this log.

---

### Build progress (2026-07-03)

Implementing per [docs/reference/extension-implementation-plan.md](docs/reference/extension-implementation-plan.md):

- ✅ §2a `pipeline/_db.py`: shared PG helpers extracted (behaviour-preserving; contamination-eval numbers unchanged).
- ✅ §2b `pipeline/_eval_helpers.py`: shared eval machinery extracted; `eval_contamination.py` is now a thin contamination-eval entrypoint.
- ✅ §3b `docker-compose.yml`: `pg_decoy` (5434) + `pg_rename_decoy` (5435), profile-gated (`--profile decoy`); default bring-up unchanged.
- ✅ §3c decoy volumes cloned; ✅ step 08 run (`decoy_map.json`, structural decoys injected + R1==R2 re-validated); ✅ §6 `09_paraphrase_questions.py` run (`question_paraphrases.jsonl`); ✅ §7 `eval_ablation.py` written.

### Extended obfuscation: built + corrupted-decoy pivot (2026-07-04 → 07-05)

- **Corrupted-decoy pivot.** The eval target is an **interactive execute-and-observe SQL agent**, and empty decoy tables / NULL decoy columns unmask themselves for free (`COUNT(*)=0` etc.). So the decoy dimension was reworked from *empty* structural decoys (step 08) into **corrupted traps** (`pipeline/10_inject_traps.py`), strictly **additive** so real data stays byte-identical and R1==R2 holds. Design + risk register: [docs/reference/corrupted-decoys-design.md](docs/reference/corrupted-decoys-design.md).
  - **Phase 1: evil-twin columns** (`trap_manifest.json`, 1,486): a NEW column holding a corrupted copy of a real column under an LLM synonym; ≤500k-row tables; join-keys → permute (RI-preserving), else a mix of sparse perturb / cat-remap / date-offset / null.
  - **Phase 2: corrupted clone tables** (`trap_table_manifest.json`, 162 over 66 DBs): a whole real table cloned + renamed with a corrupted column subset; ≤50k-row sources; R1==R2-safe by construction (gold never references a decoy table).
  - Injected into `pg_decoy` + `pg_rename_decoy`, both variants; **real data proven byte-identical** to the clean instances (532 tables each side). Naming via `gpt-5.4-mini`; all `_alt`/`_archive` fallback names scrubbed. Fixed a `sparse_perturb` int-overflow (clamp to the target int type's range).
  - R1==R2 leaves **153 order-sensitive + 21 pre-existing exec-failed** qids that are excluded from strict cross-variant EX (`artifacts/order_sensitive_qids.json`); benign (heap reorder from trap UPDATEs → different-but-valid LIMIT/float-aggregate results).
- **Packaging + publication.** All four instances dumped (`pg_dump -Fc`, zstd; ~3 GB each, ~10:1) and **published on Hugging Face**: [minhaozhang/BIRD_Obfuscation](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation). Gold + mappings + manifests consolidated into the git-tracked [`eval_dataset/`](eval_dataset/); consumer guide [docs/reference/using-the-dataset.md](docs/reference/using-the-dataset.md).
- **Eval portability.** Eval scripts resolve inputs `artifacts/` → fall back to `eval_dataset/` (a fresh clone runs with no regeneration). Postgres DSNs are now env-configurable via `PG_*_DSN` (default = local docker), so the eval can target remote Postgres / AWS RDS without code changes.

## Next (planned, in order)

1. **Run the five-arm ablation** (`eval_ablation.py`) once the evaluation model is chosen. Result rows now stamp eval metadata (model, effort, prompt version, git commit, and input artifact hashes), and resume only reuses rows with matching metadata. Report paired deltas + bootstrap CIs; exclude `order_sensitive_qids.json` from strict scoring.
2. **(Optional) AWS deployment**: the config is now portable (env DSNs, dumps on Hugging Face, tracked `eval_dataset/`). Recommended shape: single EC2 running the repo's docker-compose instances restored from the HF dumps, OpenAI key from Secrets Manager, results to S3.
3. **(Downstream, separate repo)** the interactive agent harness + the decoy-consistent-answer / trap-rate metric that actually exercises the traps.

## Decisions log

- **2026-07-03**: Reintroduce question paraphrase as an *optional* dimension (was dropped in the core pipeline for drift risk; SQL-conditioned generation mitigates it, and it targets the more sensitive axis).
- **2026-07-03**: Decoy instances are separate PG containers (`pg_*_decoy`); `pg_base`/`pg_rename` stay clean baselines. Decoy tables empty by default (invisible in stripped DDL). **SUPERSEDED 2026-07-04:** empty decoys unmask themselves under an interactive execute-and-observe agent, so decoys are now *populated* with additive corrupted data (step 10 corrupted traps); see below.
- **2026-07-03**: Grade all ablation arms by exact multiset equality against `SELECT *`-expanded gold, never loose containment (containment would let a lazy `SELECT *` pass and inflate EX).
- **2026-07-03**: Consolidated naming repo-wide to `base`/`rename`/`decoy`/`rename_decoy` (DB instances, eval arms/conditions, data fields, files). Resolves the old `sql_pg`/`sql_obfuscated` asymmetry (`sql_base`/`sql_rename`); "obfuscation" stays the umbrella term. Deliverable JSONL migrated in place.
- **2026-07-03**: Never run all four PostgreSQL instances under heavy load at once on a local Docker Desktop / WSL setup. It can OOM the WSL VM and (with `fsync=off`) corrupt volumes. Bring up only what a step/arm needs; run ablation arms sequentially; eval `--concurrency` ≤ 3; capping the WSL VM's memory in `.wslconfig` is a useful backstop. (This is a *local* Docker-Desktop/WSL limit; a well-provisioned server can run all four.)
- **2026-07-04**: Corrupted-decoy pivot (see status snapshot). Decoys carry **additive** corrupted copies of real data, never modifying real rows/columns/tables, so R1==R2 is preserved. Correlated columns ARE allowed as trap sources (additive ⇒ no cross-column invariant can break); join-key/FK columns are corrupted by **permutation only** (values stay real keys ⇒ referential integrity preserved). Row caps to bound cost: ≤500k rows for evil-twin columns, ≤50k for clone-table sources. Corruption is deterministic (hash-seeded, variant-independent salt) so a rebuild reproduces it.
- **2026-07-04**: Accept + flag benign R1!=R2 rather than chase it: 153 order-sensitive (LIMIT-without-total-order / float-aggregate gold returns a different-but-valid result once trap UPDATEs reorder the heap) + 21 pre-existing exec-failed → `artifacts/order_sensitive_qids.json`, excluded from strict cross-variant EX. Real data proven intact by order-independent fingerprint.
- **2026-07-05**: Ship the deliverable in two homes: the four PostgreSQL **databases** as `pg_dump` archives on Hugging Face (too large for git), and the **gold SQL + mappings + trap manifests** git-tracked in `eval_dataset/`. Eval scripts read `artifacts/` then fall back to `eval_dataset/`, so a fresh clone runs with no regeneration.
- **2026-07-05**: Postgres DSNs are env-configurable via `PG_*_DSN` (default = the local docker-compose ports); lets the eval target remote Postgres / AWS RDS without code changes.
