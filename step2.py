import os
import ast
import csv
import sys
import re
import json
import tempfile
import shutil
from datetime import datetime
from pydriller import Repository


def set_max_csv_field_limit():
    max_int = sys.maxsize
    while True:
        try:
            csv.field_size_limit(max_int)
            break
        except OverflowError:
            max_int = max_int // 10


set_max_csv_field_limit()

CSV_INPUT = "target_repos.csv"
CSV_OUTPUT = "prompt_commits.csv"
CHECKPOINT_OUTPUT = "step2_checkpoint.json"
RESET_FLAG = "--reset"
PROMPT_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml"}

# Temporal threshold: LLM structured prompt era
START_DATE = datetime(2024, 1, 1)

# Prefer variable names that look prompt-related; used as tier 1 of extraction
PROMPT_NAME_HINT = re.compile(r"(?i)(prompt|template|instruction|system_message|user_message)")

PROMPT_TEXT_HINT = re.compile(
    r"(?i)\b(you are|your task|answer|respond|summarize|classify|extract|"
    r"following|must|return|do not|json|yaml|xml|bullet|format)\b"
)

PROMPT_CONTEXT_HINT = re.compile(
    r"(?i)\b(prompt|template|message|messages|system|user|developer|"
    r"instruction|example|output_format|response_format)\b"
)

PROMPT_CALL_HINT = re.compile(
    r"(?i)(prompt|template|message|chat_completion|completion|generate)"
)

PROMPT_STRUCTURE_KEYS = {
    "role", "content", "messages", "message", "system", "user", "developer"
}

PROMPT_MESSAGE_ROLES = {"system", "user", "developer", "assistant"}



def touches_prompt_cross_verified(mod):
    filename = (mod.filename or "").lower()
    extension = os.path.splitext(filename)[1]

    # Dedicated prompt formats are content-bearing by definition, but still
    # require a non-empty diff.
    if extension in {".rail", ".prompt"}:
        return bool((mod.diff or "").strip())

    if extension in PROMPT_CONFIG_EXTENSIONS:
        return touches_prompt_config(mod)

    return extension == ".py" and analyze_prompt_modification(mod)[0]


def touches_prompt_config(mod):
    diff_lines = [text for _, text in (mod.diff_parsed or {}).get("added", [])]
    diff_lines += [text for _, text in (mod.diff_parsed or {}).get("deleted", [])]
    changed_text = "\n".join(diff_lines)

    if not changed_text.strip():
        return False

    config_key_hint = re.search(
        r"(?im)(?:^|[,{\[]|\n)\s*[\"']?(prompt|template|system_prompt|user_prompt|"
        r"messages?|developer|instructions?|examples?|output[_ -]?format|"
        r"response[_ -]?format)[\"']?\s*:",
        changed_text,
    )
    return bool(config_key_hint)



def _changed_line_numbers(diff_parsed, side):
    return {
        line_no
        for line_no, _ in (diff_parsed or {}).get(side, [])
        if line_no is not None
    }


def _node_overlaps_lines(node, changed_lines):
    first = getattr(node, "lineno", None)
    last = getattr(node, "end_lineno", first)
    return bool(first and last and any(first <= line <= last for line in changed_lines))


def _attribute_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _ast_prompt_analysis(source, changed_lines):
    if not source or not changed_lines:
        return False, set()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, set()

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    found_prompt = False
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not _node_overlaps_lines(node, changed_lines):
            continue
        if isinstance(parents.get(node), ast.Dict) and node in parents[node].keys:
            continue

        assignment_names = []
        call_names = []
        context_keys = []
        message_roles = set()
        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                assignment_names.extend(
                    name for target in targets if (name := _attribute_name(target))
                )
            elif isinstance(parent, ast.Call):
                if (call_name := _attribute_name(parent.func)):
                    call_names.append(call_name)
            elif isinstance(parent, ast.Dict):
                for key, value in zip(parent.keys, parent.values):
                    if key is not None and value is current:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            context_keys.append(key.value)
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "role"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        message_roles.add(value.value.lower())
            current = parent

        prompt_named_assignment = any(
            PROMPT_NAME_HINT.search(name) for name in assignment_names
        )
        prompt_call = any(PROMPT_CALL_HINT.search(name) for name in call_names)
        lowered_context_keys = {key.lower() for key in context_keys}
        inline_message_content = "content" in lowered_context_keys
        if inline_message_content and message_roles:
            inline_message_content = bool(message_roles & PROMPT_MESSAGE_ROLES)
        has_prompt_text = bool(PROMPT_TEXT_HINT.search(node.value))
        is_named_prompt = (prompt_named_assignment or prompt_call) and has_prompt_text
        if not (inline_message_content or is_named_prompt):
            continue

        found_prompt = True
        base_name = next(
            (name for name in assignment_names if PROMPT_NAME_HINT.search(name)),
            assignment_names[-1] if inline_message_content and assignment_names else None,
        )

        if base_name and context_keys:
            names.add(f'{base_name}["{context_keys[-1]}"]')
        elif base_name:
            names.add(base_name)

    return found_prompt, names


def analyze_prompt_modification(mod):
    diff_parsed = mod.diff_parsed or {}
    found_prompt = False
    names = set()
    for source, side in (
        (mod.source_code or "", "added"),
        (mod.source_code_before or "", "deleted"),
    ):
        detected, source_names = _ast_prompt_analysis(
            source, _changed_line_numbers(diff_parsed, side)
        )
        found_prompt = found_prompt or detected
        names |= source_names
    return found_prompt, names


def _ast_prompt_names(source, changed_lines):
    return _ast_prompt_analysis(source, changed_lines)[1]


def extract_prompt_var_names(mod):
    return analyze_prompt_modification(mod)[1]


def load_resume_state():
    completed_repositories = set()
    existing_commit_keys = set()

    if os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, mode="r", encoding="utf-8", newline="") as f:
            for record in csv.DictReader(f):
                repo_name = record.get("repo_name", "")
                commit_hash = record.get("commit_hash", "")
                if repo_name:
                    completed_repositories.add(repo_name)
                if repo_name and commit_hash:
                    existing_commit_keys.add((repo_name, commit_hash))

    if os.path.exists(CHECKPOINT_OUTPUT) and os.path.exists(CSV_OUTPUT):
        with open(CHECKPOINT_OUTPUT, mode="r", encoding="utf-8") as f:
            checkpoint = json.load(f)
        completed_repositories = set(checkpoint.get("completed_repositories", []))

    return completed_repositories, existing_commit_keys


def save_resume_state(completed_repositories):
    temporary_checkpoint = f"{CHECKPOINT_OUTPUT}.tmp"
    with open(temporary_checkpoint, mode="w", encoding="utf-8") as f:
        json.dump(
            {"completed_repositories": sorted(completed_repositories)},
            f,
            indent=2,
        )
    os.replace(temporary_checkpoint, CHECKPOINT_OUTPUT)


def reset_output_state():
    for path in (CSV_OUTPUT, CHECKPOINT_OUTPUT):
        if os.path.exists(path):
            os.remove(path)


def execute_step2_precise():
    print("=== Step 2: High-Precision Prompt Commit Scraping (All Repos) ===\n")

    if RESET_FLAG in sys.argv:
        reset_output_state()
        print("Reset requested: rebuilding prompt_commits.csv from scratch.\n")

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

    completed_repositories, existing_commit_keys = load_resume_state()
    if not os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

    if completed_repositories:
        print(
            f"Resuming: {len(completed_repositories)} repositories already completed.\n"
        )

    total_prompt_commits = len(existing_commit_keys)

    for idx, repo in enumerate(repos, 1):
        repo_name = repo["repo_name"]
        clone_url = repo["clone_url"]
        category = repo["category"]

        if repo_name in completed_repositories:
            print(f"[{idx}/{len(repos)}] Skipping completed: {repo_name}")
            continue

        print(f"[{idx}/{len(repos)}] Processing: {repo_name}...")
        temp_repo_dir = tempfile.mkdtemp()

        try:
            scanned = 0
            found = 0

            repo_miner = Repository(
                clone_url,
                clone_repo_to=temp_repo_dir,
                since=START_DATE,
                only_modifications_with_file_types=['.py', '.prompt', '.rail', '.yaml', '.yml', '.json']
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
                    is_python = path.lower().endswith(".py")
                    if is_python:
                        is_prompt, prompt_names_for_mod = analyze_prompt_modification(mod)
                    else:
                        is_prompt = touches_prompt_cross_verified(mod)
                        prompt_names_for_mod = set()

                    if is_prompt:
                        prompt_files.append(path)
                        prompt_var_names |= prompt_names_for_mod
                    elif path.lower().endswith(".py"):
                        # NEW: keep the diff of every co-changed Python file so Step 3
                        # can grep it for the prompt variable name, the same way we
                        # traced serper.ts by hand with `grep -rn "summarizeSearchSnippets"`.
                        consumer_candidate_diffs[path] = mod.diff or ""

                if prompt_files:
                    commit_key = (repo_name, commit.hash)
                    if commit_key in existing_commit_keys:
                        continue

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
                    existing_commit_keys.add(commit_key)

            total_prompt_commits += found
            print(f"   ✓ Scanned {scanned} commits | Found {found} high-precision prompt commits")

        except Exception as e:
            print(f"   ⚠️ Skipped {repo_name} (Error/Timeout): {e}")
        else:
            completed_repositories.add(repo_name)
            save_resume_state(completed_repositories)
            print(f"   Checkpoint saved: {len(completed_repositories)}/{len(repos)} repositories")
        finally:
            shutil.rmtree(temp_repo_dir, ignore_errors=True)

    print("\n==================================================")
    print(f" Step 2 Complete! Total High-Precision Prompt Commits: {total_prompt_commits}")
    print(f" Saved to: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")


if __name__ == "__main__":
    execute_step2_precise()