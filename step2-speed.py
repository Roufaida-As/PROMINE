import os
import csv
import re
import tempfile
import shutil
from datetime import datetime
from pydriller import Repository

CSV_INPUT = "target_repos.csv"
CSV_OUTPUT = "prompt_commits.csv"

# Temporal threshold: LLM structured prompt era
START_DATE = datetime(2024, 1, 1)

# High-Precision Compiled Regex Patterns (Strict LLM Prompting & Schema Specs)
STRICT_PROMPT_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(system|user|developer)_message\b",
        r"\b(system|user|developer)_prompt\b",
        r"\bPromptTemplate\b",
        r"\bChatPromptTemplate\b",
        r"\bresponse_model\s*=",
        r"\bStructuredOutput\b",
        r"\.from_template\(",
        r"\.from_messages\(",
        r"\.rail\b",
        r"\bGuard\.from_",
    ]
]

# File path indicators (must be specific)
EXPLICIT_PROMPT_FILES = ["prompt", "template", "system_prompt", "user_prompt", ".rail"]

def touches_prompt_cross_verified(mod):
    filename = (mod.filename or "").lower()
    diff_text = mod.diff or ""

    # Check 1: Dedicated prompt file type (.rail or .prompt) -> ALWAYS a prompt artifact
    if filename.endswith(".rail") or filename.endswith(".prompt"):
        return True

    # Check 2: File name contains "prompt" OR "template"
    is_prompt_file = any(k in filename for k in ["prompt", "template", "system_prompt"])

    # Check 3: Code diff contains explicit prompt code constructs
    has_prompt_pattern = any(pat.search(diff_text) for pat in STRICT_PROMPT_PATTERNS)

    # Check 4: Dedicated prompt file AND any modified text/string in diff
    # (Catches plain-text edits inside files like prompts.py)
    is_text_edit_in_prompt_file = is_prompt_file and len(diff_text.strip()) > 0

    # Cross-verified logic:
    # Must either be a pattern match anywhere OR any change inside a dedicated prompt file
    return has_prompt_pattern or is_text_edit_in_prompt_file

def execute_step2_precise():
    print("=== Step 2: High-Precision Prompt Commit Scraping (All Repos) ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 1 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        repos = list(csv.DictReader(f))

    fieldnames = ["category", "repo_name", "commit_hash", "author_date", "msg", "prompt_files", "all_modified_files"]
    
    # Overwrite CSV with header
    with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

    total_prompt_commits = 0

    for idx, repo in enumerate(repos, 1):
        repo_name = repo["repo_name"]
        clone_url = repo["clone_url"]
        category = repo["category"]

        print(f"[{idx}/{len(repos)}] Processing: {repo_name}...")
        temp_repo_dir = tempfile.mkdtemp()

        try:
            scanned = 0
            found = 0

            # Optimised traversal: Filter file types and date range upfront
            repo_miner = Repository(
                clone_url,
                clone_repo_to=temp_repo_dir,
                since=START_DATE,
                only_modifications_with_file_types=['.py', '.rail', '.yaml', '.json', '.toml']
            )

            for commit in repo_miner.traverse_commits():
                scanned += 1
                if len(commit.parents) > 1: # Skip merge commits
                    continue

                prompt_files = []
                all_modified_files = []

                for mod in commit.modified_files:
                    path = mod.new_path or mod.old_path
                    if not path:
                        continue
                    
                    all_modified_files.append(path)

                    if touches_prompt_cross_verified(mod):
                        prompt_files.append(path)

                if prompt_files:
                    found += 1
                    record = {
                        "category": category,
                        "repo_name": repo_name,
                        "commit_hash": commit.hash,
                        "author_date": commit.author_date.strftime("%Y-%m-%d %H:%M:%S"),
                        "msg": commit.msg.split("\n")[0][:120],
                        "prompt_files": ";".join(set(prompt_files)),
                        "all_modified_files": ";".join(all_modified_files)
                    }
                    
                    # Stream output directly to disk
                    with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writerow(record)

            total_prompt_commits += found
            print(f"   ✓ Scanned {scanned} commits | Found {found} high-precision prompt commits")

        except Exception as e:
            print(f"   ⚠️ Skipped {repo_name} (Error/Timeout): {e}")
        finally:
            shutil.rmtree(temp_repo_dir, ignore_errors=True)

    print("\n==================================================")
    print(f"✅ Step 2 Complete! Total High-Precision Prompt Commits: {total_prompt_commits}")
    print(f"📁 Saved to: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step2_precise()