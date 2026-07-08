"""Phase-1 trap generation: evil-twin columns (copy -> rename -> corrupt).

Design + rationale: docs/reference/corrupted-decoys-design.md.

STRICTLY ADDITIVE: a trap is a NEW column whose data is a corrupted copy of a
real *source* column, given a plausible synonym name. The real column is never
modified, so real cross-column invariants stay intact and R1==R2 holds.

Inputs:
  - live schema (columns + exact types, PK/unique key) via introspection here
  - artifacts/sql_column_roles.json  (Haiku-mined: join_keys, correlated_groups)
  - artifacts/table_row_counts.json  (English-keyed "schema.table" -> row count)

Operator policy (see doc decisions):
  - join-key / FK columns  -> permute ONLY (RI-preserving; B9)
  - correlated columns     -> ALLOWED as sources (reversed decision 4; additive
                              model means B5 can't fire)
  - a deterministic mix of stealthy (permute) and reliable (sparse) operators
  - only tables with <= ROW_CAP rows are trapped (decision 6)
"""
import json
import zlib
from pathlib import Path

import _corruption as C

ARTIFACTS = Path("artifacts")
ROLES_PATH = ARTIFACTS / "sql_column_roles.json"
ROW_COUNTS_PATH = ARTIFACTS / "table_row_counts.json"

ROW_CAP = 500_000
DEFAULT_TRAPS_PER_TABLE = 3
SEED = 42


# --------------------------------------------------------------------------- #
# Artifact loaders
# --------------------------------------------------------------------------- #

def load_roles() -> dict:
    return json.load(open(ROLES_PATH, encoding="utf-8")) if ROLES_PATH.exists() else {}


def load_row_counts() -> dict:
    return json.load(open(ROW_COUNTS_PATH, encoding="utf-8")) if ROW_COUNTS_PATH.exists() else {}


# --------------------------------------------------------------------------- #
# Live schema introspection
# --------------------------------------------------------------------------- #

def get_tables(conn, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",
            (schema,))
        return [r[0] for r in cur.fetchall()]


def get_columns(conn, schema: str, table: str) -> list[tuple[str, str]]:
    """(name, exact_type) in ordinal order. Exact type via format_type so
    char(n)/varchar(n)/numeric(p,s) are replicated faithfully on the copy."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            (schema, table))
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_unique_key(conn, schema: str, table: str) -> str | None:
    """A single-column PK or unique-index column, else None (caller uses ctid)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey::int2[]) "
            "WHERE n.nspname=%s AND c.relname=%s "
            "AND (i.indisprimary OR i.indisunique) AND array_length(i.indkey::int2[],1)=1 "
            "ORDER BY i.indisprimary DESC LIMIT 1",
            (schema, table))
        r = cur.fetchone()
        return r[0] if r else None


def key_ref_for(key_col: str | None) -> str:
    return C.qi(key_col) if key_col else "ctid"


# --------------------------------------------------------------------------- #
# Planning (deterministic; no DB / no LLM)
# --------------------------------------------------------------------------- #

def _cols_for_table(role_keys: list[str], schema: str, table: str) -> set[str]:
    """Extract the column names of `schema.table` from a list of
    'schema.table.column' role entries."""
    out = set()
    prefix = f"{schema}.{table}"
    for k in role_keys:
        head, _, col = k.rpartition(".")
        if head == prefix and col:
            out.add(col)
    return out


def plan_table(db: str, schema: str, table: str, columns: list[tuple[str, str]],
               roles_db: dict, rowcount: int | None,
               traps_per_table: int = DEFAULT_TRAPS_PER_TABLE) -> list[dict]:
    """Trap specs for one table (names filled later by the LLM step). Returns []
    if the table is over ROW_CAP or has no usable columns."""
    if rowcount is not None and rowcount > ROW_CAP:
        return []
    if not columns:
        return []

    join_keys = _cols_for_table(roles_db.get("join_keys", []), schema, table)
    correlated_flat = []
    for grp in roles_db.get("correlated_groups", []):
        correlated_flat.extend(grp)
    correlated = _cols_for_table(correlated_flat, schema, table)

    # Prefer query-used columns (join keys + correlated) as higher-value traps.
    def priority(nt):
        name = nt[0]
        return 0 if (name in join_keys or name in correlated) else 1

    ranked = sorted(columns, key=lambda nt: (priority(nt), nt[0]))
    chosen = ranked[:min(traps_per_table, len(ranked))]

    specs = []
    for name, typ in chosen:
        is_key = name in join_keys
        mix = zlib.crc32(f"{db}.{schema}.{table}.{name}".encode()) & 1
        op = C.choose_operator(typ, is_key, mix)
        specs.append({
            "db": db, "schema": schema, "table": table,
            "source_column": name, "source_type": typ,
            "operator": op, "is_key": is_key,
            "in_correlated_group": name in correlated,
            # decoy_name filled by the LLM naming step
        })
    return specs


def plan_db(conn, db: str, roles_db: dict, row_counts: dict,
            traps_per_table: int = DEFAULT_TRAPS_PER_TABLE) -> list[dict]:
    """Plan traps for every eligible (<=ROW_CAP) table in a DB (schema == db)."""
    specs = []
    for table in get_tables(conn, db):
        rc = row_counts.get(f"{db}.{table}")
        cols = get_columns(conn, db, table)
        specs.extend(plan_table(db, db, table, cols, roles_db, rc, traps_per_table))
    return specs


# --------------------------------------------------------------------------- #
# Application (additive: ADD COLUMN of the source's exact type, then corrupt)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Phase 2 — corrupted clone TABLES (whole decoy tables; R1==R2-safe since gold
# never references a decoy table). A clone copies ALL columns x rows, so use a
# LOWER row cap than column traps to bound storage.
# --------------------------------------------------------------------------- #

CLONE_ROW_CAP = 50_000
CLONE_FRAC = 0.30
CLONE_MIN, CLONE_MAX = 2, 8


def plan_clone_tables(conn, db: str, roles_db: dict, row_counts: dict,
                      cap: int = CLONE_ROW_CAP, frac: float = CLONE_FRAC,
                      min_t: int = CLONE_MIN, max_t: int = CLONE_MAX) -> list[dict]:
    """Pick source tables (<= cap rows) to clone, and for each decide which
    columns to corrupt (join-keys + ~half the rest; leave others exact for a
    realistic mix). Names are filled later by the LLM step."""
    tables = get_tables(conn, db)
    eligible = [t for t in tables if 0 < row_counts.get(f"{db}.{t}", 10 ** 12) <= cap]
    if not eligible:
        return []
    n = max(min_t, min(max_t, round(frac * len(eligible))))
    ranked = sorted(eligible, key=lambda t: zlib.crc32(f"{db}.{t}:clone".encode()))
    chosen = ranked[:min(n, len(eligible))]
    jk_all = roles_db.get("join_keys", [])
    specs = []
    for t in chosen:
        cols = get_columns(conn, db, t)
        if not cols:
            continue
        jk = _cols_for_table(jk_all, db, t)
        col_plan = []
        for name, typ in cols:
            is_key = name in jk
            corrupt = is_key or (zlib.crc32(f"{db}.{t}.{name}:cc".encode()) % 2 == 0)
            op = (C.choose_operator(typ, is_key, zlib.crc32(f"{db}.{t}.{name}:mix".encode()) & 1)
                  if corrupt else None)
            col_plan.append({"source_column": name, "source_type": typ,
                             "operator": op, "is_key": is_key})
        specs.append({"db": db, "source_table": t, "columns": col_plan})
    return specs


def apply_clone_table(conn, schema: str, src_table: str, decoy_table: str,
                      col_map: list[tuple], *, pct: float = 0.10, rel: float = 0.15,
                      regenerate: bool = False) -> None:
    """Create the decoy clone (renamed cols, copied data) then corrupt the chosen
    columns in place. `col_map`: list of (src_col, decoy_col, type, op, salt);
    op=None => copied exact. The clone has no PK, so corruption keys on ctid
    (fine within a single statement; the clone is standalone/regenerable)."""
    t_dec = C.qtable(schema, decoy_table)
    with conn.cursor() as cur:
        if regenerate:
            cur.execute(f"DROP TABLE IF EXISTS {t_dec};")
        sel = ", ".join(f'{C.qi(sc)} AS {C.qi(dc)}' for (sc, dc, _ty, _op, _s) in col_map)
        cur.execute(f'CREATE TABLE {t_dec} AS SELECT {sel} FROM {C.qtable(schema, src_table)};')
        for (sc, dc, ty, op, salt) in col_map:
            if not op:
                continue
            cur.execute(C.build_sql(op, schema=schema, table=decoy_table, src=dc, tgt=dc,
                                    key_ref="ctid", pg_type=ty,
                                    pct=pct, rel=rel, salt=salt))


def apply_trap(conn, spec: dict, key_col: str | None, *, salt: str,
               pct: float = 0.10, rel: float = 0.15,
               regenerate: bool = False) -> None:
    """`salt` must be VARIANT-INDEPENDENT (derived from the English source
    identity) so pg_decoy and pg_rename_decoy corrupt identical rows the same
    way — only the identifier names differ (design invariant D2)."""
    schema, table = spec["schema"], spec["table"]
    src, tgt, typ, op = (spec["source_column"], spec["decoy_name"],
                         spec["source_type"], spec["operator"])
    t = C.qtable(schema, table)
    kref = key_ref_for(key_col)
    with conn.cursor() as cur:
        if regenerate:
            cur.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS {C.qi(tgt)};")
        cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {C.qi(tgt)} {typ};")
        cur.execute(C.build_sql(op, schema=schema, table=table, src=src, tgt=tgt,
                                key_ref=kref, pg_type=typ,
                                pct=pct, rel=rel, salt=salt))
