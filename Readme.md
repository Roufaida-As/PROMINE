# PROMINE

**PROMINE (PROMpt evolution mINEing)** is an AST-based mining pipeline for
studying how prompts evolve in LLM-integrated Python repositories. It discovers
candidate repositories, identifies commits that modify prompt-like content, and
extracts comparable prompt units with their old and new text, edit signals, and
co-change context.

The project accompanies the paper:

> **PROMINE: A Tool and Dataset of Prompt Edits Mined from LLM-Integrated
> Python Repositories**

The pipeline is designed to be resumable and to run on a single machine. The
core extraction stages are deterministic and do not require an LLM. An optional stage uses an LLM to decompose eligible edits into prompt components and
label edit operators.

## What this repository provides

- **Repository selection** from GitHub using LLM-related keywords and project
  quality filters.
- **Commit-level mining** using Python's `ast` module and structural checks for
  YAML, JSON, `.prompt`, and `.rail` files.
- **Unit-level extraction** that compares individual prompt strings rather than
  treating every changed file as one prompt.
- **Deterministic signals** for placeholder changes, JSON-key changes,
  reordering, whitespace-only edits, f-string interpolation changes, and
  textual similarity.
- **Consumer candidates**, stored as diffs of co-modified non-prompt Python
  files, for later prompt/code co-evolution analysis.
- **Optional 20 edit operators taxonomy annotation** for component decomposition and edit
  operator labeling.

## Pipeline overview

```text
GitHub search
    |
    v
Step 1: repository selection  ---> target_repos.csv
    |
    v
Step 2: loose gate + confirmation ---> prompt_commits.csv
                                      step2_repo_stats.csv
    |
    v
Step 3: prompt-unit extraction ---> step3_units.jsonl
    |
    v
Step 4: optional LLM annotation ---> step4_labeled.jsonl
```

### Stage 1: repository selection

[`step1_v2.py`](./step1_v2.py) is the recommended selector. It searches public
GitHub repositories and, by default, applies these filters:

- Python repository with at least 10 stars.
- Between 100 and 20,000 commits since 2024-01-01.
- Pushed since 2025-01-01.
- Not a fork, archive, or template.
- A test directory and a dependency manifest.
- At least three commit messages containing `prompt`.

The older [`step1.py`](./step1.py) is retained for comparison with the earlier,
less restrictive selection protocol. New collection runs should normally use
`step1_v2.py`.

### Stage 2: commit-level mining

[`step2.py`](./step2.py) performs a two-level filter:

1. A recall-oriented gate finds changed prompt-like strings.
2. A precision-oriented confirmation keeps genuine modifications to existing
   prompt content and excludes additions, deletions, renames, bulk commits,
   bot commits, tests, examples, documentation, tool configuration, short
   strings, and surface-only changes.

For confirmed commits, the miner also retains modified prompt files, AST-bound
variable names, and diffs of co-modified non-prompt files.

### Stage 3: deterministic unit extraction

[`step3.py`](./step3.py) reads confirmed commits and compares prompt units
before and after each commit. A unit may be:

- A prompt-like Python string or f-string.
- A string passed through an LLM-style call.
- A role/content message.
- A prompt-like value in YAML or JSON.
- A complete `.prompt` or `.rail` file.

Units are paired using stable structural identifiers and, when necessary,
textual similarity. Each record is classified as `added`, `removed`,
`modified`, or `skipped`, and includes eligibility flags rather than silently
discarding test or example units.

### Stage 4: optional annotation

[`step4.py`](./step4.py) is optional. It sends eligible units to either an
Anthropic model or an OpenAI-compatible provider and writes structured labels
for prompt components such as role, directive, context, examples, and output
format. It also records edit operators and whether the edit is
contract-relevant.

Do not run this stage on sensitive prompt data without confirming that sending
the data to the selected provider is appropriate.

## Requirements

Use Python 3.10 or newer and ensure that `git` is available on `PATH`.

Install the core dependencies:

```bash
python -m pip install -r requirements.txt
```

For optional Step 4 annotation, install the provider you intend to use:

```bash
# Anthropic
python -m pip install anthropic

# OpenAI-compatible providers
python -m pip install openai
```

`requirements.txt` contains the dependencies used by the core mining pipeline.
The Anthropic and OpenAI-compatible clients are intentionally optional; install
only the provider you intend to use.
For a reproducible paper release, record the exact Python and package versions
used for the final run.

## Configuration and credentials

Create a local `.env` file in the repository root when using the GitHub API or
Stage 4. Never commit this file or put credentials in source code.

```dotenv
GITHUB_TOKEN=your_github_token
ANTHROPIC_API_KEY=your_anthropic_key
OPENAI_API_KEY=your_openai_compatible_key
```

`GITHUB_TOKEN` is strongly recommended for repository discovery because
unauthenticated GitHub API requests are heavily rate-limited. Only the key
needed for the stage being run is required. Stage 4 reads
`ANTHROPIC_API_KEY` for Anthropic, or the environment variable supplied with
`--api-key-env` for an OpenAI-compatible provider.

## Quick start

Run the following commands from the repository root. Start with a small
selection or one repository before launching a full collection.

### 1. Select repositories

```bash
python step1_v2.py --target-new 10
```

Useful selection options include:

```bash
python step1_v2.py --min-stars 10 --min-commits 100 --max-commits 20000
python step1_v2.py --min-prompt-hits 3 --pushed-after 2025-01-01
python step1_v2.py --no-test-filter
```

The selector is append-only and uses `step1_seen.json` to avoid reprocessing
repositories on later runs.

### 2. Mine prompt-edit commits

```bash
python step2.py --only-repo OWNER/REPO
```

After validating a pilot run, process pending repositories:

```bash
python step2.py
```

Useful controls:

```bash
python step2.py --limit 3
python step2.py --max-commits 5000
python step2.py --repo-timeout 90
python step2.py --retry-failed
python step2.py --reset
```

`--reset` clears the Stage 2 progress state and should only be used when a
fresh run is intended. Network failures are retried, and progress is recorded
in checkpoint files.

### 3. Extract prompt units

Test the unit extractor on one repository:

```bash
python step3.py --repo OWNER/REPO
```

Then process all repositories:

```bash
python step3.py
```

Useful controls:

```bash
python step3.py --limit 3
python step3.py --max-per-repo 200
python step3.py --reset
```

`--max-per-repo` is useful when balancing a dataset because prompt edits are
often concentrated in a small number of repositories.

### 4. Optionally annotate units with an LLM

Preview a pilot without making API calls:

```bash
python step4.py --pilot 40 --dry-run
```

Run a small Anthropic pilot:

```bash
python step4.py --provider anthropic --pilot 40
```

Or use an OpenAI-compatible endpoint:

```bash
python step4.py --provider openai --model YOUR_MODEL --base-url https://api.groq.com/openai/v1 --api-key-env OPENAI_API_KEY --pilot 40
```

Export a CSV for manual review:

```bash
python step4.py --export-csv pilot_sheet.csv
```

## Output files

| File | Description |
| --- | --- |
| `target_repos.csv` | Selected repositories, metadata, manifests, and prompt-hit counts. |
| `step1_seen.json` | Repository-selection resume state. |
| `prompt_commits.csv` | One row per confirmed prompt-edit commit, including file lists, variable names, and consumer-candidate diffs. |
| `step2_repo_stats.csv` | Per-repository mining funnel and skip reasons. |
| `step2_checkpoint.json` | Stage 2 resume state. |
| `step2_failed.json` | Repositories that reached the retry limit. |
| `step3_units.jsonl` | One JSON record per extracted prompt unit. |
| `step3_checkpoint.json` | Stage 3 resume state. |
| `step4_labeled.jsonl` | Optional LLM-generated component and operator annotations. |

### Unit-level record contents

Records in `step2b_units.jsonl` include identity fields such as repository,
commit SHA, file, and `unit_id`; old/new prompt text; change type and matching
method; anchor information; deterministic edit signals; path flags for tests,
examples, documentation, and tool configuration; and eligibility fields.

The extracted text is third-party repository content. Treat it as governed by
the license and terms applicable to each source repository. The commit SHA and
path fields allow records to be traced back to their source.

## Reproducibility notes

- Runs are resumable; rerunning without `--reset` processes pending work.
- Use a small pilot first to validate credentials, network access, and output
  volume.
- Keep the generated checkpoint files with the corresponding run.
- Do not compare funnel percentages across collections made with different
  selection protocols without documenting the difference.
- Dataset counts are snapshot-dependent and will change as repositories and
  histories evolve.

## Limitations

PROMINE is intentionally scoped to public Python repositories and to prompt
content that can be recognized by structural or lexical heuristics. Dynamically
constructed prompts, non-Python integrations, and prompt content hidden behind
runtime transformations may be missed. The prompt-message pre-screen also
introduces selection bias. A detected string is not automatically a valid
research prompt; use the eligibility flags and manual validation when
estimating precision.

Stage 4 annotations are model-assisted labels, not ground truth. They should
be reviewed against a human-annotated sample before being used as definitive
measurements.

## Responsible use and citation

Use this tool only with repositories and prompt data that you are permitted to
access and analyze. Respect GitHub's API terms, source-repository licenses, and
the privacy requirements of any downstream dataset release.

If you use PROMINE in academic work, cite the accompanying paper once its
bibliographic information is finalized:

```bibtex
@inproceedings{asbar2026promine,
  title     = {PROMINE: A Tool and Dataset of Prompt Edits Mined from
               LLM-Integrated Python Repositories},
  author    = {Asbar, Roufaida and Bendraou, Reda},
  booktitle = {Proceedings of the IEEE/ACM International Conference on Mining
               Software Repositories},
  year      = {2026}
}
```