#!/usr/bin/env python3
"""
Step 2c - Stage C: component split + PromptVer operator labeling (LLM)

Input : step2b_units.jsonl   (uses only units with stage_c_eligible == true)
Output: step2c_labeled.jsonl (one record per unit_id; resume-safe cache)

Setup : pip install anthropic python-dotenv      (paid Claude)   |   pip install openai python-dotenv  (free providers)
        .env  ->  ANTHROPIC_API_KEY=sk-ant-...

Usage:
  python step2c.py --pilot 40 --dry-run          # see how many units / rough token count
  python step2c.py --pilot 40                    # label a 40-unit pilot (round-robin over repos)
  python step2c.py --export-csv pilot_sheet.csv  # sheet for HAND labeling (for agreement / kappa)
  python step2c.py                               # all eligible units, capped at 25 per repo
  python step2c.py --model claude-haiku-5-5      # cheaper model
"""
import argparse
import csv
import json
import os
import time
from collections import defaultdict, Counter

from dotenv import load_dotenv

load_dotenv()

UNITS = "step2b_units.jsonl"
OUT = "step2c_labeled.jsonl"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_CHARS = 6000          # clip very long prompts
MAX_OUTPUT_TOKENS = 3000

OPS = {
    "E1": ("D", "Instruction Addition: adds a new explicit instruction"),
    "E2": ("D", "Instruction Modification: changes meaning/priority of an existing instruction"),
    "E3": ("D", "Instruction Removal: removes an instruction entirely"),
    "E4": ("Ctx", "Constraint Addition: adds a new constraint sentence"),
    "E5": ("Ctx", "Constraint Tightening: makes an existing constraint stricter/more specific"),
    "E6": ("Ctx", "Constraint Removal: removes an existing constraint"),
    "E7": ("E", "Example Addition: adds demonstration input-output pairs"),
    "E8": ("E", "Example Modification: alters content/labels of an existing example"),
    "E9": ("E", "Example Removal: removes demonstration examples"),
    "E10": ("O", "Output Format Addition: adds explicit structural/format requirements (JSON schema, list...)"),
    "E11": ("O", "Output Format Modification: changes structure, fields or strictness of the output format"),
    "E12": ("R", "Role Addition: adds a role assignment"),
    "E13": ("R", "Role Modification: alters role description/persona"),
    "E14": ("R", "Role Removal: removes the role assignment"),
    "E15": ("D", "Instruction Rephrase: rewrites instruction wording, same meaning"),
    "E16": ("E", "Example Rephrase: rewrites an example, same semantics"),
    "E17": ("rho", "Formatting Change: whitespace, bullet style, surface layout only"),
    "E18": ("rho", "Structural Reorder: reorders components/sentences without changing content"),
    "E19": ("D", "Generalisation: replaces concrete values/text with variable placeholders"),
    "E20": ("any", "Error Correction: fixes typos, grammar or syntax errors"),
}

SYSTEM = """You annotate edits to developer-written LLM prompt templates using the PromptVer framework.

A prompt template is P = <R, D, Ctx, E, O, rho>:
- R   Role: optional persona ("You are a Python expert")
- D   Directive: the core task instruction, without role or constraints
- Ctx Contextual constraints: rules, prohibitions, length limits, edge-case handling (list of sentences)
- E   Examples: few-shot input/output demonstrations (list)
- O   Output schema: explicit output-format contract (JSON schema, field names, list format, regex)
- rho Rendered text: the full concatenated text (used only to detect surface-only edits)

Edit operators (pick every operator that applies, one per atomic change):
""" + "\n".join(f"{k} [{v[0]}] {v[1]}" for k, v in OPS.items()) + """

Rules:
1. Split BOTH the old and the new prompt into R, D, Ctx, E, O by copying verbatim spans from the text. Use "" or [] when a component is absent. Never invent text.
2. Placeholders such as {name} or {{name}} stay inside whichever component they appear in. In Python f-strings, {expr} is runtime data, not a template variable.
3. Compare old vs new component by component, then choose operators. Use the minimal set that explains the change; do not add operators for components that did not change.
4. Rephrase (E15/E16) = same meaning, different wording. Modification (E2/E5/E8/E11/E13) = meaning, priority, fields or strictness changed.
5. Set contract_relevant=true only if the edit changes what downstream CODE could depend on: output fields/keys, output format, required placeholders, allowed values/enums, or a mandated structure. Describe the change in contract_note (empty string if false).
6. The hints given with the unit come from simple rules and may be wrong; verify them against the text.
Call submit_annotation with your result."""

COMP = {
    "type": "object",
    "properties": {
        "R": {"type": "string"},
        "D": {"type": "string"},
        "Ctx": {"type": "array", "items": {"type": "string"}},
        "E": {"type": "array", "items": {"type": "string"}},
        "O": {"type": "string"},
    },
    "required": ["R", "D", "Ctx", "E", "O"],
}

TOOL = {
    "name": "submit_annotation",
    "description": "Submit the component decomposition and operator labels for this prompt edit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "old": COMP,
            "new": COMP,
            "operators": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": list(OPS)},
                        "evidence": {"type": "string", "description": "short verbatim snippet or description of the change"},
                    },
                    "required": ["id", "evidence"],
                },
            },
            "contract_relevant": {"type": "boolean"},
            "contract_note": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["old", "new", "operators", "contract_relevant", "contract_note", "confidence"],
    },
}


def clip(s):
    return s if len(s) <= MAX_CHARS else s[:MAX_CHARS] + "\n[...truncated...]"


def build_user(u):
    hints = {
        "rule_hints": u.get("rule_hints"),
        "template_vars_added": u.get("vars_added"),
        "template_vars_removed": u.get("vars_removed"),
        "json_keys_added": u.get("json_keys_added"),
        "json_keys_removed": u.get("json_keys_removed"),
        "is_python_fstring": u.get("is_fstring"),
    }
    return (f"File: {u['file']}\nCommit message: {u.get('msg', '')}\n"
            f"Hints: {json.dumps(hints)}\n\n"
            f"=== OLD PROMPT ===\n{clip(u['old'])}\n=== END OLD ===\n\n"
            f"=== NEW PROMPT ===\n{clip(u['new'])}\n=== END NEW ===")


def consistency_flags(a):
    """Cheap sanity checks between the operators and the component split."""
    ids = {o["id"] for o in a.get("operators", [])}
    o, n = a["old"], a["new"]
    flags = []
    if "E12" in ids and not (not o["R"].strip() and n["R"].strip()):
        flags.append("E12_but_role_not_added")
    if "E14" in ids and not (o["R"].strip() and not n["R"].strip()):
        flags.append("E14_but_role_not_removed")
    if "E13" in ids and not (o["R"].strip() and n["R"].strip()):
        flags.append("E13_but_role_not_in_both")
    if "E10" in ids and not (not o["O"].strip() and n["O"].strip()):
        flags.append("E10_but_output_not_added")
    if "E11" in ids and not (o["O"].strip() and n["O"].strip()):
        flags.append("E11_but_output_not_in_both")
    if "E7" in ids and len(n["E"]) <= len(o["E"]):
        flags.append("E7_but_examples_not_increased")
    if "E9" in ids and len(n["E"]) >= len(o["E"]):
        flags.append("E9_but_examples_not_decreased")
    if not ids:
        flags.append("no_operator")
    return flags


def select(units, cap, pilot):
    by = defaultdict(list)
    for u in sorted(units, key=lambda x: x["unit_id"]):       # deterministic pseudo-random order
        by[u["repo_name"]].append(u)
    for r in by:
        by[r] = by[r][:cap] if cap else by[r]
    if pilot:                                                  # round-robin over repos
        out, repos = [], sorted(by)
        while len(out) < pilot and any(by.values()):
            for r in repos:
                if by[r] and len(out) < pilot:
                    out.append(by[r].pop(0))
        return out
    return [u for r in sorted(by) for u in by[r]]


def annotate(client, anthropic, model, unit, temperature):
    kwargs = dict(model=model, max_tokens=3000, system=SYSTEM, tools=[TOOL],
                  tool_choice={"type": "tool", "name": "submit_annotation"},
                  messages=[{"role": "user", "content": build_user(unit)}])
    if temperature is not None:
        kwargs["temperature"] = temperature
    for attempt in range(6):
        try:
            r = client.messages.create(**kwargs)
            block = next(b for b in r.content if b.type == "tool_use")
            return block.input, {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError):
            time.sleep(2 ** attempt * 2)
    raise RuntimeError("API failed after retries")


def parse_json(txt):
    txt = (txt or "").strip()
    start, end = txt.find("{"), txt.rfind("}")
    return json.loads(txt[start:end + 1])


def valid(ann):
    """Light schema check for providers without forced tool calling."""
    try:
        for side in ("old", "new"):
            assert all(k in ann[side] for k in ("R", "D", "Ctx", "E", "O"))
            ann[side]["Ctx"] = ann[side]["Ctx"] or []
            ann[side]["E"] = ann[side]["E"] or []
        ann["operators"] = [o for o in ann["operators"] if isinstance(o, dict) and o.get("id") in OPS]
        ann.setdefault("contract_relevant", False)
        ann.setdefault("contract_note", "")
        ann.setdefault("confidence", "low")
        return True
    except Exception:
        return False


class Fatal(Exception):
    pass


def annotate_openai(client, model, unit, temperature, sleep):
    """OpenAI-compatible endpoints (Gemini, Groq, OpenRouter, Ollama...) using JSON mode."""
    sys_msg = SYSTEM.replace(
        "Call submit_annotation with your result.",
        "Reply with ONLY one JSON object (no prose, no markdown) matching this JSON Schema:\n"
        + json.dumps(TOOL["input_schema"]))
    for attempt in range(6):
        try:
            r = client.chat.completions.create(
                model=model, temperature=0 if temperature is None else temperature,
                response_format={"type": "json_object"},
                max_tokens=MAX_OUTPUT_TOKENS,
                messages=[{"role": "system", "content": sys_msg},
                          {"role": "user", "content": build_user(unit)}])
            ann = parse_json(r.choices[0].message.content)
            if valid(ann):
                time.sleep(sleep)                     # stay under free-tier rate limits
                u = r.usage
                return ann, {"in": getattr(u, "prompt_tokens", 0) or 0,
                             "out": getattr(u, "completion_tokens", 0) or 0}
        except Exception as e:                        # rate limit (429), bad JSON, network...
            message = str(e)
            if type(e).__name__ in ("NotFoundError", "AuthenticationError",
                                    "PermissionDeniedError"):
                raise Fatal(f"{type(e).__name__}: {str(e)[:300]}")   # retrying cannot help
            if "Request too large" in message:
                raise Fatal(f"{type(e).__name__}: {message[:300]}")
            print(f"   retry {attempt + 1}: {type(e).__name__}: {message[:100]}")
        time.sleep(min(2 ** attempt * 3, 60))
    raise RuntimeError("failed after retries")


def export_csv(path, labeled_path, units_by_id):
    rows = [json.loads(l) for l in open(labeled_path, encoding="utf-8")]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig so Excel opens it cleanly
        w = csv.writer(f)
        w.writerow(["unit_id", "repo", "file", "old", "new", "llm_operators",
                    "llm_contract_relevant", "flags", "HUMAN_operators", "HUMAN_contract_relevant"])
        for r in rows:
            u = units_by_id.get(r["unit_id"], {})
            a = r["annotation"]
            w.writerow([r["unit_id"], r["repo_name"], r["file"], u.get("old", ""), u.get("new", ""),
                        ";".join(o["id"] for o in a["operators"]), a["contract_relevant"],
                        ";".join(r["consistency_flags"]), "", ""])
    print(f"wrote {path} ({len(rows)} rows) - fill the HUMAN_ columns BEFORE looking at llm_operators")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=UNITS)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-per-repo", type=int, default=25)
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic",
                    help="'openai' = any OpenAI-compatible API (Gemini, Groq, OpenRouter, Ollama)")
    ap.add_argument("--base-url", help="e.g. https://api.groq.com/openai/v1")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY", help="env var holding the key")
    ap.add_argument("--sleep", type=float, default=0, help="seconds to wait between calls (free tiers)")
    ap.add_argument("--export-csv")
    a = ap.parse_args()

    units_all = [json.loads(l) for l in open(a.input, encoding="utf-8")]
    units_by_id = {u["unit_id"]: u for u in units_all}

    if a.export_csv:
        export_csv(a.export_csv, a.out, units_by_id)
        return

    eligible = [u for u in units_all if u.get("change_type") == "modified" and u.get("stage_c_eligible")]
    chosen = select(eligible, a.max_per_repo, a.pilot)
    if a.limit:
        chosen = chosen[:a.limit]

    done = set()
    if os.path.exists(a.out):
        done = {json.loads(l)["unit_id"] for l in open(a.out, encoding="utf-8")}
    todo = [u for u in chosen if u["unit_id"] not in done]

    est_in = sum(len(u["old"]) + len(u["new"]) for u in todo) // 4 + len(todo) * 1800
    print(f"eligible={len(eligible)} selected={len(chosen)} already_done={len(chosen) - len(todo)} "
          f"to_label={len(todo)} ~input_tokens={est_in:,} model={a.model}")
    if a.dry_run or not todo:
        return

    anthropic = None
    if a.provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY
    else:
        from openai import OpenAI
        client = OpenAI(base_url=a.base_url, api_key=os.getenv(a.api_key_env, "ollama"))
    tot_in = tot_out = 0
    flagged = Counter()
    for i, u in enumerate(todo, 1):
        try:
            if a.provider == "anthropic":
                ann, usage = annotate(client, anthropic, a.model, u, a.temperature)
            else:
                ann, usage = annotate_openai(client, a.model, u, a.temperature, a.sleep)
        except Fatal as e:
            print(f"\nSTOPPED (config problem, not a rate limit): {e}\nCheck --model / key / base-url.")
            return
        except Exception as e:
            print(f"[{i}/{len(todo)}] FAILED {u['unit_id']}: {e}")
            continue
        flags = consistency_flags(ann)
        rec = {"unit_id": u["unit_id"], "repo_name": u["repo_name"], "commit_hash": u["commit_hash"],
               "file": u["file"], "key": u["key"], "model": a.model, "annotation": ann,
               "consistency_flags": flags, "usage": usage}
        with open(a.out, "a", encoding="utf-8") as f:       # append per unit = resume-safe
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tot_in += usage["in"]
        tot_out += usage["out"]
        flagged.update(flags)
        print(f"[{i}/{len(todo)}] {u['repo_name'][:28]:28} ops={','.join(o['id'] for o in ann['operators']) or '-':14} "
              f"contract={ann['contract_relevant']} conf={ann['confidence']} {flags or ''}")

    print(f"\ntokens in={tot_in:,} out={tot_out:,}")
    print("consistency flags:", dict(flagged))


if __name__ == "__main__":
    main()