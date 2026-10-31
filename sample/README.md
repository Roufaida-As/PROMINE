# Reviewer sample

This directory contains a small, sanitized snapshot of the main tabular and
JSONL outputs. It is included so reviewers can inspect the data shape without
downloading or mining the full repository collection.

The files are illustrative fixtures, not a replacement for the complete
dataset:

- `target_repos.csv` shows the repository-selection schema.
- `prompt_commits.csv` shows the confirmed-commit schema.
- `step2_repo_stats.csv` shows the per-repository funnel schema.
- `step3_units.jsonl` shows one modified prompt-unit record.
- `step4_labeled.jsonl` shows one optional annotation record.

## Fast reproduction

From the repository root, first use the existing full CSV inputs and select a
single small public repository:

```bash
python step3.py --repo OWNER/REPO
```

Replace `OWNER/REPO` with a repository present in `target_repos.csv`. The
command clones only that repository, processes its confirmed commits from
`prompt_commits.csv`, appends unit records to `step3_units.jsonl`, and updates
`step3_checkpoint.json`.

The console output has this form:

```text
[1/1] OWNER/REPO: N commits
   modified=M added=A removed=R skipped=S

=== Summary ===
change types : {'modified': M, 'added': A, 'removed': R, 'skipped': S}
STAGE C ELIGIBLE: E units in C commits
```

`N`, `M`, `A`, `R`, `S`, and `E` depend on the selected repository and may
change when its history changes. No GitHub token or LLM API key is required
for this deterministic extraction step, but network access and `git` are
required.

