#!/usr/bin/env python3
"""
pr_watchdog.py — deterministic PR/CI observation + build-trigger helper for the
pr-watchdog skill. Self-contained: stdlib + the `gh` CLI + `git` only.

GITHUB ACCESS IS VIA THE `gh` CLI ONLY. No personal access token, no
~/.cursor/mcp.json lookup, no GitHub MCP, and no dependency on pr_driver.py. In the
cursor-agent CLI the GitHub PAT is not stored on disk (the harness injects auth into
the MCP server at runtime), so anything reading a token from mcp.json fails. `gh` is
the authenticated shell tool for GitHub here.

Jenkins build/stage/test detail is read over plain unauthenticated HTTP (urllib) —
Jenkins never needed GitHub creds.

Subcommands (JSON on stdout):
  status   Emit one normalized situation JSON for a PR (see references/state-schema.md).
  resolve  Resolve a unique open PR from the current local branch.
  trigger  Post the Jenkins "pipeline please rebuild failed <slug>" PR comment (via gh)
           for every discovered server.

Exit codes:
  0  success (JSON on stdout)
  1  hard error (message on stderr) — e.g. gh missing/unauthenticated, PR not found
  2  status/resolve: no unique PR could be resolved (JSON with pr=null on stdout)

Requires: an authenticated `gh` CLI (`gh auth status`).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

REPO = "drivenets/cheetah"

# --- pure helpers (Jenkins PR build URL parsing + context mapping) -----------
JENKINS_PR_PATH_RE = re.compile(r"/PR-(\d+)/(\d+)/")
STATUS_URL_HOST_RE = re.compile(r"^https?://([^/]+)", re.IGNORECASE)

# GitHub commit-status state -> our coarse server state.
_STATE_MAP = {"success": "PASSED", "pending": "RUNNING", "failure": "FAILED", "error": "FAILED"}
# wfapi stage statuses that mean "this stage failed".
_BAD_STAGE = {"FAILED", "UNSTABLE", "ABORTED"}


def _display_name(context):
    """GitHub context -> short label (middle segment of Jenkins-*/pr-head)."""
    if context.startswith("Jenkins-") and context.endswith("/pr-head"):
        return context[len("Jenkins-"):-len("/pr-head")]
    return context


def _pipeline_slug(context):
    """Compact pipeline token for `pipeline please rebuild failed <slug>` comments.

    Lowercase the display name and strip every run of non-alphanumerics
    (e.g. "Israel-1" -> "israel1", "AWS-5" -> "aws5").
    """
    slug = re.sub(r"[^a-z0-9]+", "", _display_name(context).lower())
    return slug or "jenkins"


# --- Jenkins (unauthenticated HTTP, best-effort) -----------------------------
def _jenkins_base(host, jenkins_pr, build):
    return f"https://{host}/job/drivenets/job/cheetah/job/PR-{jenkins_pr}/{build}"


def _jenkins_get(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https host
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - enrichment is best-effort; degrade to None
        return None


def _failed_stage_names(host, jenkins_pr, build):
    """Names of failed stages (parents and children) from wfapi/describe."""
    data = _jenkins_get(_jenkins_base(host, jenkins_pr, build) + "/wfapi/describe")
    if not isinstance(data, dict):
        return []
    return [st.get("name", "") for st in data.get("stages", []) if st.get("status") in _BAD_STAGE]


def _failed_tests(host, jenkins_pr, build):
    """Failed test cases from the classic testReport (phase/suite from block names)."""
    url = (_jenkins_base(host, jenkins_pr, build)
           + "/testReport/api/json?tree=suites[enclosingBlockNames,cases[className,name,status]]")
    data = _jenkins_get(url)
    if not isinstance(data, dict):
        return []
    out, seen = [], set()
    for suite in data.get("suites", []):
        blocks = suite.get("enclosingBlockNames", []) or []
        suite_name = blocks[0] if len(blocks) >= 1 else ""
        phase = blocks[1] if len(blocks) >= 2 else ""
        for case in suite.get("cases", []):
            if case.get("status") not in ("FAILED", "REGRESSION"):
                continue
            tm, tf = case.get("name", ""), case.get("className", "")
            key = f"{tf}.{tm}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"test_method": tm, "test_file": tf, "suite": suite_name, "phase": phase})
    return out


# --- GitHub via gh -----------------------------------------------------------
def _gh(args, parse=True, check=True):
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    out = (proc.stdout or "").strip()
    if not parse:
        return out, proc.returncode
    return json.loads(out) if out else None


def _current_branch():
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _repo_root():
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "/home/dn/cheetah"


def _commit_status(sha):
    """Combined GitHub commit status: {state, total_count, statuses[]}."""
    return _gh(["api", f"repos/{REPO}/commits/{sha}/status"])


def _resolve_pr():
    branch = _current_branch()
    prs = _gh(["pr", "list", "-R", REPO, "--head", branch, "--state", "open", "--json", "number"]) or []
    nums = [p["number"] for p in prs]
    return {"pr": nums[0] if len(nums) == 1 else None, "branch": branch, "candidates": nums}


def _servers_from_statuses(statuses):
    """Map GitHub commit statuses to server rows, dedup by context (latest wins)."""
    servers, seen = [], set()
    for s in statuses:
        ctx = s.get("context", "")
        if not ctx or ctx in seen:
            continue
        seen.add(ctx)
        turl = s.get("target_url") or ""
        m = JENKINS_PR_PATH_RE.search(turl)
        hm = STATUS_URL_HOST_RE.match(turl)
        servers.append({
            "name": _display_name(ctx),
            "context": ctx,
            "state": _STATE_MAP.get(s.get("state", ""), "UNKNOWN"),
            "host": hm.group(1) if hm else "",
            "jenkins_pr": m.group(1) if m else "",
            "build": m.group(2) if m else "",
            "slug": _pipeline_slug(ctx),
            "target_url": turl,
        })
    return servers


def cmd_resolve(_args):
    result = _resolve_pr()
    print(json.dumps(result))
    return 0 if result["pr"] is not None else 2


def cmd_status(args):
    pr = args.pr
    branch_hint = ""
    if pr is None:
        resolved = _resolve_pr()
        if resolved["pr"] is None:
            print(json.dumps({"pr": None, **resolved, "error": "no-unique-branch-pr"}))
            return 2
        pr, branch_hint = resolved["pr"], resolved["branch"]

    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json",
                "number,title,url,headRefName,baseRefName,headRefOid,mergeStateStatus,isDraft"])
    sha = view["headRefOid"]
    merge_state = view.get("mergeStateStatus", "")

    combined = _commit_status(sha)
    statuses = combined.get("statuses", []) if combined else []
    total = combined.get("total_count", 0) if combined else 0
    servers = _servers_from_statuses(statuses)

    build_running = any(s["state"] in ("RUNNING", "UNKNOWN") for s in servers)
    if total == 0 or not servers:
        overall = "NO_CI"
    elif any(s["state"] == "RUNNING" for s in servers):
        overall = "RUNNING"
    elif all(s["state"] == "PASSED" for s in servers):
        overall = "PASSED"
    else:
        overall = "FAILED"

    # Best-effort Jenkins enrichment for FAILED servers: failing stage names (pre-test
    # vs test routing) and failed tests. Degrades to empty if Jenkins is unreachable.
    failed_tests, lint_validate = [], []
    for s in servers:
        if s["state"] != "FAILED" or not (s["host"] and s["build"]):
            continue
        names = _failed_stage_names(s["host"], s["jenkins_pr"], s["build"])
        s["failed_stages"] = names
        for nm in names:
            if any(k in nm.lower() for k in ("lint", "validate")):
                lint_validate.append({"server": s["name"], "stage": nm})
        for t in _failed_tests(s["host"], s["jenkins_pr"], s["build"]):
            failed_tests.append({
                "server": s["name"], "stage": t["phase"], "test": t["test_method"],
                "file": t["test_file"], "suite": t["suite"],
            })

    print(json.dumps({
        "pr": pr,
        "url": view.get("url", ""),
        "title": view.get("title", ""),
        "branch": view.get("headRefName", branch_hint),
        "base_branch": view.get("baseRefName", ""),
        "sha": sha,
        "overall": overall,
        "build_running": build_running,
        "behind": merge_state == "BEHIND",
        "merge_state_status": merge_state,
        "draft": view.get("isDraft", False),
        "servers": servers,
        "failed_tests": failed_tests,
        "lint_validate_failures": lint_validate,
    }))
    return 0


def _pr_commit_shas(pr, limit=20):
    """PR commit oids, newest first (for the trigger history fallback)."""
    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json", "commits"])
    shas = [c.get("oid") for c in (view or {}).get("commits", []) if c.get("oid")]
    return list(reversed(shas))[:limit]


def _statuses_with_fallback(pr, head_sha):
    """Commit statuses for HEAD, falling back to the last commit that had CI."""
    combined = _commit_status(head_sha)
    statuses = combined.get("statuses", []) if combined else []
    source = "head"
    if not statuses:
        for hsha in _pr_commit_shas(pr):
            hc = _commit_status(hsha)
            hs = hc.get("statuses", []) if hc else []
            if hs:
                statuses, source = hs, f"history:{hsha[:12]}"
                break
    return statuses, source


def cmd_jmc(args):
    """Resolve the relevant Israel Jenkins build URL and run jenkins_make_config.

    Importing the latest images is the precondition for running a system/E2E test
    locally. The Israel server is the one that builds + smoke-tests the image.
    """
    pr = args.pr
    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json", "headRefOid"])
    statuses, source = _statuses_with_fallback(pr, view["headRefOid"])
    servers = _servers_from_statuses(statuses)

    israel = [s for s in servers if s["name"].lower().startswith("israel") and s["host"] and s["build"]]
    if args.server:
        israel = [s for s in israel if s["slug"] == args.server] or israel
    # Prefer a PASSED Israel build — its image artifacts are ready (Artifacts & Smoke done).
    chosen = next((s for s in israel if s["state"] == "PASSED"), None) or (israel[0] if israel else None)
    if not chosen:
        print(json.dumps({"ok": False, "reason": "no-israel-server", "catalog_source": source}))
        return 0

    url = _jenkins_base(chosen["host"], chosen["jenkins_pr"], chosen["build"]) + "/"
    script = os.path.join(_repo_root(), "script", "jenkins_make_config.sh")
    command = f"{script} {url}"
    info = {
        "ok": True, "israel_server": chosen["name"], "state": chosen["state"],
        "jenkins_url": url, "jmc_script": script, "command": command, "catalog_source": source,
    }
    if not args.run:
        if chosen["state"] != "PASSED":
            info["warning"] = "chosen Israel build is not PASSED — image artifacts may not be ready yet"
        print(json.dumps(info))
        return 0

    print(json.dumps({"running": command, **{k: info[k] for k in ("israel_server", "jenkins_url")}}))
    sys.stdout.flush()
    proc = subprocess.run([script, url])  # streams; jenkins_make_config_helper needs $prod_root set
    return proc.returncode


def cmd_trigger(args):
    pr = args.pr
    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json", "headRefOid,mergeStateStatus"])
    if view.get("mergeStateStatus") == "BEHIND":
        print(json.dumps({
            "triggered": False, "servers": [],
            "reason": "branch-behind — update the branch before triggering a rebuild",
        }))
        return 0

    # A freshly pushed HEAD has no statuses; fall back to the last commit that had CI to
    # discover the (stable) server contexts. The rebuild comment rebuilds current HEAD.
    statuses, catalog_source = _statuses_with_fallback(pr, view["headRefOid"])

    # Optionally restrict to specific failed servers (by slug); default = all discovered.
    only = set(args.server or [])
    slugs = {}
    for s in statuses:
        ctx = s.get("context", "")
        if not ctx:
            continue
        slug = _pipeline_slug(ctx)
        if only and slug not in only:
            continue
        slugs.setdefault(ctx, (_display_name(ctx), slug))

    # Fallback: when discovery found nothing (e.g. a fresh HEAD after a base-merge pushed the
    # last CI commit out of the lookback window), post directly for the explicitly given
    # slugs. The caller asserts these are valid (e.g. from meta.json.known_server_slugs).
    if not slugs and only:
        for slug in only:
            slugs[slug] = (slug, slug)
        catalog_source = "explicit-slugs"

    if not slugs:
        print(json.dumps({
            "triggered": False, "servers": [], "catalog_source": catalog_source,
            "reason": "no-ci-yet — no Jenkins checks discovered and no --server slugs given; push a commit or pass --server",
        }))
        return 0

    posted = []
    for _ctx, (display, slug) in slugs.items():
        comment = f"pipeline please rebuild failed {slug}"
        if args.dry_run:
            posted.append({"server": display, "comment": comment, "posted": False})
            continue
        _out, rc = _gh(["pr", "comment", str(pr), "-R", REPO, "--body", comment], parse=False, check=False)
        posted.append({"server": display, "comment": comment, "posted": rc == 0})

    print(json.dumps({
        "triggered": not args.dry_run, "servers": posted,
        "catalog_source": catalog_source, "reason": "rebuild-requested",
    }))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="emit normalized situation JSON for a PR")
    p_status.add_argument("--pr", type=int, default=None, help="PR number (auto-detected from branch if omitted)")
    p_status.set_defaults(func=cmd_status)

    p_resolve = sub.add_parser("resolve", help="resolve a unique open PR from the local branch")
    p_resolve.set_defaults(func=cmd_resolve)

    p_trigger = sub.add_parser("trigger", help="post Jenkins rebuild request (gh pr comment) per server")
    p_trigger.add_argument("--pr", type=int, required=True, help="PR number")
    p_trigger.add_argument("--server", action="append", metavar="SLUG",
                           help="restrict rebuild to this server slug (repeatable); default = all")
    p_trigger.add_argument("--dry-run", action="store_true", help="compose comments but do not post")
    p_trigger.set_defaults(func=cmd_trigger)

    p_jmc = sub.add_parser("jmc", help="resolve the Israel Jenkins build URL and run jenkins_make_config (import images)")
    p_jmc.add_argument("--pr", type=int, required=True, help="PR number")
    p_jmc.add_argument("--server", metavar="SLUG", help="prefer this Israel server slug (e.g. israel1)")
    p_jmc.add_argument("--run", action="store_true", help="actually run jenkins_make_config.sh (else just print the resolved URL/command)")
    p_jmc.set_defaults(func=cmd_jmc)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as e:  # noqa: BLE001 - surface a single-line error for the caller
        print(f"pr_watchdog: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
