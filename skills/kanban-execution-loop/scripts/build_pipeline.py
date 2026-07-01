#!/usr/bin/env python3
"""Build a linked Kanban task pipeline for kanban-execution-loop.

This is the one mechanical action of the kanban-execution-loop skill: given a
fully composed task list and the link edges (JSON on stdin), it creates every
board task, links them into a chain, optionally starts the first, and emits the
resulting chain (JSON on stdout). It authors nothing — the skill composes the
prompts and the work branch; this script only calls the `kanban` CLI and reports.

Deterministic and idempotent-per-invocation: it runs `kanban task create` /
`kanban task link` / `kanban task start`, parses their JSON, and never eyeballs
free-form output. On the first failing CLI call it stops and reports
`{"ok": false, "error": ...}` (the skill halts on that).

stdin JSON:
  {
    "project_path": "/home/dn/cheetah",   # required; the Kanban project (main repo)
    "work_branch": "<work branch>",       # required; every task's --base-ref
    "agent_id": "claude",                 # optional; default "claude"
    "model": null,                        # accepted for parity; unused by the CLI
    "tasks": [                            # required; in creation order
      {"slug": "phase-1-validate", "title": "...", "prompt": "...",
       "auto_review_mode": "done"},
      {"slug": "plan-01-exec", "title": "...", "prompt": "...",
       "auto_review_mode": "commit"}
    ],
    "links": [["plan-01-exec", "phase-1-validate"]],  # [waiter_slug, prereq_slug]
    "start_slug": "phase-1-validate"      # optional; task to start after wiring
  }

stdout JSON:
  {
    "ok": true,
    "work_branch": "<work branch>",
    "project_path": "...",
    "agent_id": "claude",
    "tasks": [{"slug": ..., "id": ..., "column": ..., "auto_review_mode": ...}],
    "links": [{"waiter": ..., "prereq": ..., "dependency_id": ...}],
    "started": "<slug or null>"
  }
or {"ok": false, "error": "...", ...partial...} on the first failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

VALID_MODES = {"commit", "pr", "done"}


def fail(error: str, **extra) -> "None":
    payload = {"ok": False, "error": error}
    payload.update(extra)
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    sys.exit(1)


def resolve_kanban_cli() -> list[str]:
    """Prefer the CLI Kanban injected for spawned tasks; else the global binary."""
    injected = os.environ.get("KANBAN_CLI", "").strip()
    if injected:
        return [injected]
    if shutil.which("kanban"):
        return ["kanban"]
    # Last resort: npx. Kept explicit so failures are legible.
    if shutil.which("npx"):
        return ["npx", "-y", "kanban"]
    fail("Could not resolve the kanban CLI (KANBAN_CLI unset, `kanban` and `npx` not on PATH).")
    raise AssertionError("unreachable")


def run_kanban(cli: list[str], args: list[str]) -> dict:
    """Run a `kanban ...` subcommand and parse its JSON stdout."""
    cmd = cli + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:  # binary vanished, permission, etc.
        return {"ok": False, "error": f"failed to exec {cmd[0]!r}: {exc}"}
    out = (proc.stdout or "").strip()
    if not out:
        return {
            "ok": False,
            "error": f"kanban produced no JSON (exit {proc.returncode})",
            "stderr": (proc.stderr or "").strip(),
        }
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"kanban output was not JSON (exit {proc.returncode})",
            "raw": out[:2000],
            "stderr": (proc.stderr or "").strip(),
        }
    if isinstance(parsed, dict) and parsed.get("ok") is False and "error" not in parsed:
        parsed["error"] = "kanban reported ok:false"
    return parsed


def main() -> None:
    try:
        spec = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        fail(f"invalid stdin JSON: {exc}")

    project_path = str(spec.get("project_path", "")).strip()
    work_branch = str(spec.get("work_branch", "")).strip()
    agent_id = str(spec.get("agent_id", "claude")).strip() or "claude"
    tasks = spec.get("tasks") or []
    links = spec.get("links") or []
    start_slug = spec.get("start_slug")

    if not project_path:
        fail("project_path is required")
    if not work_branch:
        fail("work_branch is required")
    if not isinstance(tasks, list) or not tasks:
        fail("tasks must be a non-empty list")

    # Validate tasks up front so we never half-build the board.
    seen: set[str] = set()
    for idx, task in enumerate(tasks):
        slug = str(task.get("slug", "")).strip()
        prompt = str(task.get("prompt", "")).strip()
        mode = str(task.get("auto_review_mode", "")).strip()
        if not slug:
            fail(f"tasks[{idx}] is missing a slug")
        if slug in seen:
            fail(f"duplicate task slug: {slug!r}")
        seen.add(slug)
        if not prompt:
            fail(f"task {slug!r} is missing a prompt")
        if mode not in VALID_MODES:
            fail(f"task {slug!r} has invalid auto_review_mode {mode!r} (expected one of {sorted(VALID_MODES)})")

    for idx, edge in enumerate(links):
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            fail(f"links[{idx}] must be a [waiter_slug, prereq_slug] pair")
        waiter, prereq = str(edge[0]), str(edge[1])
        if waiter not in seen:
            fail(f"links[{idx}] references unknown waiter slug {waiter!r}")
        if prereq not in seen:
            fail(f"links[{idx}] references unknown prereq slug {prereq!r}")

    if start_slug is not None and str(start_slug) not in seen:
        fail(f"start_slug {start_slug!r} is not one of the task slugs")

    cli = resolve_kanban_cli()

    created: list[dict] = []
    slug_to_id: dict[str, str] = {}

    # 1. Create every task in backlog.
    for task in tasks:
        slug = str(task["slug"]).strip()
        mode = str(task["auto_review_mode"]).strip()
        args = [
            "task", "create",
            "--prompt", str(task["prompt"]),
            "--project-path", project_path,
            "--base-ref", work_branch,
            "--agent-id", agent_id,
            "--auto-review-enabled", "true",
            "--auto-review-mode", mode,
        ]
        title = str(task.get("title", "")).strip()
        if title:
            args += ["--title", title]

        result = run_kanban(cli, args)
        if not result.get("ok"):
            fail(
                f"failed to create task {slug!r}: {result.get('error', 'unknown error')}",
                stderr=result.get("stderr"),
                tasks=created,
            )
        task_id = (result.get("task") or {}).get("id")
        if not task_id:
            fail(f"kanban create for {slug!r} returned no task id", raw=result, tasks=created)
        slug_to_id[slug] = task_id
        created.append({
            "slug": slug,
            "id": task_id,
            "column": (result.get("task") or {}).get("column", "backlog"),
            "auto_review_mode": mode,
        })

    # 2. Link the chain: waiter waits on prereq.
    linked: list[dict] = []
    for edge in links:
        waiter, prereq = str(edge[0]), str(edge[1])
        result = run_kanban(cli, [
            "task", "link",
            "--task-id", slug_to_id[waiter],
            "--linked-task-id", slug_to_id[prereq],
            "--project-path", project_path,
        ])
        if not result.get("ok"):
            fail(
                f"failed to link {waiter!r} -> waits on {prereq!r}: {result.get('error', 'unknown error')}",
                stderr=result.get("stderr"),
                tasks=created,
                links=linked,
            )
        linked.append({
            "waiter": waiter,
            "prereq": prereq,
            "dependency_id": (result.get("dependency") or {}).get("id"),
        })

    # 3. Optionally start the first task so the pipeline runs.
    started = None
    if start_slug is not None:
        start_slug = str(start_slug)
        result = run_kanban(cli, [
            "task", "start",
            "--task-id", slug_to_id[start_slug],
            "--project-path", project_path,
        ])
        if not result.get("ok"):
            fail(
                f"failed to start {start_slug!r}: {result.get('error', 'unknown error')}",
                stderr=result.get("stderr"),
                tasks=created,
                links=linked,
            )
        started = start_slug
        for entry in created:
            if entry["slug"] == start_slug:
                entry["column"] = (result.get("task") or {}).get("column", "in_progress")

    sys.stdout.write(json.dumps({
        "ok": True,
        "work_branch": work_branch,
        "project_path": project_path,
        "agent_id": agent_id,
        "tasks": created,
        "links": linked,
        "started": started,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
