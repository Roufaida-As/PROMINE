import os
import ast
import csv
import sys
import re
import json
import tempfile
import shutil
from datetime import datetime
import argparse
import subprocess
import time
import warnings
from collections import Counter
from pydriller import Repository, ModificationType

warnings.filterwarnings("ignore")
try:   # Windows consoles (cp1252) crash on non-ASCII output
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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
STATS_OUTPUT = "step2_repo_stats.csv"     # per-repo funnel numbers (for the paper)
MAX_FILES_PER_COMMIT = 30                 # bulk commits are skipped (same limit as step2b)
MAX_FILE_CHARS = 2_000_000
MAX_CONSUMER_FILES = 30                   # cap what we store in consumer_candidate_diffs
MAX_DIFF_CHARS = 20000
MAX_GATE_CHARS = 500_000                  # skip AST gate on giant files (one repo took 1h because of them)
CODE_EXTS = (".py", ".prompt", ".rail", ".yaml", ".yml", ".json")
FAILED_FILE = "step2_failed.json"         # repo -> number of failed attempts
MAX_ATTEMPTS = 3
NET_ERRORS = ("Could not resolve host", "Failed to connect", "RPC failed", "early EOF",
              "Connection", "timed out", "unexpected disconnect")
BOT_AUTHOR = re.compile(r"(?i)(\[bot\]|dependabot|renovate|github-actions)")
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
    """Checkpoint is the single source of truth for finished repos (a repo that crashed
    half-way is NOT treated as complete); commit keys only prevent duplicate rows."""
    completed, keys = set(), set()
    if os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, encoding="utf-8", newline="") as f:
            for rec in csv.DictReader(f):
                if rec.get("repo_name") and rec.get("commit_hash"):
                    keys.add((rec["repo_name"], rec["commit_hash"]))
    if os.path.exists(CHECKPOINT_OUTPUT):
        with open(CHECKPOINT_OUTPUT, encoding="utf-8") as f:
            completed = set(json.load(f).get("completed_repositories", []))
    return completed, keys


def save_resume_state(completed):
    tmp = f"{CHECKPOINT_OUTPUT}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"completed_repositories": sorted(completed)}, f, indent=2)
    os.replace(tmp, CHECKPOINT_OUTPUT)


def reset_output_state():
    for path in (CSV_OUTPUT, CHECKPOINT_OUTPUT, STATS_OUTPUT):
        if os.path.exists(path):
            os.remove(path)


def shallow_clone(url, dest, timeout=3600):
    """History since 2024 only, NO working-tree checkout (avoids Windows long-path / invalid-filename
    failures; PyDriller reads git objects). Retries network errors. False = no commits in window."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for attempt in range(3):
        p = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--no-tags",
                            "--shallow-since=2023-12-31", url, dest],
                           capture_output=True, timeout=timeout, env=env)
        if p.returncode == 0:
            return True
        err = p.stderr.decode(errors="replace")
        if "no commits selected" in err:
            return False
        if attempt < 2 and any(k in err for k in NET_ERRORS):
            print(f"   network error, retrying in {30 * (attempt + 1)}s...")
            shutil.rmtree(dest, ignore_errors=True)
            os.makedirs(dest, exist_ok=True)
            time.sleep(30 * (attempt + 1))
            continue
        raise RuntimeError(err[:300])


def confirm_real_edit(mod, path):
    """Strict second stage. Returns (ok, reason). Uses the SAME extraction as step2b, so a commit
    only qualifies if an existing prompt string really changed (not created/deleted/moved to
    tests/examples, not whitespace-only, not just a python {expr} change)."""
    import step2b as s2b                      # lazy import (step2b imports hints from this file)
    if mod.change_type != ModificationType.MODIFY:
        return False, "not_modify(add/delete/rename)"
    if s2b._TEST.search(path) or s2b._EXAMPLE.search(path) or s2b._TOOLCFG.search(path):
        return False, "test_example_or_toolconfig_path"
    old, new = mod.source_code_before or "", mod.source_code or ""
    if len(old) > MAX_FILE_CHARS or len(new) > MAX_FILE_CHARS:
        return False, "file_too_big"
    o, n = s2b.extract(old, path), s2b.extract(new, path)
    if o is None or n is None:
        return False, "unparsable_or_yaml_missing"
    for ctype, _, _, a, b, mo, mn in s2b.pair_units(o, n):
        if ctype != "modified":
            continue
        sig = s2b.signals(a, b, mo, mn)
        if not (sig["surface_only"] or sig["reorder_only"] or sig["interp_only"]):
            return True, "ok"
    return False, "no_real_prompt_edit(added/removed/surface-only/short strings)"


def append_stats(row):
    new_file = not os.path.exists(STATS_OUTPUT)
    with open(STATS_OUTPUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new_file:
            w.writeheader()
        w.writerow(row)


def execute_step2_precise():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--max-commits", type=int, default=0, help="skip repos with more commits since 2024")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N pending repos")
    ap.add_argument("--only-repo", help="process just this repo_name")
    ap.add_argument("--retry-failed", action="store_true", help="also retry repos that failed 3 times")
    ap.add_argument("--repo-timeout", type=int, default=90, help="minutes per repo (partial results kept)")
    args = ap.parse_args()

    print("=== Step 2 (v2): High-Precision Prompt Commit Scraping ===\n")
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("WARNING: pyyaml missing -> .yaml/.yml prompts will be ignored (pip install pyyaml)\n")
    if args.reset:
        reset_output_state()
        print("Reset requested: rebuilding from scratch.\n")
    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 1 first.")
        return

    with open(CSV_INPUT, encoding="utf-8") as f:
        repos = list(csv.DictReader(f))

    def size(r):
        try:
            return int(float(r.get("commits_since_2024") or 10**9))
        except ValueError:
            return 10**9
    repos.sort(key=size)                         # smallest repos first -> early results, giants last
    if args.max_commits:
        repos = [r for r in repos if size(r) <= args.max_commits]
    if args.only_repo:
        repos = [r for r in repos if r["repo_name"] == args.only_repo]

    fieldnames = ["category", "repo_name", "commit_hash", "author_date", "msg",
                  "prompt_files", "prompt_var_names", "all_modified_files", "consumer_candidate_diffs"]
    completed, existing_keys = load_resume_state()
    if not os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
    if completed:
        print(f"Resuming: {len(completed)} repositories already completed.\n")

    failures = json.load(open(FAILED_FILE)) if os.path.exists(FAILED_FILE) else {}
    pending = [r for r in repos if r["repo_name"] not in completed]
    gave_up = [r["repo_name"] for r in pending if failures.get(r["repo_name"], 0) >= MAX_ATTEMPTS]
    if gave_up and not args.retry_failed:
        print(f"Skipping {len(gave_up)} repos that failed {MAX_ATTEMPTS}x (use --retry-failed): {gave_up[:5]}...\n")
        pending = [r for r in pending if r["repo_name"] not in gave_up]
    pending.sort(key=lambda r: (failures.get(r["repo_name"], 0), size(r)))   # previously failed go last
    if args.limit:
        pending = pending[:args.limit]
    total = len(existing_keys)

    for idx, repo in enumerate(pending, 1):
        repo_name, category = repo["repo_name"], repo["category"]
        print(f"[{idx}/{len(pending)}] {repo_name} (commits since 2024: {repo.get('commits_since_2024')})")
        tmp_dir = tempfile.mkdtemp()
        t0 = time.time()
        scanned = gated = confirmed = 0
        reasons, partial = Counter(), False
        try:
            if not shallow_clone(repo["clone_url"], tmp_dir):
                print("   no commits in window")
            else:
                miner = Repository(path_to_repo=tmp_dir, since=START_DATE)
                for commit in miner.traverse_commits():
                    scanned += 1
                    if time.time() - t0 > args.repo_timeout * 60:
                        partial = True
                        print(f"   !! time limit reached after {scanned} commits (partial)")
                        break
                    if len(commit.parents) > 1:                       # merge commits
                        continue
                    if commit.author and BOT_AUTHOR.search(commit.author.name or ""):
                        continue

                    prompt_files, var_names, all_files, consumers = [], set(), [], {}
                    commit_gated = False
                    try:                      # shallow-boundary commits have no parent -> git diff error
                        mods = list(commit.modified_files)
                    except Exception:
                        reasons["commit_diff_error(shallow boundary)"] += 1
                        continue
                    if not any((m.new_path or m.old_path or "").lower().endswith(CODE_EXTS) for m in mods):
                        continue
                    for mod in mods:
                        path = mod.new_path or mod.old_path
                        if not path:
                            continue
                        all_files.append(path)
                        is_py = path.lower().endswith(".py")
                        if is_py:
                            if max(len(mod.source_code or ""), len(mod.source_code_before or "")) > MAX_GATE_CHARS:
                                gate, names = False, set()
                                reasons["gate_skipped_huge_file"] += 1
                            else:
                                gate, names = analyze_prompt_modification(mod)  # cheap loose gate
                        else:
                            gate, names = touches_prompt_cross_verified(mod), set()
                        if gate:
                            commit_gated = True
                            ok, why = confirm_real_edit(mod, path)               # strict confirmation
                            reasons[why] += 1
                            if ok:
                                prompt_files.append(path)
                                var_names |= names
                                continue
                        if is_py and len(consumers) < MAX_CONSUMER_FILES:
                            consumers[path] = (mod.diff or "")[:MAX_DIFF_CHARS]
                    gated += commit_gated

                    if not prompt_files:
                        continue
                    if len(prompt_files) > MAX_FILES_PER_COMMIT:
                        reasons["bulk_commit"] += 1
                        continue
                    key = (repo_name, commit.hash)
                    if key in existing_keys:
                        continue
                    confirmed += 1
                    rec = {
                        "category": category, "repo_name": repo_name, "commit_hash": commit.hash,
                        "author_date": commit.author_date.strftime("%Y-%m-%d %H:%M:%S"),
                        "msg": commit.msg.split("\n")[0][:120],
                        "prompt_files": ";".join(sorted(set(prompt_files))),
                        "prompt_var_names": ";".join(sorted(var_names)),
                        "all_modified_files": ";".join(all_files),
                        "consumer_candidate_diffs": json.dumps(consumers),
                    }
                    with open(CSV_OUTPUT, "a", newline="", encoding="utf-8") as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow(rec)
                    existing_keys.add(key)
            total += confirmed
            print(f"   scanned {scanned} | loose-gate {gated} | CONFIRMED edits {confirmed} "
                  f"| {int(time.time() - t0)}s")
        except Exception as e:
            failures[repo_name] = failures.get(repo_name, 0) + 1
            json.dump(failures, open(FAILED_FILE, "w"))
            print(f"   !! failed {repo_name} (attempt {failures[repo_name]}/{MAX_ATTEMPTS}): "
                  f"{type(e).__name__}: {str(e)[:160]}")
        else:
            completed.add(repo_name)
            save_resume_state(completed)
            append_stats({"repo_name": repo_name, "commits_since_2024": repo.get("commits_since_2024"),
                          "scanned": scanned, "loose_gate": gated, "confirmed": confirmed,
                          "seconds": int(time.time() - t0), "partial": partial,
                          "reasons": json.dumps(dict(reasons))})
            print(f"   checkpoint: {len(completed)} repos done")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\n==================================================")
    print(f" Step 2 complete. Total confirmed prompt-edit commits in file: {total}")
    print(f" Saved to: {os.path.abspath(CSV_OUTPUT)}  |  funnel per repo: {STATS_OUTPUT}")
    print("==================================================")


if __name__ == "__main__":
    execute_step2_precise()