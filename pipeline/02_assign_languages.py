"""
Step 2: Assign each retained database to one of 5 schema languages.

Assignment is random with a fixed seed (deterministic).
Each language gets ~1/5 of DBs (69 / 5 ≈ 14 per language).

Reads:  artifacts/retained_dbs.json
Writes: artifacts/db_language_map.json  {db_id: language}

Run: uv run python pipeline/02_assign_languages.py
"""

import json
import random
from pathlib import Path

SEED = 42
LANGUAGES = ["english", "french", "german", "spanish", "pinyin"]

ARTIFACTS = Path("artifacts")
ARTIFACTS.mkdir(exist_ok=True)


def main():
    with open(ARTIFACTS / "retained_dbs.json") as f:
        dbs = json.load(f)  # already sorted

    n = len(dbs)
    # Build balanced language list: cycle through languages so each gets floor(n/5)
    # then distribute remainder. Using sorted assignment for pure determinism.
    base = [LANGUAGES[i % len(LANGUAGES)] for i in range(n)]
    # Shuffle with fixed seed so languages are randomly distributed across DBs
    rng = random.Random(SEED)
    rng.shuffle(base)

    db_language_map = dict(zip(dbs, base))

    # Print distribution
    from collections import Counter
    dist = Counter(db_language_map.values())
    print("Language distribution:")
    for lang in LANGUAGES:
        print(f"  {lang}: {dist[lang]}")

    out_path = ARTIFACTS / "db_language_map.json"
    with open(out_path, "w") as f:
        json.dump(db_language_map, f, indent=2)
    print(f"Written {out_path}")


if __name__ == "__main__":
    main()
