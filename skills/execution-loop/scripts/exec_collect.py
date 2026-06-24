#!/usr/bin/env python3
"""Collect a per-plan agent result into a verdict.

Combines the completion return code (from pane.log), the cursor-agent output
(pane.log stream-json, for the chat id), and git evidence (HEAD advanced + clean tree)
into a deterministic verdict. The git evidence is authoritative for "done" -
never the agent's self-report.

Also pulls the per-plan agent's implementation-loop verdict (exit_reason,
verification) from the terminal `result` event so the executor can refuse to
advance on a "committed but red" plan. Git evidence still owns "committed";
the agent self-report only gates `green`.

Reads JSON on stdin: {run_id, slug, baseline_sha, repo_root}
Writes ~/.exec-runs/<run_id>/plans/<slug>/verdict.json and emits it on stdout:
  {status, rc, baseline_sha, head_sha, clean_tree, committed, exit_reason,
   verification, loop_report_found, green, chat_id, collected_at}

Telemetry (deterministic, fail-open): after the verdict is written, invokes
`run_ledger.py ingest-pane` to turn this plan's finished pane.log + verdict.json
into the per-plan agent's run-ledger node (model, timing, tools, depth-3
subagents). Plan/run milestones flow through cli-escalation-notify, not here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


def run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _ledger() -> str:
    """Path to the run-ledger client (relative to this skill, then HOME)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.normpath(os.path.join(
        here, "..", "..", "..", "tools", "run-ledger", "client", "run_ledger.py"))
    if os.path.exists(cand):
        return cand
    return os.path.expanduser(
        "~/.drivenets/cheetah/AI/v2/private/tools/run-ledger/client/run_ledger.py")


def _ingest_pane(run_id: str, slug: str, repo: str) -> None:
    """Emit the per-plan agent node + plan_finish from pane.log. Fail-open."""
    try:
        subprocess.run(
            [sys.executable, _ledger(), "ingest-pane",
             "--run-id", run_id, "--slug", slug, "--repo", repo],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
    except Exception:
        pass


def _extract_chat_id(path: str):
    """Scan the pane log (stream-json events + sentinel) for a session id.

    Every stream-json line carries `session_id`; the first match is enough.
    """
    if not os.path.exists(path):
        return None
    try:
        for line in open(path, errors="replace"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                for k in ("session_id", "sessionId", "chat_id", "chatId",
                          "conversation_id", "conversationId"):
                    if o.get(k):
                        return o[k]
    except OSError:
        return None
    return None


def _extract_loop_report(path: str):
    """Pull the implementation-loop verdict from the terminal `result` event.

    Returns {exit_reason, verification, found}. Self-report only — used to gate
    "committed but red"; git evidence still owns "committed". A missing/unparsed
    report yields found=False, which the caller treats as not-green (fail-safe).
    """
    miss = {"exit_reason": None, "verification": None, "found": False}
    if not os.path.exists(path):
        return miss
    # Accumulate all agent prose (assistant text deltas + result events) so a
    # loop_report emitted mid-stream is caught, not only one in the final event.
    parts = []
    try:
        for line in open(path, errors="replace"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("type") == "result" and o.get("result"):
                parts.append(o["result"])
            elif o.get("type") == "assistant":
                content = (o.get("message") or {}).get("content") or []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                        parts.append(c["text"])
    except OSError:
        return miss

    text = "".join(parts)
    # Isolate the loop_report block (last occurrence) so we never match prose.
    idx = text.rfind("loop_report:")
    if idx == -1:
        return miss
    block = text[idx:]

    def _grab(pat):
        m = re.search(pat, block)
        return m.group(1) if m else None

    return {
        "exit_reason": _grab(r"\bexit_reason:\s*([\w-]+)"),
        "verification": _grab(r"\bresults:\s*([\w-]+)"),
        "found": True,
    }


def main() -> int:
    d = json.load(sys.stdin)
    run_id = d["run_id"]
    slug = d["slug"]
    baseline = d.get("baseline_sha", "")
    repo = d["repo_root"]

    plan_dir = os.path.join(run_dir(run_id), "plans", slug)
    pane_log = os.path.join(plan_dir, "pane.log")

    rc = None
    if os.path.exists(pane_log):
        for line in open(pane_log, errors="replace"):
            m = re.search(r"__EXEC_DONE__ rc=(\d+)", line)
            if m:
                rc = int(m.group(1))  # keep the last sentinel

    chat_id = _extract_chat_id(pane_log)
    lr = _extract_loop_report(pane_log)

    head = _git(repo, "rev-parse", "HEAD")
    clean = _git(repo, "status", "--porcelain") == ""
    committed = bool(head) and head != baseline and clean
    # green = committed AND the agent's own loop_report is met-criteria + tests pass.
    # A missing/unparsed report is not green (fail-safe → executor inspects).
    green = (
        committed
        and lr["exit_reason"] == "met-criteria"
        and lr["verification"] == "pass"
    )
    status = "complete" if (rc == 0 and green) else "incomplete"

    verdict = {
        "status": status,
        "rc": rc,
        "baseline_sha": baseline,
        "head_sha": head,
        "clean_tree": clean,
        "committed": committed,
        "exit_reason": lr["exit_reason"],
        "verification": lr["verification"],
        "loop_report_found": lr["found"],
        "green": green,
        "chat_id": chat_id,
        "collected_at": _now(),
    }
    with open(os.path.join(plan_dir, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump(verdict, f, indent=2)

    # Telemetry: parse this finished plan's pane.log into the agent's node and a
    # plan_finish milestone. Deterministic, fail-open — never affects the verdict.
    _ingest_pane(run_id, slug, repo)

    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
