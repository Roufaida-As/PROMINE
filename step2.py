import os
import csv
import re
import tempfile
import shutil
from pydriller import Repository

CSV_INPUT = "target_repos.csv"
CSV_OUTPUT = "prompt_commits.csv"

# Broad patterns that indicate a prompt artifact or prompt definition changed
PROMPT_PATTERNS = [
    r"prompt",
    r"system_message",
    r"user_message",
    r"system_prompt",
    r"user_prompt",
    r"PromptTemplate",
    r"ChatPromptTemplate",
    r"messages\s*=\s*\[",
    r"response_model\s*=",
    r"\.rail",
]

def touches_prompt(mod):
    """Check if a modified file or its diff contains a prompt artifact."""
    filename = mod.filename.lower()
    diff_text = mod.diff or ""

    # 1. File path match (e.g., prompts/system.py, template.json, agent.rail)
    is_prompt_file = any(k in filename for k in ["prompt", "template", "spec", ".rail"])
    
    # 2. Content patch match
    has_prompt_diff = any(re.search(pat, diff_text, re.IGNORECASE) for pat in PROMPT_PATTERNS)

    return is_prompt_file or has_prompt_diff

def execute_step2():
    print("=== Step 2: Pure Candidate Prompt-Commit Scraping ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f"❌ Error: {CSV_INPUT} not found! Run Step 1 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        repos = list(csv.DictReader(f))

    prompt_commit_records = []

    for idx, repo in enumerate(repos, 1):
        repo_name = repo["repo_name"]
        clone_url = repo["clone_url"]
        category = repo["category"]

        print(f"[{idx}/{len(repos)}] Scraping prompt commits in: {repo_name}...")
        temp_repo_dir = tempfile.mkdtemp()

        try:
            scanned_commits = 0
            prompt_commits_found = 0

            for commit in Repository(clone_url, clone_repo_to=temp_repo_dir).traverse_commits():
                scanned_commits += 1
                if len(commit.parents) > 1: # Skip merge commits
                    continue

                prompt_files = []
                all_modified_files = []

                for mod in commit.modified_files:
                    path = mod.new_path or mod.old_path
                    if not path:
                        continue
                    
                    all_modified_files.append(path)
                    
                    if touches_prompt(mod):
                        prompt_files.append(path)

                # STEP 2 FILTER: Commit MUST touch at least one prompt artifact
                if prompt_files:
                    prompt_commits_found += 1
                    prompt_commit_records.append({
                        "category": category,
                        "repo_name": repo_name,
                        "commit_hash": commit.hash,
                        "author_date": commit.author_date.strftime("%Y-%m-%d %H:%M:%S"),
                        "msg": commit.msg.split("\n")[0][:120],
                        "prompt_files": ";".join(set(prompt_files)),
                        "all_modified_files": ";".join(all_modified_files)
                    })

            print(f"   -> Scanned {scanned_commits} commits | Extracted {prompt_commits_found} prompt-touching commits")

        except Exception as e:
            print(f"   ⚠️ Error mining {repo_name}: {e}")
        finally:
            shutil.rmtree(temp_repo_dir, ignore_errors=True)

    # Save to prompt_commits.csv
    fieldnames = ["category", "repo_name", "commit_hash", "author_date", "msg", "prompt_files", "all_modified_files"]
    with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(prompt_commit_records)

    print("\n==================================================")
    print(f"✅ Step 2 Complete! Total Prompt Commits Extracted: {len(prompt_commit_records)}")
    print(f"📁 Saved to: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step2()