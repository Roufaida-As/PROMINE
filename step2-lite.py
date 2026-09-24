import os
import csv
import re
import tempfile
import shutil
from datetime import datetime
from pydriller import Repository

CSV_INPUT = "target_repos.csv"
CSV_OUTPUT = "prompt_commits.csv"

# Date limite : On ne scanne que l'ère LLM récente (depuis Janvier 2024)
START_DATE = datetime(2024, 1, 1)

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
    filename = (mod.filename or "").lower()
    diff_text = mod.diff or ""

    is_prompt_file = any(k in filename for k in ["prompt", "template", "spec", ".rail"])
    has_prompt_diff = any(re.search(pat, diff_text, re.IGNORECASE) for pat in PROMPT_PATTERNS)

    return is_prompt_file or has_prompt_diff

def execute_step2_fast():
    print("=== Step 2: Accelerated Prompt-Commit Mining (2024-Present) ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f"❌ Error: {CSV_INPUT} not found! Run Step 1 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        repos = list(csv.DictReader(f))

    fieldnames = ["category", "repo_name", "commit_hash", "author_date", "msg", "prompt_files", "all_modified_files"]
    
    # Création / Écriture de l'en-tête CSV
    with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

    total_prompt_commits = 0

    for idx, repo in enumerate(repos, 1):
        repo_name = repo["repo_name"]
        clone_url = repo["clone_url"]
        category = repo["category"]

        print(f"[{idx}/{len(repos)}] Mining (since 2024): {repo_name}...")
        temp_repo_dir = tempfile.mkdtemp()

        try:
            scanned = 0
            found = 0

            # Optimisation PyDriller : Filtre de date + extensions de fichiers autorisées
            repo_miner = Repository(
                clone_url,
                clone_repo_to=temp_repo_dir,
                since=START_DATE,
                only_modifications_with_file_types=['.py', '.rail', '.yaml', '.json', '.toml']
            )

            for commit in repo_miner.traverse_commits():
                scanned += 1
                if len(commit.parents) > 1:
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
                    
                    # Sauvegarde progressive au fur et à mesure
                    with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writerow(record)

            total_prompt_commits += found
            print(f"   -> Scanned {scanned} commits | Extracted {found} prompt commits")

        except Exception as e:
            print(f"   ⚠️ Skipping {repo_name} due to error/timeout: {e}")
        finally:
            shutil.rmtree(temp_repo_dir, ignore_errors=True)

    print("\n==================================================")
    print(f"✅ Step 2 Complete! Total Prompt Commits Saved: {total_prompt_commits}")
    print(f"📁 Saved to: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step2_fast()