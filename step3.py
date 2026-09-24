import os
import csv
import re

CSV_INPUT = "prompt_commits.csv"
CSV_OUTPUT = "coevolution_commits.csv"

# Regex patterns targeting consumer-side code constructs
CONSUMER_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bBaseModel\b",
        r"\bField\(",
        r"\bmodel_validate\b",
        r"\bparse_obj\b",
        r"\bJsonOutputParser\b",
        r"\bPydanticOutputParser\b",
        r"\bOutputParser\b",
        r"\bjson_schema\b",
        r"\bvalidator\b",
        r"\bvalidates\b",
        r"\bresponse_model\b",
    ]
]

CONSUMER_FILE_KEYWORDS = [
    "schema", "model", "parser", "validator", "contract", 
    "dto", "type", "agent", "chain", "service"
]

def touches_consumer(all_files_str):
    """
    Checks if a commit touched consumer/parser/schema files alongside the prompt.
    """
    files = [f.strip() for f in all_files_str.split(";") if f.strip()]
    
    # 1. Look for explicit consumer file paths (e.g., schemas/user.py, parsers.py, models.py)
    matched_consumer_files = []
    for f in files:
        f_lower = f.lower()
        if any(keyword in f_lower for keyword in CONSUMER_FILE_KEYWORDS):
            matched_consumer_files.append(f)

    # 2. Multi-file changes: Prompt file + at least one other application file
    is_multi_file_coevolution = len(files) > 1

    return (len(matched_consumer_files) > 0 or is_multi_file_coevolution), ";".join(matched_consumer_files)

def execute_step3():
    print("=== Step 3: Attribute-Based Consumer Filtering ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f"❌ Error: {CSV_INPUT} not found! Run Step 2 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        prompt_commits = list(reader)

    print(f"Loaded {len(prompt_commits)} candidate prompt commits from {CSV_INPUT}...\n")

    coevolution_records = []

    for record in prompt_commits:
        all_files = record.get("all_modified_files", "")
        
        is_coevolution, consumer_files_found = touches_consumer(all_files)
        
        if is_coevolution:
            coevolution_records.append({
                "category": record["category"],
                "repo_name": record["repo_name"],
                "commit_hash": record["commit_hash"],
                "author_date": record["author_date"],
                "msg": record["msg"],
                "prompt_files": record["prompt_files"],
                "consumer_files": consumer_files_found if consumer_files_found else "app_code_cochanged",
                "all_modified_files": record["all_modified_files"]
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
    print(f"✅ Step 3 Complete!")
    print(f"📊 Total Prompt Commits Evaluated: {len(prompt_commits)}")
    print(f"🎯 Valid Co-Evolution Commits Extracted: {len(coevolution_records)} ({ratio:.1f}%)")
    print(f"📁 Output Saved To: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step3()