#!/usr/bin/env python3
"""
Step 3 - Stages A + B  (deterministic, no LLM)

Stage A: recover P_old / P_new for every prompt touched by a Step-2 commit
Stage B: compute cheap rule-based signals (surface-only, reorder, {vars}, JSON keys...)

Input : prompt_commits.csv (Step 2 output)  +  target_repos.csv (for clone_url)
Output: step3_units.jsonl  - one JSON object per (commit, file, prompt key)
        step3_checkpoint.json - finished repos (resume-safe)

Step 4 (LLM component split + operator labeling) will READ step3_units.jsonl,
so Stages A+B never need to be re-run.

Usage:
  python step3.py                       # everything
  python step3.py --repo owner/name     # test on one repo first
  python step3.py --limit 3             # first 3 repos only
  python step3.py --reset               # start over
"""
import argparse
import ast
import csv
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import warnings
from collections import Counter

import pandas as pd

# Reuse the exact Step-2 heuristics so 2b agrees with what Step 2 detected.
from step2 import (
    PROMPT_NAME_HINT, PROMPT_TEXT_HINT, PROMPT_CALL_HINT,
    PROMPT_MESSAGE_ROLES, _attribute_name,
)

try:
    import yaml
except ImportError:
    yaml = None

warnings.filterwarnings("ignore")           # SyntaxWarnings from the mined repos' own code
try:                                       # Windows consoles (cp1252) crash on non-ASCII prompts
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COMMITS_CSV = "prompt_commits.csv"
REPOS_CSV = "target_repos.csv"
OUT = "step3_units.jsonl"
CKPT = "step3_checkpoint.json"
MAX_FILES_PER_COMMIT = 30        # bulk commits (e.g. 87 files) are logged as skipped
MAX_FILE_BYTES = 2_000_000
FUZZY_MIN_RATIO = 0.60
FUZZY_MAX_SIDE = 200             # skip fuzzy matching beyond 200x200 leftovers
MIN_CHARS = 20                   # drop tiny strings such as "json", "text", "{prompt}"
MIN_WORDS = 3

# Path flags (units are KEPT, just flagged; Step 4 ignores them via stage3_eligible)
_TEST = re.compile(r"(?i)(^|[\\/])(tests?|__tests__|fixtures?|mocks?)[\\/]|"
                   r"(^|[\\/])(test_[^\\/]*|[^\\/]*_test\.py|conftest\.py)$")
_EXAMPLE = re.compile(r"(?i)(^|[\\/])(examples?|docs?|documentation|cookbooks?|demos?|samples?|"
                      r"tutorials?|notebooks?)[\\/]")
_TOOLCFG = re.compile(r"(?i)(^|[\\/])(\.coderabbit[^\\/]*|\.github[\\/].*)$")


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------
def git(repo_dir, *args, timeout=1800):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True,
                          timeout=timeout, env=env)


def git_show(repo_dir, rev, path):
    p = git(repo_dir, "show", f"{rev}:{path.replace(chr(92), '/')}")
    if p.returncode != 0 or len(p.stdout) > MAX_FILE_BYTES:
        return None
    return p.stdout.decode("utf-8", errors="replace")


def clone(url, dest):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "clone", "--no-checkout", "--quiet", "--no-tags", "--shallow-since=2023-12-31", url, dest],
                       capture_output=True, timeout=3600, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="replace")[:300])


# --------------------------------------------------------------------------
# Stage A - extract prompt strings from a file version
# --------------------------------------------------------------------------
def _fstring_text(node):
    parts, exprs = [], []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            try:
                e = ast.unparse(v.value)
            except Exception:
                e = "?"
            exprs.append(e)
            parts.append("{" + e + "}")
    return "".join(parts), exprs


def mask_exprs(text, exprs):
    for e in sorted(set(exprs), key=len, reverse=True):
        text = text.replace("{" + e + "}", "{}")
    return text


def py_prompts(source):
    """Return [(key, text, meta)] for prompt-like strings, or None if unparsable.
    meta = {"f": is_fstring, "exprs": [interpolated python expressions]}"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    found = []
    for node in ast.walk(tree):
        par = parents.get(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(par, ast.JoinedStr):
                continue                      # piece of an f-string
            text, is_f, exprs = node.value, False, []
        elif isinstance(node, ast.JoinedStr):
            if isinstance(par, ast.FormattedValue):
                continue                      # format spec
            text, exprs = _fstring_text(node)
            is_f = True
        else:
            continue
        if len(text.strip()) < MIN_CHARS or len(text.split()) < MIN_WORDS:
            continue
        if isinstance(par, ast.Dict) and node in par.keys:
            continue

        assignment_names, call_names, context_keys, roles, scope = [], [], [], set(), []
        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                assignment_names += [n for t in targets if (n := _attribute_name(t))]
            elif isinstance(parent, ast.Call):
                if (n := _attribute_name(parent.func)):
                    call_names.append(n)
            elif isinstance(parent, ast.Dict):
                for k, v in zip(parent.keys, parent.values):
                    if k is not None and v is current and isinstance(k, ast.Constant) \
                            and isinstance(k.value, str):
                        context_keys.append(k.value)
                    if isinstance(k, ast.Constant) and k.value == "role" \
                            and isinstance(v, ast.Constant) and isinstance(v.value, str):
                        roles.add(v.value.lower())
            elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope.append(parent.name)
            current = parent

        named = any(PROMPT_NAME_HINT.search(n) for n in assignment_names)
        called = any(PROMPT_CALL_HINT.search(n) for n in call_names)
        inline = "content" in {k.lower() for k in context_keys}
        if inline and roles:
            inline = bool(roles & PROMPT_MESSAGE_ROLES)
        if not (inline or ((named or called) and PROMPT_TEXT_HINT.search(text))):
            continue

        base = next((n for n in assignment_names if PROMPT_NAME_HINT.search(n)),
                    assignment_names[-1] if inline and assignment_names else None)
        if not base and call_names:
            base = "call:" + call_names[0]
        base = base or "<anon>"
        ctx = f'["{context_keys[-1]}"]' if context_keys else ""
        key = f"{'.'.join(reversed(scope))}|{base}{ctx}"
        found.append((getattr(node, "lineno", 0), key, text, {"f": is_f, "exprs": exprs}))

    found.sort(key=lambda x: x[0])
    seen, out = Counter(), []
    for _, key, text, meta in found:           # ordinal disambiguates repeated keys
        out.append((f"{key}#{seen[key]}", text, meta))
        seen[key] += 1
    return out


CFG_KEY = re.compile(r"(?i)(prompt|template|instruction|system|user|developer|message|"
                     r"content|example|output[_ -]?format|response[_ -]?format)")


_LIST_ID = ("name", "id", "title", "key", "label")
_NOMETA = {"f": False, "exprs": []}


def _walk_cfg(obj, path, hints, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_cfg(v, path + [str(k)], hints + [str(k)], out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            label = str(i)
            if isinstance(v, dict):               # stable id instead of position
                for f in _LIST_ID:
                    if isinstance(v.get(f), (str, int)):
                        label = f"{f}={str(v[f])[:30]}"
                        break
            _walk_cfg(v, path + [label], hints, out)
    elif isinstance(obj, str):
        if len(obj.strip()) >= MIN_CHARS and len(obj.split()) >= MIN_WORDS \
                and any(CFG_KEY.search(h) for h in hints):
            out.append((".".join(path), obj))


def cfg_prompts(source, ext):
    try:
        if ext == ".json":
            data = json.loads(source)
        elif yaml is not None:
            data = list(yaml.safe_load_all(source))
        else:
            return None
    except Exception:
        return None
    raw = []
    _walk_cfg(data, [], [], raw)
    seen, out = Counter(), []
    for key, text in raw:
        out.append((f"{key}#{seen[key]}", text, _NOMETA))
        seen[key] += 1
    return out


def extract(source, path):
    ext = os.path.splitext(path.lower())[1]
    if ext == ".py":
        return py_prompts(source)
    if ext in {".json", ".yaml", ".yml"}:
        return cfg_prompts(source, ext)
    if ext in {".prompt", ".rail"}:
        return [("__file__", source, _NOMETA)]
    return None


def pair_units(old_items, new_items):
    """Yield (change_type, match, key, old, new, meta_old, meta_new)."""
    od = {k: t for k, t, _ in old_items}
    nd = {k: t for k, t, _ in new_items}
    om = {k: m for k, _, m in old_items}
    nm = {k: m for k, _, m in new_items}
    for k in od.keys() & nd.keys():
        if od[k] != nd[k]:
            yield "modified", "key", k, od[k], nd[k], om[k], nm[k]
    lo = [k for k in od if k not in nd]
    ln = [k for k in nd if k not in od]
    if lo and ln and len(lo) <= FUZZY_MAX_SIDE and len(ln) <= FUZZY_MAX_SIDE:
        pairs = []
        for a in lo:
            for b in ln:
                sm = difflib.SequenceMatcher(None, od[a][:3000], nd[b][:3000], autojunk=False)
                if sm.quick_ratio() >= FUZZY_MIN_RATIO:
                    pairs.append((sm.ratio(), a, b))
        used_a, used_b = set(), set()
        for r, a, b in sorted(pairs, reverse=True):
            if r < FUZZY_MIN_RATIO or a in used_a or b in used_b:
                continue
            used_a.add(a)
            used_b.add(b)
            if od[a] != nd[b]:
                yield "modified", "fuzzy", f"{a} -> {b}", od[a], nd[b], om[a], nm[b]
        lo = [k for k in lo if k not in used_a]
        ln = [k for k in ln if k not in used_b]
    for k in lo:
        yield "removed", "", k, od[k], None, om[k], None
    for k in ln:
        yield "added", "", k, None, nd[k], None, nm[k]


# --------------------------------------------------------------------------
# Stage B - cheap rule-based signals
# --------------------------------------------------------------------------
_VAR_SINGLE = re.compile(r"(?<!\{)\{\s*(\w+)\s*\}(?!\})")
_VAR_JINJA = re.compile(r"\{\{\s*(\w+)[^}]*\}\}")
_JSON_KEY = re.compile(r'["\'](\w+)["\']\s*:')


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def sentences(s):
    return [x for x in (norm(p) for p in re.split(r"(?<=[.!?:])\s+|\n+", s)) if x]


def template_vars(s):
    return set(_VAR_SINGLE.findall(s)) | set(_VAR_JINJA.findall(s))


def signals(old, new, mo=_NOMETA, mn=_NOMETA):
    no, nn = norm(old), norm(new)
    so, sn = sentences(old), sentences(new)
    is_f = bool(mo["f"] or mn["f"])
    if is_f:   # python f-string: {expr} is a code expression, NOT a template variable
        vo, vn = set(), set()
        eo, en = set(mo["exprs"]), set(mn["exprs"])
    else:
        vo, vn = template_vars(old), template_vars(new)
        eo = en = set()
    ko, kn = set(_JSON_KEY.findall(old)), set(_JSON_KEY.findall(new))

    if max(len(no), len(nn)) > 20000:
        ratio = difflib.SequenceMatcher(None, no, nn, autojunk=True).quick_ratio()
    else:
        ratio = difflib.SequenceMatcher(None, no, nn, autojunk=False).ratio()

    surface_only = no == nn
    interp_only = bool(is_f and not surface_only and
                       norm(mask_exprs(old, mo["exprs"])) == norm(mask_exprs(new, mn["exprs"])))
    reorder_only = (not surface_only) and sorted(so) == sorted(sn) and so != sn
    ud = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    lines_add = sum(1 for l in ud if l.startswith("+") and not l.startswith("+++"))
    lines_del = sum(1 for l in ud if l.startswith("-") and not l.startswith("---"))

    hints = []
    if surface_only:
        hints.append("E17")                       # whitespace / layout only
    elif reorder_only:
        hints.append("E18")                       # same sentences, new order
    elif ratio >= 0.97:
        hints.append("E20?")                      # tiny edit: typo/grammar candidate
    if vn - vo and not (vo - vn):
        hints.append("E19?")                      # new placeholder(s) only

    return {
        "surface_only": surface_only,
        "reorder_only": reorder_only,
        "interp_only": interp_only,      # only a python {expression} changed -> code edit, not prompt edit
        "is_fstring": is_f,
        "fstring_exprs_added": sorted(en - eo),
        "fstring_exprs_removed": sorted(eo - en),
        "similarity": round(ratio, 4),
        "lines_added": lines_add,
        "lines_removed": lines_del,
        "vars_added": sorted(vn - vo),
        "vars_removed": sorted(vo - vn),
        "json_keys_added": sorted(kn - ko),
        "json_keys_removed": sorted(ko - kn),
        "contract_signal": bool((vn ^ vo) or (kn ^ ko)),   # template vars or schema keys changed
        "rule_hints": hints,
    }


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def uid(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def read_input(path):
    if path.lower().endswith((".xls", ".xlsx")):
        # NOTE: Excel truncates cells at 32,767 chars -> prefer the original CSV
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    return df.fillna("")


def process_commit(repo_dir, row):
    h = row["commit_hash"]
    files = [f for f in row["prompt_files"].split(";") if f]
    base = {"repo_name": row["repo_name"], "category": row["category"],
            "commit_hash": h, "author_date": row["author_date"], "msg": row["msg"]}

    if len(files) > MAX_FILES_PER_COMMIT:
        yield {**base, "unit_id": uid(h, "bulk"), "file": None, "key": None,
               "change_type": "skipped", "reason": f"bulk_commit_{len(files)}_files"}
        return

    for path in files:
        old_src = git_show(repo_dir, f"{h}^", path)
        new_src = git_show(repo_dir, h, path)
        if new_src is None:
            yield {**base, "unit_id": uid(h, path, "gone"), "file": path, "key": None,
                   "change_type": "skipped", "reason": "file_deleted_or_unreadable"}
            continue
        if old_src is None:
            yield {**base, "unit_id": uid(h, path, "newfile"), "file": path, "key": None,
                   "change_type": "skipped", "reason": "new_file_or_root_commit"}
            continue
        o, n = extract(old_src, path), extract(new_src, path)
        if o is None or n is None:
            yield {**base, "unit_id": uid(h, path, "parse"), "file": path, "key": None,
                   "change_type": "skipped", "reason": "unparsable_or_yaml_missing"}
            continue
        flags = {"is_test": bool(_TEST.search(path)), "is_example_doc": bool(_EXAMPLE.search(path)),
                 "is_tool_config": bool(_TOOLCFG.search(path))}
        for ctype, match, key, old, new, mo, mn in pair_units(o, n):
            rec = {**base, "unit_id": uid(h, path, key, ctype), "file": path, "key": key,
                   "change_type": ctype, "match": match, "old": old, "new": new, **flags}
            if ctype == "modified":
                rec.update(signals(old, new, mo, mn))
                rec["stage3_eligible"] = not (any(flags.values()) or rec["surface_only"]
                                               or rec["reorder_only"] or rec["interp_only"])
            yield rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=COMMITS_CSV)
    ap.add_argument("--repo")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--max-per-repo", type=int, default=0,
                    help="cap Stage-C-eligible units per repo (0 = no cap; pseudo-random, deterministic)")
    a = ap.parse_args()

    if a.reset:
        for p in (OUT, CKPT):
            if os.path.exists(p):
                os.remove(p)

    df = read_input(a.input)
    urls = {r["repo_name"]: r["clone_url"] for r in csv.DictReader(open(REPOS_CSV, encoding="utf-8"))}
    done = set(json.load(open(CKPT))["done"]) if os.path.exists(CKPT) else set()

    repos = [r for r in df["repo_name"].unique() if not a.repo or r == a.repo]
    if a.limit:
        repos = repos[:a.limit]

    for i, repo in enumerate(repos, 1):
        if repo in done:
            print(f"[{i}/{len(repos)}] skip (done): {repo}")
            continue
        sub = df[df["repo_name"] == repo]
        print(f"[{i}/{len(repos)}] {repo}: {len(sub)} commits")
        tmp = tempfile.mkdtemp()
        try:
            clone(urls[repo], tmp)
            buf = []
            for _, row in sub.iterrows():            # one bad commit must not kill the repo
                try:
                    buf.extend(process_commit(tmp, row))
                except Exception as e:                # incl. RecursionError / MemoryError
                    buf.append({"repo_name": repo, "commit_hash": row["commit_hash"],
                                "unit_id": uid(row["commit_hash"], "error"), "file": None, "key": None,
                                "change_type": "skipped",
                                "reason": f"error:{type(e).__name__}:{str(e)[:80]}"})
            if a.max_per_repo:
                el = sorted((r for r in buf if r.get("stage3_eligible")), key=lambda r: r["unit_id"])
                for r in el[a.max_per_repo:]:
                    r["stage3_eligible"] = False
                    r["excluded_reason"] = "repo_cap"
            with open(OUT, "a", encoding="utf-8") as f:       # write per repo = resume-safe
                for rec in buf:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            c = Counter(r["change_type"] for r in buf)
            print(f"   modified={c['modified']} added={c['added']} removed={c['removed']} skipped={c['skipped']}")
            done.add(repo)
            json.dump({"done": sorted(done)}, open(CKPT, "w"), indent=2)
        except Exception as e:
            print(f"   !! failed {repo}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # summary
    if os.path.exists(OUT):
        recs = [json.loads(l) for l in open(OUT, encoding="utf-8")]
        mod = [r for r in recs if r["change_type"] == "modified"]
        print("\n=== Summary ===")
        print("change types :", dict(Counter(r["change_type"] for r in recs)))
        print("skip reasons :", dict(Counter(r.get("reason") for r in recs if r["change_type"] == "skipped")))
        print("surface_only :", sum(r["surface_only"] for r in mod), "/", len(mod))
        print("contract_sig :", sum(r["contract_signal"] for r in mod), "/", len(mod))
        print("rule hints   :", dict(Counter(h for r in mod for h in r["rule_hints"])))
        el = [r for r in mod if r["stage3_eligible"]]
        print("excluded     :", {k: sum(bool(r[k]) for r in mod) for k in
              ("is_test", "is_example_doc", "is_tool_config", "surface_only", "reorder_only", "interp_only")})
        print("STAGE C ELIGIBLE:", len(el), "units in", len({r["commit_hash"] for r in el}), "commits")
        print("per repo     :", dict(Counter(r["repo_name"] for r in el).most_common()))


if __name__ == "__main__":
    main()