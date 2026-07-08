# Data Directory

This folder contains the BIRD (Benchmark for Information Retrieval from Databases) benchmark dataset.
It is excluded from version control via `.gitignore`. Download the data from the
[BIRD benchmark website](https://bird-bench.github.io/) and place it here following the structure below.

## Directory Structure

```
data/
├── dev/
│   ├── dev.json               # Dev set questions with gold SQL and metadata
│   ├── dev.sql                # Gold SQL queries (tab-separated: SQL \t db_id)
│   ├── dev_tied_append.json   # Tied difficulty annotations for dev set
│   ├── dev_tables.json        # Schema metadata for all dev databases
│   └── dev_databases/
│       ├── california_schools/
│       │   ├── california_schools.sqlite
│       │   └── database_description/
│       │       ├── frpm.csv
│       │       ├── satscores.csv
│       │       └── schools.csv
│       ├── card_games/
│       ├── codebase_community/
│       ├── debit_card_specializing/
│       ├── european_football_2/
│       ├── financial/
│       ├── formula_1/
│       ├── student_club/
│       ├── superhero/
│       ├── thrombosis_prediction/
│       └── toxicology/
│           └── ...            # Each database follows the same pattern above
│
└── train/
    ├── train.json             # Train set questions with gold SQL and metadata
    ├── train_gold.sql         # Gold SQL queries (tab-separated: SQL \t db_id)
    ├── train_tables.json      # Schema metadata for all train databases
    └── train_databases/
        ├── address/
        ├── airline/
        ├── app_store/
        └── ...                # 73 databases total; each follows the same pattern
```

Each database folder contains:
- `<db_name>.sqlite`: the SQLite database file
- `database_description/`: CSV files (one per table) with column descriptions

## File Formats

### `dev.json` / `train.json`
A JSON array of question objects:
```json
{
  "question_id": 0,
  "db_id": "california_schools",
  "question": "What is the highest eligible free rate for K-12 students in Alameda County?",
  "evidence": "Eligible free rate for K-12 = `Free Meal Count (K-12)` / `Enrollment (K-12)`",
  "SQL": "SELECT ...",
  "difficulty": "simple"
}
```
`difficulty` is one of `simple`, `moderate`, or `challenging`.

### `dev.sql` / `train_gold.sql`
One gold SQL query per line, tab-separated from its database id:
```
SELECT ... FROM ...    california_schools
```

### `dev_tables.json` / `train_tables.json`
A JSON array of schema objects, one per database, containing `db_id`,
`table_names_original`, `table_names` (human-readable), `column_names_original`,
`column_names`, `column_types`, `primary_keys`, and `foreign_keys`.

## Database Counts

| Split | Databases | Questions |
|-------|-----------|-----------|
| Dev   | 11        | 1,534     |
| Train | 73        | 9,428     |
