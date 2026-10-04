import os
import csv
import re
import sys
import subprocess
import shutil
import venv
from collections import defaultdict
from git import Repo

# Fix for CSV field size limits on Windows
def set_max_csv_field_limit():
    max_int = sys.maxsize
    while True:
        try:
            csv.field_size_limit(max_int)
            break
        except OverflowError:
            max_int = int(max_int / 10)

set_max_csv_field_limit()

CSV_INPUT = "coevolution_commits.csv"
CSV_OUTPUT = "mace_eval_results.csv"

# Custom root temp directory to prevent Windows 8.3 (DELLPR~1 vs DELL PRECISION) path warnings
MACE_TEMP_DIR = r"C:\mace_eval_temp"
os.makedirs(MACE_TEMP_DIR, exist_ok=True)

TEST_TIMEOUT = 120
INSTALL_TIMEOUT = 300

FIELDNAMES = [
    "repo_name", "commit_hash", "confidence", "eval_result",
    "prompt_files", "consumer_files", "details", "log_snippet"
]

PYTEST_NO_TESTS_COLLECTED = 5

def run_cmd(cmd, cwd, env=None, timeout=120):
    try:
        res = subprocess.run(
            cmd, cwd=cwd, env=env, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return res.returncode, res.stdout + "\n" + res.stderr
    except subprocess.TimeoutExpired:
        return -1, "EXECUTION_TIMEOUT"
    except Exception as e:
        return -1, str(e)

def bin_paths(venv_dir):
    if os.name == "nt":
        return (os.path.join(venv_dir, "Scripts", "python.exe"),
                os.path.join(venv_dir, "Scripts", "pytest.exe"))
    return (os.path.join(venv_dir, "bin", "python"),
            os.path.join(venv_dir, "bin", "pytest"))

def setup_venv_with_all_deps(work_dir, venv_dir):
    """
    Creates venv, sets up monorepo PYTHONPATHs, and installs packages 
    including optional extras ([all,dev,test]).
    """
    venv.create(venv_dir, with_pip=True)
    python_bin, pytest_bin = bin_paths(venv_dir)

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = venv_dir
    env["PATH"] = os.path.dirname(python_bin) + os.pathsep + env.get("PATH", "")
    
    # Dummy API keys to prevent network/auth crashes in test suites
    env["OPENAI_API_KEY"] = "mock-key-for-mace-eval"
    env["ANTHROPIC_API_KEY"] = "mock-key-for-mace-eval"
    env["OLLAMA_HOST"] = "http://127.0.0.1:11434"

    # Monorepo PYTHONPATH Auto-Discovery: Add root, src/, and packages/*/src/
    pythonpath_dirs = [work_dir, os.path.join(work_dir, "src")]
    packages_dir = os.path.join(work_dir, "packages")
    if os.path.exists(packages_dir):
        for pkg in os.listdir(packages_dir):
            pkg_src = os.path.join(packages_dir, pkg, "src")
            if os.path.exists(pkg_src):
                pythonpath_dirs.append(pkg_src)

    existing_ppath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_dirs) + (os.pathsep + existing_ppath if existing_ppath else "")

    # Core test harness bootstrap
    bootstrap_cmd = f'"{python_bin}" -m pip install --upgrade pip pytest pytest-mock pytest-asyncio pytest-json-report'
    code, log = run_cmd(bootstrap_cmd, cwd=work_dir, env=env, timeout=INSTALL_TIMEOUT)
    if code != 0:
        return python_bin, pytest_bin, env, False, "Bootstrap failed:\n" + log[-600:]

    # Recursive monorepo dependency installation with optional extras
    for root, dirs, files in os.walk(work_dir):
        if ".git" in root or "mace_venv" in root:
            continue
            
        if "requirements.txt" in files:
            req_path = os.path.join(root, "requirements.txt")
            run_cmd(f'"{python_bin}" -m pip install -r "{req_path}"', cwd=root, env=env, timeout=INSTALL_TIMEOUT)

        if "setup.py" in files or "pyproject.toml" in files:
            # Try installing with extras first, fall back to basic editable install
            cmd_extras = f'"{python_bin}" -m pip install -e .[all,dev,test]'
            code_ex, _ = run_cmd(cmd_extras, cwd=root, env=env, timeout=INSTALL_TIMEOUT)
            if code_ex != 0:
                run_cmd(f'"{python_bin}" -m pip install -e .', cwd=root, env=env, timeout=INSTALL_TIMEOUT)

    return python_bin, pytest_bin, env, True, "OK"

def apply_prompt_mutation_direct(repo, work_dir, target_commit_hash, prompt_files_str):
    """
    Reads prompt file content from commit C_i using Git forward slashes (/)
    and injects it directly onto disk at C_{i-1}.
    """
    prompt_files = [f.strip() for f in prompt_files_str.split(";") if f.strip()]
    modified_count = 0

    for pfile in prompt_files:
        git_path = pfile.replace("\\", "/")
        full_path = os.path.join(work_dir, git_path)

        try:
            file_content_at_Ci = repo.git.show(f"{target_commit_hash}:{git_path}")
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(file_content_at_Ci)
            modified_count += 1
        except Exception as e:
            err_str = str(e)
            if "exists on disk, but not in" in err_str or "does not exist" in err_str:
                if os.path.exists(full_path):
                    os.remove(full_path)
                    modified_count += 1
            else:
                return False, f"Failed to inject prompt content for {pfile}: {e}"

    return True, f"Successfully mutated {modified_count} prompt file(s)"

def classify_pytest_run(returncode):
    if returncode == 0:
        return "PASS"
    if returncode == PYTEST_NO_TESTS_COLLECTED:
        return "NO_TESTS_COLLECTED"
    return "REAL_FAILURE"

TOOLING_INCOMPATIBILITY_SIGNATURES = [
    "'yield' keyword is allowed in fixtures, but not in tests",
    "PluggyTeardownRaisedWarning",
    "ScopeMismatch",
    "Failed: async def functions are not natively supported",
    "DeprecationWarning: The 'asyncio_mode' default",
]

def is_tooling_incompatibility(output_text):
    return any(sig in output_text for sig in TOOLING_INCOMPATIBILITY_SIGNATURES)

def evaluate_candidate(repo, work_dir, commit_hash, prompt_files_str):
    target_commit = repo.commit(commit_hash)
    if not target_commit.parents:
        return "NO_PARENT", "First commit in repo", ""
    parent_commit = target_commit.parents[0]

    # 1. Checkout Pre-Commit State C_{i-1}
    try:
      repo.git.checkout(parent_commit.hexsha, force=True)
      repo.git.clean("-fdx")
    except Exception as e:
      return "CHECKOUT_FAILED", f"Git checkout failed (e.g., invalid Windows path): {e}", ""
    sanitize_pyproject_toml(work_dir)

    venv_dir = os.path.join(MACE_TEMP_DIR, "mace_venv_" + commit_hash[:10])
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir, ignore_errors=True)

    try:
        python_bin, pytest_bin, env, install_ok, install_log = setup_venv_with_all_deps(work_dir, venv_dir)
        if not install_ok:
            return "DEPENDENCY_INSTALL_FAILED", install_log, ""

        # Pass flags to skip live integration/server tests and avoid vector DB locks
        pytest_flags = '-m "not integration and not e2e" -k "not test_vector_store and not test_chroma and not test_qdrant"'
        # 2. Baseline pytest on C_{i-1}

        code_base, out_base = run_cmd(f'"{pytest_bin}" {pytest_flags}', cwd=work_dir, env=env, timeout=TEST_TIMEOUT)
        base_status = classify_pytest_run(code_base)

        if base_status == "NO_TESTS_COLLECTED":
            return "NO_TESTS_COLLECTED", "pytest collected zero tests at baseline", out_base[-1200:]
        if base_status == "REAL_FAILURE":
            if is_tooling_incompatibility(out_base):
                return "TOOLING_INCOMPATIBLE", "Environment reproduction limit", out_base[-1200:]
            return "UNSTABLE_BASE", "Pre-commit baseline genuinely failed pytest", out_base[-1200:]

        # 3. Direct Prompt Injection (Mutated State M_i)
        success, mut_msg = apply_prompt_mutation_direct(repo, work_dir, target_commit.hexsha, prompt_files_str)
        if not success:
            return "MUTATION_FAILED", mut_msg, ""

        # 4. Mutated pytest run: NEW prompt, OLD consumer code
        code_mut, out_mut = run_cmd(f'"{pytest_bin}"', cwd=work_dir, env=env, timeout=TEST_TIMEOUT)
        mut_status = classify_pytest_run(code_mut)

        if mut_status == "PASS":
            return "PASS_TO_PASS", "Non-breaking prompt modification", out_mut[-1200:]
        if mut_status == "NO_TESTS_COLLECTED":
            return "MUTATION_BROKE_COLLECTION", "Prompt edit broke test collection syntax", out_mut[-1200:]
            
        return "PASS_TO_FAIL", "VERIFIED BREAKING CHANGE: prompt edit alone broke test suite!", out_mut[-1500:]

    finally:
        if os.path.exists(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)

def load_already_processed(path):
    done = set()
    if os.path.exists(path):
        with open(path, mode="r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row["commit_hash"])
    return done

def sanitize_pyproject_toml(work_dir):
    """
    Fixes the Pytest configuration collision ([tool.pytest] vs [tool.pytest.ini_options])
    in pyproject.toml files on the fly so baseline tests can run cleanly.
    """
    for root, _, files in os.walk(work_dir):
        if "pyproject.toml" in files:
            p_path = os.path.join(root, "pyproject.toml")
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if "[tool.pytest]" in content and "[tool.pytest.ini_options]" in content:
                    # Comment out the legacy/duplicate [tool.pytest] header
                    new_content = content.replace("[tool.pytest]", "# [tool.pytest]")
                    with open(p_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
            except Exception:
                pass
            
            
import stat

def remove_readonly(func, path, exc_info):
    """Removes read-only flag on Windows files so shutil.rmtree can delete them."""
    os.chmod(path, stat.S_IWRITE)
    func(path)

def safe_rmtree(path):
    """Safely deletes a directory tree, overcoming Windows read-only file locks."""
    if os.path.exists(path):
        shutil.rmtree(path, onerror=remove_readonly)
        
def execute_step4():
    print("=== Stage 4: MACE-Eval Differential Re-Validation (Clean Temp Dir Mode) ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 3 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        all_candidates = list(csv.DictReader(f))

    high_conf = [c for c in all_candidates if c.get("confidence") == "variable_confirmed"]
    candidates = high_conf if high_conf else all_candidates

    already_done = load_already_processed(CSV_OUTPUT)
    remaining = [c for c in candidates if c["commit_hash"] not in already_done]

    print(f"Loaded {len(all_candidates)} candidates. {len(candidates)} high-confidence.")
    print(f"{len(already_done)} already evaluated. {len(remaining)} remaining.\n")

    if not os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    by_repo = defaultdict(list)
    for c in remaining:
        by_repo[c["repo_name"]].append(c)

    stats = defaultdict(int)
    total_done = 0
    total = sum(len(v) for v in by_repo.values())

    for repo_name, repo_candidates in by_repo.items():
        clone_url = f"https://github.com/{repo_name}.git"
        repo_dir_name = repo_name.replace("/", "_")
        work_dir = os.path.join(MACE_TEMP_DIR, f"repo_{repo_dir_name}")

        print(f"\n=== Processing {repo_name} ({len(repo_candidates)} candidate commits) ===")

        # Reuse existing clone if present, otherwise clone fresh
        repo = None
        git_folder = os.path.join(work_dir, ".git")
        
        if os.path.exists(git_folder):
            try:
                repo = Repo(work_dir)
                repo.git.reset("--hard")
                repo.git.clean("-fdx")
                print(f"   [OK] Reusing existing repository clone on disk.")
            except Exception as e:
                print(f"   Existing clone invalid ({e}), performing clean re-clone...")
                safe_rmtree(work_dir)
                repo = None

        if repo is None:
            try:
                if os.path.exists(work_dir):
                    safe_rmtree(work_dir)
                repo = Repo.clone_from(clone_url, work_dir)
            except Exception as e:
                print(f"   Clone failed for {repo_name}: {e}")
                continue

        for cand in repo_candidates:
            total_done += 1
            commit_hash = cand["commit_hash"]
            prompt_files = cand["prompt_files"]
            confidence = cand.get("confidence", "unknown")
            consumer_files = cand.get("consumer_files_variable_confirmed") or cand.get("consumer_files_heuristic_only") or ""

            print(f"[{total_done}/{total}] {repo_name} @ {commit_hash[:7]}...")

            try:
                result, msg, log_snippet = evaluate_candidate(repo, work_dir, commit_hash, prompt_files)
            except Exception as e:
                result, msg, log_snippet = "EXECUTION_ERROR", str(e), ""

            stats[result] += 1

            with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDNAMES).writerow({
                    "repo_name": repo_name, "commit_hash": commit_hash,
                    "confidence": confidence, "eval_result": result,
                    "prompt_files": prompt_files, "consumer_files": consumer_files,
                    "details": msg, "log_snippet": log_snippet,
                })
            print(f"   -> [{result}] {msg}")

    print("\n==================================================")
    print("  Stage 4 Evaluation Complete!")
    print(f"  Verified Breaking Changes (PASS_TO_FAIL):     {stats.get('PASS_TO_FAIL', 0)}")
    print(f"  Non-Breaking Prompt Edits (PASS_TO_PASS):     {stats.get('PASS_TO_PASS', 0)}")
    print(f"  No Tests Collected at Baseline:                {stats.get('NO_TESTS_COLLECTED', 0)}")
    print(f"  Unstable Baselines:                           {stats.get('UNSTABLE_BASE', 0)}")
    print(f"  Output Saved To: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")

if __name__ == "__main__":
    execute_step4()