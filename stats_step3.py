"""Compute and export presentation statistics for MACE Step 3.

Usage:
    python stats_step3.py

Reads coevolution_commits.csv in streaming mode, writes step3_stats.json,
and creates Canva-ready PNG charts in step3_pngs/.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "coevolution_commits.csv"
STEP2_INPUT = ROOT / "prompt_commits.csv"
JSON_OUTPUT = ROOT / "step3_stats.json"
PNG_DIR = ROOT / "step3_pngs"

BG = "#FBFCFE"
INK = "#17324D"
MUTED = "#60758A"
GRID = "#DCE7F0"
COLORS = ["#3B82C4", "#45B79B", "#F2C14E", "#E98168", "#9AAEC0", "#7B6BB3"]


def set_csv_limit():
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def split_paths(value):
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def setup_chart(title, width=12, height=7):
    figure, axis = plt.subplots(figsize=(width, height), facecolor=BG)
    figure.patch.set_facecolor(BG)
    axis.set_facecolor(BG)
    axis.set_title(title, loc="left", fontsize=24, fontweight="bold", color=INK, pad=22)
    return figure, axis


def save(figure, name):
    PNG_DIR.mkdir(exist_ok=True)
    figure.savefig(PNG_DIR / name, dpi=240, facecolor=BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(figure)


def read_stats():
    set_csv_limit()
    total_input = 0
    rows = 0
    unique_commits = set()
    repositories = Counter()
    categories = Counter()
    confidence = Counter()
    years = Counter()
    consumer_files = Counter()
    prompt_files = Counter()
    prompt_variable_missing = 0
    variable_file_counts = []
    heuristic_file_counts = []
    modified_file_counts = []

    if STEP2_INPUT.exists():
        with STEP2_INPUT.open(newline="", encoding="utf-8") as handle:
            total_input = sum(1 for _ in handle) - 1

    with INPUT.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            commit_hash = row.get("commit_hash", "")
            if commit_hash:
                unique_commits.add(commit_hash)
            repositories[row.get("repo_name", "unknown")] += 1
            categories[row.get("category", "unknown")] += 1
            confidence[row.get("confidence", "none")] += 1
            author_date = row.get("author_date", "")
            if len(author_date) >= 4:
                years[author_date[:4]] += 1

            variable_files = split_paths(row.get("consumer_files_variable_confirmed"))
            heuristic_files = split_paths(row.get("consumer_files_heuristic_only"))
            variable_file_counts.append(len(variable_files))
            heuristic_file_counts.append(len(heuristic_files))
            modified_file_counts.append(len(split_paths(row.get("all_modified_files"))))
            if not (row.get("prompt_var_names") or "").strip():
                prompt_variable_missing += 1

            for filepath in variable_files:
                consumer_files[filepath] += 1
            for filepath in heuristic_files:
                consumer_files[filepath] += 1
            for filepath in split_paths(row.get("prompt_files")):
                prompt_files[filepath] += 1

    total_input = max(total_input, rows)
    result = {
        "source": INPUT.name,
        "step2_candidate_rows": total_input,
        "step3_extracted_rows": rows,
        "extraction_rate": round(rows / total_input * 100, 1) if total_input else 0,
        "rows_not_extracted": total_input - rows,
        "unique_commit_hashes": len(unique_commits),
        "unique_repositories": len(repositories),
        "duplicate_rows": rows - len(unique_commits),
        "categories": dict(categories),
        "confidence": dict(confidence),
        "extracted_by_year": dict(sorted(years.items())),
        "repositories_by_extracted_count": dict(repositories.most_common(20)),
        "top_consumer_files": dict(consumer_files.most_common(20)),
        "top_prompt_files": dict(prompt_files.most_common(20)),
        "prompt_variable_missing_rows": prompt_variable_missing,
        "prompt_variable_missing_rate": round(prompt_variable_missing / rows * 100, 1) if rows else 0,
        "consumer_files_per_row": {
            "variable_confirmed_average": round(sum(variable_file_counts) / rows, 2) if rows else 0,
            "heuristic_only_average": round(sum(heuristic_file_counts) / rows, 2) if rows else 0,
            "modified_files_average": round(sum(modified_file_counts) / rows, 2) if rows else 0,
        },
        "interpretation": {
            "scope": "Step 3 extracts candidate prompt/consumer co-evolution records; it does not prove semantic causality.",
            "variable_confirmed": "The extracted prompt variable appears in a co-changed Python diff.",
            "heuristic_only": "No variable match was found, but a co-changed file name suggests schema, parser, validator, agent, service, or client logic.",
            "next_step": "Step 4 replays selected historical commits and executes tests to measure behavioral impact.",
        },
    }
    return result


def confidence_chart(stats):
    labels = ["Variable confirmed", "Heuristic only"]
    values = [stats["confidence"].get("variable_confirmed", 0), stats["confidence"].get("heuristic_only", 0)]
    figure, axis = setup_chart("Step 3 extracted candidates by confidence")
    bars = axis.bar(labels, values, color=COLORS[:2], width=0.55)
    axis.set_ylabel("Extracted records", color=MUTED, fontsize=14)
    axis.grid(axis="y", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.02, str(value), ha="center", color=INK, fontsize=16, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "Variable-confirmed records are stronger evidence than filename-only matches", color=MUTED, fontsize=10)
    save(figure, "01_confidence_split.png")


def category_chart(stats):
    labels = list(stats["categories"])
    values = list(stats["categories"].values())
    figure, axis = setup_chart("Step 3 extracted candidates by category")
    bars = axis.bar(labels, values, color=COLORS[:len(values)], width=0.62)
    axis.set_ylabel("Extracted records", color=MUTED, fontsize=14)
    axis.grid(axis="y", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.02, str(value), ha="center", color=INK, fontsize=15, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "Categories may overlap because Step 1 search categories overlap", color=MUTED, fontsize=10)
    save(figure, "02_extracted_by_category.png")


def timeline_chart(stats):
    labels = list(stats["extracted_by_year"])
    values = list(stats["extracted_by_year"].values())
    figure, axis = setup_chart("Step 3 extracted candidates over time")
    axis.plot(labels, values, color=COLORS[0], marker="o", linewidth=3, markersize=9)
    axis.fill_between(labels, values, color=COLORS[0], alpha=0.12)
    axis.set_ylabel("Extracted records", color=MUTED, fontsize=14)
    axis.grid(axis="y", color=GRID)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    save(figure, "03_extracted_over_time.png")


def funnel_chart(stats):
    labels = ["Step 2 candidates", "Step 3 extracted"]
    values = [stats["step2_candidate_rows"], stats["step3_extracted_rows"]]
    figure, axis = setup_chart("Candidate reduction before execution validation", 10, 6)
    bars = axis.barh(labels[::-1], values[::-1], color=COLORS[:2][::-1], height=0.5)
    axis.set_xlabel("Records", color=MUTED, fontsize=14)
    axis.grid(axis="x", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values[::-1]):
        axis.text(value + max(values) * 0.02, bar.get_y() + bar.get_height() / 2, f"{value:,}", va="center", color=INK, fontsize=16, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, f"Step 3 extraction rate: {stats['extraction_rate']}%", color=MUTED, fontsize=10)
    save(figure, "04_step3_funnel.png")


def main():
    stats = read_stats()
    JSON_OUTPUT.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    confidence_chart(stats)
    category_chart(stats)
    timeline_chart(stats)
    funnel_chart(stats)
    print(json.dumps(stats, indent=2))
    print(f"\nWrote {JSON_OUTPUT} and 4 PNG files to {PNG_DIR}")


if __name__ == "__main__":
    main()