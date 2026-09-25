import os
import csv
import re
import json
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

# NEW: extracts the LHS identifier of an assignment line, applied to a single
# plain source line (no +/- diff prefix needed) -- used for upward scanning.
# Handles both Python ("x = ...") and JS/TS declaration syntax
# ("export const x = ...", "let x: Foo = ...").
ASSIGNMENT_LINE_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var|public|private|readonly|static)?\s*"
    r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s*(?::\s*[\w.\[\]<>, ]+)?\s*=(?!=)"
)

# Prefer variable names that look prompt-related; used as tier 1 of extraction
PROMPT_NAME_HINT = re.compile(r"(?i)(prompt|template|instruction|system_message|user_message)")

# Tier-2 fallback: generic assignment names, but skip very common/noisy identifiers
GENERIC_NAME_STOPLIST = {
    "self", "data", "result", "response", "output", "text", "value", "i", "j", "k",
    "id", "url", "path", "name", "msg", "err", "error", "list", "dict", "str", "int",
    "float", "bool", "true", "false", "none", "args", "kwargs", "res", "req", "resp",
}

MAX_SCAN_BACK = 60  # lines to look upward for the owning variable declaration

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
    is_text_edit_in_prompt_file = is_prompt_file and len(diff_text.strip()) > 0

    return has_prompt_pattern or is_text_edit_in_prompt_file


def _owning_variable(source_lines, changed_line_no):
    """
    Walks upward from a changed line (1-indexed, as pydriller reports it) to find
    the nearest preceding assignment -- i.e. which variable's definition this
    changed line actually lives inside. This is necessary because a multi-line
    prompt string's declaration line ('executeTaskPrompt = new PromptTemplate({')
    is very often NOT itself part of the diff -- only the text a few lines below
    it changed -- so grepping the diff snippet alone misses it entirely.
    """
    start = min(changed_line_no, len(source_lines))
    for i in range(start - 1, max(start - 1 - MAX_SCAN_BACK, -1), -1):
        m = ASSIGNMENT_LINE_PATTERN.match(source_lines[i])
        if m:
            return m.group(1)
    return None


def extract_prompt_var_names(mod):
    """
    Finds the variable(s) that 'own' the changed lines in this modification, by
    scanning the FULL before/after source (not just the diff snippet) upward from
    each changed line number. Falls back to tier-2 generic names only if no
    prompt-hinted name was found anywhere.
    """
    names = set()
    diff_parsed = mod.diff_parsed or {}

    after_lines = (mod.source_code or "").split("\n")
    for line_no, _ in diff_parsed.get("added", []):
        var = _owning_variable(after_lines, line_no)
        if var:
            names.add(var)

    before_lines = (mod.source_code_before or "").split("\n")
    for line_no, _ in diff_parsed.get("deleted", []):
        var = _owning_variable(before_lines, line_no)
        if var:
            names.add(var)

    tier1 = {n for n in names if PROMPT_NAME_HINT.search(n)}
    if tier1:
        return tier1
    tier2 = {n for n in names if len(n) >= 4 and n.lower() not in GENERIC_NAME_STOPLIST}
    return tier2


def execute_step2_precise():
    print("=== Step 2: High-Precision Prompt Commit Scraping (All Repos) ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 1 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        repos = list(csv.DictReader(f))

    fieldnames = [
        "category", "repo_name", "commit_hash", "author_date", "msg",
        "prompt_files", "prompt_var_names", "all_modified_files",
        "consumer_candidate_diffs",  # NEW: JSON {filepath: diff_text} for non-prompt .py files
    ]

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

            repo_miner = Repository(
                clone_url,
                clone_repo_to=temp_repo_dir,
                since=START_DATE,
                only_modifications_with_file_types=['.py', '.rail', '.yaml', '.json', '.toml']
            )

            for commit in repo_miner.traverse_commits():
                scanned += 1
                if len(commit.parents) > 1:  # Skip merge commits
                    continue

                prompt_files = []
                prompt_var_names = set()
                all_modified_files = []
                consumer_candidate_diffs = {}  # filepath -> diff text, non-prompt .py files only

                for mod in commit.modified_files:
                    path = mod.new_path or mod.old_path
                    if not path:
                        continue

                    all_modified_files.append(path)
                    is_prompt = touches_prompt_cross_verified(mod)

                    if is_prompt:
                        prompt_files.append(path)
                        prompt_var_names |= extract_prompt_var_names(mod)
                    elif path.lower().endswith(".py"):
                        # NEW: keep the diff of every co-changed Python file so Step 3
                        # can grep it for the prompt variable name, the same way we
                        # traced serper.ts by hand with `grep -rn "summarizeSearchSnippets"`.
                        consumer_candidate_diffs[path] = mod.diff or ""

                if prompt_files:
                    found += 1
                    record = {
                        "category": category,
                        "repo_name": repo_name,
                        "commit_hash": commit.hash,
                        "author_date": commit.author_date.strftime("%Y-%m-%d %H:%M:%S"),
                        "msg": commit.msg.split("\n")[0][:120],
                        "prompt_files": ";".join(set(prompt_files)),
                        "prompt_var_names": ";".join(sorted(prompt_var_names)),
                        "all_modified_files": ";".join(all_modified_files),
                        "consumer_candidate_diffs": json.dumps(consumer_candidate_diffs),
                    }

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