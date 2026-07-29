This file is the source of truth for the [Hugging Face dataset card](https://huggingface.co/datasets/minhaozhang/BIRD_Obfuscation).
Edit it here, then copy it to the Hub, so the card cannot drift away from the dataset it describes.
Everything below the line is the card itself, frontmatter included.

---

```yaml
---
license: cc-by-sa-4.0
pretty_name: BIRD Obfuscation
language:
  - en
  - fr
  - de
  - es
  - zh
task_categories:
  - text-generation
tags:
  - text-to-sql
  - bird
  - benchmark-contamination
  - agent-evaluation
  - semantic-layer
size_categories:
  - 1K<n<10K
---
```

# BIRD Obfuscation: PostgreSQL dumps

A cleaned, contamination-resistant, adversarial rebuild of the
[BIRD](https://bird-bench.github.io/) Text-to-SQL benchmark, curated as the substrate for a
**semantic-layer** agent evaluation rather than for one-shot Text-to-SQL.

**Code, gold SQL, manifests and full documentation:
[github.com/Minhao-Zhang/BIRD-Obfuscation](https://github.com/Minhao-Zhang/BIRD-Obfuscation)**

This Hub repo holds only the four database dumps. The questions, gold SQL, rename map, trap
manifests and per-question gold-quality provenance are git-tracked in that repository under
[`eval_dataset/`](https://github.com/Minhao-Zhang/BIRD-Obfuscation/tree/main/eval_dataset).
You need both halves.

## What is in the dumps

Four PostgreSQL **custom-format** dumps (`pg_dump -Fc`, zstd-compressed), one per arm. Each dump
is a whole `bird` database containing all 69 obfuscated BIRD databases as 69 schemas plus
`public`. Real rows, columns and tables are **byte-identical across all four**; only identifiers
and the presence of decoy objects differ.

Produced with PostgreSQL 18.4. Restore into **PostgreSQL 18 or newer** with `pg_restore`.

| file | arm | port | identifiers | decoys/traps | size | TOC entries |
| --- | --- | --- | --- | --- | --- | --- |
| `pg_base.dump` | base | 5432 | original English | none | 2.93 GB | 1591 |
| `pg_rename.dump` | rename | 5433 | renamed (target language) | none | 2.93 GB | 1591 |
| `pg_decoy.dump` | decoy | 5434 | original English | corrupted traps | 3.13 GB | 1915 |
| `pg_rename_decoy.dump` | rename+decoy | 5435 | renamed | corrupted traps | 3.13 GB | 1915 |

**Identifier rename.** Table and column names are translated into one of five languages, roughly
14 databases each: English (identity, kept as a noise-floor control), French, German, Spanish and
Mandarin Pinyin. The English slot exists so a subset of databases has a guaranteed near-zero
rename effect to measure against.

**Corrupted traps.** The two decoy arms add 1,486 evil-twin columns and 162 cloned tables holding
subtly wrong copies of real data under plausible synonym names, aimed at agents that explore a
schema by running queries. Every trap is strictly **additive**, which is what keeps the
ground-truth task provably intact: obfuscated gold stays execution-equivalent to the validated
original (R0==R1 against SQLite, R1==R2 across instances) for every retained question.

The TOC delta is a useful sanity check on that claim: 1915 minus 1591 is 324, which is exactly the 162
clone tables times two entries each (`TABLE` plus `TABLE DATA`). Evil-twin columns are `ALTER`s on
existing tables and so add no TOC entries. Nothing else was injected.

## The paired question set

Numbers below describe the gold data in the GitHub repo, not these dumps.

| | |
| --- | --- |
| Databases evaluated | **57** of the 69 present in the dumps |
| Questions | **6,743** (5,392 train / 1,351 test) |
| Per-database test fraction | uniform 19.4% to 20.6% |
| Per-database corpus size | 61 to 383 questions |
| Cross-split duplicate leakage | 0.22% of the test set |

**BIRD's own gold SQL is substantially and systematically mis-annotated.** Published audits report
49% to 61% error rates depending on the split. 2,739 questions were removed from this dataset by
joining against the official
[`bird_sql_dev_20251106`](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106) (corrected
dev) and [`bird23-train-filtered`](https://huggingface.co/datasets/birdsql/bird23-train-filtered)
(filtered train) releases, after which 11 databases fell below the 60-question floor and were
dropped. A later pass removed 127 duplicate questions, taking cross-split leakage from 3.6% of
the test set to 0.22%. Per-question provenance for all 10,164 pre-purge questions, including the
corrected gold SQL for every changed row, ships as `gold_quality_flags.jsonl`, and the collapsed
duplicate clusters as `dedupe_clusters.json`. Method, evidence and citations:
[gold-quality-audit.md](https://github.com/Minhao-Zhang/BIRD-Obfuscation/blob/main/docs/reference/gold-quality-audit.md).

> [!IMPORTANT]
> **The dumps carry 69 schemas; the evaluation covers 57.** The 12 databases that fell below the
> 60-question floor were not removed from the dumps, so they are still present, fully obfuscated
> and trap-injected, but no question, gold SQL or result hash references them:
> `app_store`, `bike_share_1`, `california_schools`, `college_completion`, `computer_student`,
> `cookbook`, `debit_card_specializing`, `financial`, `music_platform_2`, `retail_world`,
> `sales_in_weather`, `software_company`.
>
> For an agent over a pooled schema lake they are either extra distractors that make routing
> genuinely harder, or wasted exploration budget. Either reading is defensible, but it sets the
> `routing_recall` denominator, so state which one you used. Drive an evaluation from
> `evaluated_dbs.json` (57); use `retained_dbs.json` (69) only to reason about what a probing agent
> can see.

## Verify

```
442C830F0FF598FAEB10AEB30291FE5A628E10079A9DCC526F32F0C72BBD274D  pg_base.dump
4A89D6853562D29825244C416877734C038E75B7A47D31287A2A87F39D24AE36  pg_rename.dump
791D5FF65CD457095E3E7FF4484F693077AA45E42F4F506C6BE09AE9DD9B0C26  pg_decoy.dump
43282AA7C497B66D0B1CCE0EB9BB574CFB7925821AE4179354221524B6F4CA28  pg_rename_decoy.dump
```

`sha256sum -c SHA256SUMS.txt` on Linux or macOS, `Get-FileHash <file> -Algorithm SHA256` in
PowerShell.

## Restore

Each dump restores into a fresh database. `--no-owner` drops the dependency on the original `bird`
role so any superuser can restore it, and `-j` restores in parallel.

Into an existing server, one target database per arm you need:

```bash
createdb bird_base
pg_restore -d bird_base --no-owner --no-privileges -j 4 pg_base.dump
```

Into a fresh container, mirroring the repo's Compose setup:

```bash
docker run -d --name pg_base -e POSTGRES_USER=bird -e POSTGRES_PASSWORD=bird \
  -e POSTGRES_DB=bird -p 5432:5432 postgres:18
docker cp pg_base.dump pg_base:/tmp/pg_base.dump
docker exec pg_base pg_restore -U bird -d bird --no-owner -j 4 /tmp/pg_base.dump
```

Repeat for the other three. **On a laptop, keep at most two instances running at once**: four
concurrent PostgreSQL containers under WSL2 can exhaust memory, and with `fsync=off` a crash can
corrupt a volume.

Post-restore sanity check: a clean arm has 569 base tables across 70 schemas, and a decoy arm has
731 (569 real plus the 162 clone tables).

```sql
SELECT count(DISTINCT table_schema), count(*) FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema');
```

## Notes

- These are **logical** dumps, portable and version-flexible, not physical volume copies, so they
  restore cleanly on any OS or architecture running PostgreSQL 18 or newer.
- The dumps carry **no indexes, primary keys or foreign keys**. That is deliberate, not an export
  artifact: the benchmark withholds an explicit foreign-key catalogue so an agent has to infer
  table relationships from column names, values and the question/SQL pairs it has seen. It also
  makes restore fast.
- Full setup, obfuscation methodology, evaluation design and known limitations are documented in
  the [GitHub repository](https://github.com/Minhao-Zhang/BIRD-Obfuscation). Read
  [limitations.md](https://github.com/Minhao-Zhang/BIRD-Obfuscation/blob/main/docs/reference/limitations.md)
  before citing any number.

## License and attribution

[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Share and adapt with credit, under
the same license. This dataset is a derivative of the
[BIRD benchmark](https://bird-bench.github.io/); please credit BIRD as the upstream source, and the
`birdsql` corrected releases linked above if you rely on the gold-quality filtering.
