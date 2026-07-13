"""Shared PostgreSQL helpers for pipeline steps that execute SQL against the
BIRD schema lakes.

Consolidated from the previously duplicated copies in
07_rename_sql_and_validate.py and eval_contamination.py (which carried a "keep these
in sync" comment). Steps 08/09/ablation reuse these too.

NOTE: _transpile_helpers.py has its own exec_pg/normalise_result that are
intentionally DIFFERENT — those connections run autocommit=False and use
SET LOCAL. Do not consolidate them here.

Invariants (see AGENTS.md): DSNs default to host=127.0.0.1 (never localhost) and
are overridable per instance via PG_*_DSN env vars (see .env.example) so the eval
can point at remote Postgres / AWS RDS without code changes; new_connection issues
a plain SET statement_timeout (NOT SET LOCAL) because these connections run
autocommit=True; exec_pg uses fetchmany with a hard cap (not fetchall) to bail out
fast on runaway result sets.
"""

import hashlib
import json
import math
import numbers
import os

import psycopg2

# Load .env here (best-effort) so the DSN overrides below take effect: these
# module-level constants are read at import time, which is earlier than the eval
# scripts' own load_dotenv() call. load_dotenv is idempotent + non-overriding, so
# a later call is harmless. usecwd=True: the pipeline is run from the repo root.
try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass

# Connection strings, overridable via env (see .env.example). Defaults are the
# local docker-compose instances; set PG_*_DSN (full libpq DSN) to target remote
# Postgres / AWS RDS. Local default uses 127.0.0.1 (never localhost) deliberately
# (docs/reference/pipeline-invariants.md); a remote override naturally uses a host.
PG_BASE_DSN = os.environ.get(
    "PG_BASE_DSN", "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird")
PG_RENAME_DSN = os.environ.get(
    "PG_RENAME_DSN", "host=127.0.0.1 port=5433 dbname=bird user=bird password=bird")
PG_DECOY_DSN = os.environ.get(
    "PG_DECOY_DSN", "host=127.0.0.1 port=5434 dbname=bird user=bird password=bird")
PG_RENAME_DECOY_DSN = os.environ.get(
    "PG_RENAME_DECOY_DSN", "host=127.0.0.1 port=5435 dbname=bird user=bird password=bird")

MAX_RESULT_ROWS = 200_000
QUERY_TIMEOUT_SEC = 300  # 5 min — headroom so a slow-but-valid gold isn't dropped by a
                         # boundary timeout (makes deliverable inclusion deterministic in
                         # practice); the 200k-row cap still bounds degenerate cross-products.


class ResultSetTooLarge(Exception):
    pass


def normalise_result(rows) -> list:
    if rows is None:
        return []

    def coerce(v):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v).strip().lower()
        # Canonicalize NaN/inf so equal result sets compare equal (float('nan')
        # != float('nan') would otherwise spuriously fail R1==R2, and would
        # disagree with _transpile_helpers' comparator which does canonicalize).
        if math.isnan(f):
            return "\x00nan"
        if math.isinf(f):
            return "\x00inf" if f > 0 else "\x00-inf"
        return f

    def cell_key(v):
        # Total order across the three types coerce() can yield (None / float
        # / str) so sorted() never raises on a mixed-type column. The prior
        # `try: sorted(); except TypeError: return <unsorted>` silently fell
        # back to an order-SENSITIVE compare whenever a column mixed NULLs
        # with strings, or numeric-looking strings with words — making equal
        # result sets returned in a different row order (i.e. whenever query
        # plans differ) compare unequal. That produced spurious R1!=R2
        # mismatches (decoy validation) and could mis-grade correct
        # predictions as wrong in eval_ablation's EX scoring.
        if v is None:
            return (0, 0.0, "")
        if isinstance(v, float):
            return (1, v, "")
        return (2, 0.0, v)

    normalised = [tuple(coerce(c) for c in row) for row in rows]
    return sorted(normalised, key=lambda row: tuple(cell_key(c) for c in row))


def normalise_result_strict(rows) -> list:
    """A stricter comparator that does NOT collapse across types. Unlike
    normalise_result (which coerces every cell via float() so 1 == "1" == True ==
    Decimal('1.00')), this keeps a numeric-looking *string* ("1") distinct from a
    *number* (1) and a boolean distinct from an int, and preserves string case.
    Numbers still compare numerically (1 == 1.0 == Decimal('1.00')), and CHAR(n)
    padding still matches (leading/trailing whitespace is stripped). Reported
    alongside the lenient EX as a conservative floor, so absolute accuracy is not
    over-credited by cross-type matches. Each cell is (type_rank, num, str) — a
    total order, so sorted() never raises."""
    if rows is None:
        return []

    def scoerce(v):
        if v is None:
            return (0, 0.0, "")
        if isinstance(v, bool):
            return (1, 1.0 if v else 0.0, "")
        if isinstance(v, numbers.Number):
            f = float(v)
            if math.isnan(f):
                return (2, 0.0, "\x00nan")
            if math.isinf(f):
                return (2, 0.0, "\x00inf" if f > 0 else "\x00-inf")
            return (2, f, "")
        return (3, 0.0, str(v).strip())

    return sorted(tuple(scoerce(c) for c in row) for row in rows)


def _canonical_json(obj) -> str:
    """Stable JSON for hashing normalised result multisets.

    Tuples become lists; floats stay JSON numbers. NaN/inf never appear in
    normalise_result / normalise_result_strict outputs (they are string
    sentinels), so default json encoding is enough.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def hash_normalised_result(rows) -> str:
    """SHA-256 of the lenient ``normalise_result`` multiset (BIRD-style EX)."""
    normalised = normalise_result(rows)
    payload = _canonical_json([list(row) for row in normalised])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_normalised_result_strict(rows) -> str:
    """SHA-256 of the strict ``normalise_result_strict`` multiset."""
    normalised = normalise_result_strict(rows)
    payload = _canonical_json([list(row) for row in normalised])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def exec_pg(conn, sql: str):
    # Explicit cursor close (not just conn.rollback()) so large result-set
    # memory is released deterministically every call rather than waiting
    # on Python's GC — this loop runs ~12,000 times per connection and some
    # queries return millions of rows.
    #
    # fetchmany() with a hard cap, not fetchall(): confirmed live that a
    # missing join condition in one gold query (bike_share_1, trip x
    # weather joined on zip_code with no date match) produces a genuine
    # 19.4M-row result — no LIMIT in the SQL, so exec_pg can't know in
    # advance. fetchall() on that tries to materialize 19.4M Python tuples
    # and hangs the process at multi-GB memory. Bailing out past
    # MAX_RESULT_ROWS turns this into a fast, clean failure (routed to
    # rename_failures.jsonl) instead of an unbounded memory blowup.
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
            rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
            if len(rows) > MAX_RESULT_ROWS:
                raise ResultSetTooLarge(
                    f"result set exceeds {MAX_RESULT_ROWS} rows (likely a missing join condition)"
                )
            return rows
        finally:
            conn.rollback()


def new_connection(dsn: str, autocommit: bool = True):
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    # SET (not SET LOCAL) so it persists for the whole session under
    # autocommit — SET LOCAL is transaction-scoped and autocommit gives
    # each statement its own implicit transaction, so it would silently
    # have no effect on the next query. Without this, an unexpectedly slow
    # unindexed join (this schema has no indexes by design) can hang the
    # whole run indefinitely instead of failing one record and moving on
    # — confirmed live: repeated multi-minute-plus stalls on ordinary-sized
    # queries with no reproducible single offending record.
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{QUERY_TIMEOUT_SEC * 1000}'")
    return conn
