#!/usr/bin/env python3
"""Launch ONE per-plan agent as a top-level agent CLI in a new tmux pane.

The agent CLI is selectable — `cursor` (`cursor-agent`) or `claude`
(`claude`). Launching it as a top-level process (not a Task subagent) is what
lets it dispatch its own subagents at all.

Reads JSON on stdin:
  {run_id, slug, plan_path, branch, repo_root, agent?, model?, prompt_path?}
`agent` is "cursor" or "claude"; if omitted it falls back to the
$EXEC_LOOP_AGENT env var, then to the CLI running this run (same agent as the
main agent, auto-detected from the process tree), then "cursor".
Writes/uses ~/.exec-runs/<run_id>/plans/<slug>/{prompt.txt,agent.err,pane.log},
splits a new pane in the run's tmux session, sends the sentinel-wrapped
cursor-agent command (stream-json piped to the pane), and emits JSON:
  {pane, plan_dir, log_path, started_at}
Never blocks: a watcher (watch.sh) detects the completion sentinel.

Telemetry needs nothing here: the per-plan agent's node is emitted later by
exec_collect.py (via `run_ledger.py ingest-pane`) from the finished pane.log,
and plan milestones flow through the executor's cli-escalation-notify calls.
No hooks, no registration.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone


def run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shq(s) -> str:
    return shlex.quote(str(s))


AGENT_BIN = {"cursor": "cursor-agent", "claude": "claude"}


def resolve_agent(value) -> str:
    """Pick the agent CLI: explicit value, else $EXEC_LOOP_AGENT, else the CLI
    running this run (same agent as the main agent), else cursor."""
    return (value or os.environ.get("EXEC_LOOP_AGENT")
            or detect_main_agent() or "cursor").strip()


def _proc_argv(pid: int):
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _proc_ppid(pid: int) -> int:
    # /proc/<pid>/stat: "<pid> (<comm>) <state> <ppid> ..."; comm may hold spaces
    # and parens, so parse the fields after the final ')'.
    try:
        data = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        return int(data[data.rfind(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def detect_main_agent():
    """Identify the agent CLI running this run by walking the process tree.

    The per-plan agent should match whichever CLI the main agent runs (the one
    that triggered this skill). exec_dispatch.py is launched as a child of that
    CLI, so an `claude`/`cursor-agent` ancestor identifies it. Returns "claude"
    or "cursor", or None if neither is found (caller falls back to cursor).
    """
    pid = os.getpid()
    for _ in range(16):  # bounded walk up the process tree
        if pid <= 1:
            break
        argv = _proc_argv(pid)
        if argv:
            base = os.path.basename(argv[0])
            exe = (os.path.realpath(f"/proc/{pid}/exe")
                   if os.path.exists(f"/proc/{pid}/exe") else "")
            if base == "claude" or "/claude/" in exe:
                return "claude"
            if base == "cursor-agent" or "/cursor-agent" in exe:
                return "cursor"
        pid = _proc_ppid(pid)
    return None


def executor_skips_permissions() -> bool:
    """True if an ancestor `claude` process runs with --dangerously-skip-permissions.

    A per-plan `claude` child inherits the executor's bypass mode: if the main
    Claude agent was launched with --dangerously-skip-permissions, its spawned
    children get it too (so a headless child never stalls on a permission prompt);
    if the executor did not skip permissions, neither do its children. An explicit
    $EXEC_LOOP_SKIP_PERMISSIONS (1/0/true/false) overrides the auto-detection.

    Detection matches the flag only as a real argv element of an actual `claude`
    process, so the string appearing as data in some other command line (e.g. this
    script's own args) never false-positives.
    """
    ov = os.environ.get("EXEC_LOOP_SKIP_PERMISSIONS")
    if ov is not None:
        return ov.strip().lower() in ("1", "true", "yes", "on")
    pid = os.getpid()
    for _ in range(16):  # bounded walk up the process tree
        if pid <= 1:
            break
        argv = _proc_argv(pid)
        is_claude = bool(argv) and (
            os.path.basename(argv[0]) == "claude"
            or "/claude/" in (os.path.realpath(f"/proc/{pid}/exe")
                              if os.path.exists(f"/proc/{pid}/exe") else ""))
        if is_claude and "--dangerously-skip-permissions" in argv:
            return True
        pid = _proc_ppid(pid)
    return False


def agent_invocation(agent, repo_root, model, agent_err, prompt_expr,
                     resume_sid=None) -> str:
    """The bare agent CLI call (the caller backgrounds it): stdout to the pane,
    stderr split off to `agent_err`.

    `prompt_expr` is a shell expression that yields the prompt (already quoted) —
    e.g. `"$(cat prompt.txt)"` for a fresh dispatch, or a quoted directive for a
    resume. `resume_sid`, when given, continues that agent's existing chat instead
    of starting a new one. Both CLIs emit stream-json (one JSON object per line,
    each carrying `session_id`, ending in a `type:"result"` event) so the watcher
    and collector are agent-agnostic.
    """
    resume = f"--resume {shq(resume_sid)} " if resume_sid else ""
    model_flag = f"--model {shq(model)} " if model and model != "auto" else ""
    if agent == "claude":
        # cwd is repo_root (the caller cd's there), so no explicit workspace flag.
        # Inherit the executor's permission mode: only skip permissions if the
        # main Claude agent itself was launched with --dangerously-skip-permissions.
        skip = ("--dangerously-skip-permissions "
                if executor_skips_permissions() else "")
        return (
            f"claude -p --output-format stream-json --verbose "
            f"{skip}{resume}{model_flag}"
            f"{prompt_expr} 2> {shq(agent_err)}"
        )
    # default: cursor-agent
    return (
        f"cursor-agent -p --force --trust "
        f"--output-format stream-json --stream-partial-output "
        f"{resume}{model_flag}--workspace {shq(repo_root)} "
        f"{prompt_expr} 2> {shq(agent_err)}"
    )


def main() -> int:
    data = json.load(sys.stdin)
    run_id = data["run_id"]
    slug = data["slug"]
    repo_root = data["repo_root"]
    model = data.get("model")
    agent = resolve_agent(data.get("agent"))
    if agent not in AGENT_BIN:
        print(json.dumps({"error": "unknown-agent", "detail": agent}))
        return 1
    if shutil.which(AGENT_BIN[agent]) is None:
        print(json.dumps({"error": "agent-not-available",
                          "detail": AGENT_BIN[agent]}))
        return 1

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

    agent_err = os.path.join(plan_dir, "agent.err")
    pane_log = os.path.join(plan_dir, "pane.log")
    pid_path = os.path.join(plan_dir, "agent.pid")
    open(pane_log, "a").close()  # so the watcher has a target file immediately

    # Open a real PANE (split) in the session's active window so the agent is
    # watchable next to the executor — not a separate window/tab.
    r = subprocess.run(
        ["tmux", "split-window", "-t", session, "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(json.dumps({"error": "split-window-failed", "detail": r.stderr.strip()}))
        return 1
    pane = r.stdout.strip()
    with open(os.path.join(plan_dir, "pane"), "w", encoding="utf-8") as f:
        f.write(pane)  # so exec_collect.py can reap the pane after the plan
    with open(os.path.join(plan_dir, "agent"), "w", encoding="utf-8") as f:
        f.write(agent)  # so exec_collect.py / exec_resume.py know the CLI to use

    # stream-json keeps the run visible LIVE in the pane (assistant deltas, tool
    # calls, final result) while remaining parseable — and it carries session_id
    # for `--resume`. stdout flows through `tee` so it is shown in the pane AND
    # saved to pane.log; only stderr is split off to agent.err. No telemetry env
    # is needed: the agent's node is parsed from pane.log later.
    #
    # The agent CLI is backgrounded inside the subshell so its real PID is written
    # to agent.pid (the watcher uses it for liveness; exec_collect.py reaps it).
    # `wait` yields the true exit code for the sentinel.
    invocation = agent_invocation(
        agent, repo_root, model, agent_err, f'"$(cat {shq(prompt_path)})"')
    cmd = (
        f"cd {shq(repo_root)} && "
        f"( {invocation} & "
        f'ap=$! ; echo $ap > {shq(pid_path)} ; wait $ap ; '
        f'echo "__EXEC_DONE__ rc=$?" ) | tee -a {shq(pane_log)}'
    )

    time.sleep(1.0)  # let the new pane's shell finish its rc startup before send-keys
    subprocess.run(["tmux", "send-keys", "-t", pane, cmd, "C-m"], check=False)

    print(json.dumps({
        "pane": pane,
        "plan_dir": plan_dir,
        "log_path": pane_log,
        "pid_path": pid_path,
        "agent": agent,
        "started_at": _now(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
