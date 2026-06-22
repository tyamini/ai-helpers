#!/usr/bin/env python3
"""Launch ONE per-plan agent as a top-level `cursor-agent` in a new tmux window.

Because the per-plan agent is a fresh top-level process (not a Task subagent),
it is free to dispatch its own implementer/reviewer Task subagents -> the run
gains real depth-3 (executor -> per-plan agent -> implementer/reviewer).

Reads JSON on stdin:
  {run_id, slug, plan_path, branch, repo_root, model?, prompt_path?, parent?}
Writes/uses ~/.exec-runs/<run_id>/plans/<slug>/{prompt.txt,agent.json,agent.err,pane.log},
opens a new window in the run's tmux session, sends the sentinel-wrapped
cursor-agent command, and emits JSON:
  {pane, plan_dir, log_path, result_path, started_at}
Never blocks: a watcher (watch.sh) detects the completion sentinel.
"""
from __future__ import annotations

import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import time


def run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def shq(s) -> str:
    return shlex.quote(str(s))


def main() -> int:
    data = json.load(sys.stdin)
    run_id = data["run_id"]
    slug = data["slug"]
    repo_root = data["repo_root"]
    model = data.get("model")
    parent = data.get("parent", "")

    rd = run_dir(run_id)
    try:
        session = open(os.path.join(rd, "tmux_session"), encoding="utf-8").read().strip()
    except OSError:
        print(json.dumps({"error": "no-tmux-session", "detail": "run exec_session.py first"}))
        return 1

    plan_dir = os.path.join(rd, "plans", slug)
    os.makedirs(plan_dir, exist_ok=True)
    prompt_path = os.path.join(plan_dir, "prompt.txt")
    src = data.get("prompt_path")
    if src and os.path.abspath(src) != os.path.abspath(prompt_path):
        shutil.copyfile(src, prompt_path)
    if not os.path.exists(prompt_path):
        print(json.dumps({"error": "prompt-missing", "detail": prompt_path}))
        return 1

    agent_json = os.path.join(plan_dir, "agent.json")
    agent_err = os.path.join(plan_dir, "agent.err")
    pane_log = os.path.join(plan_dir, "pane.log")
    open(pane_log, "a").close()  # so the watcher's `tail -F` has a target now

    r = subprocess.run(
        ["tmux", "new-window", "-d", "-t", session, "-n", slug[:20],
         "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(json.dumps({"error": "new-window-failed", "detail": r.stderr.strip()}))
        return 1
    pane = r.stdout.strip()

    # Env assignments must live INSIDE the subshell: `VAR=val ( ... )` is a
    # bash syntax error, so we `export` them as the subshell's first statements.
    exports = f"export RUN_LEDGER_RUN_ID={shq(run_id)}; "
    if parent:
        exports += f"export RUN_LEDGER_PARENT={shq(parent)}; "
    model_flag = f"--model {shq(model)} " if model and model != "auto" else ""

    cmd = (
        f"cd {shq(repo_root)} && "
        f"( {exports}"
        f"cursor-agent -p --force --trust --output-format json "
        f"{model_flag}--workspace {shq(repo_root)} "
        f'"$(cat {shq(prompt_path)})" '
        f"> {shq(agent_json)} 2> {shq(agent_err)} ; "
        f'echo "__EXEC_DONE__ rc=$?" ) | tee -a {shq(pane_log)}'
    )

    time.sleep(1.0)  # let the new window's shell finish its rc startup before send-keys
    subprocess.run(["tmux", "send-keys", "-t", pane, cmd, "C-m"], check=False)

    print(json.dumps({
        "pane": pane,
        "plan_dir": plan_dir,
        "log_path": pane_log,
        "result_path": agent_json,
        "started_at": _now(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
