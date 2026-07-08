"""
Step 3b (advisory, non-blocking): cross-check schema_rename_map.json translations
against BIRD's own column_description/value_description CSVs.

BIRD ships a data/{split}/{split}_databases/<db_id>/database_description/<table>.csv
per table with columns: original_column_name, column_name, column_description,
data_format, value_description. These are a human-authored, independent source
of what a column actually means — useful for catching a translation that is
syntactically fine but semantically off (e.g. a generic dictionary translation
that ignores the domain, which 03_generate_rename_map.py's full-schema-context
prompting is meant to avoid but can still miss).

This does not gate the pipeline: a flagged translation is a suggestion for
manual review, not a validated bug like an R0==R1/R1==R2 mismatch. Nothing
downstream reads this file automatically.

Reads:
  artifacts/retained_dbs.json
  artifacts/db_language_map.json
  artifacts/schema_rename_map.json
  data/{split}/{split}_databases/<db_id>/database_description/*.csv

Writes:
  artifacts/translation_quality_flags.jsonl
    {db_id, table, original_column, translated_column, description, concern}

Run: uv run python pipeline/03b_check_translation_quality.py
"""

import csv
import json
import os
import re
import time
from pathlib import Path

import anthropic

DATA = Path("data")
ARTIFACTS = Path("artifacts")

CLIENT = anthropic.AnthropicBedrock()
MODEL = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001")

LANGUAGE_NAMES = {
    "french": "French",
    "german": "German",
    "spanish": "Spanish",
    "pinyin": "Mandarin Pinyin (romanised)",
}


def find_description_dir(db_id: str) -> Path | None:
    for split in ("train", "dev"):
        p = DATA / split / f"{split}_databases" / db_id / "database_description"
        if p.exists():
            return p
    return None


def load_descriptions(desc_dir: Path) -> dict[str, dict[str, dict]]:
    """Returns {table_csv_stem: {original_column_name: {description, value_description}}}."""
    result = {}
    for csv_path in desc_dir.glob("*.csv"):
        rows = {}
        with open(csv_path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                orig = (row.get("original_column_name") or "").strip()
                if not orig:
                    continue
                rows[orig] = {
                    "description": (row.get("column_description") or "").strip(),
                    "value_description": (row.get("value_description") or "").strip(),
                }
        result[csv_path.stem] = rows
    return result


def match_table_descriptions(table: str, all_desc: dict[str, dict]) -> dict | None:
    """Match a SQLite table name to a description CSV by exact then case-insensitive stem."""
    if table in all_desc:
        return all_desc[table]
    lower_map = {k.lower(): v for k, v in all_desc.items()}
    return lower_map.get(table.lower())


def build_check_prompt(db_id: str, language: str, entries: list[dict]) -> str:
    lang_label = LANGUAGE_NAMES.get(language, language)
    lines = []
    for e in entries:
        desc = e["description"] or "(no description)"
        lines.append(
            f'- table "{e["table"]}", column "{e["original"]}" -> "{e["translated"]}"\n'
            f'  description: {desc}'
        )
    entries_block = "\n".join(lines)

    return f"""You are reviewing machine translations of database column names from English to {lang_label}, for the database "{db_id}". Each entry shows the original English column name, its {lang_label} translation, and a human-written description of what the column actually means.

Flag ONLY translations that are semantically wrong or misleading given the description — not stylistic preferences, not translations that are merely a bit generic. A flag should represent a translation a native-speaking database designer would consider actually incorrect once they read the description.

Entries:
{entries_block}

Return ONLY a JSON array of flagged entries, each as {{"original": "...", "translated": "...", "concern": "one-sentence explanation"}}. If nothing is flagged, return an empty array []. Do not include any other text."""


def call_llm(prompt: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            msg = CLIENT.messages.create(
                model=MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def extract_json(text: str) -> list:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    start = text.find("[")
    end = text.rfind("]") + 1
    if start != -1 and end > start:
        return json.loads(text[start:end])
    raise ValueError(f"No JSON array found in response: {text[:200]}")


def check_db(db_id: str, language: str, rename_map: dict) -> list[dict]:
    """Returns a list of flag records for this db_id. Empty if nothing to check or flag."""
    if language == "english":
        return []  # identity map, nothing translated to check

    desc_dir = find_description_dir(db_id)
    if desc_dir is None:
        return []

    all_desc = load_descriptions(desc_dir)

    # Reconstruct per-table original->translated pairs with descriptions.
    # rename_map is a flat {original: translated} dict shared across all tables
    # in the db (table and column names share one key space per obfuscation.md
    # §4), so we look up each CSV's columns against it directly.
    entries = []
    for table_stem, cols in all_desc.items():
        for orig_col, meta in cols.items():
            translated = rename_map.get(orig_col)
            if translated is None or translated == orig_col:
                continue
            if not meta["description"]:
                continue  # nothing to cross-check against
            entries.append({
                "table": table_stem,
                "original": orig_col,
                "translated": translated,
                "description": meta["description"],
            })

    if not entries:
        return []

    prompt = build_check_prompt(db_id, language, entries)
    response = call_llm(prompt)
    flagged = extract_json(response)

    flags = []
    for f in flagged:
        # Recover the table/description for this flagged column from entries,
        # since the LLM only echoes back original/translated/concern.
        match = next((e for e in entries if e["original"] == f.get("original")), None)
        flags.append({
            "db_id": db_id,
            "table": match["table"] if match else None,
            "original_column": f.get("original"),
            "translated_column": f.get("translated"),
            "description": match["description"] if match else None,
            "concern": f.get("concern"),
        })
    return flags


def main():
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)
    with open(ARTIFACTS / "db_language_map.json") as f:
        lang_map = json.load(f)
    with open(ARTIFACTS / "schema_rename_map.json", encoding="utf-8") as f:
        all_rename_maps = json.load(f)

    out_path = ARTIFACTS / "translation_quality_flags.jsonl"

    # Resume support: skip DBs already checked.
    done_dbs = set()
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                done_dbs.add(json.loads(line)["db_id"])

    out_f = open(out_path, "a", encoding="utf-8")
    total_flags = 0
    for i, db_id in enumerate(dbs):
        if db_id in done_dbs:
            print(f"[{i+1}/{len(dbs)}] {db_id} — already checked, skipping")
            continue

        language = lang_map.get(db_id, "english")
        print(f"[{i+1}/{len(dbs)}] {db_id} ({language})", end=" ... ", flush=True)
        try:
            flags = check_db(db_id, language, all_rename_maps.get(db_id, {}))
            if flags:
                for flag in flags:
                    out_f.write(json.dumps(flag, ensure_ascii=False) + "\n")
                total_flags += len(flags)
                print(f"{len(flags)} flagged")
            else:
                # Write a marker record so resume logic can skip this db_id
                # next run even though it produced no flags.
                out_f.write(json.dumps({"db_id": db_id, "flags": 0}, ensure_ascii=False) + "\n")
                print("clean")
            out_f.flush()
        except Exception as e:
            print(f"ERROR: {e}")

    out_f.close()
    print(f"\nDone. {total_flags} translations flagged for manual review across {len(dbs)} DBs.")
    print(f"Results written to {out_path}")
    print("This check is advisory — it does not modify schema_rename_map.json or block the pipeline.")


if __name__ == "__main__":
    main()
