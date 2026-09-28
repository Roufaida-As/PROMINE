import pandas as pd

df = pd.read_csv("mace_eval_results.csv")
breaking_changes = df[df["eval_result"] == "PASS_TO_FAIL"]

print(f"=== Found {len(breaking_changes)} Verified PASS_TO_FAIL Candidates ===\n")

for idx, row in breaking_changes.iterrows():
    print(f"📍 Repo: {row['repo_name']}")
    print(f"   Commit: {row['commit_hash']}")
    print(f"   Prompt File(s): {row['prompt_files']}")
    print(f"   Consumer File(s): {row['consumer_files']}")
    print(f"   Details: {row['details']}")
    
    # Extract the short test summary or failure traceback from the log tail
    log = str(row.get('log_snippet', ''))
    if "short test summary info" in log:
        failure_summary = "short test summary info" + log.split("short test summary info")[-1]
    else:
        failure_summary = log[-800:]

    print(f"\n   --- Pytest Failure Traceback ---\n{failure_summary.strip()}")
    print("\n" + "=" * 70 + "\n")