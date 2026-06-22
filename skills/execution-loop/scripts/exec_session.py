#!/usr/bin/env python3
"""Resolve or create the tmux session for an execution-loop run.

Reuses the caller's current session when run inside tmux ($TMUX set);
otherwise creates a dedicated detached session `exec-loop-<run_id>`.
Writes the chosen session name to ~/.exec-runs/<run_id>/tmux_session and
emits JSON on stdout: {"session": <name>, "origin": "reused"|"created"}.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess


def run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()

    rd = run_dir(a.run_id)
    os.makedirs(rd, exist_ok=True)

    if _tmux("-V").returncode != 0:
        print(json.dumps({"error": "tmux-not-available"}))
        return 1

    if os.environ.get("TMUX"):
        r = _tmux("display-message", "-p", "#{session_name}")
        session = r.stdout.strip()
        origin = "reused"
        if not session:
            print(json.dumps({"error": "current-session-unresolved"}))
            return 1
    else:
        session = f"exec-loop-{a.run_id}"
        if _tmux("has-session", "-t", session).returncode != 0:
            c = _tmux("new-session", "-d", "-s", session)
            if c.returncode != 0:
                print(json.dumps({"error": "create-session-failed",
                                  "detail": c.stderr.strip()}))
                return 1
        origin = "created"

    with open(os.path.join(rd, "tmux_session"), "w", encoding="utf-8") as f:
        f.write(session + "\n")
    print(json.dumps({"session": session, "origin": origin}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
