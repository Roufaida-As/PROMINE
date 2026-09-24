# Pipeline Architecture: The MACE Framework

To construct PromptImpact-Bench, we propose the MACE (Mining, Attribute-Filtering, Contract-Linking, and Execution-Validation) pipeline. Adapted from software repository mining methodologies (e.g., SWE-bench), MACE extracts, decouples, and re-validates real-world co-evolution instances where natural-language prompt edits break downstream structured-output consumers.

# The pipeline operates in four deterministic phases:

### Target Identification (MACE-Select): 
Filters repositories requiring active Python test harnesses and explicit dependency manifests.

### Candidate Scraping (MACE-Scrape):
Traverses post-2024 commit histories via AST-guided pattern matching to isolate prompt modifications.

### Attribute Linking (MACE-Link):
Pairs prompt edits with co-occurring changes in downstream consumers (Pydantic models, JSON parsers, and validation guards).

### Containerized Validation (MACE-Eval): 
Replays historical commits inside isolated environments to measure differential test-suite pass/fail rates.