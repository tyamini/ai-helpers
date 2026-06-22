#!/usr/bin/env python3
"""Collect a per-plan agent result into a verdict.

Combines the completion return code (from pane.log), the cursor-agent output
(pane.log stream-json, for the chat id), and git evidence (HEAD advanced + clean tree)
into a deterministic verdict. The git evidence is authoritative for "done" -
never the agent's self-report.

Reads JSON on stdin: {run_id, slug, baseline_sha, repo_root}
Writes ~/.exec-runs/<run_id>/plans/<slug>/verdict.json and emits it on stdout:
  {status, rc, baseline_sha, head_sha, clean_tree, committed, chat_id, collected_at}
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys


def run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


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

    head = _git(repo, "rev-parse", "HEAD")
    clean = _git(repo, "status", "--porcelain") == ""
    committed = bool(head) and head != baseline and clean
    status = "complete" if (rc == 0 and committed) else "incomplete"

    verdict = {
        "status": status,
        "rc": rc,
        "baseline_sha": baseline,
        "head_sha": head,
        "clean_tree": clean,
        "committed": committed,
        "chat_id": chat_id,
        "collected_at": _now(),
    }
    with open(os.path.join(plan_dir, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump(verdict, f, indent=2)
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
