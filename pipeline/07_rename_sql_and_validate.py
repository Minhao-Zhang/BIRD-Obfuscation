"""
Step 7: Apply rename map to transpiled PostgreSQL SQL (R1→R2) and validate.

For each question:
  - Apply schema_rename_map to sql_base via sqlglot AST pass → sql_rename
  - Execute sql_base against pg_base → R1
  - Execute sql_rename against pg_rename → R2
  - Assert R1 == R2 (rename integrity check)

Reads:
  workdir/train_transpiled.jsonl
  workdir/test_transpiled.jsonl
  artifacts/schema_rename_map.json

Writes:
  artifacts/train_final.jsonl
  artifacts/test_final.jsonl
  workdir/rename_failures.jsonl

Run: uv run python pipeline/07_rename_sql_and_validate.py
"""

import json
import re
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from _db import (
    PG_BASE_DSN,
    PG_RENAME_DSN,
    exec_pg,
    new_connection,
    normalise_result,
)

ARTIFACTS = Path("artifacts")
WORKDIR = Path("workdir")


def rename_evidence(evidence: str, rename_map: dict) -> str:
    """Substitute column/table name occurrences in a natural-language hint.

    Longest identifiers first so a substring match (e.g. "critic" inside
    "critic_likes") can't fire before the word-boundary regex for the
    longer name is tried — \\b already prevents cross-token clobbering,
    ordering just avoids doing the shorter replacement inside a longer
    identifier's own text if the translation itself contains word chars.
    """
    if not evidence:
        return evidence
    for orig in sorted(rename_map, key=len, reverse=True):
        translated = rename_map[orig]
        if translated == orig:
            continue
        evidence = re.sub(rf"\b{re.escape(orig)}\b", translated, evidence)
    return evidence


def rename_sql(pg_sql: str, db_id: str, rename_map: dict) -> str:
    """Apply rename_map to identifier nodes in a PostgreSQL SQL string.

    Some VALUES-materialization fallbacks from step 5 pass 2 embed an
    entire result set as literal SQL (up to ~128MB observed, e.g. a
    million-row donor table dumped as `VALUES (...), (...), ...`), which
    makes sqlglot.parse() extremely slow/memory-hungry — enough to hang the
    process and exhaust system memory on the largest observed case. Every
    identifier this pipeline emits is double-quoted (see AGENTS.md
    "All identifiers are quoted when emitting SQL"), so a rename can only
    ever fire on a `"key"` occurrence. Plain substring matching (without
    quotes) is NOT safe here — literal VALUES data routinely contains
    coincidental substring hits (e.g. a "san francisco..." address string
    containing "id"/"city"/"station" fragments) that would defeat the
    short-circuit for exactly the huge queries it needs to protect. Skip
    parsing entirely when no rename-map key appears in its quoted form
    anywhere in the raw text.
    """
    if not any(f'"{k}"' in pg_sql for k in rename_map):
        return pg_sql

    try:
        statements = sqlglot.parse(pg_sql, read="postgres")
    except Exception:
        return pg_sql

    result_parts = []
    for stmt in statements:
        if stmt is None:
            continue
        # Collect nodes to rename before mutating: stmt.walk() is a live
        # generator over the tree, and node.set() below creates a new
        # Identifier subtree that the walker then descends into and
        # revisits indefinitely if mutated mid-iteration (confirmed by
        # direct reproduction — hangs and grows unbounded memory even on
        # a trivial identity rename). Collecting first, then mutating
        # after the walk completes, avoids the loop entirely.
        #
        # An exp.Identifier that is a Table's `db` (schema-qualifier) arg
        # must never be renamed even if its text matches a rename-map
        # key — e.g. db_id "superhero" is also a table renamed to
        # "superheld" in that DB's own map, and without this guard the
        # schema qualifier "superhero"."superheld" gets corrupted into
        # "superheld"."superheld", which doesn't exist. Only rename an
        # Identifier when it's the `this`-arg of its parent (covers real
        # column/table/alias names); skip it when it's the parent's
        # `db`/`catalog` arg.
        to_rename = []
        for node in stmt.walk():
            if isinstance(node, exp.Identifier):
                parent = node.parent
                if parent is not None and parent.args.get("db") is node:
                    continue
                if parent is not None and parent.args.get("catalog") is node:
                    continue
            if isinstance(node, (exp.Table, exp.Column, exp.Identifier)) and node.name in rename_map:
                to_rename.append(node)
        for node in to_rename:
            node.set("this", exp.Identifier(this=rename_map[node.name], quoted=True))
        try:
            result_parts.append(stmt.sql(dialect="postgres"))
        except Exception:
            result_parts.append(pg_sql)
    return "; ".join(result_parts) if result_parts else pg_sql


def process_file(input_path: Path, output_path: Path,
                 fail_path: Path, pg_orig, pg_obfusc,
                 all_rename_maps: dict, label: str):
    with open(input_path, encoding="utf-8") as f:
        questions = [json.loads(line) for line in f]

    done_ids = set()
    if output_path.exists():
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                done_ids.add(json.loads(line)["question_id"])
    if fail_path.exists():
        with open(fail_path, encoding="utf-8") as f:
            for line in f:
                done_ids.add(json.loads(line)["question_id"])

    remaining = [q for q in questions if q["question_id"] not in done_ids]
    print(f"\n{label}: {len(questions)} total, {len(done_ids)} done, {len(remaining)} remaining")

    ok = fail = 0
    out_f = open(output_path, "a", encoding="utf-8")
    fail_f = open(fail_path, "a", encoding="utf-8")

    for i, q in enumerate(remaining):
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(remaining)} ok={ok} fail={fail}")

        db_id = q["db_id"]
        pg_sql = q["sql_base"]
        rename_map = all_rename_maps.get(db_id, {})

        sql_rename = rename_sql(pg_sql, db_id, rename_map)

        try:
            r1_raw = exec_pg(pg_orig, pg_sql)
            r1 = normalise_result(r1_raw)
        except Exception as e:
            fail_f.write(json.dumps({**q, "error": f"r1_exec_failed: {e}"},
                                     ensure_ascii=False) + "\n")
            fail += 1
            continue

        try:
            r2_raw = exec_pg(pg_obfusc, sql_rename)
            r2 = normalise_result(r2_raw)
        except Exception as e:
            fail_f.write(json.dumps({**q, "sql_rename": sql_rename,
                                      "error": f"r2_exec_failed: {e}"},
                                     ensure_ascii=False) + "\n")
            fail += 1
            continue

        if r1 != r2:
            fail_f.write(json.dumps({**q, "sql_rename": sql_rename,
                                      "error": "r1_r2_mismatch"},
                                     ensure_ascii=False) + "\n")
            fail += 1
            continue

        record = {
            "question_id": q["question_id"],
            "db_id": q["db_id"],
            "question": q["question"],
            "evidence": q.get("evidence", ""),
            "evidence_rename": rename_evidence(q.get("evidence", ""), rename_map),
            "difficulty": q.get("difficulty", ""),
            "sql_sqlite": q["sql_sqlite"],
            "sql_base": pg_sql,
            "sql_rename": sql_rename,
        }
        out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
        ok += 1

    out_f.close()
    fail_f.close()
    print(f"  {label} done: ok={ok}, fail={fail}")


def main():
    with open(ARTIFACTS / "schema_rename_map.json", encoding="utf-8") as f:
        all_rename_maps = json.load(f)

    pg_orig = new_connection(PG_BASE_DSN)
    pg_obfusc = new_connection(PG_RENAME_DSN)

    fail_path = WORKDIR / "rename_failures.jsonl"

    process_file(
        WORKDIR / "train_transpiled.jsonl",
        ARTIFACTS / "train_final.jsonl",
        fail_path, pg_orig, pg_obfusc, all_rename_maps, "train"
    )
    process_file(
        WORKDIR / "test_transpiled.jsonl",
        ARTIFACTS / "test_final.jsonl",
        fail_path, pg_orig, pg_obfusc, all_rename_maps, "test"
    )

    pg_orig.close()
    pg_obfusc.close()
    print("\nStep 7 complete.")


if __name__ == "__main__":
    main()
