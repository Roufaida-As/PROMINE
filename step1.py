import os
import csv
import re
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
    ("outlines", "outlines language:python"),
]

def search_repositories(query_str, min_stars=5, updated_after="2025-01-01"):
    url = "https://api.github.com/search/repositories"
    full_q = f"{query_str} stars:>={min_stars} pushed:>={updated_after}"
    params = {"q": full_q, "sort": "stars", "order": "desc", "per_page": 30}
    
    res = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if res.status_code == 403:
        print("Rate limit reached. Waiting 60 seconds...")
        time.sleep(60)
        return []
    elif res.status_code != 200:
        print(f"Repository search failed ({res.status_code}): {res.text[:200]}")
        return []
    return res.json().get("items", [])

def check_repo_tree_deep(full_name, default_branch="main"):
    url = f"https://api.github.com/repos/{full_name}/git/trees/{default_branch}?recursive=1"
    res = requests.get(url, headers=HEADERS, timeout=30)
    
    if res.status_code != 200 and default_branch == "main":
        url = f"https://api.github.com/repos/{full_name}/git/trees/master?recursive=1"
        res = requests.get(url, headers=HEADERS, timeout=30)
        
    if res.status_code != 200:
        print(f"Tree lookup failed for {full_name} ({res.status_code})")
        return False, []

    tree_data = res.json()
    if tree_data.get("truncated"):
        print(f"Tree is truncated for {full_name}; skipping qualification")
        return False, []

    tree = tree_data.get("tree", [])
    paths = [item["path"].lower() for item in tree]

    has_tests = any(
        p.startswith(("test/", "tests/"))
        or "/test/" in p
        or "/tests/" in p
        or p.endswith(("pytest.ini", "tox.ini"))
        for p in paths
    )

    manifest_names = (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "poetry.lock",
        "uv.lock",
    )
    manifests = [
        p for p in paths 
        if p.rsplit("/", 1)[-1] in manifest_names
    ]
    
    return (has_tests and len(manifests) > 0), manifests[:3]

def count_commits_since(full_name, branch, since="2024-01-01T00:00:00Z"):
    url = f"https://api.github.com/repos/{full_name}/commits"
    params = {"sha": branch, "since": since, "per_page": 1}
    res = requests.get(url, headers=HEADERS, params=params, timeout=30)

    if res.status_code != 200:
        print(f"Commit lookup failed for {full_name} ({res.status_code})")
        return ""

    last_page = re.search(r"[?&]page=(\d+)>; rel=\"last\"", res.headers.get("Link", ""))
    return int(last_page.group(1)) if last_page else len(res.json())

def execute_step_1_fixed():
    qualified_dataset = []
    seen_repositories = set()
    print("=== Step 1: Repository Selection & CSV Export ===\n")

    for category, query in TARGET_QUERIES:
        print(f"Targeting Framework Category: '{category}'...")
        repos = search_repositories(query)
        
        for repo in repos:
            repo_name = repo["full_name"]
            if repo_name in seen_repositories:
                print(f"  [DUPLICATE SKIPPED] {repo_name} (already found in another category)")
                continue
            seen_repositories.add(repo_name)

            stars = repo["stargazers_count"]
            branch = repo.get("default_branch", "main")
            clone_url = repo["clone_url"]
            
            is_qualified, found_manifests = check_repo_tree_deep(repo_name, branch)
            time.sleep(0.3)
            
            if is_qualified:
                commits_since_2024 = count_commits_since(repo_name, branch)
                qualified_dataset.append({
                    "category": category,
                    "repo_name": repo_name,
                    "clone_url": clone_url,
                    "stars": stars,
                    "commits_since_2024": commits_since_2024,
                    "default_branch": branch,
                    "manifests": ";".join(found_manifests)
                })
                print(
                    f"  [QUALIFIED] {repo_name} "
                    f"(Stars: {stars}, Commits since 2024: {commits_since_2024})"
                )

    # Export to CSV
    csv_filename = "target_repos.csv"
    fieldnames = [
        "category",
        "repo_name",
        "clone_url",
        "stars",
        "commits_since_2024",
        "default_branch",
        "manifests",
    ]
    
    with open(csv_filename, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(qualified_dataset)

    print(f"\nTotal Qualified Repositories Exported: {len(qualified_dataset)}")
    print(f" Dataset written to {os.path.abspath(csv_filename)}")
    return qualified_dataset

if __name__ == "__main__":
    execute_step_1_fixed()