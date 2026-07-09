[English](README.md) · **中文**

# 数据目录

本文件夹包含 BIRD(Benchmark for Information Retrieval from Databases)基准数据集,已在 `.gitignore` 中排除,不纳入版本控制。请从
[BIRD 基准网站](https://bird-bench.github.io/) 下载数据,按下面的结构放到这里。

## 目录结构

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

每个数据库文件夹包含:
- `<db_name>.sqlite`:SQLite 数据库文件
- `database_description/`:CSV 文件(每张表一个),含各列说明

## 文件格式

### `dev.json` / `train.json`
由问题对象组成的 JSON 数组:
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
`difficulty` 取值为 `simple`、`moderate` 或 `challenging`。

### `dev.sql` / `train_gold.sql`
每行一条标准 SQL 查询,与所属数据库 id 用制表符分隔:
```
SELECT ... FROM ...    california_schools
```

### `dev_tables.json` / `train_tables.json`
由 schema 对象组成的 JSON 数组,每个数据库对应一个对象,包含 `db_id`、
`table_names_original`、`table_names`(人类可读)、`column_names_original`、
`column_names`、`column_types`、`primary_keys` 和 `foreign_keys`。

## 数据库数量

| 划分  | 数据库 | 问题数 |
|-------|-----------|-----------|
| Dev   | 11        | 1,534     |
| Train | 73        | 9,428     |
