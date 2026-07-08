"""
Step 3: Generate schema rename map using an LLM.

For each non-english DB, calls Claude to translate all table/column names
into the assigned language. English-slot DBs keep original identifiers.

Reads:
  artifacts/retained_dbs.json
  artifacts/db_language_map.json
  data/{split}/{split}_tables.json (for schema structure)
  data/{split}/{split}_databases/<db_id>/<db_id>.sqlite (for actual table/column names)

Writes:
  artifacts/schema_rename_map.json
    {db_id: {original_identifier: translated_identifier}}

Run: uv run python pipeline/03_generate_rename_map.py
"""

import json
import re
import sqlite3
import time
from pathlib import Path

import anthropic
import os

SEED = 42  # used for consistency pass ordering only
DATA = Path("data")
ARTIFACTS = Path("artifacts")
ARTIFACTS.mkdir(exist_ok=True)

CLIENT = anthropic.AnthropicBedrock()
MODEL = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001")

LANGUAGE_NAMES = {
    "french": "French",
    "german": "German",
    "spanish": "Spanish",
    "pinyin": "Mandarin Pinyin (romanised, Latin characters only, snake_case)",
}

# Cross-database concepts checked by the consistency pass (obfuscation.md §4).
# Kept short and generic on purpose: these are identifiers likely to recur
# verbatim across unrelated domains, where a canonical translation is more
# valuable than per-DB variation. Domain-specific compounds are left to the
# per-DB translation pass, per the "domain-coherent term takes precedence"
# rule in obfuscation.md.
COMMON_CONCEPTS = [
    "id", "name", "created_at", "updated_at", "status", "type",
    "description", "code", "date", "email", "category",
]


def find_sqlite_path(db_id: str) -> Path:
    for split in ("train", "dev"):
        p = DATA / split / f"{split}_databases" / db_id / f"{db_id}.sqlite"
        if p.exists():
            return p
    raise FileNotFoundError(f"SQLite not found for {db_id}")


def get_schema_identifiers(db_id: str) -> dict[str, list[str]]:
    """Returns {table_name: [col_name, ...]} from the actual SQLite DB."""
    path = find_sqlite_path(db_id)
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]
    result = {}
    for tbl in tables:
        cur.execute(f'PRAGMA table_info("{tbl}")')
        cols = [r[1] for r in cur.fetchall()]
        result[tbl] = cols
    conn.close()
    return result


def build_translation_prompt(db_id: str, language: str,
                              schema: dict[str, list[str]]) -> str:
    lang_label = LANGUAGE_NAMES[language]
    schema_text = []
    for tbl, cols in schema.items():
        col_list = ", ".join(cols)
        schema_text.append(f"  Table: {tbl}\n  Columns: {col_list}")
    schema_block = "\n\n".join(schema_text)

    return f"""You are a database schema translator. Translate all table and column names from the database "{db_id}" into {lang_label}.

Rules:
1. Use snake_case (e.g. date_of_birth → date_de_naissance for French)
2. Choose terminology a native-speaking database designer would naturally use — not word-for-word dictionary lookup
3. The full database context is provided so you can make domain-coherent choices
4. Keep single-letter identifiers as-is (e.g. "id" can stay "id" in most languages, or use the natural equivalent)
5. Return ONLY a JSON object mapping each original identifier to its translation. Do not include any other text.
6. Table names and column names share the same flat mapping — no nesting by table.
7. If a name is already appropriate in the target language (e.g. "email", "id"), you may keep it unchanged.

Database: {db_id}

Schema:
{schema_block}

Return JSON: {{"original_name": "translated_name", ...}}"""


def call_llm(prompt: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            msg = CLIENT.messages.create(
                model=MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def extract_json(text: str) -> dict:
    """Extract JSON object from LLM response (handles markdown code blocks)."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Find first { ... } block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(text[start:end])
    raise ValueError(f"No JSON found in response: {text[:200]}")


def to_snake_case(s: str) -> str:
    """Ensure result is valid snake_case PostgreSQL identifier."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    if not s or s[0].isdigit():
        s = "col_" + s
    # PostgreSQL limit: 63 bytes
    return s[:63]


def generate_rename_map_for_db(db_id: str, language: str,
                                schema: dict[str, list[str]]) -> dict[str, str]:
    """Returns {original: translated} map. English DBs get identity map."""
    all_identifiers = list(schema.keys())
    for cols in schema.values():
        all_identifiers.extend(cols)
    # Deduplicate preserving order
    seen = set()
    unique_ids = []
    for x in all_identifiers:
        if x not in seen:
            seen.add(x)
            unique_ids.append(x)

    if language == "english":
        return {name: name for name in unique_ids}

    prompt = build_translation_prompt(db_id, language, schema)
    response = call_llm(prompt)
    raw_map = extract_json(response)

    # Normalise keys and values
    result = {}
    for orig in unique_ids:
        translated = raw_map.get(orig, orig)
        result[orig] = to_snake_case(translated)

    # Check for collisions within this DB's map values
    from collections import Counter
    val_counts = Counter(result.values())
    collisions = {v: k for k, v in result.items() if val_counts[v] > 1}
    if collisions:
        print(f"  WARNING: translation collisions in {db_id}: {collisions}")

    return result


def build_consistency_prompt(language: str, concept_choices: dict[str, dict[str, int]]) -> str:
    lang_label = LANGUAGE_NAMES[language]
    lines = []
    for concept, choices in concept_choices.items():
        options = ", ".join(f'"{v}" (used {n}x)' for v, n in sorted(choices.items(), key=lambda kv: -kv[1]))
        lines.append(f'- "{concept}": {options}')
    choices_block = "\n".join(lines)

    return f"""The following identifiers were independently translated into {lang_label} across several unrelated database schemas. Each shows the different translations already chosen and how often each was used.

{choices_block}

For each identifier, pick ONE canonical {lang_label} translation to standardise on across all databases (prefer the most natural/common term; the usage counts are a hint, not a rule). Use snake_case.

Return ONLY a JSON object mapping each identifier to its chosen canonical translation. Do not include any other text.

Return JSON: {{"id": "...", "name": "...", ...}}"""


def run_consistency_pass(rename_map: dict, lang_map: dict, dbs: list[str]) -> None:
    """Normalise translations of common cross-database concepts per language slot.

    Mutates rename_map in place. Only touches identifiers that are an exact,
    bare match for a COMMON_CONCEPTS entry — domain-coherent compounds (e.g.
    "critic_id") are untouched, per the "domain-coherent term takes
    precedence" rule in obfuscation.md §4.
    """
    by_language: dict[str, list[str]] = {}
    for db_id in dbs:
        language = lang_map.get(db_id, "english")
        if language == "english":
            continue
        by_language.setdefault(language, []).append(db_id)

    for language, lang_dbs in by_language.items():
        concept_choices: dict[str, dict[str, int]] = {}
        for db_id in lang_dbs:
            db_rename = rename_map.get(db_id, {})
            for concept in COMMON_CONCEPTS:
                if concept in db_rename:
                    translated = db_rename[concept]
                    concept_choices.setdefault(concept, {})
                    concept_choices[concept][translated] = concept_choices[concept].get(translated, 0) + 1

        # Nothing to normalise if every DB already agreed (or concept absent).
        concept_choices = {c: v for c, v in concept_choices.items() if len(v) > 1}
        if not concept_choices:
            print(f"Consistency pass ({language}): already consistent, nothing to normalise")
            continue

        print(f"Consistency pass ({language}): normalising {list(concept_choices)}")
        prompt = build_consistency_prompt(language, concept_choices)
        try:
            response = call_llm(prompt)
            canonical = extract_json(response)
        except Exception as e:
            print(f"  ERROR: consistency pass failed for {language}: {e}")
            continue

        for db_id in lang_dbs:
            db_rename = rename_map.get(db_id, {})
            for concept, canon in canonical.items():
                if concept in db_rename:
                    db_rename[concept] = to_snake_case(canon)

            # Re-check for collisions the normalisation may have introduced
            # (e.g. canonical "name" translation now matches another key's
            # existing translation within the same DB).
            from collections import Counter
            val_counts = Counter(db_rename.values())
            collisions = {v: k for k, v in db_rename.items() if val_counts[v] > 1}
            if collisions:
                print(f"  WARNING: consistency pass introduced collisions in {db_id}: {collisions}")


def main():
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)
    with open(ARTIFACTS / "db_language_map.json") as f:
        lang_map = json.load(f)

    out_path = ARTIFACTS / "schema_rename_map.json"
    # Load existing partial results if any (allows resume)
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            rename_map = json.load(f)
    else:
        rename_map = {}

    for i, db_id in enumerate(dbs):
        if db_id in rename_map:
            print(f"[{i+1}/{len(dbs)}] {db_id} — already done, skipping")
            continue

        language = lang_map.get(db_id, "english")
        print(f"[{i+1}/{len(dbs)}] {db_id} ({language})", end=" ... ", flush=True)

        try:
            schema = get_schema_identifiers(db_id)
            db_rename = generate_rename_map_for_db(db_id, language, schema)
            rename_map[db_id] = db_rename
            n = len(db_rename)
            print(f"{n} identifiers")

            # Save after each DB so progress is not lost
            with open(out_path, "w") as f:
                json.dump(rename_map, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\nAll DBs translated. Running consistency pass...")
    run_consistency_pass(rename_map, lang_map, dbs)
    with open(out_path, "w") as f:
        json.dump(rename_map, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Rename map saved to {out_path}")
    print(f"Total DBs mapped: {len(rename_map)}")


if __name__ == "__main__":
    main()
