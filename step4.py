import os
import csv
import re
import sys
import subprocess
import tempfile
import shutil
import venv
from collections import defaultdict
from git import Repo

# Fix for CSV field size limits
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

TEST_TIMEOUT = 120
INSTALL_TIMEOUT = 240  # dependency installs are heavier than `pip install pytest`

FIELDNAMES = [
    "repo_name", "commit_hash", "confidence", "eval_result",
    "prompt_files", "consumer_files", "details", "log_snippet"
]

# Time control: process at most this many candidate commits per repo. Breadth
# across repos is more valuable evidence for RQ1 than exhausting one repo's
# full commit list, and this keeps a full run from taking hours-to-days.
MAX_COMMITS_PER_REPO = 4

# pytest's own exit codes: 0 = all passed, 1 = some failed, 2 = interrupted,
# 3 = internal error, 4 = usage error, 5 = no tests were collected at all.
# 5 is NOT the same claim as 1 -- "nothing to run" is a different failure
# mode than "we ran tests and they broke", and conflating them was hiding
# the real problem (see NO_TESTS_COLLECTED below).
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


def setup_venv_with_repo_deps(work_dir, venv_dir):
    """
    Creates a fresh venv AND installs the target repo's own dependencies as
    they exist at the currently checked-out commit -- not just pytest.
    Without this, every test file fails to even import the package under
    test, which is what was producing UNSTABLE_BASE on 100% of candidates.
    Returns (python_bin, pytest_bin, env, install_ok, install_log).
    """
    venv.create(venv_dir, with_pip=True)
    python_bin, pytest_bin = bin_paths(venv_dir)

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = venv_dir
    env["PATH"] = os.path.dirname(python_bin) + os.pathsep + env.get("PATH", "")

    code, log = run_cmd(f'"{python_bin}" -m pip install --upgrade pip pytest',
                         cwd=work_dir, env=env, timeout=INSTALL_TIMEOUT)
    if code != 0:
        return python_bin, pytest_bin, env, False, "pip/pytest bootstrap failed:\n" + log[-1200:]

    # Install the repo's OWN dependencies, in priority order. This is the
    # actual fix: a bare venv + pytest cannot import the package being tested.
    has_pyproject = os.path.exists(os.path.join(work_dir, "pyproject.toml"))
    has_setup_py = os.path.exists(os.path.join(work_dir, "setup.py"))
    has_requirements = os.path.exists(os.path.join(work_dir, "requirements.txt"))

    if has_pyproject or has_setup_py:
        code, log = run_cmd(f'"{python_bin}" -m pip install -e .',
                             cwd=work_dir, env=env, timeout=INSTALL_TIMEOUT)
        if code != 0 and has_requirements:
            # editable install can fail on old/broken metadata; fall back
            code, log = run_cmd(f'"{python_bin}" -m pip install -r requirements.txt',
                                 cwd=work_dir, env=env, timeout=INSTALL_TIMEOUT)
    elif has_requirements:
        code, log = run_cmd(f'"{python_bin}" -m pip install -r requirements.txt',
                             cwd=work_dir, env=env, timeout=INSTALL_TIMEOUT)
    else:
        # No manifest found at all -- proceed anyway, but flag it so this
        # is visible in the output rather than silently assumed fine.
        return python_bin, pytest_bin, env, True, "NO_MANIFEST_FOUND (no pyproject.toml/setup.py/requirements.txt)"

    if code != 0:
        return python_bin, pytest_bin, env, False, "dependency install failed:\n" + log[-1200:]

    return python_bin, pytest_bin, env, True, "OK"


def classify_pytest_run(returncode):
    if returncode == 0:
        return "PASS"
    if returncode == PYTEST_NO_TESTS_COLLECTED:
        return "NO_TESTS_COLLECTED"
    return "REAL_FAILURE"


# Known signatures of "we can't reproduce a period-correct toolchain" failures,
# as opposed to a genuine baseline code failure. These are NOT fixable by
# picking different dependency versions -- e.g. pytest permanently removed
# support for generator-style ("yield") tests years ago, so no pytest version
# is simultaneously old enough to allow it and new enough to install cleanly
# alongside everything else. Distinguishing this from a real failure matters:
# it's an honest limitation of environment reproduction, not evidence the
# commit's code is unstable.
TOOLING_INCOMPATIBILITY_SIGNATURES = [
    "'yield' keyword is allowed in fixtures, but not in tests",
    "PluggyTeardownRaisedWarning",
    "fixture '.*' not found",
    "ScopeMismatch",
    "Failed: async def functions are not natively supported",
    "DeprecationWarning: The 'asyncio_mode' default",
]


def is_tooling_incompatibility(output_text):
    return any(sig in output_text for sig in TOOLING_INCOMPATIBILITY_SIGNATURES) or \
           bool(re.search(r"fixture '[^']*' not found", output_text))


def evaluate_candidate(repo, work_dir, commit_hash, prompt_files_str):
    """
    Runs the baseline/mutation differential test for ONE commit, reusing an
    already-cloned repo (repo, work_dir passed in) rather than re-cloning.
    """
    target_commit = repo.commit(commit_hash)
    if not target_commit.parents:
        return "NO_PARENT", "First commit in repo", ""
    parent_commit = target_commit.parents[0]

    # 1. Checkout Pre-Commit State (C_{i-1}), discarding any leftovers
    repo.git.checkout(parent_commit.hexsha, force=True)
    repo.git.clean("-fdx")  # remove untracked files/venvs left by a previous candidate

    venv_dir = os.path.join(tempfile.gettempdir(), "mace_venv_" + commit_hash[:10])
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir, ignore_errors=True)

    try:
        python_bin, pytest_bin, env, install_ok, install_log = setup_venv_with_repo_deps(work_dir, venv_dir)
        if not install_ok:
            return "DEPENDENCY_INSTALL_FAILED", install_log, ""

        # 2. Baseline pytest on C_{i-1}
        code_base, out_base = run_cmd(f'"{pytest_bin}"', cwd=work_dir, env=env, timeout=TEST_TIMEOUT)
        base_status = classify_pytest_run(code_base)

        if base_status == "NO_TESTS_COLLECTED":
            return "NO_TESTS_COLLECTED", "pytest collected zero tests at baseline (not a failure, nothing to compare)", out_base[-1200:]
        if base_status == "REAL_FAILURE":
            if is_tooling_incompatibility(out_base):
                return ("TOOLING_INCOMPATIBLE",
                        "Baseline can't run under any reproducible modern toolchain "
                        "(e.g. removed pytest yield-test syntax) -- not a code failure, "
                        "a period-environment reproduction limit. Discarded, but counted separately.",
                        out_base[-1200:])
            return "UNSTABLE_BASE", "Pre-commit baseline genuinely failed pytest (discarded, too noisy to attribute)", out_base[-1200:]

        # 3. Extract prompt-only patch from C_i and apply it on top of C_{i-1}
        prompt_files = [f.strip() for f in prompt_files_str.split(";") if f.strip()]
        patch_text = repo.git.diff(parent_commit.hexsha, target_commit.hexsha, "--", *prompt_files)
        if not patch_text.strip():
            return "EMPTY_PATCH", "No prompt patch generated", ""

        patch_file = os.path.join(tempfile.gettempdir(), f"mace_patch_{commit_hash[:10]}.patch")
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_text)

        code_patch, out_patch = run_cmd(f'git apply "{patch_file}"', cwd=work_dir, timeout=30)
        if code_patch != 0:
            return "PATCH_FAILED", "Failed to apply prompt-only patch", out_patch[-800:]

        # 4. Mutated pytest run: NEW prompt, OLD consumer code
        code_mut, out_mut = run_cmd(f'"{pytest_bin}"', cwd=work_dir, env=env, timeout=TEST_TIMEOUT)
        mut_status = classify_pytest_run(code_mut)

        if mut_status == "PASS":
            return "PASS_TO_PASS", "Non-breaking prompt modification", out_mut[-1200:]
        if mut_status == "NO_TESTS_COLLECTED":
            return "MUTATION_BROKE_COLLECTION", "Prompt-only patch broke test collection itself (e.g. syntax error) -- inspect manually", out_mut[-1500:]
        return "PASS_TO_FAIL", "VERIFIED BREAKING CHANGE: prompt edit alone broke the existing test suite", out_mut[-1500:]

    finally:
        if os.path.exists(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)


def load_already_processed(path):
    """Supports resuming: commits already in the output CSV are skipped."""
    done = set()
    if os.path.exists(path):
        with open(path, mode="r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row["commit_hash"])
    return done


def execute_step4():
    print("=== Stage 4: MACE-Eval Differential Re-Validation ===\n")

    if not os.path.exists(CSV_INPUT):
        print(f" Error: {CSV_INPUT} not found! Run Step 3 first.")
        return

    with open(CSV_INPUT, mode="r", encoding="utf-8") as f:
        all_candidates = list(csv.DictReader(f))

    high_conf = [c for c in all_candidates if c.get("confidence") == "variable_confirmed"]
    candidates = high_conf if high_conf else all_candidates

    already_done = load_already_processed(CSV_OUTPUT)
    remaining = [c for c in candidates if c["commit_hash"] not in already_done]

    print(f"Loaded {len(all_candidates)} candidates. {len(candidates)} are high-confidence.")
    print(f"{len(already_done)} already processed in a previous run (resuming). "
          f"{len(remaining)} left to evaluate.\n")

    if not os.path.exists(CSV_OUTPUT):
        with open(CSV_OUTPUT, mode="w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    # Group by repo so each repo is cloned exactly ONCE and its commits are
    # evaluated by checking out within the same clone, not re-cloning per commit.
    by_repo = defaultdict(list)
    for c in remaining:
        by_repo[c["repo_name"]].append(c)

    if MAX_COMMITS_PER_REPO:
        skipped_for_cap = sum(max(0, len(v) - MAX_COMMITS_PER_REPO) for v in by_repo.values())
        by_repo = {repo: cands[:MAX_COMMITS_PER_REPO] for repo, cands in by_repo.items()}
        if skipped_for_cap:
            print(f"Capping at {MAX_COMMITS_PER_REPO} commit(s)/repo for breadth+time: "
                  f"skipping {skipped_for_cap} extra candidate(s) from over-represented repos.\n")

    stats = defaultdict(int)
    total_done = 0
    total = sum(len(v) for v in by_repo.values())

    for repo_name, repo_candidates in by_repo.items():
        clone_url = f"https://github.com/{repo_name}.git"
        work_dir = tempfile.mkdtemp(prefix="mace_repo_")
        print(f"\n=== Cloning {repo_name} once for {len(repo_candidates)} candidate commit(s) ===")

        try:
            repo = Repo.clone_from(clone_url, work_dir)
        except Exception as e:
            print(f"   ⚠️ Clone failed for {repo_name}: {e}")
            for cand in repo_candidates:
                total_done += 1
                stats["CLONE_FAILED"] += 1
                with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=FIELDNAMES).writerow({
                        "repo_name": repo_name, "commit_hash": cand["commit_hash"],
                        "confidence": cand.get("confidence", "unknown"),
                        "eval_result": "CLONE_FAILED", "prompt_files": cand["prompt_files"],
                        "consumer_files": cand.get("consumer_files_variable_confirmed") or "",
                        "details": str(e)[-800:], "log_snippet": "",
                    })
            continue

        for cand in repo_candidates:
            total_done += 1
            commit_hash = cand["commit_hash"]
            prompt_files = cand["prompt_files"]
            confidence = cand.get("confidence", "unknown")
            consumer_files = (cand.get("consumer_files_variable_confirmed")
                               or cand.get("consumer_files_heuristic_only") or "")

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

        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n==================================================")
    print("  Stage 4 Evaluation Complete!")
    print(f"  Verified Breaking Changes (PASS_TO_FAIL):     {stats.get('PASS_TO_FAIL', 0)}")
    print(f"  Non-Breaking Prompt Edits (PASS_TO_PASS):     {stats.get('PASS_TO_PASS', 0)}")
    print(f"  No Tests Collected at Baseline:                {stats.get('NO_TESTS_COLLECTED', 0)}")
    print(f"  Unstable Baselines (real pre-existing failures): {stats.get('UNSTABLE_BASE', 0)}")
    print(f"  Tooling/Environment Incompatible (not a code issue): {stats.get('TOOLING_INCOMPATIBLE', 0)}")
    print(f"  Dependency Install Failed:                     {stats.get('DEPENDENCY_INSTALL_FAILED', 0)}")
    print(f"  Patch Failed to Apply:                         {stats.get('PATCH_FAILED', 0)}")
    print(f"  Clone Failed:                                  {stats.get('CLONE_FAILED', 0)}")
    print(f"  Output Saved To: {os.path.abspath(CSV_OUTPUT)}")
    print("==================================================")


if __name__ == "__main__":
    execute_step4()