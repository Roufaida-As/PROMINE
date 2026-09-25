import os
import csv
import sys
import re
import json

# Fix for '_csv.Error: field larger than field limit'
def set_max_csv_field_limit():
    max_int = sys.maxsize
    while True:
        try:
            csv.field_size_limit(max_int)
            break
        except OverflowError:
            max_int = int(max_int / 10)

set_max_csv_field_limit()

CSV_INPUT = "prompt_commits.csv"
CSV_OUTPUT = "coevolution_commits.csv"

# Non-consumer noise files that should NEVER count as a downstream consumer
EXCLUDED_EXTENSIONS = (
    ".md", ".txt", ".rst", ".gitignore", ".env", ".dockerignore",
    "dockerfile", ".yml", ".yaml", ".cfg", ".ini", ".png", ".jpg"
)

# Explicit keywords in file paths indicating consumer/parser/schema logic
# (kept as the FALLBACK layer only, see touches_true_consumer)
CONSUMER_FILE_KEYWORDS = [
    "schema", "model", "parser", "validator", "guard",
    "contract", "dto", "type", "agent", "chain", "service", "client"
]


def is_heuristic_consumer_file(filepath):
    """
    Filename-only heuristic, used ONLY as a fallback (see touches_true_consumer).
    Deliberately narrower than the original version: no longer accepts "any .py
    file that isn't the prompt file" -- that rule alone was making Step 3 pass
    almost every multi-file Python commit, regardless of relevance. Now it
    requires an explicit consumer-shaped keyword in the path.
    """
    f_lower = filepath.lower()
    if f_lower.endswith(EXCLUDED_EXTENSIONS) or "docs/" in f_lower or "github/" in f_lower:
        return False
    return any(keyword in f_lower for keyword in CONSUMER_FILE_KEYWORDS)


def touches_true_consumer(prompt_files_str, prompt_var_names_str, all_files_str, consumer_diffs_json):
    """
    PRIMARY CHECK (variable-confirmed): does any extracted prompt variable name
    (e.g. 'executeTaskPrompt', 'summarizeSearchSnippets') literally appear in the
    diff of a co-changed, non-prompt Python file? This is the automated version
    of `grep -rn "<promptVarName>"` that found serper.ts by hand.

    FALLBACK CHECK (heuristic-only): if the primary check finds nothing -- usually
    because the consumer file isn't Python, or Step 2 had no variable names to
    extract -- fall back to the old filename-keyword heuristic, but label it
    explicitly so low-confidence rows can be told apart from confirmed ones.

    Returns: (is_coevolution, variable_confirmed_files, heuristic_only_files, confidence)
    """
    prompt_files = set(f.strip().lower() for f in prompt_files_str.split(";") if f.strip())
    all_files = [f.strip() for f in all_files_str.split(";") if f.strip()]
    non_prompt_files = [f for f in all_files if f.lower() not in prompt_files]

    var_names = [v.strip() for v in prompt_var_names_str.split(";") if v.strip()]
    var_patterns = [re.compile(r"\b" + re.escape(v) + r"\b") for v in var_names]

    try:
        consumer_diffs = json.loads(consumer_diffs_json) if consumer_diffs_json else {}
    except json.JSONDecodeError:
        consumer_diffs = {}

    # --- Primary: variable-confirmed matches ---
    variable_confirmed_files = []
    if var_patterns:
        for filepath, diff_text in consumer_diffs.items():
            if any(pat.search(diff_text) for pat in var_patterns):
                variable_confirmed_files.append(filepath)

    # --- Fallback: filename heuristic (only used if primary found nothing) ---
    heuristic_only_files = []
    if not variable_confirmed_files:
        for f in non_prompt_files:
            if is_heuristic_consumer_file(f):
                heuristic_only_files.append(f)

    if variable_confirmed_files:
        confidence = "variable_confirmed"
    elif heuristic_only_files:
        confidence = "heuristic_only"
    else:
        confidence = "none"

    is_coevolution = bool(variable_confirmed_files or heuristic_only_files)
    return is_coevolution, variable_confirmed_files, heuristic_only_files, confidence


def execute_step3_strict():
    print("=== Step 3: Strict Attribute-Based Consumer Filtering (variable-match first) ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f"❌ Error: {CSV_INPUT} not found! Run Step 2 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        prompt_commits = list(reader)

    print(f"Loaded {len(prompt_commits)} candidate prompt commits from {CSV_INPUT}...\n")

    coevolution_records = []
    n_variable_confirmed = 0
    n_heuristic_only = 0

    for record in prompt_commits:
        prompt_files = record.get("prompt_files", "")
        prompt_var_names = record.get("prompt_var_names", "")
        all_files = record.get("all_modified_files", "")
        consumer_diffs_json = record.get("consumer_candidate_diffs", "")

        is_coevolution, var_confirmed, heuristic_only, confidence = touches_true_consumer(
            prompt_files, prompt_var_names, all_files, consumer_diffs_json
        )

        if is_coevolution:
            if confidence == "variable_confirmed":
                n_variable_confirmed += 1
            else:
                n_heuristic_only += 1

            coevolution_records.append({
                "category": record["category"],
                "repo_name": record["repo_name"],
                "commit_hash": record["commit_hash"],
                "author_date": record["author_date"],
                "msg": record["msg"],
                "prompt_files": prompt_files,
                "prompt_var_names": prompt_var_names,
                "consumer_files_variable_confirmed": ";".join(var_confirmed),
                "consumer_files_heuristic_only": ";".join(heuristic_only),
                "confidence": confidence,
                "all_modified_files": all_files,
            })

    fieldnames = [
        "category", "repo_name", "commit_hash", "author_date",
        "msg", "prompt_files", "prompt_var_names",
        "consumer_files_variable_confirmed", "consumer_files_heuristic_only",
        "confidence", "all_modified_files"
    ]

    with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(coevolution_records)

    total = len(coevolution_records)
    ratio = (total / len(prompt_commits)) * 100 if prompt_commits else 0

    print("==================================================")
    print(f"✅ Step 3 Complete!")
    print(f"📊 Total Prompt Commits Evaluated: {len(prompt_commits)}")
    print(f"🎯 True Co-Evolution Commits Extracted: {total} ({ratio:.1f}%)")
    print(f"   ├─ variable_confirmed (high confidence): {n_variable_confirmed}")
    print(f"   └─ heuristic_only (lower confidence, filename-based): {n_heuristic_only}")
    print(f"📁 Output Saved To: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")


if __name__ == "__main__":
    execute_step3_strict()