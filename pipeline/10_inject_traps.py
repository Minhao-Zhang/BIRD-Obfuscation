"""Step 10: inject corrupted evil-twin TRAP columns into the decoy instances.

Design + rationale: docs/reference/corrupted-decoys-design.md. This SUPERSEDES
step 08's empty-decoy columns with populated, subtly-wrong copies of real
columns (copy -> rename -> corrupt), so an interactive execute-and-observe agent
can't dismiss decoys as empty. STRICTLY ADDITIVE: real columns are never
modified, so R1==R2 still holds and the original stays available.

Phases (--phase):
  rowcounts  exact per-table row counts from pg_base -> artifacts/table_row_counts.json
  plan       select trap source columns + operators per table -> artifacts/trap_plan.json
             (join-keys->permute [B9]; correlated allowed [rev. decision 4]; <=500k tables)
  name       cheap-LLM plausible synonym names per source, per variant/language,
             collision-checked -> artifacts/trap_manifest.json (the ground-truth manifest)
  inject     ADD COLUMN (source's exact type) + copy+corrupt, into pg_decoy (base) and/or
             pg_rename_decoy (rename). Corruption salt is VARIANT-INDEPENDENT (D2).
  all        rowcounts -> plan -> name -> inject

Validation is REUSED from step 08 (traps are additive, so the same R1==R2 gate applies):
  python pipeline/08_inject_decoys.py --phase validate --variants base   # then rename

OOM: inject one variant at a time (--variants base | rename), keeping <=2 instances hot.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import _corruption as C
import _traps as T
from _db import (
    PG_BASE_DSN, PG_RENAME_DSN, PG_DECOY_DSN, PG_RENAME_DECOY_DSN, new_connection,
)

ARTIFACTS = Path("artifacts")
RENAME_MAP_PATH = ARTIFACTS / "schema_rename_map.json"
LANG_MAP_PATH = ARTIFACTS / "db_language_map.json"
RETAINED_PATH = ARTIFACTS / "retained_dbs.json"
TRAP_PLAN_PATH = ARTIFACTS / "trap_plan.json"
TRAP_MANIFEST_PATH = ARTIFACTS / "trap_manifest.json"

SEED = 42
CLEAN_DSN = {"base": PG_BASE_DSN, "rename": PG_RENAME_DSN}
DECOY_DSN = {"base": PG_DECOY_DSN, "rename": PG_RENAME_DECOY_DSN}


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def dump_json(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def salt_for(spec) -> str:
    """Variant-independent corruption salt (English source identity) so both
    decoy instances corrupt identically (D2)."""
    return f"{SEED}:{spec['db']}.{spec['table']}.{spec['source_column']}"


# --------------------------------------------------------------------------- #
# Phase: rowcounts
# --------------------------------------------------------------------------- #

def phase_rowcounts():
    conn = new_connection(PG_BASE_DSN)
    counts = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.nspname||'.'||c.relname, "
                "(xpath('/row/cnt/text()', query_to_xml("
                "format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname), "
                "false, true, '')))[1]::text::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind='r' AND n.nspname NOT IN "
                "('pg_catalog','information_schema','pg_toast')")
            for name, cnt in cur.fetchall():
                counts[name] = int(cnt)
    finally:
        conn.close()
    dump_json(T.ROW_COUNTS_PATH, counts)
    over = sum(1 for v in counts.values() if v > T.ROW_CAP)
    print(f"rowcounts: {len(counts)} tables -> {T.ROW_COUNTS_PATH} "
          f"({over} over {T.ROW_CAP} cap will be skipped)")


# --------------------------------------------------------------------------- #
# Phase: plan
# --------------------------------------------------------------------------- #

def phase_plan(dbs, traps_per_table):
    roles = T.load_roles()
    row_counts = T.load_row_counts()
    conn = new_connection(PG_BASE_DSN)
    plan = []
    try:
        for db in dbs:
            plan.extend(T.plan_db(conn, db, roles.get(db, {}), row_counts, traps_per_table))
    finally:
        conn.close()
    dump_json(TRAP_PLAN_PATH, plan)
    ops = Counter(s["operator"] for s in plan)
    ndbs = len({s["db"] for s in plan})
    ntbl = len({(s["db"], s["table"]) for s in plan})
    print(f"plan: {len(plan)} traps over {ntbl} tables / {ndbs} dbs -> {TRAP_PLAN_PATH}")
    print(f"  operators: {dict(ops)}")
    print(f"  join-key(permute) traps: {sum(1 for s in plan if s['is_key'])}")


# --------------------------------------------------------------------------- #
# Phase: name  (cheap LLM synonym per source, per variant/language)
# --------------------------------------------------------------------------- #

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


def _real_idents(rmap_db: dict, variant: str) -> set:
    """Lowercased real identifiers to avoid colliding with (English keys for
    base, renamed values for rename)."""
    if variant == "base":
        return {k.lower() for k in rmap_db.keys()}
    return {v.lower() for v in rmap_db.values()}


def _name_prompt(language, db, items):
    lines = "\n".join(f'{i + 1}. {t}."{c}"  (type {ty})' for i, (t, c, ty) in enumerate(items))
    return (
        f"Database '{db}' is in {language}. For each numbered real column below, invent ONE "
        f"plausible ALTERNATIVE column name: a believable {language} synonym or sibling of that "
        f"column holding the SAME kind of data (e.g. release_year -> premiere_year), snake_case, "
        f"<= 60 chars, NOT equal to the original name. "
        f"Return ONLY a JSON array of exactly {len(items)} strings, in the SAME ORDER as listed "
        f"(element i is the new name for column i). No prose, no code fences.\n\n"
        f"Columns:\n{lines}"
    )


def phase_name(model, effort):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    plan = load_json(TRAP_PLAN_PATH)
    lang = load_json(LANG_MAP_PATH)
    rmap = load_json(RENAME_MAP_PATH)

    by_db = {}
    for s in plan:
        by_db.setdefault(s["db"], []).append(s)

    reasoning = model.startswith(("gpt-5", "o1", "o3", "o4"))
    manifest = []
    for db, specs in by_db.items():
        rmap_db = rmap.get(db, {})
        for variant in ("base", "rename"):
            language = "english" if variant == "base" else lang.get(db, "english")
            taken = _real_idents(rmap_db, variant)
            # LLM items: (renamed_table, renamed_source, type) so names fit the schema
            items, keys = [], []
            for s in specs:
                tbl = s["table"] if variant == "base" else rmap_db.get(s["table"], s["table"])
                col = s["source_column"] if variant == "base" else rmap_db.get(s["source_column"], s["source_column"])
                items.append((tbl, col, s["source_type"]))
                keys.append(f"{tbl}.{col}")
            prompt = _name_prompt(language, db, items)
            kwargs = dict(model=model, input=prompt,
                          instructions="You output only a strict JSON array of strings.")
            if reasoning:
                kwargs["reasoning"] = {"effort": effort}
            try:
                resp = client.responses.create(**kwargs)
                arr = json.loads(_strip_fences(resp.output_text))
                if not isinstance(arr, list):
                    arr = []
            except Exception as e:
                print(f"  [{db}/{variant}] naming failed: {e}", flush=True)
                arr = []
            # order-based, collision-safe assignment
            used, fell_back = set(), 0
            for i, s in enumerate(specs):
                col = items[i][1]
                raw = arr[i] if (i < len(arr) and isinstance(arr[i], str) and arr[i].strip()) else None
                if raw is None:
                    fell_back += 1
                nm = (raw or f"{col}_alt").strip().lower().replace(" ", "_")[:60] or "col_alt"
                cand, j = nm, 2
                while cand.lower() in taken or cand.lower() in used:
                    cand = f"{nm}_{j}"
                    j += 1
                used.add(cand.lower())
                s.setdefault("names", {})[variant] = cand
            warn = f"  !! {fell_back}/{len(specs)} FELL BACK" if fell_back else ""
            print(f"  [{db}/{variant}] named {len(specs)} traps (lang={language}){warn}", flush=True)

    for s in plan:
        manifest.append({
            "db": s["db"], "table": s["table"], "source_column": s["source_column"],
            "source_type": s["source_type"], "operator": s["operator"],
            "is_key": s["is_key"], "in_correlated_group": s.get("in_correlated_group", False),
            "salt": salt_for(s), "names": s.get("names", {}),
        })
    dump_json(TRAP_MANIFEST_PATH, manifest)
    print(f"name: wrote {len(manifest)} trap entries -> {TRAP_MANIFEST_PATH}")


# --------------------------------------------------------------------------- #
# Phase: inject
# --------------------------------------------------------------------------- #

def phase_inject(variants, pct, rel, regenerate):
    manifest = load_json(TRAP_MANIFEST_PATH)
    rmap = load_json(RENAME_MAP_PATH)
    by_db = {}
    for m in manifest:
        by_db.setdefault(m["db"], []).append(m)

    for variant in variants:
        clean = new_connection(CLEAN_DSN[variant])   # introspect key columns
        decoy = new_connection(DECOY_DSN[variant])   # apply
        added = skipped = 0
        try:
            for db, entries in by_db.items():
                rmap_db = rmap.get(db, {})
                key_cache = {}
                for m in entries:
                    if variant == "base":
                        schema, table, src = m["db"], m["table"], m["source_column"]
                    else:
                        schema = m["db"]
                        table = rmap_db.get(m["table"], m["table"])
                        src = rmap_db.get(m["source_column"], m["source_column"])
                    decoy_name = m["names"].get(variant)
                    if not decoy_name:
                        skipped += 1
                        continue
                    if table not in key_cache:
                        key_cache[table] = T.get_unique_key(clean, schema, table)
                    spec = {
                        "schema": schema, "table": table, "source_column": src,
                        "source_type": m["source_type"], "operator": m["operator"],
                        "decoy_name": decoy_name,
                    }
                    T.apply_trap(decoy, spec, key_cache[table], salt=m["salt"],
                                 pct=pct, rel=rel, regenerate=regenerate)
                    added += 1
            print(f"inject[{variant}]: {added} trap columns added, {skipped} skipped", flush=True)
        finally:
            clean.close()
            decoy.close()


# --------------------------------------------------------------------------- #
# Phase 2 — corrupted clone TABLES (plan-tables | name-tables | inject-tables)
# --------------------------------------------------------------------------- #

TRAP_TABLE_PLAN_PATH = ARTIFACTS / "trap_table_plan.json"
TRAP_TABLE_MANIFEST_PATH = ARTIFACTS / "trap_table_manifest.json"


def phase_plan_tables(dbs):
    roles = T.load_roles()
    row_counts = T.load_row_counts()
    conn = new_connection(PG_BASE_DSN)
    plan = []
    try:
        for db in dbs:
            plan.extend(T.plan_clone_tables(conn, db, roles.get(db, {}), row_counts))
    finally:
        conn.close()
    dump_json(TRAP_TABLE_PLAN_PATH, plan)
    ncols = sum(len(s["columns"]) for s in plan)
    ncorr = sum(1 for s in plan for c in s["columns"] if c["operator"])
    print(f"plan-tables: {len(plan)} clone tables over {len({s['db'] for s in plan})} dbs "
          f"({ncols} cols, {ncorr} corrupted) -> {TRAP_TABLE_PLAN_PATH}")


def _name_tables_prompt(language, db, items):
    blocks = [f'{i + 1}. table "{t}" (columns: {", ".join(cols)})' for i, (t, cols) in enumerate(items)]
    return (
        f"Database '{db}' is in {language}. For each numbered source table below, invent a plausible "
        f"SIBLING table: a believable {language} table name (snake_case, <=60 chars) distinct from the "
        f"original, AND a plausible alternative {language} name for EACH column (snake_case, same order). "
        f"Return ONLY a JSON array of {len(items)} objects in order: "
        f'[{{"table": "<name>", "columns": ["<name>", ...]}}]. No prose, no code fences.\n\n'
        f"Tables:\n" + "\n".join(blocks)
    )


def _clean_ident(s, fallback):
    nm = (s or fallback).strip().lower().replace(" ", "_")[:60]
    return nm or fallback


def phase_name_tables(model, effort):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    plan = load_json(TRAP_TABLE_PLAN_PATH)
    lang = load_json(LANG_MAP_PATH)
    rmap = load_json(RENAME_MAP_PATH)
    by_db = {}
    for s in plan:
        by_db.setdefault(s["db"], []).append(s)
    reasoning = model.startswith(("gpt-5", "o1", "o3", "o4"))
    for db, specs in by_db.items():
        rmap_db = rmap.get(db, {})
        for variant in ("base", "rename"):
            language = "english" if variant == "base" else lang.get(db, "english")
            taken_tbl = _real_idents(rmap_db, variant)
            items = []
            for s in specs:
                tname = s["source_table"] if variant == "base" else rmap_db.get(s["source_table"], s["source_table"])
                cnames = [(c["source_column"] if variant == "base" else rmap_db.get(c["source_column"], c["source_column"]))
                          for c in s["columns"]]
                items.append((tname, cnames))
            kwargs = dict(model=model, input=_name_tables_prompt(language, db, items),
                          instructions="You output only a strict JSON array of objects.")
            if reasoning:
                kwargs["reasoning"] = {"effort": effort}
            try:
                arr = json.loads(_strip_fences(client.responses.create(**kwargs).output_text))
                if not isinstance(arr, list):
                    arr = []
            except Exception as e:
                print(f"  [{db}/{variant}] table-naming failed: {e}", flush=True)
                arr = []
            used_tbl = set()
            for i, s in enumerate(specs):
                obj = arr[i] if (i < len(arr) and isinstance(arr[i], dict)) else {}
                tnm = _clean_ident(obj.get("table"), items[i][0] + "_archive")
                cand, k = tnm, 2
                while cand.lower() in taken_tbl or cand.lower() in used_tbl:
                    cand, k = f"{tnm}_{k}", k + 1
                used_tbl.add(cand.lower())
                craw = obj.get("columns") if isinstance(obj.get("columns"), list) else []
                used_col, cols_out = set(), []
                for j, c in enumerate(s["columns"]):
                    raw = craw[j] if (j < len(craw) and isinstance(craw[j], str) and craw[j].strip()) else None
                    cnm = _clean_ident(raw, items[i][1][j] + "_alt")
                    cc, k2 = cnm, 2
                    while cc.lower() in used_col:
                        cc, k2 = f"{cnm}_{k2}", k2 + 1
                    used_col.add(cc.lower())
                    cols_out.append(cc)
                s.setdefault("names", {})[variant] = {"table": cand, "columns": cols_out}
            print(f"  [{db}/{variant}] named {len(specs)} clone tables (lang={language})", flush=True)
    dump_json(TRAP_TABLE_MANIFEST_PATH, plan)
    print(f"name-tables: wrote {len(plan)} clone-table entries -> {TRAP_TABLE_MANIFEST_PATH}")


def phase_inject_tables(variants, pct, rel, regenerate):
    manifest = load_json(TRAP_TABLE_MANIFEST_PATH)
    rmap = load_json(RENAME_MAP_PATH)
    by_db = {}
    for s in manifest:
        by_db.setdefault(s["db"], []).append(s)
    for variant in variants:
        decoy = new_connection(DECOY_DSN[variant])
        made = 0
        try:
            for db, specs in by_db.items():
                rmap_db = rmap.get(db, {})
                for s in specs:
                    names = s["names"][variant]
                    src_table = s["source_table"] if variant == "base" else rmap_db.get(s["source_table"], s["source_table"])
                    col_map = []
                    for j, c in enumerate(s["columns"]):
                        sc = c["source_column"] if variant == "base" else rmap_db.get(c["source_column"], c["source_column"])
                        salt = f"{SEED}:{db}.{s['source_table']}.{c['source_column']}:clone"
                        col_map.append((sc, names["columns"][j], c["source_type"], c["operator"], salt))
                    T.apply_clone_table(decoy, db, src_table, names["table"], col_map,
                                        pct=pct, rel=rel, regenerate=regenerate)
                    made += 1
            print(f"inject-tables[{variant}]: {made} clone tables created", flush=True)
        finally:
            decoy.close()


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Step 10: inject corrupted trap columns")
    ap.add_argument("--phase", choices=["rowcounts", "plan", "name", "inject", "all",
                                        "plan-tables", "name-tables", "inject-tables", "all-tables"],
                    default="all")
    ap.add_argument("--variants", default="base,rename", help="comma list for inject (OOM: one at a time)")
    ap.add_argument("--traps-per-table", type=int, default=3)
    ap.add_argument("--pct", type=float, default=0.10, help="fraction of rows corrupted (decision 5)")
    ap.add_argument("--rel", type=float, default=0.15, help="relative magnitude for sparse_perturb")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--limit", type=int, default=None, help="first N dbs (dry run)")
    ap.add_argument("--regenerate", action="store_true", help="DROP existing trap column before re-adding")
    args = ap.parse_args()

    dbs = load_json(RETAINED_PATH)
    if args.limit:
        dbs = dbs[: args.limit]
    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())

    if args.phase in ("rowcounts", "all"):
        phase_rowcounts()
    if args.phase in ("plan", "all"):
        phase_plan(dbs, args.traps_per_table)
    if args.phase in ("name", "all"):
        from dotenv import load_dotenv
        load_dotenv()
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY not set (needed for --phase name). Copy .env.example to .env.")
        phase_name(args.model, args.effort)
    if args.phase in ("inject", "all"):
        phase_inject(variants, args.pct, args.rel, args.regenerate)

    # --- Phase 2: corrupted clone tables ---
    if args.phase in ("plan-tables", "all-tables"):
        phase_plan_tables(dbs)
    if args.phase in ("name-tables", "all-tables"):
        from dotenv import load_dotenv
        load_dotenv()
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY not set (needed for --phase name-tables).")
        phase_name_tables(args.model, args.effort)
    if args.phase in ("inject-tables", "all-tables"):
        phase_inject_tables(variants, args.pct, args.rel, args.regenerate)


if __name__ == "__main__":
    main()
