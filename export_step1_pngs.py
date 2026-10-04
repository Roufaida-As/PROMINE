"""Export MACE Step 1 statistics as Canva-ready PNG images.

Usage:
    python export_step1_pngs.py

Images are written to the step1_pngs directory at 240 DPI.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


ROOT = Path(__file__).resolve().parent
STATS_PATH = ROOT / "step1_stats.json"
OUTPUT_DIR = ROOT / "step1_pngs"

BG = "#FBFCFE"
INK = "#17324D"
MUTED = "#60758A"
GRID = "#DCE7F0"
COLORS = ["#3B82C4", "#45B79B", "#F2C14E", "#E98168", "#9AAEC0"]


def setup(title, width=12, height=7):
    figure, axis = plt.subplots(figsize=(width, height), facecolor=BG)
    figure.patch.set_facecolor(BG)
    axis.set_facecolor(BG)
    axis.set_title(title, loc="left", fontsize=24, fontweight="bold", color=INK, pad=22)
    return figure, axis


def save(figure, name):
    figure.savefig(OUTPUT_DIR / name, dpi=240, facecolor=BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(figure)


def bar_chart(stats):
    labels = ["Instructor", "Guardrails AI", "LangChain", "Pydantic AI", "Outlines"]
    keys = ["instructor", "guardrails-ai", "langchain", "pydantic-ai", "outlines"]
    values = [stats["categories"].get(key, 0) for key in keys]
    figure, axis = setup("Qualified repositories by search category")
    bars = axis.bar(labels, values, color=COLORS, width=0.62)
    axis.set_ylabel("Qualified rows", color=MUTED, fontsize=14)
    axis.set_ylim(0, max(values + [1]) * 1.25)
    axis.grid(axis="y", color=GRID, linewidth=1)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=12)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + 0.7, str(value), ha="center", color=INK, fontsize=16, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "Snapshot from target_repos.csv | Categories may overlap", color=MUTED, fontsize=10)
    save(figure, "01_category_counts.png")


def app_type_chart(stats):
    labels = list(stats["app_types"])
    values = list(stats["app_types"].values())
    figure, axis = setup("Heuristic application types")
    wedges, _ = axis.pie(values, colors=COLORS, startangle=90, wedgeprops={"width": 0.42, "edgecolor": BG})
    axis.text(0, 0.05, str(sum(values)), ha="center", va="center", fontsize=34, fontweight="bold", color=INK)
    axis.text(0, -0.16, "rows", ha="center", va="center", fontsize=13, color=MUTED)
    axis.legend(wedges, [f"{label}: {value}" for label, value in zip(labels, values)], loc="center left", bbox_to_anchor=(0.95, 0.5), frameon=False, fontsize=12, labelcolor=INK)
    axis.set_aspect("equal")
    figure.text(0.01, 0.01, "Heuristic labels from repository names and manifest paths; validate manually", color=MUTED, fontsize=10)
    save(figure, "02_application_types.png")


def manifest_chart(stats):
    labels = list(stats["manifest_types"])
    values = list(stats["manifest_types"].values())
    figure, axis = setup("Dependency manifest evidence")
    bars = axis.barh(labels[::-1], values[::-1], color=COLORS[: len(values)][::-1], height=0.52)
    axis.set_xlabel("Manifest paths found", color=MUTED, fontsize=14)
    axis.grid(axis="x", color=GRID, linewidth=1)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", colors=MUTED, labelsize=13)
    for bar, value in zip(bars, values[::-1]):
        axis.text(value + 1, bar.get_y() + bar.get_height() / 2, str(value), va="center", color=INK, fontsize=16, fontweight="bold")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "A manifest supports environment reconstruction; it does not guarantee installation success", color=MUTED, fontsize=10)
    save(figure, "03_manifest_evidence.png")


def summary_card(stats):
    figure, axis = plt.subplots(figsize=(12, 6.75), facecolor=BG)
    axis.set_facecolor(BG)
    axis.axis("off")
    axis.text(0.02, 0.89, "MACE Step 1", fontsize=17, color="#3B82C4", fontweight="bold", transform=axis.transAxes)
    axis.text(0.02, 0.78, "Repository qualification snapshot", fontsize=29, color=INK, fontweight="bold", transform=axis.transAxes)
    metrics = [(str(stats["qualified_rows"]), "qualified rows", COLORS[0]), (str(stats["unique_repositories"]), "unique repositories", COLORS[1]), (str(stats["duplicate_rows"]), "duplicate category match", COLORS[2]), (str(len(stats["excluded"])), "manual exclusions", COLORS[3])]
    for index, (value, label, color) in enumerate(metrics):
        x = 0.02 + index * 0.245
        box = FancyBboxPatch((x, 0.43), 0.205, 0.22, boxstyle="round,pad=0.012", facecolor="white", edgecolor=GRID, linewidth=1.5, transform=axis.transAxes)
        axis.add_patch(box)
        axis.text(x + 0.025, 0.54, value, fontsize=32, color=color, fontweight="bold", transform=axis.transAxes)
        axis.text(x + 0.025, 0.47, label, fontsize=11, color=MUTED, transform=axis.transAxes)
    axis.text(0.02, 0.29, "Qualification gate", fontsize=15, color=INK, fontweight="bold", transform=axis.transAxes)
    axis.text(0.02, 0.21, "Active GitHub repository  ->  test signal  ->  dependency manifest  ->  CSV export", fontsize=16, color=MUTED, transform=axis.transAxes)
    axis.text(0.02, 0.10, "Search lens: Instructor | Guardrails AI | LangChain | Pydantic AI | Outlines", fontsize=12, color=MUTED, transform=axis.transAxes)
    save(figure, "00_step1_summary.png")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    summary_card(stats)
    bar_chart(stats)
    app_type_chart(stats)
    manifest_chart(stats)
    print(f"Exported 4 PNG files to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()