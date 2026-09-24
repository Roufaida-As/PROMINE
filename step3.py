import os
import csv
import sys
import re

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
CONSUMER_FILE_KEYWORDS = [
    "schema", "model", "parser", "validator", "guard", 
    "contract", "dto", "type", "agent", "chain", "service", "client"
]

def is_valid_consumer_file(filepath):
    """
    Checks if a modified file path belongs to a downstream consumer component.
    """
    f_lower = filepath.lower()
    
    # 1. Skip non-code / noise assets
    if f_lower.endswith(EXCLUDED_EXTENSIONS) or "docs/" in f_lower or "github/" in f_lower:
        return False
        
    # 2. Check for explicit consumer/schema/parser path keywords
    if any(keyword in f_lower for keyword in CONSUMER_FILE_KEYWORDS):
        return True
        
    # 3. Python files (e.g. main.py, app.py) that are NOT the prompt file itself
    if f_lower.endswith(".py") and not any(p in f_lower for p in ["prompt", "template"]):
        return True

    return False

def touches_true_consumer(prompt_files_str, all_files_str):
    """
    STRICT FILTER: A commit is co-evolution ONLY IF it modifies a prompt artifact AND
    at least one valid downstream consumer/code file in the same commit.
    """
    prompt_files = set(f.strip().lower() for f in prompt_files_str.split(";") if f.strip())
    all_files = [f.strip() for f in all_files_str.split(";") if f.strip()]
    
    # Find files touched in this commit that are NOT the prompt files
    non_prompt_files = [f for f in all_files if f.lower() not in prompt_files]
    
    matched_consumer_files = []
    for f in non_prompt_files:
        if is_valid_consumer_file(f):
            matched_consumer_files.append(f)

    # STRICT RULE: Must have at least ONE true consumer/code file alongside the prompt
    is_true_coevolution = len(matched_consumer_files) > 0

    return is_true_coevolution, ";".join(matched_consumer_files)

def execute_step3_strict():
    print("=== Step 3: Strict Attribute-Based Consumer Filtering ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 2 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        prompt_commits = list(reader)

    print(f"Loaded {len(prompt_commits)} candidate prompt commits from {CSV_INPUT}...\n")

    coevolution_records = []

    for record in prompt_commits:
        prompt_files = record.get("prompt_files", "")
        all_files = record.get("all_modified_files", "")
        
        is_coevolution, consumer_files_found = touches_true_consumer(prompt_files, all_files)
        
        if is_coevolution:
            coevolution_records.append({
                "category": record["category"],
                "repo_name": record["repo_name"],
                "commit_hash": record["commit_hash"],
                "author_date": record["author_date"],
                "msg": record["msg"],
                "prompt_files": prompt_files,
                "consumer_files": consumer_files_found,
                "all_modified_files": all_files
            })

    # Export to coevolution_commits.csv
    fieldnames = [
        "category", "repo_name", "commit_hash", "author_date", 
        "msg", "prompt_files", "consumer_files", "all_modified_files"
    ]
    
    with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(coevolution_records)

    ratio = (len(coevolution_records) / len(prompt_commits)) * 100 if prompt_commits else 0

    print("==================================================")
    print(f"✅ Step 3 Complete (Strict Filter Applied)!")
    print(f"📊 Total Prompt Commits Evaluated: {len(prompt_commits)}")
    print(f"🎯 True Co-Evolution Commits Extracted: {len(coevolution_records)} ({ratio:.1f}%)")
    print(f"📁 Output Saved To: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step3_strict()