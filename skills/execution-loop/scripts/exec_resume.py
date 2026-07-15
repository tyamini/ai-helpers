#!/usr/bin/env python3
"""Inject a directive into a running/finished per-plan agent by resuming its chat.

Resuming (not a fresh launch) preserves the agent's context. The resume stream is
wrapped EXACTLY like a dispatch — backgrounded with its PID rewritten to
`agent.pid`, output appended to the plan's `pane.log`, terminated by the
`__EXEC_DONE__ rc=$?` sentinel — so the same `scripts/watch.sh` re-wakes the
executor and `scripts/exec_collect.py` reads the real loop_report from `pane.log`.
A resume that writes to a side log the watcher isn't reading is how a
finished-but-hung agent used to strand the executor; this script prevents that.

The `--resume` mechanics are agent-specific (`cursor` vs `claude`); this script
owns them so the executor never hand-rolls the CLI.

Reads JSON on stdin:
  {run_id, slug, directive, agent?, model?, repo_root?, chat_id?}
`agent`/`chat_id` default to what dispatch/collect recorded for this plan.
Emits JSON on stdout: {pane, log_path, pid_path, chat_id, agent, started_at}
  (or {error, detail}). Never blocks — start watch.sh after it, as for dispatch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exec_dispatch import (  # noqa: E402
    AGENT_BIN, agent_invocation, resolve_agent, run_dir, shq, _now)


def _read(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _chat_id_from_log(pane_log: str):
    """First session_id seen in the stream (system/init emits it first)."""
    if not os.path.exists(pane_log):
        return None
    for line in open(pane_log, errors="replace"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            for k in ("session_id", "sessionId", "chat_id", "chatId"):
                if o.get(k):
                    return o[k]
    return None


def _pane_alive(pane: str) -> bool:
    if not pane:
        return False
    r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                       capture_output=True, text=True)
    return pane in r.stdout.split()


def main() -> int:
    d = json.load(sys.stdin)
    run_id = d["run_id"]
    slug = d["slug"]
    directive = d["directive"]
    repo_root = d.get("repo_root") or os.getcwd()
    model = d.get("model")

    plan_dir = os.path.join(run_dir(run_id), "plans", slug)
    if not os.path.isdir(plan_dir):
        print(json.dumps({"error": "no-plan-dir", "detail": plan_dir}))
        return 1
    pane_log = os.path.join(plan_dir, "pane.log")
    pid_path = os.path.join(plan_dir, "agent.pid")
    agent_err = os.path.join(plan_dir, "agent.err")

    # agent: explicit, else what dispatch recorded (the `agent` file), else
    # env/cursor.
    agent = resolve_agent(d.get("agent") or _read(os.path.join(plan_dir, "agent")))
    if agent not in AGENT_BIN:
        print(json.dumps({"error": "unknown-agent", "detail": agent}))
        return 1

    chat_id = d.get("chat_id") or _chat_id_from_log(pane_log)
    if not chat_id:
        print(json.dumps({"error": "no-chat-id",
                          "detail": "cannot resume without a session id"}))
        return 1

    session = _read(os.path.join(run_dir(run_id), "tmux_session"))
    pane = _read(os.path.join(plan_dir, "pane"))
    # collect only reaps a pane when the plan is green; a not-green plan (the only
    # case that gets a directive) keeps its pane. If it's gone, split a fresh one.
    if not _pane_alive(pane):
        if not session:
            print(json.dumps({"error": "no-tmux-session"}))
            return 1
        r = subprocess.run(
            ["tmux", "split-window", "-t", session, "-P", "-F", "#{pane_id}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(json.dumps({"error": "split-window-failed",
                              "detail": r.stderr.strip()}))
            return 1
        pane = r.stdout.strip()
        with open(os.path.join(plan_dir, "pane"), "w", encoding="utf-8") as f:
            f.write(pane)

    invocation = agent_invocation(
        agent, repo_root, model, agent_err, shq(directive), resume_sid=chat_id)
    cmd = (
        f"cd {shq(repo_root)} && "
        f"( {invocation} & "
        f'ap=$! ; echo $ap > {shq(pid_path)} ; wait $ap ; '
        f'echo "__EXEC_DONE__ rc=$?" ) | tee -a {shq(pane_log)}'
    )
    time.sleep(1.0)
    subprocess.run(["tmux", "send-keys", "-t", pane, cmd, "C-m"], check=False)

    print(json.dumps({
        "pane": pane,
        "log_path": pane_log,
        "pid_path": pid_path,
        "chat_id": chat_id,
        "agent": agent,
        "started_at": _now(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
