"""Compute and export presentation statistics for MACE Step 2.

Usage:
    python stats_step2.py

Reads the potentially large prompt_commits.csv in streaming mode, writes
step2_stats.json, and creates Canva-ready PNG charts in step2_pngs/.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "prompt_commits.csv"
JSON_OUTPUT = ROOT / "step2_stats.json"
PNG_DIR = ROOT / "step2_pngs"

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
    return [item.strip().lower() for item in (value or "").split(";") if item.strip()]


def classify_prompt_path(path):
    if path.endswith(".rail"):
        return ".rail file"
    if path.endswith(".prompt"):
        return ".prompt file"
    if "template" in path:
        return "template-named file"
    if "prompt" in path:
        return "prompt-named file"
    return "code/config matched by pattern"


def read_stats():
    set_csv_limit()
    categories = Counter()
    repositories = Counter()
    commits = set()
    years = Counter()
    months = Counter()
    prompt_path_types = Counter({
        ".rail file": 0,
        ".prompt file": 0,
        "template-named file": 0,
        "prompt-named file": 0,
        "code/config matched by pattern": 0,
    })
    modified_file_counts = []
    variable_rows = 0
    consumer_diff_rows = 0
    rows = 0

    with INPUT.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            category = row.get("category", "unknown")
            repo = row.get("repo_name", "unknown")
            categories[category] += 1
            repositories[repo] += 1
            if row.get("commit_hash"):
                commits.add(row["commit_hash"])

            date = (row.get("author_date") or "")[:7]
            if len(date) == 7 and date[4] == "-":
                years[date[:4]] += 1
                months[date] += 1

            paths = split_paths(row.get("prompt_files"))
            prompt_path_types.update(classify_prompt_path(path) for path in paths)
            modified_file_counts.append(len(split_paths(row.get("all_modified_files"))))
            if (row.get("prompt_var_names") or "").strip():
                variable_rows += 1
            if row.get("consumer_candidate_diffs") not in (None, "", "{}"):  # JSON field is large; do not parse it.
                consumer_diff_rows += 1

    average_files = round(sum(modified_file_counts) / len(modified_file_counts), 2) if modified_file_counts else 0
    median_files = sorted(modified_file_counts)[len(modified_file_counts) // 2] if modified_file_counts else 0
    return {
        "source": INPUT.name,
        "candidate_prompt_commits": rows,
        "unique_commit_hashes": len(commits),
        "unique_repositories": len(repositories),
        "duplicate_rows": rows - len(commits),
        "categories": dict(categories),
        "repositories_by_candidate_count": dict(repositories.most_common(20)),
        "commits_by_year": dict(sorted(years.items())),
        "commits_by_month": dict(sorted(months.items())),
        "prompt_path_types": dict(prompt_path_types),
        "variable_name_extracted_rows": variable_rows,
        "variable_name_extracted_rate": round(variable_rows / rows * 100, 1) if rows else 0,
        "rows_with_cochanged_python_diffs": consumer_diff_rows,
        "cochanged_python_diff_rate": round(consumer_diff_rows / rows * 100, 1) if rows else 0,
        "modified_files_per_candidate": {"average": average_files, "median": median_files, "max": max(modified_file_counts, default=0)},
        "interpretation": {
            "scope": "Step 2 detects candidate prompt-related commits; it does not yet prove consumer co-evolution.",
            "method": "Candidates are found with path indicators and strict prompt-related code patterns.",
            "next_step": "Step 3 links extracted prompt variables or consumer-shaped files to the prompt change.",
        },
    }


def setup_chart(title, width=12, height=7):
    figure, axis = plt.subplots(figsize=(width, height), facecolor=BG)
    figure.patch.set_facecolor(BG)
    axis.set_facecolor(BG)
    axis.set_title(title, loc="left", fontsize=24, fontweight="bold", color=INK, pad=22)
    return figure, axis


def save(figure, name):
    figure.savefig(PNG_DIR / name, dpi=240, facecolor=BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(figure)


def category_chart(stats):
    labels = list(stats["categories"])
    values = list(stats["categories"].values())
    figure, axis = setup_chart("Step 2 candidates by search category")
    bars = axis.bar(labels, values, color=COLORS[:len(values)], width=0.62)
    axis.set_ylabel("Prompt-commit candidates", color=MUTED, fontsize=14)
    axis.grid(axis="y", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.02, str(value), ha="center", color=INK, fontsize=15, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "A repository may contribute candidates to more than one search category", color=MUTED, fontsize=10)
    save(figure, "01_candidates_by_category.png")


def year_chart(stats):
    labels = list(stats["commits_by_year"])
    values = list(stats["commits_by_year"].values())
    figure, axis = setup_chart("Prompt-commit candidates by year")
    axis.plot(labels, values, color=COLORS[0], marker="o", linewidth=3, markersize=9)
    axis.fill_between(labels, values, color=COLORS[0], alpha=0.12)
    axis.set_ylabel("Candidates", color=MUTED, fontsize=14)
    axis.grid(axis="y", color=GRID)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    save(figure, "02_candidates_over_time.png")


def path_chart(stats):
    labels = [
        ".rail file",
        ".prompt file",
        "template-named file",
        "prompt-named file",
        "code/config matched by pattern",
    ]
    values = [stats["prompt_path_types"].get(label, 0) for label in labels]
    figure, axis = setup_chart("How candidate prompt files were detected")
    bars = axis.barh(labels[::-1], values[::-1], color=COLORS[:len(values)][::-1], height=0.52)
    axis.set_xlabel("Prompt-file occurrences", color=MUTED, fontsize=14)
    axis.grid(axis="x", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values[::-1]):
        axis.text(value + 0.5, bar.get_y() + bar.get_height() / 2, str(value), va="center", color=INK, fontsize=14, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "One commit can modify multiple prompt-related files", color=MUTED, fontsize=10)
    save(figure, "03_detection_sources.png")


def coverage_chart(stats):
    labels = ["Variable name\nextracted", "Co-changed Python\ndiff available"]
    values = [stats["variable_name_extracted_rate"], stats["cochanged_python_diff_rate"]]
    figure, axis = setup_chart("Evidence retained for Step 3", 10, 6)
    bars = axis.bar(labels, values, color=[COLORS[1], COLORS[2]], width=0.55)
    axis.set_ylabel("Percentage of candidates", color=MUTED, fontsize=14)
    axis.set_ylim(0, 110)
    axis.grid(axis="y", color=GRID)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + 3, f"{value}%", ha="center", color=INK, fontsize=17, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "These are extraction signals, not confirmed co-evolution results", color=MUTED, fontsize=10)
    save(figure, "04_step3_readiness.png")


def main():
    stats = read_stats()
    JSON_OUTPUT.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    PNG_DIR.mkdir(exist_ok=True)
    category_chart(stats)
    year_chart(stats)
    path_chart(stats)
    coverage_chart(stats)
    print(json.dumps(stats, indent=2))
    print(f"\nWrote {JSON_OUTPUT} and 4 PNG files to {PNG_DIR}")


if __name__ == "__main__":
    main()