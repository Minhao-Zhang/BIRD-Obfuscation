# Pipeline invariants: detailed rationale

`AGENTS.md` lists these invariants tersely as rules to preserve when editing the pipeline. This file is the **forensic record**: why each rule exists, and the empirical evidence behind it. Every one was confirmed against a live PostgreSQL and the real worst-case databases, not assumed from documentation. Read the relevant section before changing the code the rule protects.

---

## Step 4: pgloader load into `pg_base`

### pgloader runs as a container, not a host install
Step 4 uses `dimitri/pgloader:v3.6.7`. There is no well-packaged native Windows build, and pgloader is a Common Lisp binary that's awkward to install reliably across environments; since Docker is already a hard dependency (the PostgreSQL instances run in Compose), running pgloader the same way collapses the environment prerequisite to "Docker is running." `load_db()` bind-mounts each SQLite file **read-only** into the container and pipes the LOAD DATABASE script over stdin as `pgloader /dev/stdin`: pgloader does **not** accept `-` for stdin, it must be the literal path `/dev/stdin`. It loads `pg_base` with an **unrenamed, exact copy** of each SQLite DB into its own schema (`db_id.table_name`).

### Reaches `pg_base` via `host.docker.internal`
Not via the Compose network / service name. pgloader's DSN hostname grammar rejects underscores, and both Compose service names (`pg_base`, `pg_rename`) contain one. Joining the Compose network and using the service name for DNS would fail to parse. `--add-host=host.docker.internal:host-gateway` is passed for Linux-host portability even though Docker Desktop resolves it automatically.

### Do not add `reset sequences` back to the WITH clause
pgloader v3 has an unfixed bug (dimitri/pgloader#1651; PR #1701 proposed a fix but was closed unmerged, landing only in the v4 rewrite, PR #1705, never backported) where `quote identifiers` + sequence reset emits `pg_get_serial_sequence()` calls with literal double-quotes baked into the column name, causing a hard `42703` error on any mixed-case serial/PK column: exactly the DBs (`works_cycles`, etc.) `quote identifiers` exists to handle. This pipeline never inserts into `pg_base` after load, so sequence starting values are irrelevant; the clause is dropped, not worked around.

### Do not add `create indexes` (without `no`) back to the WITH clause
With `quote identifiers` active, pgloader's auto-generated `CREATE UNIQUE INDEX` / `ADD PRIMARY KEY` DDL leaves the column name **unquoted inside the index definition** even though the column itself is correctly created and quoted in `CREATE TABLE`. Postgres folds the unquoted name and fails to find it. Confirmed empirically: 51 hard errors on `works_cycles` alone, one per table. There is no WITH-clause option to keep indexes but fix only the quoting bug inside them (PK + index creation share one flag in pgloader's grammar). `pg_base` is index-free by necessity, not just choice. This only costs query speed since the pipeline only ever `SELECT`s from it.

### Do not add `foreign keys` (or remove `no foreign keys`): methodology decision, not a workaround
The downstream agentic Text-to-SQL task should not receive FK relationships for free (see [../methodology/obfuscation.md](../methodology/obfuscation.md) §2 "Deliberately absent from the schema lake"). It also avoids a real pgloader crash: SQLite's `FOREIGN KEY (col) REFERENCES OtherTable` shorthand (omitting the referenced column) makes `PRAGMA foreign_key_list` return a null `to`-column, which some pgloader builds crash on generating DDL for: 176 such FKs exist across 15 of 69 retained DBs, not a corner case. This also keeps `pg_base` consistent with `pg_rename`, which has never created FK constraints.

### CAST rule for `DEFAULT CURRENT_TIMESTAMP` columns
`CAST type datetime when default 'current_timestamp' to timestamptz drop default`. Without it, pgloader quotes SQLite's `DEFAULT CURRENT_TIMESTAMP` as the literal string `'current_timestamp'` in the emitted DDL, which a `timestamptz` column rejects: a hard `CREATE TABLE` failure that aborts the **entire** load for any DB with such a column (confirmed: `works_cycles`, `movie_3`, 80 tables total).

### CAST rule for the MySQL-style zero-date sentinel
`type date when default '0000-00-00' to date drop default`. SQLite's `0000-00-00` sentinel (`formula_1.races`, `thrombosis_prediction.Laboratory`) is out of PostgreSQL's `date` range and also aborts the whole DB's `CREATE TABLE`.

### CAST `blob to text`
Some SQLite columns are declared `BLOB` but every row's actual storage class is `TEXT` (e.g. a hex-encoded image stored as a string): `book_publishing_company.pub_info.logo`, `works_cycles.ProductPhoto`/`Document`, `movie_3.staff.picture`. pgloader's default `BLOB→bytea` cast tries to base64-decode the text value, fails, and **silently drops the row** (exit code still 0). Retargeting to `text` sidesteps the decode path entirely (pgloader's base64 decision is keyed off the CAST *target* type, not the source declaration).

### FIXNUM-overflow hang: the most dangerous failure mode
A SQLite `INTEGER` column holding a value outside SBCL's FIXNUM range (~±4.6×10^18) crashes pgloader's `integer-to-string` transform, and pgloader **does not exit, it hangs indefinitely at 0% CPU**. It doesn't respect `--on-error-stop`, doesn't error out, and would silently stall an unattended run forever (had to `docker kill` the container). Confirmed on `talkingdata`'s `app_id`/`device_id` (legitimate 64-bit hashes) and `events_relevant.timestamp` (declared `DATETIME` but holding the same bignums). Fixed via `EXTRA_CASTS` in `04_load_pg_base.py`: a column-scoped `CAST column tbl.col to bigint` rule with **no `using` clause** bypasses the crashing transform (no-`using` falls back to a generic, fixnum-safe stringifier). **Only checked for `app_id`/`device_id` in `talkingdata`. Not ruled out elsewhere in the corpus.** If step 4 hangs at 0% CPU with no new log output for >30s on a non-huge table, suspect this first.

### `EXTRA_CASTS` must be listed *before* the global type-wide CAST rules
pgloader's CAST matching stops at the first rule that matches, in list order. A column-scoped override for a type also covered by a global rule (e.g. a `DATETIME DEFAULT current_timestamp` column) silently loses to the global rule if listed second, confirmed by hitting exactly this on `works_cycles.CountryRegion.ModifiedDate`, where the override had no effect until moved ahead of the global rules.

### WITH clause must include `quote identifiers`
pgloader downcases SQLite identifiers by default (`src/params.lisp` `*identifier-case*` defaults to `:downcase`, applied to every source loader including SQLite). That default only wins for identifiers matching `^[A-Za-z_][A-Za-z0-9_$]*$`; names with spaces/punctuation were already forced into the quoted/case-preserved branch. So plain PascalCase tables (e.g. all 65 of `works_cycles`') were being silently downcased even without risky punctuation: the exact failure the old no-casing-directive WITH clause would hit. `quote identifiers` is valid WITH-clause grammar for SQLite sources (shared rule with MySQL sources, not MySQL-exclusive). The final WITH clause is `create tables, create no indexes, quote identifiers, no foreign keys` plus the CAST rules above.

### `check=True` is necessary but not sufficient: pgloader returns 0 even on data-losing failures
Confirmed directly, including with `--on-error-stop` against a `FATAL` schema-creation error: pgloader still exited 0 with zero tables created. Step 4 therefore verifies two things after every load, independent of exit code:
- `verify_casing()`: diffs SQLite `PRAGMA table_info` against `pg_base`'s `information_schema.tables/columns`.
- `verify_row_counts()`: diffs `SELECT COUNT(*)` per table; this is the check that catches a table existing correctly but silently missing rows (e.g. a rejected-row COPY error). Two real pre-existing data-quality defects in BIRD's `works_cycles.sqlite` were only found this way (a header row baked into `CountryRegion`'s data; the BLOB-as-hex-TEXT columns above).

If either check starts failing, don't just retry. Read the pgloader log for the actual error.

---

## Steps 5 & 7: sqlglot transpile, rename, and SQL emission

### All emitted identifiers are quoted
`05_transpile_sql.py` and `07_rename_sql_and_validate.py` double-quote every identifier. Quoting an all-lowercase name is harmless; unquoted mixed-case/punctuated names would be misinterpreted by PostgreSQL. Don't special-case "looks lowercase, skip quoting."

### Schema qualification must skip CTE aliases
A `WITH x AS (...)` alias parses as `exp.Table` when referenced later: sqlglot's AST doesn't distinguish a CTE reference from a real table reference. Blindly schema-qualifying every `exp.Table` turns `FROM x` into `FROM "db_id"."x"`, which doesn't exist. Collect CTE alias names (`{cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}`) and exclude them. Not hypothetical: 9 gold queries use `WITH` (`card_games`, `formula_1` ×6, `toxicology`) and hit exactly this before the fix.

### Never mutate a sqlglot AST node while iterating a live `stmt.walk()`
`node.set(...)` creates a new child subtree the walker then descends into and revisits, confirmed to hang and grow unbounded memory even on a trivial identity rename (fires on every rename, not an edge case). Collect the list of nodes to change first, then mutate after the walk completes. See `rename_sql()` in `07_rename_sql_and_validate.py`.

### Never rename an `exp.Identifier` that is a Table's `db`/`catalog` arg
Three DBs (`superhero`, `sales_in_weather`, `university`) have a table whose name equals the `db_id` itself. Naively renaming every `Identifier` matching a rename-map key corrupts the schema qualifier (`"superhero"."superheld"` → `"superheld"."superheld"`, which doesn't exist). Check `node.parent.args.get("db") is node` (and `"catalog"`) and skip those.

### Fix-batch exports give agents schema context from `pg_base`, not SQLite
`get_pg_schema_ddl()` in `_transpile_helpers.py` (used by `05c_export_fix_batch.py`) reads the live `pg_base` `information_schema`, because pgloader does its own type inference (SQLite's dynamic typing doesn't map 1:1 to PostgreSQL) and may spell an identifier differently if a casing edge case slips through. Handing an agent SQLite's `CREATE TABLE` text describes a schema that may not match what it's actually querying.

### Evidence hints get the same rename-map substitution as SQL
Word-boundary regex, longest identifiers first. See `rename_evidence()` in `07_rename_sql_and_validate.py`. Output records carry both `evidence` (original) and `evidence_rename`; downstream consumers should consume the obfuscated one.

---

## Step 6: clone-and-rename `pg_rename`

### Renames in place inside an already-cloned volume: does not reload from SQLite or connect to `pg_base`
An earlier version re-read every SQLite file and re-inferred column types independently via `_pg_helpers.py`'s data-sampling `infer_pg_type()` (NUMERIC/TEXT only): a second, coarser inference pass that could silently disagree with the type pgloader already chose and verified in `pg_base`. Cloning `pg_base`'s Docker volume before running step 6 removes that risk entirely: `pg_rename`'s types are `pg_base`'s types by construction, and `06_build_pg_rename.py` only issues `ALTER TABLE ... RENAME TO` / `RENAME COLUMN` per `schema_rename_map.json`: a fast, catalog-only operation regardless of row count. `_pg_helpers.py`'s `infer_pg_type()`/`copy_data()`/`get_sqlite_schema()` are no longer used by any script; `find_sqlite_path()` is still used by step 0.

---

## Execution & connections

### `exec_pg()` in step 7 must use `fetchmany()` with a hard row cap, never `fetchall()`
At least one gold query (`bike_share_1`, a join missing a date condition) returns 19.4M rows with no `LIMIT`: `fetchall()` materializes tens of millions of Python tuples and hangs the process at multi-GB memory. Overflow past `MAX_RESULT_ROWS` raises `ResultSetTooLarge`, routed to `rename_failures.jsonl` like any other failure.

### `SET LOCAL` silently does nothing under `autocommit=True`; use plain `SET`
`SET LOCAL` is transaction-scoped, and autocommit gives every statement its own implicit transaction, so it has no effect on the next query (confirmed via `SHOW statement_timeout` returning `'0'` immediately after). `_transpile_helpers.py`'s `exec_pg()` correctly uses `SET LOCAL` because its connections run `autocommit=False`; `07_rename_sql_and_validate.py`'s connections are `autocommit=True` and must use plain `SET`.

### Postgres DSNs default to `host=127.0.0.1`, not `host=localhost`
On this project's Windows/Docker Desktop setup, `localhost` resolves IPv6-first and the IPv6 connect attempt takes 20+ seconds before falling back to IPv4 (confirmed via raw `/dev/tcp/localhost/5432` at 21s vs. `/dev/tcp/127.0.0.1/5432` instant). Every fresh connection pays this tax; don't reintroduce `localhost` in the local default. The DSNs are overridable per instance via `PG_*_DSN` env vars (`_db.py`, see `.env.example`) so the eval can target remote Postgres / AWS RDS (a remote override naturally uses a hostname) but the local docker default must stay `127.0.0.1`.

---

## Cross-cutting

### Explicit `encoding="utf-8"` on all reads touching `schema_rename_map.json` or question/evidence text
Without it, Python defaults to the platform codepage (`cp1252` on Windows), which crashes on the first non-ASCII French/German/Spanish/Pinyin identifier. Writes already do this (`json.dumps(..., ensure_ascii=False)` + explicit UTF-8 handles); reads must match.

### `01_split.py` split logic stays per-DB-independent and reproducible
Seed each DB's `random.Random` off a stable hash of `(SEED, db_id)` (e.g. `zlib.crc32`, **not** Python's `hash()`, which is salted per-process). Never reuse one `Random` instance's state across DBs.

### Docker Compose WAL tuning is intentional
`wal_level=minimal`, `fsync=off`, `max_parallel_workers=0`, `shm_size: 256mb`, etc. All four instances are always rebuildable (the clean `pg_base`/`pg_rename` from SQLite, the `*_decoy` pair by cloning the clean volumes + re-injecting traps), so durability doesn't matter; the tuning is for bulk-load speed. Keep it.
