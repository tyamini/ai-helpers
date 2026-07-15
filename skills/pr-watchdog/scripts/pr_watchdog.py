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
  watch    Block-poll a PR until CI transitions (PASSED / FAILED, or an early base CONFLICT)
           or max-runtime, then exit — so a backgrounded run's completion notification wakes
           the agent even after its turn has ended. A branch merely BEHIND base is benign and
           never wakes the watcher. Liveness is independent of any agent turn.
  trigger  Post a Jenkins rebuild request PR comment (via gh). With --full: post the single
           global "pipeline please rebuild" (no slug, no "failed") for a fresh HEAD (new
           commit, base-merge, PR-prefix change); always posts. Without --full: rebuild the
           failed servers ("pipeline please rebuild failed <slug>" per discovered server) —
           but ONLY for a HEAD that was actually built. A HEAD with no CI statuses of its own
           (a freshly pushed fix/merge) is auto-promoted to a clean full rebuild, so anything
           pushed always rebuilds clean.

Exit codes:
  0  success (JSON on stdout) — for `watch`, a WATCH_TRANSITION was reached
  1  hard error (message on stderr) — e.g. gh missing/unauthenticated, PR not found;
     for `watch`, too many consecutive poll errors (WATCH_FATAL)
  2  status/resolve: no unique PR could be resolved (JSON with pr=null on stdout)
  10 watch: max-runtime cap reached before any transition (WATCH_MAXRUNTIME)

Requires: an authenticated `gh` CLI (`gh auth status`).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
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


def _all_test_cases(host, jenkins_pr, build):
    """Every test case from the classic testReport (phase/suite from block names).

    Returns one row per case with its status — the full executed-test catalog for a
    build, used both by `_failed_tests` (filter to failures) and by the `tests-ran`
    subcommand (Feature 2: verify PR-added tests actually ran).
    """
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
            tm, tf = case.get("name", ""), case.get("className", "")
            key = f"{phase}|{tf}.{tm}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"test_method": tm, "test_file": tf, "suite": suite_name,
                        "phase": phase, "status": case.get("status", "")})
    return out


def _failed_tests(host, jenkins_pr, build):
    """Failed test cases from the classic testReport (phase/suite from block names)."""
    out, seen = [], set()
    for c in _all_test_cases(host, jenkins_pr, build):
        if c["status"] not in ("FAILED", "REGRESSION"):
            continue
        key = f"{c['test_file']}.{c['test_method']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({k: c[k] for k in ("test_method", "test_file", "suite", "phase")})
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


def _pr_head_sha(pr):
    """HEAD commit oid for a PR (gh pr view headRefOid is not on all gh builds)."""
    data = _gh(["api", f"repos/{REPO}/pulls/{pr}"])
    return data.get("head", {}).get("sha", "")


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


def _situation(pr, branch_hint=""):
    """Compute one normalized situation dict for a PR (shared by status + watch)."""
    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json",
                "number,title,url,headRefName,baseRefName,mergeStateStatus,isDraft"])
    sha = _pr_head_sha(pr)
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

    return {
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
    }


def cmd_status(args):
    pr = args.pr
    branch_hint = ""
    if pr is None:
        resolved = _resolve_pr()
        if resolved["pr"] is None:
            print(json.dumps({"pr": None, **resolved, "error": "no-unique-branch-pr"}))
            return 2
        pr, branch_hint = resolved["pr"], resolved["branch"]

    print(json.dumps(_situation(pr, branch_hint)))
    return 0


def _watch_actionable(sit):
    """Return a transition reason when the situation is terminal/actionable, else None.

    The watcher blocks while a build is RUNNING and wakes the agent only on a change worth
    acting on — so silence can never be mistaken for green.

    A branch that is merely BEHIND base is benign: it must NOT wake the agent or interrupt
    an in-progress build. Chasing a moving base would abort a running build for no reason.
    Base reconciliation is handled later, only when a new CI is about to be triggered anyway
    (the loop's PASSED / idle / trigger paths) — never as a standalone reaction to `behind`.

    The lone exception is a base CONFLICT (mergeStateStatus == DIRTY) caught EARLY — while the
    build has only just begun and nothing has passed yet. A conflicted PR can never merge even
    if it goes green, so it is worth resolving the conflict and restarting CI now instead of
    wasting a full run. A conflict discovered once the build is well underway (something has
    already passed) is left for the normal PASSED/FAILED transition to handle.
    """
    overall = sit.get("overall")
    if overall == "PASSED":
        return "passed"
    if overall == "FAILED" and not sit.get("build_running"):
        return "failed"
    servers = sit.get("servers", [])
    build_just_started = (sit.get("build_running") and servers
                          and not any(s.get("state") == "PASSED" for s in servers))
    if sit.get("merge_state_status") == "DIRTY" and build_just_started:
        return "conflict-early"
    return None


def cmd_watch(args):
    """Block-poll a PR until its CI transitions, then exit so a backgrounded run's
    completion notification wakes the agent. Liveness is independent of any agent turn.

    Stdout is a stream of one-line markers (the background terminal file is the audit):
      WATCH_POLL <iso> overall=.. running=.. behind=..   (one per poll, flushed)
      WATCH_TRANSITION <reason> <situation-json>          (exit 0 — agent should act)
      WATCH_MAXRUNTIME <situation-json>                   (exit 10 — cap hit)
      WATCH_ERROR <msg>                                   (transient; keeps looping)
      WATCH_FATAL <msg>                                   (exit 1 — too many errors)
    """
    pr = args.pr
    deadline = time.monotonic() + args.max_runtime
    consecutive_errors = 0
    last_sit = None
    while True:
        try:
            sit = _situation(pr)
            last_sit = sit
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001 - a transient gh/Jenkins blip must not kill the watcher
            consecutive_errors += 1
            print(f"WATCH_ERROR {e}", flush=True)
            if consecutive_errors >= args.error_tolerance:
                print(f"WATCH_FATAL {consecutive_errors} consecutive poll errors; last: {e}", flush=True)
                return 1
            time.sleep(min(args.interval, 60))
            continue

        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"WATCH_POLL {now} overall={sit['overall']} "
              f"running={sit['build_running']} behind={sit['behind']}", flush=True)

        reason = _watch_actionable(sit)
        if reason is not None:
            print(f"WATCH_TRANSITION {reason} {json.dumps(sit)}", flush=True)
            return 0

        if time.monotonic() >= deadline:
            print(f"WATCH_MAXRUNTIME {json.dumps(last_sit or sit)}", flush=True)
            return 10

        time.sleep(args.interval)


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
    sha = _pr_head_sha(pr)
    statuses, source = _statuses_with_fallback(pr, sha)
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


def _post_full_rebuild(pr, dry_run, reason, extra=None):
    """Post the single global 'pipeline please rebuild' (clean full rebuild)."""
    comment = "pipeline please rebuild"
    payload = {"triggered": False, "full": True, "comment": comment, "reason": reason}
    if extra:
        payload.update(extra)
    if dry_run:
        print(json.dumps(payload))
        return 0
    _out, rc = _gh(["pr", "comment", str(pr), "-R", REPO, "--body", comment], parse=False, check=False)
    payload["triggered"] = rc == 0
    print(json.dumps(payload))
    return 0


def cmd_trigger(args):
    pr = args.pr

    # Full rebuild: a fresh HEAD (new commit, base-merge, or PR-prefix change) needs a
    # complete rebuild of every pipeline. Post the single global comment with no server
    # slug and no "failed" qualifier, and never bail out — a brand-new HEAD has no statuses
    # to discover, so this must always post (no host, no fail).
    if args.full:
        return _post_full_rebuild(pr, args.dry_run, "full-rebuild-requested")

    view = _gh(["pr", "view", str(pr), "-R", REPO, "--json", "mergeStateStatus"])
    if view.get("mergeStateStatus") == "BEHIND":
        print(json.dumps({
            "triggered": False, "servers": [],
            "reason": "branch-behind — update the branch before triggering a rebuild",
        }))
        return 0

    # A freshly pushed HEAD has no statuses; fall back to the last commit that had CI to
    # discover the (stable) server contexts. The rebuild comment rebuilds current HEAD.
    sha = _pr_head_sha(pr)
    statuses, catalog_source = _statuses_with_fallback(pr, sha)

    # SAFETY: any HEAD that has no CI statuses of its OWN is a freshly pushed commit — a fix
    # commit or a base-merge — so `catalog_source` fell back to a historical commit. Such a
    # HEAD MUST get a complete rebuild of every pipeline; a per-server "rebuild failed <slug>"
    # would rerun only the previously-failed stages and can leave stages unvalidated against
    # the new code (risking a false green). Auto-promote to a clean full rebuild so that a
    # caller which pushed something but forgot --full still rebuilds clean. Per-server retries
    # remain available only for a HEAD that was actually built (catalog_source == "head").
    if catalog_source != "head":
        return _post_full_rebuild(
            pr, args.dry_run, "fresh-head-auto-full",
            {"auto_promoted_full": True, "catalog_source": catalog_source, "head_sha": sha},
        )

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


# --- Feature 1: request @codex / @copilot review on functional commits ------
# The review trigger the bots recognise, plus an invisible per-SHA marker so the
# request is idempotent per commit (re-request on a new SHA, skip if already asked
# for THIS SHA) without needing any external state.
_REVIEW_TRIGGERS = {"codex": "@codex review", "copilot": "@copilot review"}


def _review_marker(sha, bot):
    return f"<!-- pr-watchdog:review sha={sha[:12]} bot={bot} -->"


def _issue_comments(pr):
    """All issue comments on a PR as {created_at, body} dicts (paginated)."""
    out, _rc = _gh(["api", "--paginate", f"repos/{REPO}/issues/{pr}/comments",
                    "--jq", ".[] | {created_at, body}"], parse=False, check=False)
    rows = []
    for line in (out or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _commit_parent_count(sha):
    """Number of parents of a commit (>=2 == a merge commit)."""
    data = _gh(["api", f"repos/{REPO}/commits/{sha}", "--jq", ".parents | length"],
               parse=False, check=False)[0]
    try:
        return int(data)
    except (TypeError, ValueError):
        return 0


def _commit_date(sha):
    """Committer date (ISO-8601 UTC) of a commit — the 'this SHA exists since' timestamp."""
    return _gh(["api", f"repos/{REPO}/commits/{sha}",
                "--jq", ".commit.committer.date"], parse=False, check=False)[0].strip()


def cmd_review_request(args):
    """Post '@codex review' / '@copilot review' for the current HEAD unless review was
    already requested for THIS SHA. A bot counts as already-requested when EITHER our own
    exact-SHA marker is present OR any '@<bot> review' comment (ours or a human's) was
    created at/after the HEAD commit's date — so we never duplicate a request that already
    covers the current code, yet still re-request on a genuinely newer commit.

    --skip-merge-commit skips a merge (>=2 parents) HEAD, so a caller that cannot tell
    a base-merge/reconcile commit from a functional one (e.g. observing a user-pushed
    HEAD) never asks for review on a pure reconcile. The watchdog omits the flag after
    its OWN pushes that it knows carry a functional fix (even when the batch also merged
    base, making HEAD a merge commit).
    """
    pr = args.pr
    sha = args.sha or _pr_head_sha(pr)
    bots = [b.strip() for b in (args.bots or "codex,copilot").split(",")
            if b.strip() and b.strip() in _REVIEW_TRIGGERS]

    if args.skip_merge_commit and _commit_parent_count(sha) >= 2:
        print(json.dumps({"sha": sha, "posted": [], "already": [], "skipped": "merge-commit"}))
        return 0

    head_date = _commit_date(sha)
    comments = _issue_comments(pr)
    all_markers = "\n".join((c.get("body") or "") for c in comments)

    already = []
    for bot in bots:
        trig = _REVIEW_TRIGGERS[bot]
        if _review_marker(sha, bot) in all_markers:
            already.append(bot)
            continue
        # A pre-existing trigger (manual or ours) counts only if it post-dates this commit,
        # i.e. it reviewed THIS code — an older one reviewed a superseded HEAD.
        for c in comments:
            created = c.get("created_at", "")
            if head_date and created and created < head_date:
                continue
            if trig.lower() in (c.get("body") or "").lower():
                already.append(bot)
                break

    todo = [b for b in bots if b not in already]
    posted = []
    for bot in todo:
        body = f"{_REVIEW_TRIGGERS[bot]}\n{_review_marker(sha, bot)}"
        if args.dry_run:
            posted.append(bot)
            continue
        _out, rc = _gh(["pr", "comment", str(pr), "-R", REPO, "--body", body], parse=False, check=False)
        if rc == 0:
            posted.append(bot)
    print(json.dumps({"sha": sha, "posted": posted, "already": already, "skipped": None}))
    return 0


# --- Feature 2: catalog of tests actually executed in CI ---------------------
def cmd_tests_ran(args):
    """Emit the full catalog of test cases that CI actually executed, per server.

    Feature 2's deterministic data source: a subagent cross-references this against
    the tests ADDED in the PR diff to prove each added test really ran (not silently
    filtered out by PR-label/stage selection, a skip marker, or suite registration).
    """
    pr = args.pr
    sha = _pr_head_sha(pr)
    statuses, source = _statuses_with_fallback(pr, sha)
    servers_out = []
    for s in _servers_from_statuses(statuses):
        if not (s["host"] and s["build"]):
            continue
        cases = _all_test_cases(s["host"], s["jenkins_pr"], s["build"])
        servers_out.append({
            "name": s["name"], "slug": s["slug"], "state": s["state"],
            "build": s["build"], "target_url": s["target_url"],
            "test_count": len(cases), "tests": cases,
        })
    print(json.dumps({
        "pr": pr, "head_sha": sha, "catalog_source": source, "servers": servers_out,
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

    p_watch = sub.add_parser(
        "watch",
        help="block-poll a PR until CI transitions (PASSED/FAILED, or an early base conflict) "
             "or max-runtime, then exit (a branch merely BEHIND base never wakes the watcher)",
    )
    p_watch.add_argument("--pr", type=int, required=True, help="PR number")
    p_watch.add_argument("--interval", type=int, default=600,
                         help="seconds between polls while the build is running (default 600)")
    p_watch.add_argument("--max-runtime", type=int, default=86400, dest="max_runtime",
                         help="wall-clock cap in seconds before exiting 10 (default 86400 = 24h)")
    p_watch.add_argument("--error-tolerance", type=int, default=5, dest="error_tolerance",
                         help="consecutive poll errors tolerated before exiting 1 (default 5)")
    p_watch.set_defaults(func=cmd_watch)

    p_trigger = sub.add_parser("trigger", help="post Jenkins rebuild request (gh pr comment) per server")
    p_trigger.add_argument("--pr", type=int, required=True, help="PR number")
    p_trigger.add_argument("--full", action="store_true",
                           help="post a single global 'pipeline please rebuild' (no slug, no 'failed') "
                                "for a fresh HEAD (new commit, base-merge, PR-prefix change); always posts")
    p_trigger.add_argument("--server", action="append", metavar="SLUG",
                           help="restrict rebuild to this server slug (repeatable); default = all")
    p_trigger.add_argument("--dry-run", action="store_true", help="compose comments but do not post")
    p_trigger.set_defaults(func=cmd_trigger)

    p_review = sub.add_parser(
        "review-request",
        help="post '@codex review'/'@copilot review' for HEAD if not already requested for this SHA")
    p_review.add_argument("--pr", type=int, required=True, help="PR number")
    p_review.add_argument("--sha", default=None, help="commit to request review on (default: PR HEAD)")
    p_review.add_argument("--bots", default="codex,copilot",
                          help="comma-separated bots to request (default: codex,copilot)")
    p_review.add_argument("--skip-merge-commit", action="store_true", dest="skip_merge_commit",
                          help="skip when HEAD is a merge commit (a base-merge/reconcile, not a functional commit)")
    p_review.add_argument("--dry-run", action="store_true", help="compose comments but do not post")
    p_review.set_defaults(func=cmd_review_request)

    p_tests = sub.add_parser(
        "tests-ran", help="emit the full catalog of test cases CI actually executed (per server)")
    p_tests.add_argument("--pr", type=int, required=True, help="PR number")
    p_tests.set_defaults(func=cmd_tests_ran)

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
