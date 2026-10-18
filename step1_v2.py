#!/usr/bin/env python3
"""
Step 1 (v2) - Repository selection, built to yield MORE prompt edits per repo.

Improvements over v1
  * pagination + star-range slicing (v1 only read page 1 = 30 repos/query)
  * enforces the paper's criteria: stars >= N, commits since 2024 >= N (v1 never filtered)
  * excludes templates / starters / tutorials (they mostly create files -> 'skipped' units)
  * cheap pre-screen: repos whose commit messages never mention "prompt" are dropped
  * append-only + resume-safe: re-running only examines NEW repos, keeps existing rows,
    so Steps 2 / 2b can be re-run without --reset and process only the new repos

Usage:
  python step1_v2.py --target-new 300
  python step1_v2.py --min-commits 100 --min-prompt-hits 3 --max-pages 5
Needs GITHUB_TOKEN in .env (without it you get ~60 requests/hour = unusable).
"""
import argparse
import csv
import json
import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"} if TOKEN else {}

CSV_OUT = "target_repos.csv"
SEEN_FILE = "step1_seen.json"
FIELDS = ["category", "repo_name", "clone_url", "stars", "commits_since_2024",
          "default_branch", "manifests", "prompt_commit_hits"]

# (category label used by Step 2, GitHub search keywords). Qualifiers are added automatically.
QUERIES = [
    ("instructor",   "instructor structured output pydantic"),
    ("pydantic-ai",  "pydantic-ai"),
    ("langchain",    "langchain agent"),
    ("langchain",    "langchain rag"),
    ("guardrails",   "guardrails llm validation"),
    ("outlines",     "outlines structured generation"),
    ("llm-app",      "topic:llm"),
    ("llm-app",      "topic:ai-agents"),
    ("llm-app",      "topic:rag"),
    ("llm-app",      "topic:openai"),
]
STAR_SLICES = [(10, 30), (31, 100), (101, 300), (301, 1000), (1001, 5000), (5001, None)]

# names that signal boilerplate / learning repos (their history is mostly initial commits)
BAD_NAME = re.compile(r"(?i)(template|starter|boilerplate|cookiecutter|scaffold|tutorial|course|"
                      r"workshop|awesome|cheatsheet|learn|bootcamp|demo|example|sample|playground)")
MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py",
             "setup.cfg", "poetry.lock", "uv.lock")


# ---------------------------------------------------------------- GitHub helpers
def gh(url, params=None, search=False):
    """GET with rate-limit awareness. Returns Response or None."""
    for attempt in range(5):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"   network error: {e}; retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0" \
                or r.status_code == 429:
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(5, min(reset - int(time.time()) + 2, 3700))
            print(f"   rate limit hit; sleeping {wait}s")
            time.sleep(wait)
            continue
        if search:
            time.sleep(2.2)                      # search API: 30 requests/minute
        return r
    return None


def search_repos(keywords, lo, hi, pushed_after, max_pages):
    stars = f"{lo}..{hi}" if hi else f">={lo}"
    q = (f"{keywords} language:python stars:{stars} pushed:>={pushed_after} "
         f"fork:false archived:false template:false")
    for page in range(1, max_pages + 1):
        r = gh("https://api.github.com/search/repositories",
               {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page}, search=True)
        if r is None or r.status_code != 200:
            print(f"   search failed ({getattr(r, 'status_code', '?')}) for: {q[:70]}")
            return
        items = r.json().get("items", [])
        yield from items
        if len(items) < 100:
            return


def commits_since(full, branch, since="2024-01-01T00:00:00Z"):
    r = gh(f"https://api.github.com/repos/{full}/commits", {"sha": branch, "since": since, "per_page": 1})
    if r is None or r.status_code != 200:
        return None
    m = re.search(r"[?&]page=(\d+)>; rel=\"last\"", r.headers.get("Link", ""))
    return int(m.group(1)) if m else len(r.json())


def prompt_commit_hits(full):
    """Pre-screen proxy: commits since 2024 whose MESSAGE mentions 'prompt'."""
    r = gh("https://api.github.com/search/commits",
           {"q": f"prompt repo:{full} committer-date:>=2024-01-01", "per_page": 1}, search=True)
    if r is None or r.status_code != 200:
        return None
    return r.json().get("total_count", 0)


def tree_check(full, branch, need_tests):
    r = gh(f"https://api.github.com/repos/{full}/git/trees/{branch}?recursive=1")
    if r is None or r.status_code != 200:
        return False, [], "tree_unavailable"
    data = r.json()
    if data.get("truncated"):
        return False, [], "tree_truncated"
    paths = [t["path"].lower() for t in data.get("tree", [])]
    has_tests = any(p.startswith(("test/", "tests/")) or "/test/" in p or "/tests/" in p
                    or p.endswith(("pytest.ini", "tox.ini")) for p in paths)
    mans = [p for p in paths if p.rsplit("/", 1)[-1] in MANIFESTS]
    if not mans:
        return False, [], "no_manifest"
    if need_tests and not has_tests:
        return False, [], "no_tests"
    return True, mans[:3], "ok"


# ---------------------------------------------------------------- persistence
def load_seen():
    return set(json.load(open(SEEN_FILE))) if os.path.exists(SEEN_FILE) else set()


def save_seen(seen):
    json.dump(sorted(seen), open(SEEN_FILE, "w"))


def ensure_csv():
    """Create file / upgrade an old v1 file to the new header without losing rows."""
    rows = []
    if os.path.exists(CSV_OUT):
        rows = list(csv.DictReader(open(CSV_OUT, encoding="utf-8")))
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return {r["repo_name"] for r in rows}


def append_row(row):
    with open(CSV_OUT, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-stars", type=int, default=10)
    ap.add_argument("--min-commits", type=int, default=100, help="commits since 2024-01-01")
    ap.add_argument("--max-commits", type=int, default=20000)
    ap.add_argument("--min-prompt-hits", type=int, default=3,
                    help="commits whose message mentions 'prompt' (0 disables the pre-screen)")
    ap.add_argument("--pushed-after", default="2025-01-01")
    ap.add_argument("--max-pages", type=int, default=5, help="pages of 100 per (query, star slice)")
    ap.add_argument("--target-new", type=int, default=300, help="stop after this many NEW repos")
    ap.add_argument("--no-test-filter", action="store_true", help="do not require a tests/ folder")
    a = ap.parse_args()

    if not TOKEN:
        print("WARNING: no GITHUB_TOKEN in .env - the API will rate-limit you almost immediately.")

    known = ensure_csv()                      # repos already in target_repos.csv (kept as is)
    seen = load_seen() | known
    new = 0
    reasons = {}
    print(f"existing repos: {len(known)} | examined before: {len(seen)}\n")

    def reject(why):
        reasons[why] = reasons.get(why, 0) + 1

    for category, kw in QUERIES:
        for lo, hi in STAR_SLICES:
            if hi is not None and hi < a.min_stars:
                continue
            lo = max(lo, a.min_stars)
            print(f"[{category}] '{kw}' stars {lo}..{hi or 'inf'}  (new so far: {new})")
            for repo in search_repos(kw, lo, hi, a.pushed_after, a.max_pages):
                full = repo["full_name"]
                if full in seen:
                    continue
                seen.add(full)
                if BAD_NAME.search(full.split("/")[-1]):
                    reject("template_like_name")
                    continue
                branch = repo.get("default_branch", "main")
                n_commits = commits_since(full, branch)
                if n_commits is None or not (a.min_commits <= n_commits <= a.max_commits):
                    reject("commit_count")
                    continue
                hits = 0
                if a.min_prompt_hits:
                    hits = prompt_commit_hits(full)
                    if hits is None or hits < a.min_prompt_hits:
                        reject("few_prompt_commits")
                        continue
                ok, mans, why = tree_check(full, branch, not a.no_test_filter)
                if not ok:
                    reject(why)
                    continue
                append_row({"category": category, "repo_name": full, "clone_url": repo["clone_url"],
                            "stars": repo["stargazers_count"], "commits_since_2024": n_commits,
                            "default_branch": branch, "manifests": ";".join(mans),
                            "prompt_commit_hits": hits})
                new += 1
                print(f"  [QUALIFIED] {full} (stars {repo['stargazers_count']}, "
                      f"commits {n_commits}, prompt-msg commits {hits})")
                save_seen(seen)
                if new >= a.target_new:
                    print(f"\nTarget of {a.target_new} new repos reached.")
                    _summary(new, known, reasons)
                    return
            save_seen(seen)
    _summary(new, known, reasons)


def _summary(new, known, reasons):
    print("\n=== Step 1 v2 summary ===")
    print(f"new repos added : {new}   total in {CSV_OUT}: {len(known) + new}")
    print("rejections      :", dict(sorted(reasons.items(), key=lambda x: -x[1])))
    print("Next: python step2.py ; python step2b.py   (no --reset: only new repos are processed)")


if __name__ == "__main__":
    main()