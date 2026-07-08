"""
Step 4: Load all 69 retained databases into pg_base with pgloader.

Each SQLite DB becomes a PostgreSQL schema: db_id.table_name.
Row data is copied exactly. DDL uses original BIRD identifiers.

Reads:  artifacts/retained_dbs.json
        data/{split}/{split}_databases/<db_id>/<db_id>.sqlite

Writes: Creates schemas+tables+data in pg_base (port 5432)

Prerequisite: Docker must be running (pgloader is run via the
`dimitri/pgloader:v3.6.7` image — no host install needed).
Run: uv run python pipeline/04_load_pg_base.py
"""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql

sys.path.insert(0, str(Path(__file__).parent))
from _pg_helpers import find_sqlite_path

PG_BASE_DSN = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"
# host.docker.internal resolves to the Docker host from inside a container —
# used instead of joining the compose network by service name because
# pgloader's DSN hostname grammar rejects underscores (our service names,
# pg_base/pg_rename, both have one) and reset_schema() below already
# has to reach pg_base from the host via the published port anyway.
PGLOADER_TARGET_URI = "postgresql://bird:bird@host.docker.internal:5432/bird"
PGLOADER_IMAGE = "dimitri/pgloader:v3.6.7"
ARTIFACTS = Path("artifacts")

# Per-DB CAST rules for data-quality defects found during the actual
# production run (2026-07-01) that a type-wide rule can't safely cover,
# because they're specific columns whose *content* doesn't match their
# declared SQLite type — not a systematic pgloader bug applicable to every
# DB. Each entry is a list of column-scoped CAST clauses (no leading/
# trailing comma) appended to the global CAST rules below.
EXTRA_CASTS: dict[str, list[str]] = {
    # app_id/device_id are legitimate 64-bit hashes outside SBCL's FIXNUM
    # range; pgloader's default int->bigint cast attaches a `integer-to-
    # string` transform that crashes (TYPE-ERROR) and then hangs the whole
    # process instead of exiting (confirmed: had to `docker kill` it).
    # `column ... to bigint` with NO `using` clause bypasses that transform
    # entirely (confirmed via pgloader source: a column-scoped rule with no
    # `using` short-circuits the default rule lookup before it ever attaches
    # the crashing transform). events_relevant.timestamp is also affected:
    # despite being declared DATETIME, every row's actual value is one of
    # these same bignums, not a real timestamp.
    "talkingdata": [
        "column app_events.app_id to bigint",
        "column app_all.app_id to bigint",
        "column app_labels.app_id to bigint",
        "column app_events_relevant.app_id to bigint",
        "column events.device_id to bigint",
        "column events_relevant.device_id to bigint",
        "column events_relevant.timestamp to bigint",
        "column gender_age.device_id to bigint",
        "column gender_age_test.device_id to bigint",
        "column gender_age_train.device_id to bigint",
        "column phone_brand_device_model2.device_id to bigint",
        "column sample_submission.device_id to bigint",
    ],
    # PlayerInfo.birthyear is declared DATE but every value is a bare year
    # string like "1981" — not a valid date literal. Preserve as text
    # rather than trying to force it into a date.
    "ice_hockey_draft": ["column PlayerInfo.birthyear to text"],
    # organization.Established / politics.Independence are declared DATE
    # and mostly hold real ISO dates, but each table's data includes one
    # literal header row baked in as an actual row (e.g. the string
    # "Established" stored where a date belongs) — a pre-existing BIRD
    # data defect, not a pipeline bug. Casting to text preserves both the
    # valid dates and the corrupted row rather than losing the whole table.
    "mondial_geo": [
        "column organization.Established to text",
        "column politics.Independence to text",
    ],
    # players.birthDate/deathDate use SQLite's "0000-00-00" as a sentinel
    # for "unknown"/"not applicable" (e.g. still alive), which PostgreSQL's
    # date type rejects as out of range. Preserve as text.
    "professional_basketball": [
        "column players.birthDate to text",
        "column players.deathDate to text",
    ],
    # Player.height is declared INTEGER but every value is stored with
    # SQLite storage class REAL (e.g. 182.88, centimeters converted from
    # inches) — a genuine mixed-type column that
    # pipeline/00_audit_sqlite_identifiers.py didn't catch because it only
    # checked for non-numeric strings in numeric columns, not for floats
    # specifically (see AUDIT_FINDINGS.md). The `using float-to-string`
    # transform is REQUIRED: a bare `to real` makes pgloader serialize the
    # double with SBCL's default float printer, which emits Common Lisp
    # notation ("182.88d0") that PostgreSQL's real parser rejects
    # ("invalid input syntax for type real: 182.88d0"), dropping all 11,060
    # rows while still exiting 0. float-to-string formats it as a plain
    # decimal PostgreSQL accepts (confirmed empirically 2026-07-04).
    "european_football_2": ["column Player.height to real using float-to-string"],
    # Same header-row-baked-into-data defect as mondial_geo above:
    # CountryRegion's first row is the literal string "ModifiedDate" stored
    # where a DATETIME value belongs (row is ('Cou', 'Name', 'ModifiedDate')
    # — a truncated header row, not real data). Casting to text preserves
    # the other 238 real ISO-timestamp rows alongside the corrupted one.
    "works_cycles": ["column CountryRegion.ModifiedDate to text"],
}


def require_docker() -> None:
    """Fail fast with setup guidance if Docker isn't reachable."""
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for step 4 but was not found on PATH.")
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Docker is installed but not reachable (daemon not running?). "
            "Start Docker Desktop (or `docker compose up -d` the stack) and retry."
        )


def reset_schema(db_id: str) -> None:
    """Drop/recreate the target schema so pgloader starts from a clean slate."""
    with psycopg2.connect(PG_BASE_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(db_id)))
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(db_id)))


def pgloader_command_script(db_id: str) -> str:
    """Build the LOAD DATABASE command script for one SQLite database.

    The SQLite file is bind-mounted into the pgloader container at a fixed
    path (see load_db()); the script below refers to that in-container path,
    not the host path, since pgloader runs inside the container.

    WITH clause decisions, each confirmed empirically against a live Docker
    Postgres + the real worst-case DB (works_cycles):

    - `create no indexes`: pgloader's auto-generated PRIMARY KEY/UNIQUE INDEX
      DDL leaves the column name unquoted inside the index definition (e.g.
      `CREATE UNIQUE INDEX ... ON "MovieFiles" (Id)`) even though the column
      itself is correctly created as `"Id"`. PostgreSQL folds the unquoted
      `Id` to `id`, doesn't find it, and every PK/index on a mixed-case
      column fails — reproduced on works_cycles: 51 hard errors, one per
      table. PK/index creation is bundled under this single WITH flag in
      pgloader's grammar (`create-indexes`); there's no way to keep indexes
      but drop only PKs. This pipeline only ever SELECTs from pg_base,
      so missing indexes cost query speed, not correctness.
    - `no foreign keys`: this is a deliberate methodology choice, not just a
      bug workaround — the downstream agentic Text-to-SQL task should not
      receive FK relationships for free; the agent is expected to infer them
      from column names/values like a human analyst would. It also happens to
      side-step a real pgloader bug where SQLite's shorthand
      `FOREIGN KEY (col) REFERENCES OtherTable` (omitting the referenced
      column, meaning "OtherTable's primary key") crashes FK-DDL generation
      in some pgloader builds. This also makes pg_base consistent with
      pg_rename, which is now a renamed volume clone of pg_base
      (see 06_build_pg_rename.py) and therefore inherits the absence of
      FK constraints automatically — without this, pg_base would have
      had FKs that pg_rename silently lacked.
    - `quote identifiers`: preserves original SQLite identifier casing
      (pgloader downcases by default) — see obfuscation.md §4.
    - The first CAST rule fixes a separate, unrelated bug: pgloader quotes
      SQLite's `DEFAULT CURRENT_TIMESTAMP` as the literal string
      `'current_timestamp'` in the emitted PostgreSQL DDL, which a
      `timestamptz` column then rejects (`22007: invalid input syntax`).
      Affects 80 tables across works_cycles and movie_3 — without this,
      both DBs would abort with zero tables created and pgloader would
      still exit 0 (see verify_row_counts() below; do not trust exit code).
    - The second CAST rule (`type blob to text`) fixes a third bug, found
      during the actual production run: some SQLite columns are declared
      BLOB but store their values using SQLite's TEXT storage class (e.g. a
      hex-encoded GIF as a text string, not real binary) —
      book_publishing_company.pub_info.logo, works_cycles.ProductPhoto's
      photo columns, works_cycles.Document.Document, movie_3.staff.picture.
      pgloader's default BLOB->bytea cast then tries to base64-decode a
      plain text value and rejects the row ("Unexpected end of Base64
      data"), silently dropping it — pgloader still exits 0, and
      verify_row_counts() is what catches it (e.g. book_publishing_company's
      pub_info: 7 SQLite rows, 1 survived in pg_base). Overriding the
      cast target to `text` sidesteps the decode path entirely (confirmed:
      pgloader's base64 decision is keyed off the CAST-target pgsql type,
      not the source declaration, so this is safe) — this pipeline doesn't
      need true binary values, only the row and its other columns intact
      for SQL execution-accuracy testing.
    - The third global CAST rule (`when default '0000-00-00'`) fixes a
      fourth DDL-level bug in the same family as the CURRENT_TIMESTAMP one:
      SQLite's `DATE DEFAULT '0000-00-00'` (MySQL-style zero-date sentinel,
      present in formula_1.races and thrombosis_prediction.Laboratory) gets
      quoted into PostgreSQL DDL as a literal, and year 0000 is out of
      PostgreSQL's date range — the whole DB's CREATE TABLE aborts, zero
      tables created, before any data-loading errors even have a chance to
      surface for that DB. Cast target is `date` (not `text`): confirmed no
      row in either affected table actually holds "0000-00-00" as data, only
      the DDL default uses it, so the column stays a real date type.
    - `reset sequences` is omitted: pgloader v3 has a separate unfixed bug
      (dimitri/pgloader#1651) combining it with `quote identifiers` that
      corrupts `pg_get_serial_sequence()` calls. This pipeline never inserts
      into pg_base after load, so sequence starting values don't matter.
    - EXTRA_CASTS (module-level dict above) adds per-DB, per-column CAST
      rules for defects too narrow for a type-wide rule — see its own
      docstring for what each one fixes and why. All were found by actually
      running the full 69-DB migration once and diagnosing every failure,
      not anticipated in advance. These MUST come before the global type
      rules below: pgloader's cast-rule matching stops at the first rule
      that matches, in list order, so a column-scoped override has to be
      listed first or a global `type X` rule for the same source type wins
      instead and the column-specific fix never gets consulted. (Confirmed
      by hitting exactly this: works_cycles.CountryRegion.ModifiedDate is
      declared DATETIME DEFAULT current_timestamp, so with the global
      `type datetime when default 'current_timestamp'` rule listed first it
      always matched before the column override could apply.)
    """
    schema_search_path = db_id.replace("'", "''")
    cast_rules = list(EXTRA_CASTS.get(db_id, []))
    cast_rules += [
        "type datetime when default 'current_timestamp' to timestamptz drop default",
        "type date when default '0000-00-00' to date drop default",
        "type blob to text",
    ]
    cast_clause = ",\n     ".join(cast_rules)
    return f"""LOAD DATABASE
  FROM sqlite:///data/{db_id}.sqlite
  INTO {PGLOADER_TARGET_URI}

WITH create tables, create no indexes, quote identifiers, no foreign keys

CAST {cast_clause}

SET search_path TO '{schema_search_path}'
;
"""


def load_db(db_id: str, sqlite_path: Path) -> None:
    reset_schema(db_id)
    script = pgloader_command_script(db_id)
    subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "--add-host=host.docker.internal:host-gateway",
            "-v", f"{sqlite_path.resolve()}:/data/{db_id}.sqlite:ro",
            PGLOADER_IMAGE, "pgloader", "/dev/stdin",
        ],
        input=script.encode("utf-8"),
        check=True,
    )


def sqlite_identifiers(sqlite_path: Path) -> set[tuple[str, str | None]]:
    """Return {(table, None), (table, column), ...} exactly as SQLite spells them."""
    conn = sqlite3.connect(str(sqlite_path))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]
    idents: set[tuple[str, str | None]] = set()
    for tbl in tables:
        idents.add((tbl, None))
        cur.execute(f'PRAGMA table_info("{tbl}")')
        for r in cur.fetchall():
            idents.add((tbl, r[1]))
    conn.close()
    return idents


def sqlite_row_counts(sqlite_path: Path) -> dict[str, int]:
    """Return {table: row_count} from the SQLite source."""
    conn = sqlite3.connect(str(sqlite_path))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]
    counts = {}
    for tbl in tables:
        cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
        counts[tbl] = cur.fetchone()[0]
    conn.close()
    return counts


def pg_identifiers(pg_cur, db_id: str) -> set[tuple[str, str | None]]:
    """Return the same shape, read back from information_schema after load."""
    pg_cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (db_id,),
    )
    tables = {r[0] for r in pg_cur.fetchall()}
    idents: set[tuple[str, str | None]] = {(t, None) for t in tables}
    pg_cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s",
        (db_id,),
    )
    idents.update((t, c) for t, c in pg_cur.fetchall())
    return idents


def pg_row_counts(pg_cur, db_id: str, tables: list[str]) -> dict[str, int]:
    """Return {table: row_count} read back from pg_base."""
    counts = {}
    for tbl in tables:
        pg_cur.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(db_id), sql.Identifier(tbl))
        )
        counts[tbl] = pg_cur.fetchone()[0]
    return counts


def verify_casing(pg_cur, db_id: str, sqlite_path: Path) -> list[str]:
    """Confirm pgloader's `quote identifiers` actually preserved SQLite spelling.

    This is the empirical check AUDIT_FINDINGS.md called for: don't trust that
    `quote identifiers` behaves as documented, verify it against every loaded DB.
    """
    expected = sqlite_identifiers(sqlite_path)
    actual = pg_identifiers(pg_cur, db_id)
    missing = expected - actual
    return [f"{t}.{c}" if c else t for t, c in sorted(missing, key=lambda x: (x[0], x[1] or ""))]


def verify_row_counts(pg_cur, db_id: str, sqlite_path: Path) -> list[str]:
    """Confirm every table has the same row count in pg_base as in SQLite.

    pgloader returns exit code 0 even on hard per-row COPY failures — confirmed
    empirically (works_cycles: a literal header row baked into CountryRegion's
    data, and hex-string-typed BLOB columns in ProductPhoto/Document that
    pgloader's type inference misreads as base64, both cause silent partial
    loads with `check=True` in load_db() none the wiser). This is the only
    check that catches "table exists, casing is right, but rows went missing."
    """
    expected = sqlite_row_counts(sqlite_path)
    actual = pg_row_counts(pg_cur, db_id, list(expected))
    return [f"{t}: sqlite={n}, pg_base={actual.get(t)}" for t, n in expected.items() if actual.get(t) != n]


def main():
    require_docker()
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)

    ok = fail = 0
    casing_mismatches: dict[str, list[str]] = {}
    row_count_mismatches: dict[str, list[str]] = {}
    with psycopg2.connect(PG_BASE_DSN) as verify_conn:
        with verify_conn.cursor() as verify_cur:
            for i, db_id in enumerate(dbs):
                print(f"[{i+1}/{len(dbs)}] {db_id}", end=" ... ", flush=True)
                try:
                    sqlite_path = find_sqlite_path(db_id)
                    load_db(db_id, sqlite_path)
                    missing_idents = verify_casing(verify_cur, db_id, sqlite_path)
                    bad_counts = verify_row_counts(verify_cur, db_id, sqlite_path)
                    if missing_idents:
                        casing_mismatches[db_id] = missing_idents
                    if bad_counts:
                        row_count_mismatches[db_id] = bad_counts
                    if missing_idents or bad_counts:
                        fail += 1
                        print(f"ERROR: {len(missing_idents)} identifiers, {len(bad_counts)} "
                              f"row-count mismatches: {(missing_idents + bad_counts)[:5]}")
                    else:
                        ok += 1
                        print("loaded, casing + row counts verified")
                except Exception as e:
                    fail += 1
                    print(f"ERROR: {e}")
                    # A query error (e.g. a table missing entirely after a
                    # FATAL pgloader failure) leaves this shared connection's
                    # transaction aborted. Roll back so the next DB's checks
                    # don't all fail with "current transaction is aborted,
                    # commands ignored until end of transaction block" —
                    # which would mask every subsequent DB's real result.
                    verify_conn.rollback()

    print(f"\nDone. Loaded schemas: {ok}; failed: {fail}")
    if casing_mismatches:
        print(f"Casing mismatches in {len(casing_mismatches)} DBs — pgloader did not preserve "
              f"SQLite identifier spelling despite `quote identifiers`. See details above.")
    if row_count_mismatches:
        print(f"Row-count mismatches in {len(row_count_mismatches)} DBs — pgloader silently "
              f"dropped or rejected rows during COPY (exit code 0 either way). See details above.")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
