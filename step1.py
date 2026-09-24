import os
import csv
import time
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Security Notice: Remember to keep API tokens out of production source code or public repos.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

TARGET_QUERIES = [
    ("instructor", "instructor pydantic language:python"),
    ("guardrails-ai", "guardrails language:python"),
    ("langchain", "langchain template language:python"),
    ("pydantic-ai", "pydantic-ai agent language:python"),
    ("outlines", "outlines llm structured language:python"),
]

def search_repositories(query_str, min_stars=5, updated_after="2025-01-01"):
    url = "https://api.github.com/search/repositories"
    full_q = f"{query_str} stars:>={min_stars} pushed:>={updated_after}"
    params = {"q": full_q, "sort": "stars", "order": "desc", "per_page": 30}
    
    res = requests.get(url, headers=HEADERS, params=params)
    if res.status_code == 403:
        print("Rate limit reached. Waiting 60 seconds...")
        time.sleep(60)
        return []
    elif res.status_code != 200:
        return []
    return res.json().get("items", [])

def check_repo_tree_deep(full_name, default_branch="main"):
    url = f"https://api.github.com/repos/{full_name}/git/trees/{default_branch}?recursive=1"
    res = requests.get(url, headers=HEADERS)
    
    if res.status_code != 200 and default_branch == "main":
        url = f"https://api.github.com/repos/{full_name}/git/trees/master?recursive=1"
        res = requests.get(url, headers=HEADERS)
        
    if res.status_code != 200:
        return False, []

    tree = res.json().get("tree", [])
    paths = [item["path"].lower() for item in tree]

    has_tests = any(
        "test" in p or "tests" in p or p.endswith("pytest.ini") 
        for p in paths
    )
    
    manifests = [
        p for p in paths 
        if any(p.endswith(m) for m in ["pyproject.toml", "requirements.txt", "setup.py"])
    ]
    
    return (has_tests and len(manifests) > 0), manifests[:3]

def execute_step_1_fixed():
    qualified_dataset = []
    print("=== Step 1: Repository Selection & CSV Export ===\n")

    for category, query in TARGET_QUERIES:
        print(f"Targeting Framework Category: '{category}'...")
        repos = search_repositories(query)
        
        for repo in repos:
            repo_name = repo["full_name"]
            stars = repo["stargazers_count"]
            branch = repo.get("default_branch", "main")
            clone_url = repo["clone_url"]
            
            is_qualified, found_manifests = check_repo_tree_deep(repo_name, branch)
            time.sleep(0.3)
            
            if is_qualified:
                qualified_dataset.append({
                    "category": category,
                    "repo_name": repo_name,
                    "clone_url": clone_url,
                    "stars": stars,
                    "default_branch": branch,
                    "manifests": ";".join(found_manifests)
                })
                print(f"  [QUALIFIED] {repo_name} (Stars: {stars})")

    # Export to CSV
    csv_filename = "target_repos.csv"
    fieldnames = ["category", "repo_name", "clone_url", "stars", "default_branch", "manifests"]
    
    with open(csv_filename, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(qualified_dataset)

    print(f"\n✅ Total Qualified Repositories Exported: {len(qualified_dataset)}")
    print(f"📁 Dataset written to {os.path.abspath(csv_filename)}")
    return qualified_dataset

if __name__ == "__main__":
    execute_step_1_fixed()