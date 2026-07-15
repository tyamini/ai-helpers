#!/usr/bin/env python3
"""predict_tests.py — Stage 2p engine for pr-watchdog (Feature 2 early prediction).

Deterministically predicts, BEFORE CI finishes, whether each test ADDED by a PR will
actually run under the PR's current prefix. This is the automation that replaces any
on-the-fly grepping: the loop/subagent RUNS this and reads its JSON.

It REUSES the maintained mark->suite mapping in the cheetah repo's
`.jenkins/pr_prefix_rules/recommend_test_suites.py` (single source of truth parsed from
`common.groovy`, incl. the getCliStages array case) rather than reinventing that parser.

  Set A = suites the PR prefix enables            (prefixes_mapping.yaml)
  Set B = suites each added test is eligible for   (its pytest marks x _token_to_labels,
          restricted to TESTS_* suite labels)
  A test WILL RUN iff  A ∩ B != empty.

Classification per added test item:
  will-run    : eligible TESTS_* suites intersect the prefix-enabled suites
  wont-run    : eligible TESTS_* suites exist but NONE is enabled by the prefix
  cant-tell   : no stage marker maps to a TESTS_* suite (folder-based/unmarked collection,
                or a C++ gtest) — only the real run (Stage 2t) can decide

GitHub access is via the `gh` CLI. Emits one JSON object on stdout.

Usage:
  predict_tests.py --pr N [--repo-root PATH] [--prefix "Zebra, Cli"]
                   [--mapping-file PATH] [--ref SHA]
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys

REPO = "drivenets/cheetah"
MAPPING_REPO = "drivenets/jenkins-pipeline-shared"
MAPPING_PATH = "resources/prefixes_mapping.yaml"
RECO_REL = ".jenkins/pr_prefix_rules/recommend_test_suites.py"
COMMON_GROOVY_REL = "common.groovy"
TEST_FILE_RE = re.compile(r"(^|/)test[^/]*\.py$|_tests?\.py$|Tests?\.(cpp|cc)$|_test\.(cpp|cc)$")


def _gh(args, parse=True, check=True):
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = (proc.stdout or "").strip()
    if not parse:
        return out, proc.returncode
    return json.loads(out) if out else None


def _repo_root(explicit):
    if explicit:
        return explicit
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else os.getcwd()


def _load_reco(repo_root):
    """Import recommend_test_suites.py from the repo and return the module (engine reuse)."""
    path = os.path.join(repo_root, RECO_REL)
    if not os.path.isfile(path):
        raise RuntimeError(f"engine not found: {path} (need the cheetah .jenkins prefix-rules script)")
    spec = importlib.util.spec_from_file_location("recommend_test_suites", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gh_file(path, ref):
    """Contents of a repo file at a ref (authoritative — never trust a drifted local checkout)."""
    out, rc = _gh(["api", f"repos/{REPO}/contents/{path}?ref={ref}", "--jq", ".content"],
                  parse=False, check=False)
    if rc != 0 or not out:
        return ""
    import base64
    return base64.b64decode(out).decode("utf-8", "replace")


def _fetch_mapping(mapping_file):
    if mapping_file:
        with open(mapping_file, encoding="utf-8") as f:
            return f.read()
    out, rc = _gh(["api", f"repos/{MAPPING_REPO}/contents/{MAPPING_PATH}", "--jq", ".content"],
                  parse=False, check=False)
    if rc != 0 or not out:
        raise RuntimeError("could not fetch prefixes_mapping.yaml (Set A source of truth)")
    import base64
    return base64.b64decode(out).decode("utf-8", "replace")


def _parse_prefix_map(text):
    """label(name) -> set(TESTS_* vars enabled). Dependency-free parse of prefixes_mapping.yaml.

    Structure:  <Category>:\n  - <Label>:\n    vars_to_enable:\n      - TESTS_X ...
    """
    result, cur, in_enable = {}, None, False
    for raw in text.splitlines():
        if re.match(r"^\S", raw):  # top-level category — resets label context
            cur, in_enable = None, False
            continue
        m = re.match(r"^\s*-\s*(.+?):\s*$", raw)  # a sub-label list entry
        if m:
            cur = m.group(1).strip()
            result.setdefault(cur, set())
            in_enable = False
            continue
        if cur is None:
            continue
        if re.match(r"^\s*vars_to_enable\s*:\s*$", raw):
            in_enable = True
            continue
        if re.match(r"^\s*vars_to_disable\s*:", raw):
            in_enable = False
            continue
        if in_enable:
            vm = re.match(r"^\s*-\s*(\S+)\s*$", raw)
            if vm:
                result[cur].add(vm.group(1))
    return result


def _enabled_suites(prefix, prefix_map):
    """Union of vars_to_enable for every label in the (comma-separated) prefix, case-insensitively."""
    wanted = [p.strip().casefold() for p in prefix.split(",") if p.strip()]
    by_fold = {}
    for name, vars_ in prefix_map.items():
        by_fold.setdefault(name.casefold(), (name, set()))
        by_fold[name.casefold()][1].update(vars_)
    enabled, matched = set(), []
    for w in wanted:
        if w in by_fold:
            matched.append(by_fold[w][0])
            enabled |= by_fold[w][1]
    return enabled, matched, [w for w in wanted if w not in by_fold]


# --- per-item pytest marks (AST) ---------------------------------------------
def _mark_name(target):
    """pytest.mark.<NAME> -> NAME."""
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute) \
            and target.value.attr == "mark":
        return target.attr
    return None


def _decorator_marks(node, denylist):
    marks = set()
    for d in getattr(node, "decorator_list", []):
        target = d.func if isinstance(d, ast.Call) else d
        name = _mark_name(target)
        if name:
            marks.add(name)
    return {m for m in marks if m not in denylist}


def _module_pytestmark(tree, denylist):
    marks = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            for v in values:
                target = v.func if isinstance(v, ast.Call) else v
                name = _mark_name(target)
                if name and name not in denylist:
                    marks.add(name)
    return marks


_TRIVIAL_BASES = {"object", "TestCase", "unittest.TestCase"}


def _base_names(node):
    """Non-trivial base-class names of a ClassDef (marks CAN be inherited from these)."""
    out = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            name = b.id
        elif isinstance(b, ast.Attribute):
            name = b.attr
        else:
            continue
        if name not in _TRIVIAL_BASES:
            out.append(name)
    return out


def _items_with_marks(text, denylist):
    """[(item_id, set(own+class+module marks), [unresolved_base_names])] for test funcs/methods.

    NOTE: pytest marks are ALSO inherited from base classes, but resolving a base defined in
    another module is out of scope here. We surface the unresolved base names so the caller can
    avoid a false `wont-run` for a class that might inherit a stage-selecting marker.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    mod_marks = _module_pytestmark(tree, denylist)
    items = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cmarks = _decorator_marks(node, denylist) | mod_marks
            bases = _base_names(node)
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name.startswith("test"):
                    items.append((f"{node.name}::{b.name}", _decorator_marks(b, denylist) | cmarks, bases))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            items.append((node.name, _decorator_marks(node, denylist) | mod_marks, []))
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--repo-root", default=None, help="cheetah checkout (default: git toplevel)")
    ap.add_argument("--prefix", default=None, help="PR prefix (default: from PR title before ':')")
    ap.add_argument("--mapping-file", default=None, help="local prefixes_mapping.yaml (default: fetch via gh)")
    ap.add_argument("--ref", default=None, help="commit to read test/groovy sources at (default: PR head)")
    args = ap.parse_args()

    repo_root = _repo_root(args.repo_root)
    reco = _load_reco(repo_root)
    denylist = getattr(reco, "MARK_DENYLIST", set())

    view = _gh(["pr", "view", str(args.pr), "-R", REPO, "--json", "title,headRefOid,headRefName"])
    ref = args.ref or view.get("headRefOid") or "HEAD"
    prefix = args.prefix if args.prefix is not None else (view.get("title", "").split(":", 1)[0] if ":" in view.get("title", "") else "")

    # Set B engine: mark -> suite labels, from common.groovy at the PR head (authoritative),
    # falling back to the synced worktree copy only if the fetch fails.
    common_text = _gh_file(COMMON_GROOVY_REL, ref)
    if not common_text:
        p = os.path.join(repo_root, COMMON_GROOVY_REL)
        common_text = open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else ""
    token_to_labels = reco._token_to_labels(common_text)

    def eligible_suites(marks):
        out = set()
        for m in marks:
            out |= {lbl for lbl in token_to_labels.get(m, set()) if lbl.startswith("TESTS_")}
        return out

    # Set A: prefix -> enabled suites.
    prefix_map = _parse_prefix_map(_fetch_mapping(args.mapping_file))
    enabled, matched_labels, unknown_labels = _enabled_suites(prefix, prefix_map)

    # Added test files from the authoritative PR diff.
    names, _rc = _gh(["pr", "diff", str(args.pr), "-R", REPO, "--name-only"], parse=False, check=False)
    added_files = [f for f in (names or "").splitlines() if TEST_FILE_RE.search(f)]

    items_out, counts = [], {"will-run": 0, "wont-run": 0, "cant-tell": 0}
    wont_suites = set()
    for f in added_files:
        if f.endswith((".cpp", ".cc")):
            items_out.append({"file": f, "item": "(C++ gtest)", "marks": [], "eligible_suites": [],
                              "verdict": "cant-tell",
                              "note": "C++ unit test — runs in TESTS_QUAGGA_UT if registered in the UT build target; not marker-predictable"})
            counts["cant-tell"] += 1
            continue
        text = _gh_file(f, ref)
        for item_id, marks, bases in _items_with_marks(text, denylist):
            elig = eligible_suites(marks)
            note = None
            if elig & enabled:
                verdict = "will-run"
            elif elig:
                # eligible suites exist but none enabled -> would be wont-run, UNLESS the class
                # extends a base that may contribute a stage marker we can't resolve statically.
                if bases:
                    verdict = "cant-tell"
                    note = f"own marks map only to non-enabled suites; extends {bases} — may inherit a stage marker (unresolved)"
                else:
                    verdict = "wont-run"
                    wont_suites |= elig
            else:
                verdict = "cant-tell"
                if bases:
                    note = f"no own stage marker maps to a suite; extends {bases} — may inherit one (unresolved)"
                else:
                    note = "no stage marker maps to a suite (folder-collected/unmarked?)"
            counts[verdict] += 1
            item = {"file": f, "item": item_id, "marks": sorted(marks),
                    "eligible_suites": sorted(elig), "verdict": verdict}
            if bases:
                item["extends"] = bases
            if note:
                item["note"] = note
            items_out.append(item)

    # Suggested fix for wont-run: prefixes that WOULD enable the missing suites.
    suggested_prefixes = sorted({
        name for name, vars_ in prefix_map.items() if vars_ & wont_suites
    }) if wont_suites else []

    if counts["wont-run"]:
        result = "some-wont-run"
    elif counts["will-run"]:
        result = "all-will-run" if not counts["cant-tell"] else "will-run-plus-cant-tell"
    else:
        result = "cant-tell-only"

    print(json.dumps({
        "pr": args.pr, "head_sha": ref, "prefix": prefix,
        "prefix_labels_matched": matched_labels, "prefix_labels_unknown": unknown_labels,
        "enabled_suites": sorted(enabled),
        "added_test_files": added_files,
        "counts": counts, "result": result,
        "items": items_out,
        "suggested_prefixes_for_wont_run": suggested_prefixes,
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - single-line error for the caller
        print(f"predict_tests: {e}", file=sys.stderr)
        sys.exit(1)
