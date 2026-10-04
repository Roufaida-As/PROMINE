"""Compute presentation statistics for MACE Step 1.

Usage:
    python stats_step1.py

The script reads target_repos.csv and writes step1_stats.json. App types are
transparent heuristics based on repository names and manifest paths; they are
not intended to replace manual validation.
"""

import csv
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "target_repos.csv"
OUTPUT = ROOT / "step1_stats.json"

EXCLUDED = [
    {"category": "guardrails-ai", "repo_name": "BerriAI/litellm", "manifests": "cookbook/anthropic_agent_sdk/requirements.txt; cookbook/litellm-ollama-docker-image/requirements.txt; cookbook/livekit_agent_sdk/requirements.txt"},
    {"category": "guardrails-ai", "repo_name": "data-privacy-stack/presidio", "manifests": "docs/samples/deployments/openai-anonymaztion-and-deanonymaztion-best-practices/src/api/requirements.txt; docs/samples/deployments/openai-anonymaztion-and-deanonymaztion-best-practices/src/client_app/requirements.txt; docs/samples/deployments/redacting-telemetry/app/requirements.txt"},
    {"category": "pydantic-ai", "repo_name": "pydantic/pydantic-ai", "manifests": "clai/pyproject.toml; examples/pyproject.toml; pydantic_ai_slim/pyproject.toml"},
    {"category": "guardrails-ai", "repo_name": "guardrails-ai/guardrails", "manifests": "pyproject.toml; server_ci/requirements.txt"},
    {"category": "guardrails-ai", "repo_name": "NVIDIA-NeMo/Guardrails", "manifests": "examples/deployment/gliner_server/pyproject.toml; nemoguardrails/library/factchecking/align_score/requirements.txt; nemoguardrails/library/jailbreak_detection/requirements.txt"},
    {"category": "guardrails-ai", "repo_name": "IBM/mcp-context-forge", "manifests": "docs/requirements.txt; mcp-servers/python/data_analysis_server/pyproject.toml; mcp-servers/python/data_analysis_server/requirements.txt"},
    {"category": "guardrails-ai", "repo_name": "future-agi/future-agi", "manifests": "futureagi/agentic_eval/setup.py; futureagi/ee/requirements.txt; futureagi/model_serving/pyproject.toml"},
]


def classify_app(row):
    text = f"{row['repo_name']} {row['manifests']}".lower()
    if any(word in text for word in ("template", "scaffold", "starter")):
        return "Template / starter"
    if any(word in text for word in ("sdk", "library", "pydantic-ai", "instructor", "mcpadapt")):
        return "SDK / library"
    if any(word in text for word in ("rag", "chatbot", "agent", "assistant", "research")):
        return "Agent / RAG application"
    if any(word in text for word in ("server", "backend", "api", "platform", "service")):
        return "Service / platform"
    return "Other Python project"


def main():
    with INPUT.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    unique_repos = {row["repo_name"] for row in rows}
    categories = Counter(row["category"] for row in rows)
    app_types = Counter(classify_app(row) for row in rows)
    manifest_types = Counter()
    for row in rows:
        for manifest in row["manifests"].split(";"):
            name = manifest.strip().lower()
            if name.endswith("pyproject.toml"):
                manifest_types["pyproject.toml"] += 1
            elif name.endswith("requirements.txt"):
                manifest_types["requirements.txt"] += 1
            elif name.endswith("setup.py"):
                manifest_types["setup.py"] += 1

    result = {
        "source": INPUT.name,
        "qualified_rows": len(rows),
        "unique_repositories": len(unique_repos),
        "duplicate_rows": len(rows) - len(unique_repos),
        "categories": dict(categories),
        "app_types": dict(app_types),
        "manifest_types": dict(manifest_types),
        "stars": {
            "min": min((int(row["stars"]) for row in rows), default=0),
            "median": sorted(int(row["stars"]) for row in rows)[len(rows) // 2] if rows else 0,
            "max": max((int(row["stars"]) for row in rows), default=0),
        },
        "excluded": EXCLUDED,
        "exclusion_reason": "Manual exclusion because repository scale and nested manifests made the scan/install process too expensive and blocked the local machine.",
        "classification_note": "App types are heuristic labels derived from repository names and manifest paths; they require manual validation.",
    }
    OUTPUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"\nWrote {OUTPUT}")


if __name__ == "__main__":
    main()