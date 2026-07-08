# Corrupted-decoy design — risk register & decisions

**Status:** IMPLEMENTED (both phases injected into `pg_decoy`/`pg_rename_decoy`).
Captures the design, the known pitfalls, and (§5) the as-built parameters. The
risk register (§2) and decisions (§3) are retained as the rationale for §5.

**Supersedes (conditionally):** the `Populate decoy tables with rows? → No` default in
[extension-implementation-plan.md §0](extension-implementation-plan.md). That default is
correct for a **single-shot** text-to-SQL eval (the model never probes, so empty decoys are
invisible and free). It is a **flaw** for an **interactive / execute-and-observe SQL agent**:
`COUNT(*)=0` on a decoy table or a 100%-NULL decoy column unmasks the decoy instantly, so it
contributes zero distractive pressure. This doc assumes the interactive paradigm is the target
(confirmed 2026-07-04). **Scope:** this repo's *only* job is to produce the Postgres DB
(dataset) + a **trap manifest** suitable for that task; the interactive eval harness lives in a
**separate repo**.

---

## 1. Concept — "copy → rename → corrupt"

Instead of empty tables / NULL columns, build each decoy from a **real** column (or table):

1. **Copy** a real column `C` → gives realistic type, null-fraction, cardinality, value
   distribution *for free* (far easier + more convincing than synthesising from scratch).
2. **Rename** the copy to a plausible *synonym* (`price → list_price/unit_cost`, not `price_copy`).
3. **Corrupt** the copy so it is subtly wrong — turning the decoy from an inert *distractor*
   into an actual *trap* (an agent that picks it gets a wrong answer).

**Corruption operators**, on a stealth ↔ reliably-wrong spectrum:

| operator | single-column profiling detects it? | traps on |
| --- | --- | --- |
| **Permutation** (reassign existing values to other rows) | No — min/max/mean/histogram/distinct all identical | row-specific lookups/joins only (invisible to aggregates) |
| **Sparse perturb** (corrupt p≈10–30% of rows) | Barely — aggregates move slightly | the p% of rows touched |
| **Global perturb** (`C±ε`, `C·(1±ε)`, all rows) | Yes, if the agent has a range expectation | almost always |
| in-domain category remap / date offset / rounding / unit-shift | varies | varies |

Applies at two granularities: **evil-twin column** (added to a real table) and **corrupted
clone table** (clone a real table, rename it + columns, corrupt a subset).

---

## 2. Risk register

### A. Validation-invariant risks (must not break R1==R2 or the gold)
- **A1 — `SELECT *` leakage.** Corrupted/populated decoy columns are safe *only* because gold
  never references them and the one leak path (`SELECT *`) is stripped by
  `gold_star_expanded`. If that snapshot is stale or incomplete (a `SELECT *` gold not
  captured), a corrupted column leaks into the expanded gold → R1≠R2 → wrong gold.
  *Mitigation:* assert every `SELECT *` gold has an expansion; re-run the full gate.
- **A2 — comparator dependency.** Re-validation relies on the fixed `normalise_result`
  (Finding A, currently **uncommitted**). Commit it first, or spurious order-sensitive
  mismatches will mask (or masquerade as) real ones.
- **A3 — re-inject ordering.** `gold_star_expanded` reads real columns *before* injection.
  Any re-generation of decoys must re-snapshot **before** re-injecting, or it captures decoy
  columns as if they were real.

### B. Detectability giveaways (defeat the decoy's purpose)
- **B1 — domain plausibility.** Out-of-domain corrupted values (negative `age`, future
  `birth_date`, out-of-range codes) are spotted by profiling. Clamp/constrain per column.
- **B2 — aggregate giveaways.** Global perturbation shifts min/max/mean/sum. Prefer
  sparse/permutation for stealth.
- **B3 — key / uniqueness.** Noise on a unique/ID column creates collisions or dangling
  references → looks fake. Use **permutation** for key-like columns (preserves uniqueness,
  every value stays real; also a great *join* trap).
- **B4 — format / pattern.** Corrupted strings that break a column's implicit format
  (emails, phone numbers, postal/ISO codes) are obvious. Corruption must be format-aware.
- **B5 — cross-column / cross-row invariants (the big one).** Real columns are often
  correlated: `total = qty × price`, `start_date ≤ end_date`, `city/state/zip` consistency,
  derived/denormalised columns. Corrupting **one** column of a correlated set breaks the
  invariant → a consistency-checking agent detects the fake. Isolated per-column corruption
  is unsafe for correlated columns. *Mitigation:* detect and skip correlated columns, or
  corrupt the whole consistent set together.
- **B6 — calibration.** Too weak → `C'≈C` (a mere duplicate: suspicious, and no trap). Too
  strong → obviously fake. The corruption rate needs tuning.
- **B7 — null-fraction anomalies.** Adding/removing NULLs can shift the null fraction to an
  implausible level.
- **B8 — isolated decoy tables.** Unreferenced clone tables can look suspicious in schema
  exploration. Partly hidden by the no-FK-anywhere design, but islands can still stand out;
  consider populating a plausible key column with real IDs from a related table.
- **B9 — foreign-key / join-key integrity (cross-table).** *Real* gold joins are **not** at
  risk — traps are additive and never touch real keys (R1==R2 confirmed every join is
  preserved on the decoy instances). The risk is the **trap's own** keys: naive value-noise on
  a join-key/FK-like column produces **dangling references** (keys with no matching parent
  row), which a referential-integrity check (`fk NOT IN (SELECT pk)` → orphans) uses to unmask
  the decoy — and trapping child/parent tables inconsistently (one ≤N, the other skipped as a
  giant) makes the orphans more obvious. This DB has **no declared FKs** (loaded with
  `no foreign keys`), so join keys must be *inferred* (name patterns `*_id`, type + value-subset
  matching) — imperfect, so we may miss one and corrupt it. *Mitigation:* treat inferred
  join-key/FK columns like correlated columns (decision 4) — skip them, or corrupt **only via
  permutation** (every value stays a real, existing key ⇒ RI preserved, still a stealthy join
  trap; see B3). Clone-table key columns must likewise keep valid parent keys.

### C. Trap-effectiveness risks
- **C1 — stealth vs. reliably-wrong tension.** Sparse/permutation corruption may **not**
  overlap the rows a given question touches → the trap silently doesn't fire and the decoy
  has no effect for that question. Must choose a philosophy: *probabilistic + measure a trap
  rate*, vs. *dense/targeted (reliable but couples decoy to specific questions)*.
- **C2 — operator ↔ query mismatch.** Permutation is invisible to aggregates (`SUM` over a
  permuted column is unchanged); it only traps row-specific ops. Match the operator to the
  query types you intend to trap.
- **C3 — signal size.** Too sparse → EX barely moves and arms can't be distinguished; too
  dense → unrealistic. Calibrate to a measurable-but-plausible effect.

### D. Reproducibility / determinism
- **D1 — stable row identity.** Deterministic hash-based corruption needs a stable primary
  key. PK-less tables → `ctid` is **not** stable across reloads/rebuilds → non-reproducible.
  Fall back to a deterministic `row_number()` over a fixed *total* order (ties make it
  ambiguous; some tables lack a natural stable order).
- **D2 — seed on semantics, not names.** Seed corruption on `(db, source_column, operator)`,
  **not** on the decoy's per-variant (renamed) name — otherwise `pg_decoy` (English) and
  `pg_rename_decoy` (target-lang) diverge in ways unrelated to renaming and **confound the
  ablation**.
- **D3 — no PG `random()`.** It is non-reproducible, and re-cloning resets the decoy volumes.
  Use hash-based pseudo-randomness (e.g. `('x'||md5(pk::text))::bit(32)::int`) so a rebuild
  reproduces the identical dataset.

### E. Cost / operational
- **E1 — storage.** Populating reverses the current "free" property. Evil-twin columns on
  multi-million-row tables (e.g. `ratings`) cost a full column each; clone tables cost full
  table data. This can add many GB. Budget + measure.
- **E2 — write cost & bloat.** The `UPDATE` filling a column on a huge table is heavy WAL,
  and with `autovacuum=off` the dead tuples are never reclaimed → the volume grows *more*
  than the data itself. May need a `VACUUM FULL` (rewrites the table, needs free space) or an
  accepted bloat budget.
- **E3 — idempotency / re-runs.** `inject_variant` currently *skips* existing tables/columns,
  so re-running will **not** update corruption. Need a `--regenerate`/force path that drops
  and recreates decoy objects.
- **E4 — full re-validation.** Every decoy-data change requires re-running the R1==R2 gate
  (~45 min per variant, OOM-safe one variant at a time).

### F. Sequencing / scope
- **F1 — paradigm before build.** Corrupted decoys only matter under an interactive
  execute→observe→refine harness, which **does not exist yet** (`eval_ablation.py` is
  single-shot). Building corrupted decoys before the interactive harness may be premature —
  confirm the harness is actually coming.
- **F2 — trap-rate metric needs a new artifact.** Measuring whether an agent got trapped
  requires the *decoy-consistent answer* (gold computed using `C'` instead of `C`). Define
  and compute it if we want a trap-rate metric.
- **F3 — contamination orthogonality.** The model may already know the real BIRD values from
  pretraining; corrupting a copy doesn't address memorization. Orthogonal, but note it.

### G. Multilingual / variant
- **G1 — data language.** Decoy column *values* are copied from real columns whose **data**
  is in the DB's language (non-English DBs). Renames touch identifiers only, not data, so
  string corruption (typos, category remap) must respect the data's language/charset.

---

## 3. Decisions (settled 2026-07-04)
1. **Paradigm — interactive SQL agent: YES**, but this repo only produces the Postgres DB +
   trap manifest; the eval harness is a **separate repo** (see Scope above). ⇒ **F1** resolved
   (no harness work here); **F2** computed downstream, but this repo **must emit the manifest**.
2. **Trapping philosophy — DO BOTH: a mixture of trap types.** Support and mix *stealthy*
   (permutation / sparse-perturb) *and* *reliable* (denser / targeted) operators across
   columns — not a single style (C1–C3).
3. **Scope — BOTH** evil-twin columns *and* corrupted clone tables (a mixture) (E1, B8).
4. **Correlated columns — ALLOWED as trap sources (REVERSED 2026-07-04).** The original "skip"
   came from B5 (corrupting a column in a `total=qty*price` set breaks the invariant). But the
   final design is strictly **additive** — we never modify the real column, only add a corrupted
   *copy* under a new name — so real cross-column invariants are always preserved and **B5 can't
   fire**. Correlated columns (prices, quantities, dates) are the highest-value trap targets, so
   they ARE eligible sources. **Join-key/FK columns still permute-only (B9 stands).**
5. **Corruption rate — 10% of rows per trap column** (sparse) (B6, C3).
   ⚠️ *Confirm interpretation:* "10 percent" = 10% of **rows** corrupted per trap column
   (not 10% of columns turned into traps). Proceeding on the rows reading.
6. **Storage — skip large tables.** Inject traps only into tables with **≤ N rows**; skip
   anything bigger to bound the UPDATE/storage/bloat cost (E1–E2).
   Survey (2026-07-04; 569 real tables, ~362M rows total): 494 tables (87%) are ≤100k rows yet
   hold just **1.2%** of all rows; 532 (93.5%) are ≤500k; the 37 tables >500k hold **~99%** of
   the data (`language_corpus.pages_mots` 129M, `bike_share_1.status` 72M, `talkingdata…` 32M,
   `movie_platform.ratings` 15.5M, …). **N = 500,000 (locked 2026-07-04)** →
   532/569 (93.5%) coverage, skips only the 37 giants. Clone-table sources (which copy all
   columns) likely need a **lower** cap or a count limit.

## 4. Hard invariants & prerequisites
- **The original is never mutated — decoys are strictly ADDITIVE.** Traps are *added* copies
  (new columns / new tables); every real column/table stays byte-identical to the clean
  instance. This is what keeps the correct answer reachable in the decoy DB **and** preserves
  R1==R2. *(Design requirement: after all these traps, the original must still be available.)*
- **Emit a trap manifest** (per trap: `db`, real source table/column, decoy name, operator,
  params, seed). It is the ground truth the separate eval repo needs to (a) know which is the
  trap vs. the original and (b) compute the decoy-consistent answer for a trap-rate metric (F2).
- Commit the `normalise_result` fix (A2) before re-validating.
- Never mutate `pg_base`/`pg_rename` or `*_final.jsonl` (the extension-plan golden rule).
- Re-snapshot `gold_star_expanded` before any re-inject (A3).

## 5. Implementation (as built)

Driver: [`pipeline/10_inject_traps.py`](../../pipeline/10_inject_traps.py); operators
[`pipeline/_corruption.py`](../../pipeline/_corruption.py); planners/appliers
[`pipeline/_traps.py`](../../pipeline/_traps.py). Two additive trap granularities:

**Phase 1 — evil-twin columns** (`--phase rowcounts,plan,name,inject`). A NEW column
on a real table holding a corrupted copy of a real *source* column under an LLM
synonym. Table cap **≤ 500 000 rows** (decision 6); up to 3 traps/table, preferring
query-used columns (join-keys + correlated). Manifest: `artifacts/trap_manifest.json`
(1486 traps).

**Phase 2 — corrupted clone tables** (`--phase plan-tables,name-tables,inject-tables`).
A whole real table cloned (`CREATE TABLE … AS SELECT … FROM real`) under an LLM
synonym table+column names, then a subset of columns corrupted. Table cap **≤ 50 000
rows** (`CLONE_ROW_CAP`); per DB clone **30% of eligible tables, 2–8** (`CLONE_FRAC`,
`CLONE_MIN/MAX`); within a clone, corrupt every join-key + ~half the remaining columns,
leaving the rest exact for a realistic mix. Manifest: `artifacts/trap_table_manifest.json`
(162 clone tables / 66 DBs / 1228 cols, 724 corrupted). **R1==R2-safe by construction**
— gold never references a decoy table, so clone corruption cannot move gold results.

**Operators** (`choose_operator`, deterministic mix per decision 2): join-keys →
`permute` only (B9/B3); numeric → `permute`/`sparse_perturb`; text → `permute`/
`sparse_cat_remap`; temporal → `sparse_date_offset`; else `sparse_null`. All corruption
is a pure hash function of a per-row key + a **variant-independent** salt (D2/D3), keyed
on a single-col PK/unique when present, else `ctid` (clone tables always use `ctid`).
`sparse_perturb` on integers computes the add in `numeric` and **clamps to the target
int type's range** so a large `bigint` (or an `int4`/`int2` on assignment) cannot
overflow. `pct=0.10` (decision 5), `rel=0.15`.

**Verification.** Real data proven byte-identical to the clean instances on both decoy
volumes (order-independent per-column fingerprint over all 532 trapped tables). Clone
tables: row counts match source, exact-copy columns identical, permute columns
multiset-identical yet 100% row-moved, sparse columns changed. Phase-1 R1==R2 leaves
153 benign order-sensitive + 21 pre-existing exec-failed qids (`--regenerate` re-runs;
flagged in `artifacts/order_sensitive_qids.json`).

## 6. Naming, known limitations, and manifest schema

**Naming** (`--phase name` / `name-tables`, model `gpt-5.4-mini` via `.env`
`OPENAI_API_KEY`). One LLM call per (db, variant) asks for an **ordered JSON array**
of synonyms; results are assigned by position and made collision-safe against real
identifiers and already-used decoy names. Base variant = English; rename variant =
the DB's target language (multilingual, incl. pinyin). On a large DB the model can
return a **short array**, so the tail falls back to a `<name>_alt` / `<table>_archive`
placeholder — a uniform, obviously-synthetic suffix an execute-and-observe agent could
use to unmask the decoy. Mitigation: regenerate only the affected columns **chunked
≤ 20 per call** (`scratchpad`-style one-off, model `gpt-5.4-mini`) so the array cannot
truncate. The shipped manifests have **0** `_alt`/`_archive`/`_copy` names (verified
against the live decoy instances).

**Known limitations (not fixed):**
- *Casing-style tell.* On PascalCase DBs (e.g. `works_cycles`, AdventureWorks-style),
  decoy identifiers are snake_case and sit beside PascalCase real columns — a stylistic
  giveaway. Uniform across the whole naming step; would require casing-aware name
  generation to fix.
- *Trap-fire coverage (C1).* Sparse/permute traps only affect the rows/ops a question
  actually touches; a given question may not hit a trap. This is by design (measure a
  trap rate downstream), not a defect.

**Manifest schema** (the ground-truth handoff to the separate eval repo). Both live in
`artifacts/`. The **salt** is variant-independent (English source identity), so
`pg_decoy` and `pg_rename_decoy` corrupt the same rows the same way (D2); `names.base`
is the English decoy identifier, `names.rename` the target-language one.

- `trap_manifest.json` — evil-twin **columns**; one entry per trap:
  `{db, table, source_column, source_type, operator, is_key, in_correlated_group,
  salt, names:{base, rename}}`. The decoy column added to `<db>.<table>` is
  `names.<variant>`; it is a corrupted copy of `source_column`.
- `trap_table_manifest.json` — corrupted clone **tables**; one entry per clone:
  `{db, source_table, columns:[{source_column, source_type, operator, is_key}],
  names:{base:{table, columns:[…]}, rename:{table, columns:[…]}}}`. Column `columns[i]`
  maps to `names.<variant>.columns[i]`; `operator:null` = copied exact (uncorrupted).
- `order_sensitive_qids.json` — qids to exclude from strict EX scoring (153 order-
  sensitive + 21 pre-existing exec-failed).

To compute a **decoy-consistent answer** (trap-rate metric, F2), the eval repo rewrites
the gold SQL to read `names.<variant>` in place of `source_column` (or the clone table
in place of `source_table`) and runs it on the decoy instance.

Related: [extension-implementation-plan.md](extension-implementation-plan.md),
[pipeline-invariants.md](pipeline-invariants.md),
[../methodology/obfuscation-extensions.md](../methodology/obfuscation-extensions.md).
