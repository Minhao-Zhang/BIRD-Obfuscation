"""
Step 8: Inject decoy tables/columns into the two *_decoy PostgreSQL instances
so schema-linking is harder, WITHOUT changing any gold-query result.

Layer 3 of the extended obfuscation (docs/methodology/obfuscation-extensions.md
§2; build spec in docs/reference/extension-implementation-plan.md §5). Golden
rule: never touch pg_base / pg_rename or the existing *_final.jsonl —
all writes go to the decoy clones and to new artifacts.

Four phases (select with --phase; default runs all):
  1 generate  Ask a cheap model for plausible-but-fake decoy tables + confusable
              decoy columns per DB per variant; validate every name (snake_case,
              <=60 chars, no collision with a real table/column or the db_id);
              persist artifacts/decoy_map.json (seeded, regeneratable).
  2 (part of generate) Expand real-table SELECT * / t.* in the ~5 affected gold
              queries to the explicit real-column list, read from the CLEAN
              instance BEFORE injection -> artifacts/gold_star_expanded.jsonl.
  3 inject    Apply the "base" variant to pg_decoy (5434) and the
              "rename" variant to pg_rename_decoy (5435). Idempotent:
              every object is skipped if information_schema already has it.
              All identifiers quoted; no FK constraints; decoy tables empty.
  4 validate  R1==R2 acceptance gate: for every question, gold on the clean
              instance (R1) must equal gold on the decoy instance (R2), using
              the *expanded* SQL for the star questions. Expected: 0 failures.

Reads:
  artifacts/retained_dbs.json, artifacts/db_language_map.json,
  artifacts/{train,test}_final.jsonl
  live pg_base (5432) / pg_rename (5433)  -- read real schema/columns
  live pg_decoy (5434) / pg_rename_decoy (5435)  -- inject + validate

Writes:
  artifacts/decoy_map.json, artifacts/gold_star_expanded.jsonl
  workdir/decoy_validated.jsonl (resume log), workdir/decoy_failures.jsonl

Dependency: the two *_decoy instances must already be cloned from their clean
counterparts (see extension-implementation-plan.md §3c) before phases inject /
validate can run. Re-cloning a decoy volume resets it -> re-run this step.

Run:
  uv run python pipeline/08_inject_decoys.py --limit 20   # dry run, first 20 dbs
  uv run python pipeline/08_inject_decoys.py              # generate + inject + validate
  uv run python pipeline/08_inject_decoys.py --validate-only
"""

from __future__ import annotations

import argparse
import json
import os
import random
import zlib
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from _db import (
    PG_BASE_DSN,
    PG_RENAME_DSN,
    PG_DECOY_DSN,
    PG_RENAME_DECOY_DSN,
    exec_pg,
    new_connection,
    normalise_result,
)

# load_dotenv is only needed for the OpenAI generation phase; import lazily in
# main() so that importing this module for offline unit tests never requires
# python-dotenv/openai to be importable in a minimal environment.

ARTIFACTS = Path("artifacts")
WORKDIR = Path("workdir")
DECOY_MAP_PATH = ARTIFACTS / "decoy_map.json"
GOLD_STAR_PATH = ARTIFACTS / "gold_star_expanded.jsonl"
VALIDATED_PATH = WORKDIR / "decoy_validated.jsonl"
FAILURES_PATH = WORKDIR / "decoy_failures.jsonl"

# Per-DB-independent, reproducible seed base (mirrors 01_split.py). The actual
# RNG for each (db_id, variant) is derived via zlib.crc32 below.
SEED = 42

VARIANTS = ("base", "rename")
DSN_FOR_VARIANT_CLEAN = {"base": PG_BASE_DSN, "rename": PG_RENAME_DSN}
DSN_FOR_VARIANT_DECOY = {"base": PG_DECOY_DSN, "rename": PG_RENAME_DECOY_DSN}
GOLD_FIELD = {"base": "sql_base", "rename": "sql_rename"}
EXPANDED_FIELD = {"base": "sql_base_expanded", "rename": "sql_rename_expanded"}

# Postgres identifiers are capped at 63 bytes; the spec caps decoy names at 60.
MAX_IDENT_LEN = 60

# Column types a decoy is allowed to declare. Anything else coerces to text.
ALLOWED_TYPES = {"integer", "bigint", "numeric", "text", "date", "boolean", "timestamptz"}
_TYPE_ALIASES = {
    "int": "integer", "int4": "integer", "serial": "integer", "smallint": "integer",
    "int8": "bigint", "bigserial": "bigint",
    "float": "numeric", "float4": "numeric", "float8": "numeric", "real": "numeric",
    "double": "numeric", "double precision": "numeric", "decimal": "numeric", "money": "numeric",
    "varchar": "text", "char": "text", "character": "text", "character varying": "text",
    "string": "text", "citext": "text", "uuid": "text",
    "bool": "boolean",
    "datetime": "timestamptz", "timestamp": "timestamptz",
    "timestamp with time zone": "timestamptz", "timestamp without time zone": "timestamptz",
}

SYSTEM_INSTRUCTIONS_DECOY = (
    "You extend database schemas with plausible but FAKE distractor objects for "
    "a schema-linking robustness test. Return JSON only — no prose, no markdown "
    "code fences."
)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable offline, no DB / no API)
# --------------------------------------------------------------------------- #

def clamp_n_tables(real_table_count: int, frac: float, min_tables: int, max_tables: int) -> int:
    """Number of decoy tables for a DB: round(frac * real_count), clamped to
    [min_tables, max_tables]."""
    n = round(frac * real_table_count)
    return max(min_tables, min(max_tables, n))


def is_snake_case(name: str) -> bool:
    """snake_case: all-lowercase, non-digit first char, only unicode
    letters/digits/underscore. Accepts accented lowercase (French/German/
    Spanish target languages); rejects uppercase, spaces, hyphens, dots."""
    if not name or not isinstance(name, str):
        return False
    if name != name.lower():
        return False
    if name[0].isdigit():
        return False
    return all(ch == "_" or ch.isalnum() for ch in name)


def is_valid_decoy_name(name: str, real_identifiers_lower: set[str], db_id: str) -> bool:
    """A decoy identifier is valid iff it is snake_case, <= MAX_IDENT_LEN chars,
    does not equal the db_id (the superhero/sales_in_weather/university
    schema-qualifier trap), and collides (case-insensitively) with no real
    table/column in the schema."""
    if not is_snake_case(name):
        return False
    if len(name) > MAX_IDENT_LEN:
        return False
    low = name.lower()
    if low == db_id.lower():
        return False
    if low in real_identifiers_lower:
        return False
    return True


def coerce_type(t) -> str:
    """Coerce a model-supplied column type to the allowlist; default text."""
    if not isinstance(t, str):
        return "text"
    key = t.strip().lower()
    if key in ALLOWED_TYPES:
        return key
    return _TYPE_ALIASES.get(key, "text")


def strip_json_fences(text: str) -> str:
    """Strip a leading ```json / ``` fence the model may have added anyway."""
    import re
    text = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n?```$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _from_clause(select: exp.Select):
    # sqlglot renamed the arg key to "from_" around v25; keep a fallback.
    return select.args.get("from") or select.args.get("from_")


def _table_sources(select: exp.Select) -> list[exp.Table]:
    """Real base tables in THIS select's own FROM + JOINs (does not descend into
    derived tables / subqueries / VALUES — those are not exp.Table)."""
    sources: list[exp.Table] = []
    frm = _from_clause(select)
    if frm is not None and isinstance(frm.this, exp.Table):
        sources.append(frm.this)
    for j in select.args.get("joins") or []:
        if isinstance(j.this, exp.Table):
            sources.append(j.this)
    return sources


def _star_projections(select: exp.Select) -> list[exp.Expression]:
    """Top-level projection nodes that are a bare Star or a qualified t.*.
    COUNT(*) is an exp.Count wrapping a Star, never a top-level Star, so it is
    correctly excluded."""
    out = []
    for p in select.expressions:
        if isinstance(p, exp.Star):
            out.append(p)
        elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
            out.append(p)
    return out


def sql_has_real_table_star(sql: str) -> bool:
    """True iff the SQL contains a Select whose projection has a real-table
    star (top-level or in a subquery)."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return False
    if tree is None:
        return False
    for sel in tree.find_all(exp.Select):
        if _star_projections(sel) and _table_sources(sel):
            return True
    return False


def _quoted_column(name: str, table: str | None):
    if table:
        return exp.column(name, table=table, quoted=True)
    return exp.column(name, quoted=True)


def expand_stars_in_sql(sql: str, resolve_columns, default_schema: str | None = None) -> str:
    """Expand real-table SELECT * / t.* to the explicit, ordered, quoted
    real-column list.

    resolve_columns(schema, table) must return the real column names of that
    table in ordinal order. A bare * over a single source expands unqualified;
    over multiple sources it qualifies each column with the source's alias (or
    name). A qualified t.* always expands qualified by t. Non-star SQL is
    round-tripped through sqlglot unchanged (harmless for execution).
    """
    tree = sqlglot.parse_one(sql, read="postgres")
    # Collect the selects first: we mutate each select's projection list, and
    # find_all() yields a live generator — snapshot it to a list (AGENTS.md:
    # never mutate a sqlglot tree mid-walk).
    for sel in list(tree.find_all(exp.Select)):
        sources = _table_sources(sel)
        if not sources or not _star_projections(sel):
            continue

        multi = len(sources) > 1
        ordered: list[tuple[str, list[str]]] = []
        by_key: dict[str, list[str]] = {}
        for t in sources:
            alias = t.alias or t.name
            schema = t.db or default_schema
            cols = list(resolve_columns(schema, t.name))
            ordered.append((alias, cols))
            # allow both the alias and the bare table name as a t.* qualifier
            by_key[alias] = cols
            by_key.setdefault(t.name, cols)

        new_projs: list[exp.Expression] = []
        for p in sel.expressions:
            if isinstance(p, exp.Star):
                for alias, cols in ordered:
                    for c in cols:
                        new_projs.append(_quoted_column(c, alias if multi else None))
            elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
                qual = p.table
                cols = by_key.get(qual)
                if cols is None:
                    new_projs.append(p)  # unresolvable qualifier: leave as-is
                    continue
                for c in cols:
                    new_projs.append(_quoted_column(c, qual))
            else:
                new_projs.append(p)
        sel.set("expressions", new_projs)

    return tree.sql(dialect="postgres")


def validate_generation(raw: dict, real_tables_lower: set[str],
                        real_identifiers_lower: set[str],
                        real_cols_by_table: dict[str, list[str]],
                        db_id: str) -> tuple[list[dict], dict, list[str]]:
    """Validate/repair a model's raw decoy proposal for one (db_id, variant).

    Returns (decoy_tables, decoy_columns, dropped_log). Everything invalid is
    dropped and recorded in dropped_log — no silent truncation.
    """
    dropped: list[str] = []
    # names claimed so far within this variant (decoy tables + decoy columns)
    # so decoys don't collide with each other either.
    claimed_lower: set[str] = set(real_identifiers_lower)

    # map lowercased real table name -> canonical real table name
    canon_table = {t.lower(): t for t in real_cols_by_table}

    decoy_tables: list[dict] = []
    for dt in raw.get("decoy_tables", []) or []:
        if not isinstance(dt, dict):
            dropped.append(f"table (not an object): {dt!r}")
            continue
        name = dt.get("name")
        if not isinstance(name, str) or not is_valid_decoy_name(name, claimed_lower, db_id):
            dropped.append(f"table name invalid/collision: {name!r}")
            continue
        cols_out = []
        col_names_lower: set[str] = set()
        for c in dt.get("columns", []) or []:
            if not isinstance(c, dict):
                dropped.append(f"column of {name} (not an object): {c!r}")
                continue
            cname = c.get("name")
            if not isinstance(cname, str) or not is_valid_decoy_name(cname, claimed_lower, db_id):
                dropped.append(f"column name invalid/collision in {name}: {cname!r}")
                continue
            if cname.lower() in col_names_lower:
                dropped.append(f"duplicate column in {name}: {cname!r}")
                continue
            col_names_lower.add(cname.lower())
            cols_out.append({"name": cname, "type": coerce_type(c.get("type"))})
        if not cols_out:
            dropped.append(f"table {name} dropped: no valid columns")
            continue
        claimed_lower.add(name.lower())
        decoy_tables.append({"name": name, "columns": cols_out})

    decoy_columns: dict[str, list[dict]] = {}
    for real_table, cols in (raw.get("decoy_columns", {}) or {}).items():
        if not isinstance(real_table, str) or real_table.lower() not in canon_table:
            dropped.append(f"decoy_columns target not a real table: {real_table!r}")
            continue
        target = canon_table[real_table.lower()]
        entries = []
        for c in cols or []:
            if not isinstance(c, dict):
                dropped.append(f"decoy column of {target} (not an object): {c!r}")
                continue
            cname = c.get("name")
            if not isinstance(cname, str) or not is_valid_decoy_name(cname, claimed_lower, db_id):
                dropped.append(f"decoy column invalid/collision on {target}: {cname!r}")
                continue
            mimics = c.get("mimics")
            if not isinstance(mimics, str):
                mimics = None
            entries.append({"name": cname, "type": coerce_type(c.get("type")), "mimics": mimics})
            claimed_lower.add(cname.lower())
        if entries:
            decoy_columns[target] = entries

    return decoy_tables, decoy_columns, dropped


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

def read_real_schema(conn, db_id: str) -> dict[str, list[tuple[str, str]]]:
    """{table_name: [(column_name, data_type), ...]} for schema = db_id, in
    ordinal order."""
    conn.rollback()
    schema: dict[str, list[tuple[str, str]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
            (db_id,),
        )
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (db_id, tbl),
            )
            schema[tbl] = [(c, t) for c, t in cur.fetchall()]
    return schema


def make_column_resolver(conn):
    """Return resolve(schema, table) -> [col names], caching information_schema
    reads. Reads before injection so it sees only real columns."""
    cache: dict[tuple[str, str], list[str]] = {}

    def resolve(schema: str, table: str) -> list[str]:
        key = (schema, table)
        if key not in cache:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    (schema, table),
                )
                cache[key] = [r[0] for r in cur.fetchall()]
        return cache[key]

    return resolve


def render_ddl(db_id: str, schema: dict[str, list[tuple[str, str]]]) -> str:
    """Stripped DDL (names + types only) for the generation prompt."""
    parts = []
    for tbl, cols in schema.items():
        col_lines = ",\n".join(f'    "{c}" {t}' for c, t in cols)
        parts.append(f'CREATE TABLE "{db_id}"."{tbl}" (\n{col_lines}\n)')
    return "\n\n".join(parts)


def table_exists(conn, db_id: str, table: str) -> bool:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (db_id, table),
        )
        return cur.fetchone() is not None


def column_exists(conn, db_id: str, table: str, column: str) -> bool:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (db_id, table, column),
        )
        return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# OpenAI generation
# --------------------------------------------------------------------------- #

def is_reasoning_model(model: str) -> bool:
    """gpt-5* and o1/o3/o4-style models take reasoning={"effort": ...};
    gpt-4o-mini and friends reject it."""
    m = (model or "").lower()
    if m.startswith("gpt-5"):
        return True
    return len(m) >= 2 and m[0] == "o" and m[1].isdigit()


def build_decoy_prompt(db_id: str, language: str, ddl: str, n_tables: int, k: int) -> str:
    return (
        f"You are extending a {language} database schema with plausible but FAKE "
        f"distractor objects for a schema-linking robustness test. Given the real "
        f"schema below, produce:\n"
        f"- {n_tables} decoy TABLE(s): plausible for this domain, {language} "
        f"snake_case names, each with 2-5 columns (name + Postgres type).\n"
        f"- For {k} of the real tables, 1-3 decoy COLUMN(s) that are CONFUSABLE "
        f"near-synonyms or siblings of an existing real column "
        f'(e.g. real "release_year" -> "release_year_est").\n'
        f'Rules: never reuse a real table/column name or the database id "{db_id}"; '
        f"snake_case; each name <= 60 chars; column types must be one of "
        f"{sorted(ALLOWED_TYPES)}.\n"
        f"Output JSON only, exactly this shape:\n"
        f'{{"decoy_tables": [{{"name": "<snake_case>", "columns": '
        f'[{{"name": "<snake_case>", "type": "<pg type>"}}]}}], '
        f'"decoy_columns": {{"<real_table_name>": '
        f'[{{"name": "<snake_case>", "type": "<pg type>", "mimics": "<real_column_name>"}}]}}}}\n\n'
        f"Real schema:\n{ddl}"
    )


def call_model_for_decoys(client, model: str, effort: str, prompt: str) -> dict:
    kwargs = dict(model=model, instructions=SYSTEM_INSTRUCTIONS_DECOY, input=prompt)
    if is_reasoning_model(model):
        kwargs["reasoning"] = {"effort": effort}
    response = client.responses.create(**kwargs)
    text = strip_json_fences(response.output_text)
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Phase 1 — generate decoy_map.json
# --------------------------------------------------------------------------- #

def generate_decoy_map(dbs: list[str], lang_map: dict, model: str, effort: str,
                       n_tables_frac: float, min_tables: int, max_tables: int,
                       k_cols_frac: float) -> dict:
    import openai  # local import: only the generation phase needs the SDK
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    conns = {v: new_connection(DSN_FOR_VARIANT_CLEAN[v]) for v in VARIANTS}
    decoy_map: dict = {}
    try:
        for db_id in dbs:
            decoy_map[db_id] = {}
            for variant in VARIANTS:
                language = "english" if variant == "base" else lang_map.get(db_id, "english")
                schema = read_real_schema(conns[variant], db_id)
                real_tables = list(schema.keys())
                real_cols_by_table = {t: [c for c, _ in cols] for t, cols in schema.items()}
                real_tables_lower = {t.lower() for t in real_tables}
                real_identifiers_lower = set(real_tables_lower)
                for cols in real_cols_by_table.values():
                    real_identifiers_lower.update(c.lower() for c in cols)

                n_tables = clamp_n_tables(len(real_tables), n_tables_frac, min_tables, max_tables)
                k = max(1, min(len(real_tables), round(k_cols_frac * len(real_tables))))

                # Reproducible per-(db, variant) RNG (mirrors 01_split.py). Used
                # to deterministically down-select which real tables get column
                # decoys, keeping the manifest reproducible if the model returns
                # more than we asked for.
                rng = random.Random(zlib.crc32(f"{SEED}:{db_id}:{variant}".encode()))

                ddl = render_ddl(db_id, schema)
                prompt = build_decoy_prompt(db_id, language, ddl, n_tables, k)
                try:
                    raw = call_model_for_decoys(client, model, effort, prompt)
                except Exception as e:
                    print(f"  [{db_id}/{variant}] generation failed: {e}", flush=True)
                    decoy_map[db_id][variant] = {"decoy_tables": [], "decoy_columns": {}}
                    continue

                tables, columns, dropped = validate_generation(
                    raw, real_tables_lower, real_identifiers_lower, real_cols_by_table, db_id,
                )

                # Deterministically cap column-decoy targets at k using the RNG
                # over a sorted key set (reproducible regardless of dict order).
                if len(columns) > k:
                    keep = set(rng.sample(sorted(columns), k))
                    for t in list(columns):
                        if t not in keep:
                            dropped.append(f"decoy_columns target trimmed (>k): {t}")
                            del columns[t]

                decoy_map[db_id][variant] = {"decoy_tables": tables, "decoy_columns": columns}
                if dropped:
                    print(f"  [{db_id}/{variant}] dropped {len(dropped)} invalid: "
                          f"{'; '.join(dropped[:5])}{' ...' if len(dropped) > 5 else ''}",
                          flush=True)
                print(f"  [{db_id}/{variant}] {len(tables)} decoy tables, "
                      f"{sum(len(v) for v in columns.values())} decoy columns "
                      f"(lang={language})", flush=True)
    finally:
        for c in conns.values():
            c.close()
    return decoy_map


# --------------------------------------------------------------------------- #
# Phase 2 — gold_star_expanded.jsonl
# --------------------------------------------------------------------------- #

def dedup_questions() -> list[dict]:
    """Dedup {train,test}_final.jsonl by question_id (keep last)."""
    seen: dict[str, dict] = {}
    for name in ("train_final.jsonl", "test_final.jsonl"):
        path = ARTIFACTS / name
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                seen[r["question_id"]] = r
    return list(seen.values())


def compute_gold_star_expanded(questions: list[dict]) -> None:
    """Detect real-table star gold and expand it against the CLEAN instances
    (real columns only). Writes artifacts/gold_star_expanded.jsonl."""
    conns = {v: new_connection(DSN_FOR_VARIANT_CLEAN[v]) for v in VARIANTS}
    resolvers = {v: make_column_resolver(conns[v]) for v in VARIANTS}
    n = 0
    try:
        with open(GOLD_STAR_PATH, "w", encoding="utf-8") as out:
            for q in questions:
                pg = q.get("sql_base", "")
                ob = q.get("sql_rename", "")
                pg_star = sql_has_real_table_star(pg)
                ob_star = sql_has_real_table_star(ob)
                if not (pg_star or ob_star):
                    continue
                db_id = q["db_id"]
                rec = {"question_id": q["question_id"]}
                rec["sql_base_expanded"] = (
                    expand_stars_in_sql(pg, resolvers["base"], db_id) if pg_star else pg
                )
                rec["sql_rename_expanded"] = (
                    expand_stars_in_sql(ob, resolvers["rename"], db_id) if ob_star else ob
                )
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
                n += 1
    finally:
        for c in conns.values():
            c.close()
    print(f"gold_star_expanded: {n} question(s) with a real-table star -> {GOLD_STAR_PATH}")


def load_gold_star_expanded() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not GOLD_STAR_PATH.exists():
        return out
    with open(GOLD_STAR_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec["question_id"]] = rec
    return out


# --------------------------------------------------------------------------- #
# Phase 3 — inject
# --------------------------------------------------------------------------- #

def inject_variant(conn, db_id: str, variant_def: dict) -> tuple[int, int, int, int]:
    """Idempotently create decoy tables + add decoy columns for one variant.
    Returns (tables_created, tables_skipped, columns_added, columns_skipped)."""
    t_new = t_skip = c_new = c_skip = 0
    for dt in variant_def.get("decoy_tables", []):
        name = dt["name"]
        if table_exists(conn, db_id, name):
            t_skip += 1
            continue
        col_defs = ", ".join(f'"{c["name"]}" {c["type"]}' for c in dt["columns"])
        sql = f'CREATE TABLE "{db_id}"."{name}" ( {col_defs} )'
        with conn.cursor() as cur:
            cur.execute(sql)
        t_new += 1
    for real_table, cols in variant_def.get("decoy_columns", {}).items():
        for c in cols:
            if column_exists(conn, db_id, real_table, c["name"]):
                c_skip += 1
                continue
            sql = f'ALTER TABLE "{db_id}"."{real_table}" ADD COLUMN "{c["name"]}" {c["type"]}'
            with conn.cursor() as cur:
                cur.execute(sql)
            c_new += 1
    return t_new, t_skip, c_new, c_skip


def inject_all(decoy_map: dict, dbs: list[str], populate: bool) -> None:
    if populate:
        raise NotImplementedError(
            "--populate is a stub: decoy tables are intentionally left empty "
            "(invisible in the stripped DDL the eval shows). Populating them is "
            "only relevant if a downstream agent can run exploratory queries."
        )
    conns = {v: new_connection(DSN_FOR_VARIANT_DECOY[v]) for v in VARIANTS}
    try:
        for variant in VARIANTS:
            conn = conns[variant]
            tot = [0, 0, 0, 0]
            for db_id in dbs:
                vdef = decoy_map.get(db_id, {}).get(variant)
                if not vdef:
                    continue
                counts = inject_variant(conn, db_id, vdef)
                tot = [a + b for a, b in zip(tot, counts)]
            print(f"inject {variant}: tables +{tot[0]} (skip {tot[1]}), "
                  f"columns +{tot[2]} (skip {tot[3]})", flush=True)
    finally:
        for c in conns.values():
            c.close()


# --------------------------------------------------------------------------- #
# Phase 4 — re-validate R1==R2
# --------------------------------------------------------------------------- #

def load_validated() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not VALIDATED_PATH.exists():
        return done
    with open(VALIDATED_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done.add((rec["question_id"], rec["variant"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _append(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def revalidate(questions: list[dict], expanded: dict[str, dict],
               variants: tuple[str, ...] = VARIANTS) -> int:
    """R1==R2 gate + SELECT*-expansion self-check. Returns failure count.

    ``variants`` restricts which variant(s) run: passing a single variant only
    opens that variant's clean+decoy instances (2 total), so the OOM constraint
    of never running all four PostgreSQL instances at once can be honoured by
    validating base and rename in separate invocations.
    """
    WORKDIR.mkdir(exist_ok=True)
    done = load_validated()
    clean = {v: new_connection(DSN_FOR_VARIANT_CLEAN[v]) for v in variants}
    decoy = {v: new_connection(DSN_FOR_VARIANT_DECOY[v]) for v in variants}
    ok = fail = skipped = 0
    try:
        for variant in variants:
            gold_field = GOLD_FIELD[variant]
            exp_field = EXPANDED_FIELD[variant]
            clean_conn = clean[variant]
            decoy_conn = decoy[variant]
            for q in questions:
                qid = q["question_id"]
                if (qid, variant) in done:
                    skipped += 1
                    continue
                clean_gold = q.get(gold_field)
                if not clean_gold:
                    continue
                expanded_sql = expanded.get(qid, {}).get(exp_field)

                # R1: clean gold on the clean instance.
                try:
                    r1 = normalise_result(exec_pg(clean_conn, clean_gold))
                except Exception as e:
                    _append(FAILURES_PATH, {"question_id": qid, "variant": variant,
                                            "error": f"r1_exec_failed: {e}"})
                    fail += 1
                    continue

                decoy_gold = clean_gold
                if expanded_sql is not None:
                    # Self-check: the expanded gold on the CLEAN instance must
                    # reproduce the original gold's result (expansion is a no-op
                    # there — it only equals the real columns).
                    try:
                        r_exp = normalise_result(exec_pg(clean_conn, expanded_sql))
                    except Exception as e:
                        _append(FAILURES_PATH, {"question_id": qid, "variant": variant,
                                                "error": f"expanded_exec_failed_clean: {e}"})
                        fail += 1
                        continue
                    if r_exp != r1:
                        _append(FAILURES_PATH, {"question_id": qid, "variant": variant,
                                                "error": "star_expansion_changed_clean_result"})
                        fail += 1
                        continue
                    decoy_gold = expanded_sql

                # R2: (expanded) gold on the decoy instance.
                try:
                    r2 = normalise_result(exec_pg(decoy_conn, decoy_gold))
                except Exception as e:
                    _append(FAILURES_PATH, {"question_id": qid, "variant": variant,
                                            "error": f"r2_exec_failed: {e}"})
                    fail += 1
                    continue

                if r1 != r2:
                    _append(FAILURES_PATH, {"question_id": qid, "variant": variant,
                                            "error": "r1_r2_mismatch"})
                    fail += 1
                    continue

                _append(VALIDATED_PATH, {"question_id": qid, "variant": variant})
                ok += 1
                if (ok + fail) % 500 == 0:
                    print(f"  validated ok={ok} fail={fail} (skipped {skipped})", flush=True)
    finally:
        for c in clean.values():
            c.close()
        for c in decoy.values():
            c.close()
    print(f"revalidate done: ok={ok}, fail={fail}, skipped(already done)={skipped}")
    if fail:
        print(f"  -> {fail} failure(s) in {FAILURES_PATH}")
    return fail


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Step 8: inject decoy schema into the *_decoy instances")
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--effort", default="low",
                        choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    parser.add_argument("--n-tables-frac", type=float, default=0.4)
    parser.add_argument("--min-tables", type=int, default=2)
    parser.add_argument("--max-tables", type=int, default=15)
    parser.add_argument("--k-cols-frac", type=float, default=0.5,
                        help="fraction of real tables that get decoy columns")
    parser.add_argument("--regenerate", action="store_true",
                        help="regenerate artifacts/decoy_map.json even if it exists")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N retained dbs (dry run)")
    parser.add_argument("--validate-only", action="store_true",
                        help="skip generate+inject, run only phase 4")
    parser.add_argument("--phase", choices=["generate", "inject", "validate", "all"], default="all")
    parser.add_argument("--variants", default=",".join(VARIANTS),
                        help="comma-separated subset of variants for the validate phase "
                             "(base,rename). Restrict to one to keep only that variant's "
                             "2 PostgreSQL instances hot (OOM constraint).")
    parser.add_argument("--populate", action="store_true",
                        help="(stub, off) populate decoy tables with rows")
    args = parser.parse_args()

    phase = "validate" if args.validate_only else args.phase

    dbs = load_json(ARTIFACTS / "retained_dbs.json")
    if args.limit:
        dbs = dbs[:args.limit]
    db_set = set(dbs)

    run_generate = phase in ("generate", "all")
    run_inject = phase in ("inject", "all")
    run_validate = phase in ("validate", "all")

    # --- Phase 1 + 2: generate ---
    if run_generate:
        from dotenv import load_dotenv
        load_dotenv()
        lang_map = load_json(ARTIFACTS / "db_language_map.json")

        if DECOY_MAP_PATH.exists() and not args.regenerate:
            print(f"{DECOY_MAP_PATH} exists — skipping generation (use --regenerate to overwrite).")
        else:
            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY not set. Copy .env.example to .env and fill it in.")
            print(f"Generating decoy_map for {len(dbs)} db(s) with {args.model} ...")
            decoy_map = generate_decoy_map(
                dbs, lang_map, args.model, args.effort,
                args.n_tables_frac, args.min_tables, args.max_tables, args.k_cols_frac,
            )
            with open(DECOY_MAP_PATH, "w", encoding="utf-8") as f:
                json.dump(decoy_map, f, ensure_ascii=False, indent=2)
            print(f"Wrote {DECOY_MAP_PATH}")

        # gold_star_expanded (Phase 2): read real columns BEFORE injection.
        questions = dedup_questions()
        if args.limit:
            questions = [q for q in questions if q["db_id"] in db_set]
        print(f"Computing gold_star_expanded over {len(questions)} question(s) ...")
        compute_gold_star_expanded(questions)

    # --- Phase 3: inject ---
    if run_inject:
        if not DECOY_MAP_PATH.exists():
            raise SystemExit(f"{DECOY_MAP_PATH} missing — run the generate phase first.")
        decoy_map = load_json(DECOY_MAP_PATH)
        print("Injecting decoys into pg_decoy (5434) / pg_rename_decoy (5435) ...")
        inject_all(decoy_map, dbs, args.populate)

    # --- Phase 4: validate ---
    if run_validate:
        if not GOLD_STAR_PATH.exists():
            print(f"WARNING: {GOLD_STAR_PATH} missing — star questions will not be expanded.")
        questions = dedup_questions()
        if args.limit:
            questions = [q for q in questions if q["db_id"] in db_set]
        expanded = load_gold_star_expanded()
        sel_variants = tuple(v for v in VARIANTS if v in
                             {s.strip() for s in args.variants.split(",") if s.strip()})
        if not sel_variants:
            raise SystemExit(f"--variants matched nothing; choose from {VARIANTS}.")
        print(f"Re-validating R1==R2 over {len(questions)} question(s) x "
              f"{len(sel_variants)} variant(s): {', '.join(sel_variants)} ...")
        fails = revalidate(questions, expanded, sel_variants)
        if fails:
            raise SystemExit(f"ACCEPTANCE GATE FAILED: {fails} R1!=R2 failure(s). See {FAILURES_PATH}.")
        print("ACCEPTANCE GATE PASSED: 0 R1!=R2 failures.")


if __name__ == "__main__":
    main()
