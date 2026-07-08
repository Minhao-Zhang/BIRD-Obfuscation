"""Deterministic data-corruption operators for the "copy -> rename -> corrupt"
decoy traps.

Design + rationale: docs/reference/corrupted-decoys-design.md.

Model: a trap column is a COPY of a real *source* column, given a plausible
(renamed) name, then a fraction of its rows are corrupted. The SOURCE (real)
column is NEVER modified — traps are strictly additive, so the original stays
byte-identical and R1==R2 holds (design invariant §4). Value corruption in a
decoy column is validation-safe: gold never references decoy columns, and the
only leak path (`SELECT *`) is stripped by gold_star_expanded.

Determinism (D3): corruption is a pure function of a per-row key + a per-op salt,
via md5 hashing in SQL. Same DB + same salts => identical corruption on rebuild;
no RNG state, and PG `random()` is deliberately not used. `rand01()` returns a
float in [0,1); a row is "selected" for corruption when its selection-salted
rand01 is < pct (so exactly ~pct of rows are hit, deterministically).

Each `op_*` returns ONE SQL statement (the caller has already `ADD`ed the target
column). Operators sit on the stealth<->reliably-wrong spectrum (see the doc):
  - permute            : multiset-preserving (aggregates identical); RI-preserving
                         for keys; only traps row-level lookups/joins.  [stealthy]
  - sparse_perturb     : shifts ~pct of numeric rows.                   [medium]
  - sparse_cat_remap   : flips ~pct of rows to another in-domain value. [medium]
  - sparse_date_offset : shifts ~pct of date/timestamp rows.           [medium]
  - sparse_null        : nulls ~pct of rows.                            [mild]
"""

# --------------------------------------------------------------------------- #
# SQL literal / identifier helpers
# --------------------------------------------------------------------------- #

def sql_lit(s: str) -> str:
    """Python str -> SQL single-quoted string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def qi(ident: str) -> str:
    """Quote a SQL identifier."""
    return '"' + str(ident).replace('"', '""') + '"'


def qtable(schema: str, table: str) -> str:
    return f"{qi(schema)}.{qi(table)}"


# --------------------------------------------------------------------------- #
# Deterministic per-row pseudo-randomness
# --------------------------------------------------------------------------- #

def rand01(key_sql: str, salt: str) -> str:
    """SQL expr → deterministic float in [0,1) from a row key + salt.

    md5(key||salt) → first 8 hex chars → bit(32) → signed int → shift to
    [0, 2^32) → divide by 2^32. Stable across runs (no RNG state).
    """
    return (
        "((('x' || substr(md5((" + key_sql + ")::text || " + sql_lit(salt)
        + "), 1, 8))::bit(32)::int)::bigint + 2147483648)::float8 / 4294967296.0"
    )


# --------------------------------------------------------------------------- #
# Type classification
# --------------------------------------------------------------------------- #

_NUMERIC = {"smallint", "integer", "bigint", "numeric", "decimal", "real",
            "double precision", "int2", "int4", "int8", "float4", "float8"}
_INTEGER = {"smallint", "integer", "bigint", "int2", "int4", "int8"}
_TEMPORAL = {"date", "timestamp", "timestamp without time zone",
             "timestamp with time zone", "timestamptz"}
_TEXT = {"text", "character varying", "varchar", "character", "char", "bpchar", '"char"'}


def _base_type(pg_type: str) -> str:
    # drop length/precision modifiers: "character varying(255)" -> "character varying"
    return (pg_type or "").lower().strip().split("(")[0].strip()


def classify_type(pg_type: str) -> str:
    t = _base_type(pg_type)
    if t in _NUMERIC:
        return "numeric"
    if t in _TEMPORAL:
        return "temporal"
    if t in _TEXT or t.startswith("character"):
        return "text"
    if t in ("boolean", "bool"):
        return "bool"
    return "other"


def is_integer_type(pg_type: str) -> bool:
    return _base_type(pg_type) in _INTEGER


# int8/int4/int2 value ranges — sparse_perturb must clamp to the *target* type's
# range or `col + noise` overflows (bigint arithmetic overflows near int8 max;
# int4/int2 columns overflow on assignment even when the arithmetic fits int8).
_INT_BOUNDS = {
    "smallint": (-32768, 32767), "int2": (-32768, 32767),
    "integer": (-2147483648, 2147483647), "int4": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
    "int8": (-9223372036854775808, 9223372036854775807),
}


def int_bounds(pg_type: str):
    """(min, max) for an integer type, else None."""
    return _INT_BOUNDS.get(_base_type(pg_type))


# --------------------------------------------------------------------------- #
# Operators — each returns one SQL statement
# --------------------------------------------------------------------------- #

def op_sparse_perturb(schema, table, src, tgt, key_sql, *, pct, rel, is_int, salt,
                      bounds=None):
    """Add relative noise (±rel) to ~pct of numeric rows; rest copied exact.

    For integers the add is done in `numeric` (arbitrary precision, so `col*rel`
    can't overflow) and, when `bounds` (the target int type's min/max) is given,
    clamped into range before the cast back — otherwise a large bigint + noise
    overflows int8, or an int4/int2 column overflows on assignment."""
    t = qtable(schema, table)
    sel = rand01(key_sql, salt + ":sel")
    mag = rand01(key_sql, salt + ":mag")
    noise = f"({qi(src)} * {rel} * (2*({mag}) - 1))"
    if is_int:
        # ensure a corrupted int row actually changes: round away from zero-ish
        delta = f"(CASE WHEN {noise} >= 0 THEN ceil({noise}) ELSE floor({noise}) END)"
        raw = f"({qi(src)}::numeric + {delta})"
        if bounds:
            lo, hi = bounds
            raw = f"LEAST(GREATEST({raw}, {lo}::numeric), {hi}::numeric)"
        val = f"({raw})::bigint"
    else:
        val = f"({qi(src)} + {noise})"
    return (
        f"UPDATE {t} SET {qi(tgt)} = CASE "
        f"WHEN {qi(src)} IS NOT NULL AND ({sel}) < {pct} THEN {val} "
        f"ELSE {qi(src)} END;"
    )


def op_permute(schema, table, src, tgt, key_ref, *, salt):
    """Full cyclic-shift permutation in a hash order: every row gets another
    row's value. Multiset-preserving (aggregates identical) and RI-preserving
    for keys. `key_ref` is a raw SQL reference to a unique row key (e.g. '"id"'
    for a PK/unique column, or 'ctid' as a keyless fallback) used to join the
    shuffled values back."""
    t = qtable(schema, table)
    order = rand01(key_ref, salt)
    return (
        f"WITH o AS (SELECT {key_ref} AS k, {qi(src)} AS v, "
        f"row_number() OVER (ORDER BY {order}, {key_ref}) AS rn, "
        f"count(*) OVER () AS n FROM {t}), "
        f"s AS (SELECT a.k AS k, b.v AS nv FROM o a JOIN o b ON b.rn = (a.rn % a.n) + 1) "
        f"UPDATE {t} x SET {qi(tgt)} = s.nv FROM s WHERE x.{key_ref} = s.k;"
    )


def op_sparse_cat_remap(schema, table, src, tgt, key_sql, *, pct, salt):
    """Flip ~pct of rows to a different value drawn from the column's own
    distinct domain (stays in-domain, so no out-of-domain giveaway)."""
    t = qtable(schema, table)
    sel = rand01(key_sql, salt + ":sel")
    pick = rand01(key_sql, salt + ":pick")
    return (
        f"WITH dom AS (SELECT array_agg(DISTINCT {qi(src)}) AS vals "
        f"FROM {t} WHERE {qi(src)} IS NOT NULL) "
        f"UPDATE {t} SET {qi(tgt)} = CASE "
        f"WHEN {qi(src)} IS NOT NULL AND ({sel}) < {pct} "
        f"AND (SELECT coalesce(array_length(vals,1),0) FROM dom) > 1 "
        f"THEN (SELECT vals[1 + floor(({pick}) * array_length(vals,1))::int] FROM dom) "
        f"ELSE {qi(src)} END;"
    )


def op_sparse_date_offset(schema, table, src, tgt, key_sql, *, pct, max_days, salt):
    """Shift ~pct of date/timestamp rows by ±max_days (real date/timestamp types)."""
    t = qtable(schema, table)
    sel = rand01(key_sql, salt + ":sel")
    mag = rand01(key_sql, salt + ":mag")
    off = f"((floor(({mag}) * {2 * max_days}) - {max_days})::int * interval '1 day')"
    return (
        f"UPDATE {t} SET {qi(tgt)} = CASE "
        f"WHEN {qi(src)} IS NOT NULL AND ({sel}) < {pct} THEN {qi(src)} + {off} "
        f"ELSE {qi(src)} END;"
    )


def op_sparse_null(schema, table, src, tgt, key_sql, *, pct, salt):
    """Null out ~pct of rows; rest copied exact."""
    t = qtable(schema, table)
    sel = rand01(key_sql, salt)
    return (
        f"UPDATE {t} SET {qi(tgt)} = CASE WHEN ({sel}) < {pct} THEN NULL "
        f"ELSE {qi(src)} END;"
    )


# --------------------------------------------------------------------------- #
# Operator selection (deterministic mix of stealthy + reliable — decision 2)
# --------------------------------------------------------------------------- #

def choose_operator(pg_type: str, is_key: bool, mix_bit: int) -> str:
    """Pick an operator name for a (type, is_key) column. `mix_bit` (0/1),
    derived by the caller from a hash of the column, alternates stealthy vs
    reliable so a DB gets a mixture of trap types (decision 2).

    Keys/FK-like columns ALWAYS permute (RI-preserving; B9).
    """
    if is_key:
        return "permute"
    cat = classify_type(pg_type)
    if cat == "numeric":
        return "permute" if mix_bit == 0 else "sparse_perturb"
    if cat == "temporal":
        return "sparse_date_offset"
    if cat in ("text", "bool"):
        return "permute" if mix_bit == 0 else "sparse_cat_remap"
    return "sparse_null"  # fallback for unusual types


def build_sql(op: str, *, schema, table, src, tgt, key_ref,
              is_int=False, pg_type=None, pct=0.10, rel=0.15, max_days=180, salt=""):
    """Dispatch to the named operator and return its SQL statement.

    `key_ref` is a raw SQL reference to a unique row key: `'"id"'` for a
    PK/unique column, or `'ctid'` as a fallback for keyless tables. Pass
    `pg_type` so integer perturbation can clamp to that type's range.
    """
    if pg_type is not None:
        is_int = is_integer_type(pg_type)
    bounds = int_bounds(pg_type) if pg_type is not None else None
    if op == "permute":
        return op_permute(schema, table, src, tgt, key_ref, salt=salt)
    if op == "sparse_perturb":
        return op_sparse_perturb(schema, table, src, tgt, key_ref,
                                 pct=pct, rel=rel, is_int=is_int, salt=salt,
                                 bounds=bounds)
    if op == "sparse_cat_remap":
        return op_sparse_cat_remap(schema, table, src, tgt, key_ref, pct=pct, salt=salt)
    if op == "sparse_date_offset":
        return op_sparse_date_offset(schema, table, src, tgt, key_ref,
                                     pct=pct, max_days=max_days, salt=salt)
    if op == "sparse_null":
        return op_sparse_null(schema, table, src, tgt, key_ref, pct=pct, salt=salt)
    raise ValueError(f"unknown operator: {op}")
