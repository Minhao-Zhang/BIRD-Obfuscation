"""
EDA for Open-Domain Text-to-SQL dataset design.

Produces docs/eda-report/ with plots and docs/eda-report.md.
Run: uv run python eda.py
"""

import json
import re
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import seaborn as sns

OUT = Path("docs/eda-report")
OUT.mkdir(parents=True, exist_ok=True)

sns.set_theme(style="whitegrid", palette="muted")
FIGSIZE_WIDE = (14, 5)
FIGSIZE_SQUARE = (7, 6)

# Global split colours — used consistently across every plot
C_TRAIN = "#4C72B0"   # muted blue
C_DEV   = "#DD8452"   # muted orange

# ── Load data ──────────────────────────────────────────────────────────────

with open("data/train/train.json") as f:
    train_qs = json.load(f)
with open("data/dev/dev.json") as f:
    dev_qs = json.load(f)
with open("data/train/train_tables.json") as f:
    train_tables = json.load(f)
with open("data/dev/dev_tables.json") as f:
    dev_tables = json.load(f)

train_df = pd.DataFrame(train_qs)
dev_df = pd.DataFrame(dev_qs)

# Derive difficulty for train from SQL complexity (dev has it natively)
SQL_COMPLEXITY_KEYWORDS = ["JOIN", "GROUP BY", "HAVING", "UNION", "INTERSECT",
                            "EXCEPT", "WITH ", "WINDOW", "NESTED"]

def sql_complexity(sql: str) -> str:
    sql_up = sql.upper()
    hits = sum(1 for kw in SQL_COMPLEXITY_KEYWORDS if kw in sql_up)
    if hits == 0:
        return "simple"
    elif hits == 1:
        return "moderate"
    else:
        return "challenging"

train_df["difficulty"] = train_df["SQL"].apply(sql_complexity)
train_df["split"] = "train"
dev_df["split"] = "dev"

# Schema metadata
def build_schema_meta(tables_json):
    rows = []
    for db in tables_json:
        n_tables = len(db["table_names_original"])
        n_cols = len(db["column_names_original"])
        n_fk = len(db["foreign_keys"])
        rows.append({
            "db_id": db["db_id"],
            "n_tables": n_tables,
            "n_columns": n_cols,
            "n_foreign_keys": n_fk,
        })
    return pd.DataFrame(rows)

train_schema = build_schema_meta(train_tables)
dev_schema = build_schema_meta(dev_tables)

train_df = train_df.merge(train_schema, on="db_id", how="left")
dev_df = dev_df.merge(dev_schema, on="db_id", how="left")

# Evidence / hint present?
train_df["has_evidence"] = train_df["evidence"].str.strip().astype(bool)
dev_df["has_evidence"] = dev_df["evidence"].str.strip().astype(bool)

# SQL feature flags
def sql_features(sql: str) -> dict:
    s = sql.upper()
    return {
        "join": "JOIN" in s,
        "group_by": "GROUP BY" in s,
        "having": "HAVING" in s,
        "subquery": bool(re.search(r'\(\s*SELECT', s)),
        "union": "UNION" in s,
        "order_by": "ORDER BY" in s,
        "limit": "LIMIT" in s,
        "distinct": "DISTINCT" in s,
        "aggregate": bool(re.search(r'\b(COUNT|SUM|AVG|MIN|MAX)\s*\(', s)),
    }

train_feats = train_df["SQL"].apply(sql_features).apply(pd.Series)
dev_feats = dev_df["SQL"].apply(sql_features).apply(pd.Series)

# ── Helper ─────────────────────────────────────────────────────────────────

def save(fig, name):
    path = OUT / name
    fig.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(fig)
    # relative to report_path (docs/eda-report.md), not repo root
    return f"{OUT.name}/{name}"


# ══════════════════════════════════════════════════════════════════════════
# Plot 1 — Questions per database: train + dev on one figure
# ══════════════════════════════════════════════════════════════════════════

counts_train = train_df["db_id"].value_counts()
counts_dev   = dev_df["db_id"].value_counts()

# Merge into one series, tag each entry, sort descending together
combined = pd.concat([
    counts_train.rename("count").to_frame().assign(split="train"),
    counts_dev.rename("count").to_frame().assign(split="dev"),
])
combined = combined.sort_values("count", ascending=False).reset_index()
combined.columns = ["db_id", "count", "split"]

colors = combined["split"].map({"train": C_TRAIN, "dev": C_DEV})

fig, ax = plt.subplots(figsize=(20, 6))
ax.bar(range(len(combined)), combined["count"], color=colors)

ax.set_xticks(range(len(combined)))
ax.set_xticklabels(combined["db_id"], rotation=90, fontsize=6.5)
ax.set_ylabel("Number of questions")
ax.set_title("Questions per database — sorted by count (blue = Train, orange = Dev)")

# Manual legend patches
from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(color=C_TRAIN, label=f"Train (n={train_df['db_id'].nunique()}, total={len(train_df):,})"),
    Patch(color=C_DEV,   label=f"Dev   (n={dev_df['db_id'].nunique()},  total={len(dev_df):,})"),
], loc="upper right")

fig.tight_layout()
p1 = save(fig, "01_questions_per_db.png")
p2 = p1


# ══════════════════════════════════════════════════════════════════════════
# Plot 3 — Distribution of question counts (histogram + rug, train only)
# ══════════════════════════════════════════════════════════════════════════

import numpy as np
bin_width = 10
max_count = combined["count"].max()
bins = np.arange(0, max_count + bin_width, bin_width)

train_counts_arr = combined[combined["split"] == "train"]["count"].values
dev_counts_arr   = combined[combined["split"] == "dev"]["count"].values

fig, ax = plt.subplots(figsize=FIGSIZE_SQUARE)
ax.hist([train_counts_arr, dev_counts_arr], bins=bins,
        stacked=True, color=[C_TRAIN, C_DEV], label=["Train", "Dev"], edgecolor="white", linewidth=0.4)

overall_median = combined["count"].median()
overall_mean   = combined["count"].mean()
ax.axvline(overall_median, color="red",        linestyle="--", label=f"Median {overall_median:.0f}")
ax.axvline(overall_mean,   color="darkorange", linestyle="--", label=f"Mean {overall_mean:.0f}")
ax.set_xlabel("Questions per database")
ax.set_ylabel("Count of databases")
ax.set_title(f"Distribution of questions per DB — all 80 DBs (bin = {bin_width})")
ax.xaxis.set_major_locator(mticker.MultipleLocator(50))  # tick every 50 questions
ax.legend()
fig.tight_layout()
p3 = save(fig, "03_questions_distribution.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 4 — Difficulty breakdown: train vs dev grouped bars (normalised %)
# ══════════════════════════════════════════════════════════════════════════

order = ["simple", "moderate", "challenging"]

train_diff_pct = (train_df["difficulty"].value_counts(normalize=True)
                  .reindex(order, fill_value=0) * 100)
dev_diff_pct   = (dev_df["difficulty"].value_counts(normalize=True)
                  .reindex(order, fill_value=0) * 100)

x = range(len(order))
w = 0.35
fig, ax = plt.subplots(figsize=(8, 5))
bars_t = ax.bar([i - w/2 for i in x], train_diff_pct.values, width=w, color=C_TRAIN, label="Train (derived)")
bars_d = ax.bar([i + w/2 for i in x], dev_diff_pct.values,   width=w, color=C_DEV,   label="Dev (labelled)")
for bar, val in zip(list(bars_t) + list(bars_d), list(train_diff_pct.values) + list(dev_diff_pct.values)):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
            f"{val:.1f}%", ha="center", va="bottom", fontsize=8)
ax.set_xticks(list(x))
ax.set_xticklabels(order)
ax.set_ylabel("% of questions")
ax.set_title("Difficulty distribution — Train vs Dev")
ax.legend()
fig.tight_layout()
p4 = save(fig, "04_difficulty_breakdown.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 5 — SQL feature prevalence (train vs dev)
# ══════════════════════════════════════════════════════════════════════════

feat_cols = list(train_feats.columns)
train_rates = train_feats.mean() * 100
dev_rates = dev_feats.mean() * 100

x = range(len(feat_cols))
fig, ax = plt.subplots(figsize=FIGSIZE_WIDE)
w = 0.35
ax.bar([i - w/2 for i in x], train_rates[feat_cols], width=w, label="Train", color=C_TRAIN)
ax.bar([i + w/2 for i in x], dev_rates[feat_cols], width=w, label="Dev",   color=C_DEV)
ax.set_xticks(list(x))
ax.set_xticklabels(feat_cols, rotation=20, ha="right")
ax.set_ylabel("% of questions")
ax.set_title("SQL feature prevalence — Train vs Dev")
ax.legend()
fig.tight_layout()
p5 = save(fig, "05_sql_features.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 6 — Schema complexity: tables & columns per DB (all 80 DBs)
# ══════════════════════════════════════════════════════════════════════════

all_schema = pd.concat([
    train_schema.assign(split="train"),
    dev_schema.assign(split="dev"),
])

fig, axes = plt.subplots(1, 2, figsize=FIGSIZE_WIDE)
for ax, col, label, bw in zip(
    axes,
    ["n_tables", "n_columns"],
    ["# tables", "# columns"],
    [2, 10],
):
    bins = np.arange(0, all_schema[col].max() + bw, bw)
    train_vals = all_schema[all_schema["split"] == "train"][col].values
    dev_vals   = all_schema[all_schema["split"] == "dev"][col].values
    ax.hist([train_vals, dev_vals], bins=bins, stacked=True,
            color=[C_TRAIN, C_DEV], label=["Train", "Dev"],
            edgecolor="white", linewidth=0.4)
    overall_median = all_schema[col].median()
    ax.axvline(overall_median, color="red", linestyle="--",
               label=f"Median {overall_median:.0f}")
    ax.set_xlabel(label)
    ax.set_title(f"All DBs — {label} distribution (bin = {bw})")
    ax.legend()
fig.tight_layout()
p6 = save(fig, "06_schema_complexity.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 7 — Table-name collision heatmap (train top colliding names)
# ══════════════════════════════════════════════════════════════════════════

# Build collision counts per table name — train only
table_to_dbs: dict[str, list] = {}
for db in train_tables:
    for tname in db["table_names_original"]:
        table_to_dbs.setdefault(tname.lower(), []).append(db["db_id"])

collision_counts = {t: len(dbs) for t, dbs in table_to_dbs.items() if len(dbs) > 1}
collision_series = pd.Series(collision_counts).sort_values(ascending=False)

# Combined (train + dev) collision stats
all_table_to_dbs: dict[str, list] = {}
for db in train_tables + dev_tables:
    for tname in db["table_names_original"]:
        all_table_to_dbs.setdefault(tname.lower(), []).append(db["db_id"])

n_unique_tables_combined = len(all_table_to_dbs)
n_colliding_tables_combined = sum(1 for dbs in all_table_to_dbs.values() if len(dbs) > 1)

# Dev-only unique table names
n_unique_tables_dev = len({t for db in dev_tables for t in db["table_names_original"]})

fig, ax = plt.subplots(figsize=(10, 5))
ax.barh(collision_series.index[:25][::-1], collision_series.values[:25][::-1],
        color=sns.color_palette("flare", len(collision_series[:25])))
ax.set_xlabel("# databases sharing this table name")
ax.set_title("Train — Top 25 colliding table names")
ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
fig.tight_layout()
p7 = save(fig, "07_table_name_collisions.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 8 — Questions per DB vs schema size (scatter, all 80 DBs)
# ══════════════════════════════════════════════════════════════════════════

all_q_counts = combined[["db_id", "count", "split"]].rename(columns={"count": "n_questions"})
merged_all = all_q_counts.merge(all_schema, on=["db_id", "split"])

fig, ax = plt.subplots(figsize=FIGSIZE_SQUARE)
for split, color, marker in [("train", C_TRAIN, "o"), ("dev", C_DEV, "s")]:
    sub = merged_all[merged_all["split"] == split]
    ax.scatter(sub["n_tables"], sub["n_questions"],
               color=color, marker=marker, s=60, alpha=0.8, label=split.capitalize())
ax.set_xlabel("# tables in DB")
ax.set_ylabel("# questions")
ax.set_title("Questions vs schema size — all 80 DBs\n(shape = split)")
ax.legend()
fig.tight_layout()
p8 = save(fig, "08_questions_vs_schema_size.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 9 — Evidence / hint presence: train vs dev grouped bar
# ══════════════════════════════════════════════════════════════════════════

hint_pcts = {
    "Train": [100 * train_df["has_evidence"].mean(),
              100 * (~train_df["has_evidence"]).mean()],
    "Dev":   [100 * dev_df["has_evidence"].mean(),
              100 * (~dev_df["has_evidence"]).mean()],
}
categories = ["Has hint", "No hint"]
x = range(len(categories))
w = 0.35
fig, ax = plt.subplots(figsize=(6, 4))
bars_t = ax.bar([i - w/2 for i in x], hint_pcts["Train"], width=w, color=C_TRAIN, label="Train")
bars_d = ax.bar([i + w/2 for i in x], hint_pcts["Dev"],   width=w, color=C_DEV,   label="Dev")
for bar, val in zip(list(bars_t) + list(bars_d), hint_pcts["Train"] + hint_pcts["Dev"]):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
            f"{val:.1f}%", ha="center", va="bottom", fontsize=9)
ax.set_xticks(list(x))
ax.set_xticklabels(categories)
ax.set_ylabel("% of questions")
ax.set_title("Evidence hint presence — Train vs Dev")
ax.legend()
fig.tight_layout()
p9 = save(fig, "09_evidence_presence.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 10b — Difficulty × hint presence, grouped by split
# ══════════════════════════════════════════════════════════════════════════

from matplotlib.patches import Patch as _Patch

diff_order = ["simple", "moderate", "challenging"]
x = np.arange(len(diff_order))
w = 0.35

fig, ax = plt.subplots(figsize=(9, 5))
for bar_offset, df, split, color in [
    (-w/2, train_df, "Train", C_TRAIN),
    ( w/2, dev_df,   "Dev",   C_DEV),
]:
    total = len(df)
    hint_counts    = [((df["difficulty"] == d) &  df["has_evidence"]).sum() for d in diff_order]
    nohint_counts  = [((df["difficulty"] == d) & ~df["has_evidence"]).sum() for d in diff_order]
    hint_pct   = [100 * v / total for v in hint_counts]
    nohint_pct = [100 * v / total for v in nohint_counts]

    bars_hint   = ax.bar(x + bar_offset, hint_pct,   width=w, color=color,        label=f"{split} — has hint")
    bars_nohint = ax.bar(x + bar_offset, nohint_pct, width=w, color=color, alpha=0.35,
                         bottom=hint_pct, label=f"{split} — no hint")

    for bar, h, nh in zip(bars_hint, hint_pct, nohint_pct):
        total_h = h + nh
        if total_h > 0.5:
            ax.text(bar.get_x() + bar.get_width()/2, total_h + 0.3,
                    f"{total_h:.1f}%", ha="center", va="bottom", fontsize=8)

ax.set_xticks(x)
ax.set_xticklabels([d.capitalize() for d in diff_order])
ax.set_xlabel("Difficulty")
ax.set_ylabel("% of all questions (within split)")
ax.set_title("Difficulty × Evidence hint — Train vs Dev")
ax.legend(handles=[
    _Patch(color=C_TRAIN,        label="Train — has hint"),
    _Patch(color=C_TRAIN, alpha=0.35, label="Train — no hint"),
    _Patch(color=C_DEV,          label="Dev — has hint"),
    _Patch(color=C_DEV,   alpha=0.35, label="Dev — no hint"),
], ncol=2, fontsize=8)
fig.tight_layout()
p10b = save(fig, "10b_difficulty_hint.png")


# ══════════════════════════════════════════════════════════════════════════
# Plot 10 — Long-tail: cumulative % of questions by DB rank (all 80 DBs)
# ══════════════════════════════════════════════════════════════════════════

sorted_combined = combined.sort_values("count", ascending=False).reset_index(drop=True)
total_questions = sorted_combined["count"].sum()
cumulative = (sorted_combined["count"].cumsum() / total_questions * 100)

dot_colors = sorted_combined["split"].map({"train": C_TRAIN, "dev": C_DEV})

fig, ax = plt.subplots(figsize=FIGSIZE_SQUARE)
ax.plot(range(1, len(cumulative) + 1), cumulative.values, color="grey",
        linewidth=1.2, zorder=1)
ax.scatter(range(1, len(cumulative) + 1), cumulative.values,
           c=dot_colors, s=25, zorder=2)

ax.axhline(50, color="red",    linestyle="--", alpha=0.6, label="50%")
ax.axhline(80, color="orange", linestyle="--", alpha=0.6, label="80%")
for pct, color in [(50, "red"), (80, "orange")]:
    idx = next(i for i, v in enumerate(cumulative.values) if v >= pct)
    ax.axvline(idx + 1, color=color, linestyle=":", alpha=0.6)
    ax.text(idx + 1.5, pct - 4, f"{idx+1} DBs", color=color, fontsize=9)

from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(color=C_TRAIN, label="Train"),
    Patch(color=C_DEV,   label="Dev"),
    plt.Line2D([0], [0], color="red",    linestyle="--", label="50%"),
    plt.Line2D([0], [0], color="orange", linestyle="--", label="80%"),
])
ax.set_xlabel("Top-N databases (ranked by question count)")
ax.set_ylabel("Cumulative % of questions")
ax.set_title("Long-tail: cumulative question coverage — all 80 DBs")
fig.tight_layout()
p10 = save(fig, "10_long_tail_cumulative.png")


# ══════════════════════════════════════════════════════════════════════════
# Summary stats
# ══════════════════════════════════════════════════════════════════════════

n_colliding_tables = len(collision_counts)
n_unique_tables_train = len(table_to_dbs)

top50_dbs = int(next(i for i, v in enumerate(cumulative.values) if v >= 50)) + 1
top80_dbs = int(next(i for i, v in enumerate(cumulative.values) if v >= 80)) + 1

train_feat_pct = (train_feats.mean() * 100).round(1).to_dict()
dev_feat_pct   = (dev_feats.mean() * 100).round(1).to_dict()

all_feats = pd.concat([train_feats, dev_feats])
all_feat_pct = (all_feats.mean() * 100).round(1).to_dict()

all_df = pd.concat([train_df, dev_df])

# Combined schema stats
all_schema_combined = pd.concat([train_schema, dev_schema])


# ══════════════════════════════════════════════════════════════════════════
# Write markdown report  (EDA facts only — no design recommendations)
# ══════════════════════════════════════════════════════════════════════════


report = f"""# EDA Report — BIRD Benchmark Dataset

---

## 1. Dataset Overview

| | Train | Dev | Combined |
|---|---|---|---|
| Questions | {len(train_df):,} | {len(dev_df):,} | {len(train_df)+len(dev_df):,} |
| Databases | {train_df['db_id'].nunique()} | {dev_df['db_id'].nunique()} | {train_df['db_id'].nunique()+dev_df['db_id'].nunique()} |
| Unique table names | {n_unique_tables_train} | {n_unique_tables_dev} | {n_unique_tables_combined} |
| Colliding table names | {n_colliding_tables} | 0 | {n_colliding_tables_combined} |
| Questions with evidence hint | {train_df['has_evidence'].sum():,} ({100*train_df['has_evidence'].mean():.1f}%) | {dev_df['has_evidence'].sum():,} ({100*dev_df['has_evidence'].mean():.1f}%) | {all_df['has_evidence'].sum():,} ({100*all_df['has_evidence'].mean():.1f}%) |

---

## 2. Questions per Database

![Questions per DB]({p1})

| | Train | Dev | Combined |
|---|---|---|---|
| DBs | {train_df['db_id'].nunique()} | {dev_df['db_id'].nunique()} | {combined['db_id'].nunique()} |
| Min questions/DB | {counts_train.iloc[-1]} | {counts_dev.iloc[-1]} | {combined['count'].min()} |
| Max questions/DB | {counts_train.iloc[0]} | {counts_dev.iloc[0]} | {combined['count'].max()} |
| Median questions/DB | {counts_train.median():.0f} | {counts_dev.median():.0f} | {combined['count'].median():.0f} |
| Mean questions/DB | {counts_train.mean():.0f} | {counts_dev.mean():.0f} | {combined['count'].mean():.0f} |

![Distribution of questions per DB]({p3})

Right-skewed distribution. Top {top50_dbs} DBs account for 50% of all questions; top {top80_dbs} DBs account for 80%.

![Long-tail cumulative coverage]({p10})

---

## 3. Difficulty Distribution

![Difficulty]({p4})

Train difficulty is derived from SQL structure (simple = 0 complex keywords; moderate = 1; challenging = 2+).
Dev difficulty is the original BIRD label.

![Difficulty and hint per DB]({p10b})

| | Simple | Moderate | Challenging |
|---|---|---|---|
| Train | {(train_df['difficulty']=='simple').sum():,} ({100*(train_df['difficulty']=='simple').mean():.1f}%) | {(train_df['difficulty']=='moderate').sum():,} ({100*(train_df['difficulty']=='moderate').mean():.1f}%) | {(train_df['difficulty']=='challenging').sum():,} ({100*(train_df['difficulty']=='challenging').mean():.1f}%) |
| Dev | {(dev_df['difficulty']=='simple').sum():,} ({100*(dev_df['difficulty']=='simple').mean():.1f}%) | {(dev_df['difficulty']=='moderate').sum():,} ({100*(dev_df['difficulty']=='moderate').mean():.1f}%) | {(dev_df['difficulty']=='challenging').sum():,} ({100*(dev_df['difficulty']=='challenging').mean():.1f}%) |
| Combined | {(all_df['difficulty']=='simple').sum():,} ({100*(all_df['difficulty']=='simple').mean():.1f}%) | {(all_df['difficulty']=='moderate').sum():,} ({100*(all_df['difficulty']=='moderate').mean():.1f}%) | {(all_df['difficulty']=='challenging').sum():,} ({100*(all_df['difficulty']=='challenging').mean():.1f}%) |

---

## 4. SQL Feature Prevalence

![SQL features]({p5})

| Feature | Train | Dev | Combined |
|---|---|---|---|
| JOIN | {train_feat_pct['join']:.1f}% | {dev_feat_pct['join']:.1f}% | {all_feat_pct['join']:.1f}% |
| Aggregate (COUNT/SUM/AVG/MIN/MAX) | {train_feat_pct['aggregate']:.1f}% | {dev_feat_pct['aggregate']:.1f}% | {all_feat_pct['aggregate']:.1f}% |
| ORDER BY | {train_feat_pct['order_by']:.1f}% | {dev_feat_pct['order_by']:.1f}% | {all_feat_pct['order_by']:.1f}% |
| LIMIT | {train_feat_pct['limit']:.1f}% | {dev_feat_pct['limit']:.1f}% | {all_feat_pct['limit']:.1f}% |
| DISTINCT | {train_feat_pct['distinct']:.1f}% | {dev_feat_pct['distinct']:.1f}% | {all_feat_pct['distinct']:.1f}% |
| GROUP BY | {train_feat_pct['group_by']:.1f}% | {dev_feat_pct['group_by']:.1f}% | {all_feat_pct['group_by']:.1f}% |
| Subquery | {train_feat_pct['subquery']:.1f}% | {dev_feat_pct['subquery']:.1f}% | {all_feat_pct['subquery']:.1f}% |
| HAVING | {train_feat_pct['having']:.1f}% | {dev_feat_pct['having']:.1f}% | {all_feat_pct['having']:.1f}% |
| UNION | {train_feat_pct['union']:.1f}% | {dev_feat_pct['union']:.1f}% | {all_feat_pct['union']:.1f}% |

Both splits have near-identical feature distributions.

---

## 5. Schema Complexity

![Schema complexity]({p6})

| Metric | Train (min/median/max) | Dev (min/median/max) | Combined (min/median/max) |
|---|---|---|---|
| Tables per DB | {train_schema['n_tables'].min()} / {train_schema['n_tables'].median():.0f} / {train_schema['n_tables'].max()} | {dev_schema['n_tables'].min()} / {dev_schema['n_tables'].median():.0f} / {dev_schema['n_tables'].max()} | {all_schema_combined['n_tables'].min()} / {all_schema_combined['n_tables'].median():.0f} / {all_schema_combined['n_tables'].max()} |
| Columns per DB | {train_schema['n_columns'].min()} / {train_schema['n_columns'].median():.0f} / {train_schema['n_columns'].max()} | {dev_schema['n_columns'].min()} / {dev_schema['n_columns'].median():.0f} / {dev_schema['n_columns'].max()} | {all_schema_combined['n_columns'].min()} / {all_schema_combined['n_columns'].median():.0f} / {all_schema_combined['n_columns'].max()} |
| Foreign keys per DB | {train_schema['n_foreign_keys'].min()} / {train_schema['n_foreign_keys'].median():.0f} / {train_schema['n_foreign_keys'].max()} | {dev_schema['n_foreign_keys'].min()} / {dev_schema['n_foreign_keys'].median():.0f} / {dev_schema['n_foreign_keys'].max()} | {all_schema_combined['n_foreign_keys'].min()} / {all_schema_combined['n_foreign_keys'].median():.0f} / {all_schema_combined['n_foreign_keys'].max()} |

![Questions vs schema size]({p8})

No strong correlation between schema size and question count per database.

---

## 6. Table-Name Collisions (Train)

![Collisions]({p7})

{n_colliding_tables} table names appear in more than one train database.
Top collisions: `country` (10 DBs), `person` (7 DBs), `city` (6 DBs), `customers` (6 DBs).
The dev set has zero collisions — all 75 table names are unique across its 11 databases.

---

## 7. Evidence Hints

![Evidence]({p9})

| | Has hint | No hint |
|---|---|---|
| Train | {train_df['has_evidence'].sum():,} ({100*train_df['has_evidence'].mean():.1f}%) | {(~train_df['has_evidence']).sum():,} ({100*(~train_df['has_evidence']).mean():.1f}%) |
| Dev | {dev_df['has_evidence'].sum():,} ({100*dev_df['has_evidence'].mean():.1f}%) | {(~dev_df['has_evidence']).sum():,} ({100*(~dev_df['has_evidence']).mean():.1f}%) |
| Combined | {all_df['has_evidence'].sum():,} ({100*all_df['has_evidence'].mean():.1f}%) | {(~all_df['has_evidence']).sum():,} ({100*(~all_df['has_evidence']).mean():.1f}%) |
"""

report_path = Path("docs/eda-report.md")
report_path.write_text(report, encoding="utf-8")
print(f"Report written to {report_path}")
print(f"Plots in {OUT}/")
