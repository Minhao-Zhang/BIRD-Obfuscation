"""Shared helpers for step 5 transpile / validate / apply-fix scripts."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path

QUERY_TIMEOUT_SEC = 60

import psycopg2
import sqlglot
import sqlglot.expressions as exp

PG_BASE_DSN = "host=127.0.0.1 port=5432 dbname=bird user=bird password=bird"
DATA = Path("data")
WORKDIR = Path("workdir")
NEEDS_FIX_PATH = WORKDIR / "transpilation_needs_fix.jsonl"
FIXES_PATH = WORKDIR / "transpilation_fixes.jsonl"
FAILURES_PATH = WORKDIR / "transpilation_failures.jsonl"
OUTPUT_PATHS = {
    "train": WORKDIR / "train_transpiled.jsonl",
    "test": WORKDIR / "test_transpiled.jsonl",
}


def find_sqlite_path(db_id: str) -> Path:
    for split in ("train", "dev"):
        p = DATA / split / f"{split}_databases" / db_id / f"{db_id}.sqlite"
        if p.exists():
            return p
    raise FileNotFoundError(f"SQLite not found for {db_id}")


def normalise_result(rows) -> list:
    if rows is None:
        return []
    def coerce(v):
        if v is None:
            return None
        try:
            f = float(v)
            if math.isnan(f):
                return "__nan__"
            if math.isinf(f):
                return "__inf__" if f > 0 else "__neg_inf__"
            return f
        except (TypeError, ValueError):
            s = str(v).strip().lower()
            if s == "nan":
                return "__nan__"
            return s
    normalised = [tuple(coerce(c) for c in row) for row in rows]

    def _sort_key(row: tuple) -> tuple:
        key_parts = []
        for v in row:
            if v is None:
                key_parts.append((0, ""))
            elif isinstance(v, float):
                key_parts.append((1, v))
            else:
                key_parts.append((2, str(v)))
        return tuple(key_parts)

    return sorted(normalised, key=_sort_key)


def exec_sqlite(db_id: str, sql: str, timeout_sec: float = QUERY_TIMEOUT_SEC):
    path = find_sqlite_path(db_id)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = None
    holder: list = []
    err: list[BaseException] = []

    def run() -> None:
        try:
            cur = conn.cursor()
            cur.execute(sql)
            holder.append(cur.fetchall())
        except BaseException as e:
            err.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        conn.interrupt()
        thread.join(5)
        conn.close()
        raise TimeoutError(f"sqlite query exceeded {timeout_sec}s")
    conn.close()
    if err:
        raise err[0]
    return holder[0]


def exec_pg(pg_conn, db_id: str, sql: str, timeout_sec: float = QUERY_TIMEOUT_SEC):
    pg_conn.rollback()
    cur = pg_conn.cursor()
    cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_sec * 1000)}'")
    cur.execute(sql)
    return cur.fetchall()


def get_pg_schema_ddl(pg_conn, db_id: str) -> str:
    pg_conn.rollback()
    cur = pg_conn.cursor()
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        (db_id,),
    )
    tables = [r[0] for r in cur.fetchall()]
    parts = []
    for tbl in tables:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (db_id, tbl),
        )
        cols = cur.fetchall()
        col_lines = ",\n".join(
            f'    "{c}" {t}{" NOT NULL" if nullable == "NO" else ""}'
            for c, t, nullable in cols
        )
        parts.append(f'CREATE TABLE "{db_id}"."{tbl}" (\n{col_lines}\n)')
    pg_conn.rollback()
    return "\n\n".join(parts)


def _apply_pg_postprocess(stmt, db_id: str) -> None:
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if not table.args.get("db") and table.name and table.name.lower() not in cte_names:
            table.set("db", exp.Identifier(this=db_id, quoted=True))
    for ident in stmt.find_all(exp.Identifier):
        if ident.name:
            ident.set("quoted", True)


def postprocess_pg_sql(pg_sql: str, db_id: str) -> str:
    try:
        statements = sqlglot.parse(pg_sql, read="postgres")
    except Exception:
        return pg_sql
    result_parts = []
    for stmt in statements:
        if stmt is None:
            continue
        _apply_pg_postprocess(stmt, db_id)
        try:
            result_parts.append(stmt.sql(dialect="postgres"))
        except Exception:
            return pg_sql
    return "; ".join(result_parts) if result_parts else pg_sql


def transpile_sql(sqlite_sql: str, db_id: str) -> str:
    try:
        statements = sqlglot.parse(sqlite_sql, read="sqlite")
    except Exception:
        return sqlite_sql
    result_parts = []
    for stmt in statements:
        if stmt is None:
            continue
        _apply_pg_postprocess(stmt, db_id)
        try:
            pg_sql = stmt.sql(dialect="postgres")
        except Exception:
            pg_sql = sqlite_sql
        result_parts.append(pg_sql)
    return "; ".join(result_parts) if result_parts else sqlite_sql


def compare_r0_r1(db_id: str, sqlite_sql: str, pg_sql: str, pg_conn) -> tuple[bool, str | None]:
    """Return (match, pg_error). Raises on SQLite exec failure."""
    r0 = normalise_result(exec_sqlite(db_id, sqlite_sql))
    pg_error = None
    try:
        pg_conn.rollback()
        r1 = normalise_result(exec_pg(pg_conn, db_id, pg_sql))
    except Exception as e:
        pg_error = str(e)
        r1 = None
    return r0 == r1, pg_error


def load_done_ids(*paths: Path) -> set[str]:
    done: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["question_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def append_jsonl(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def transpile_status() -> dict:
    done = load_done_ids(*OUTPUT_PATHS.values(), FAILURES_PATH)
    needs = load_jsonl(NEEDS_FIX_PATH)
    pending = [r for r in needs if r["question_id"] not in done]
    return {
        "train_ok": sum(1 for _ in open(OUTPUT_PATHS["train"], encoding="utf-8")) if OUTPUT_PATHS["train"].exists() else 0,
        "test_ok": sum(1 for _ in open(OUTPUT_PATHS["test"], encoding="utf-8")) if OUTPUT_PATHS["test"].exists() else 0,
        "failures": sum(1 for _ in open(FAILURES_PATH, encoding="utf-8")) if FAILURES_PATH.exists() else 0,
        "needs_fix_total": len(needs),
        "needs_fix_pending": len(pending),
        "fixes_queued": sum(1 for _ in open(FIXES_PATH, encoding="utf-8")) if FIXES_PATH.exists() else 0,
    }
