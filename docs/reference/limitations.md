# Known limitations & scope caveats

Honest limitations of this dataset both as a text-to-SQL **asset** and as a
contamination / robustness **instrument**. Several were surfaced by external review;
where a limitation has been mitigated in code, that is noted. None of these
invalidate the delta-based ablation conclusions, but they bound how the numbers
should be read and reported.

## 1. English databases carry no identifier rename (they are the control)

14 of 69 databases are assigned the `english` "language", so their rename map is a
complete no-op: for those DBs `pg_rename` is identical to `pg_base` and
`sql_rename == sql_base`. Weighted by questions this is ~23% of the corpus
(≈2,326 / 10,164 rows). This is **deliberate**. English is the **noise-floor
control** for the rename dimension (an un-renamed arm should show ≈0 rename delta).

Implications for reporting:

- **Report the rename effect per-language, with English as the null, never as a
  single pooled delta.** A pooled "rename effect" is structurally diluted by ~23%
  of guaranteed-null rows and would understate the real per-language effect.
- These DBs still receive **full decoy-trap and paraphrase** treatment, so they are
  **not** unobfuscated overall. Only the *rename axis* is a no-op for them. "23%
  is byte-for-byte public BIRD" is true only of `pg_rename`, not of `pg_decoy` /
  `pg_rename_decoy` or the paraphrase arm.
- If a use case needs *every* row to probe identifier recall, synonym-rename the
  English DBs (English→English synonyms). Not currently done, because it would
  remove the clean noise-floor control, a deliberate trade-off.

## 2. Result comparison is type-lenient (BIRD-style)

The default comparator (`normalise_result`) coerces every cell to a float, or else a
stripped/lower-cased string. So `1 == "1" == True == Decimal("1.00")` and string case
is ignored. This matches BIRD's spirit and is applied **symmetrically to every arm**,
so it **cancels in the contamination/ablation deltas** (the headline signal). It can,
however, **over-credit absolute EX** (a model's `"1"` matches gold `1`).

Mitigation (implemented): the eval now reports a **strict EX alongside the lenient
one** (`normalise_result_strict`: no cross-type collapse, numeric equality preserved,
case-sensitive, CHAR padding still stripped) as a conservative floor. Quote the
strict column for absolute-accuracy claims. NaN/inf are now canonicalized in
`normalise_result` too, so it agrees with the step-5 comparator and a returned NaN no
longer causes a spurious `R1!=R2`.

## 3. Validation inclusion has an execution budget

A gold is admitted to the deliverable by executing it under a `statement_timeout`
(now **300 s**) and a **200,000-row** fetch cap, against an intentionally index-free
schema. The row cap is deterministic; the timeout is not, at the boundary. A gold
that finishes just under vs. just over could flip run-to-run. In practice this touches
only a handful of **degenerate** golds (missing-join cross-products, e.g. one 19.4M-row
gold), recorded as `exec_failed` in `artifacts/order_sensitive_qids.json`. The 300 s
headroom makes inclusion effectively deterministic for legitimate queries; a real
text-to-SQL answer with >200k rows is essentially nonexistent in BIRD.

## 4. Rename-map collisions are detected but resolved manually

Step 3 warns on value collisions; step 6 renames each DB and, on any error, rolls that
DB back (leaving it unrenamed). Cross-table column-name collisions are harmless in
PostgreSQL (columns are table-scoped) and are the only kind present in the shipped map.
A **same-scope** collision (two identifiers → one name within a table, or two tables →
one name) would fail the `ALTER`.

Mitigation (implemented): step 6 now **exits non-zero and lists any failed DB**, so a
silently-dropped database is impossible. The tell is loud, and the fix is to resolve
the collision in the map and re-run. Automatic collision resolution (suffixing) is not
done.

## 5. `VALUES`-materialized golds

~11.5% of golds contain a `VALUES` clause, but only **~0.5%** are genuinely table-free
(constants baked in, where `R0==R1` / `R1==R2` are trivially self-satisfied); the rest
use `VALUES` legitimately (in-lists / derived tables alongside real tables). So a
"~12% circular validation" reading would be an overstatement. The truly-constant set
is ~0.5%. A few of those constant golds are very large literal dumps (up to ~4.4M
characters): faithful transpilations, but not natural SQL and poor as "known-true SQL"
for a memory-learning downstream agent. Treat them as edge cases.

## 6. Smaller caveats

- The rename short-circuit skips any SQL containing no quoted rename-map key. It is
  safe **only** because every identifier is quoted upstream (a documented invariant);
  it fails open (silent no-op) if an unquoted identifier ever reaches it.
- Evidence-hint renaming is sequential regex substitution over NL text (not gold SQL):
  low stakes; a later short key could in principle match inside an inserted translation.
- Even non-English DBs retain ~3-15% of original identifiers via identity mappings
  (numbers, already-native words, untranslatable tokens).

## Precision notes on the integrity guarantees

- **Trap additivity is guaranteed by construction**, not by a runtime fingerprint:
  real columns are only ever read as the corruption *source*; every corruption writes
  the *new* decoy column; clone tables are fresh `CREATE TABLE AS`. The committed gate
  is the `R1==R2` re-run. (Any "fingerprint" comparison mentioned informally was an
  ad-hoc verification, not part of the shipped pipeline.)
- **"Real data byte-identical" means real column *values* are unmodified** (per-column
  multiset identical). Physical row *order* and table *arity* are **not** identical on
  the decoy instances: phase-1 `ADD COLUMN` + `UPDATE` rewrites the heap (reordering
  physical rows, the reason for the 153 order-sensitive exclusions on
  `LIMIT`-without-total-order / float-aggregate golds), and added decoy columns widen
  `SELECT *`.

## Status summary

| # | Limitation | Status |
| --- | --- | --- |
| 1 | English DBs = no rename (control) | by design; **report rename per-language** |
| 2 | Lenient comparator inflates absolute EX | **mitigated**: strict EX reported alongside; NaN canonicalized |
| 3 | Timeout-boundary non-determinism | **mitigated**: timeout raised to 300 s; affects only degenerate golds |
| 4 | Collision → silent DB drop | **mitigated**: step 6 fails loud (non-zero exit) |
| 5 | `VALUES` framing / large literal dumps | documented (truly-circular ≈0.5%, not ~12%) |
| 6 | Short-circuit / evidence regex / identity leakage | documented; low stakes |
