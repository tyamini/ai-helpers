# execution-loop run state

All run state lives under `~/.exec-runs/<run_id>/` (machine-local, never
committed). `run_id` is generated at Stage 2 (`YYYYMMDD-HHMMSS-<6hex>`).

```
~/.exec-runs/<run_id>/
  meta.json            # run-level metadata (below)
  tmux_session         # one line: the session name used for this run
  plans/
    <NNN>-<slug>/      # NNN = zero-padded plan index; slug = sanitized plan name
      prompt.txt       # the per-plan agent prompt (from references/dispatch-prompt.md)
      agent.json       # cursor-agent --output-format json output (one JSON object)
      agent.err        # cursor-agent stderr
      pane.log         # tee of the wrapped command incl. the completion sentinel
      verdict.json     # evidence-based verdict (below)
```

## meta.json
```json
{
  "run_id": "20260622-061654-fede2b",
  "branch": "<work-branch>",
  "parent": "<parent-branch>",
  "repo_root": "/home/dn/cheetah",
  "tmux_session": "exec-loop-<run_id> | <reused session>",
  "session_origin": "created | reused",
  "is_cli_context": true,
  "model": "<model or null for cursor-agent default>",
  "plans": ["<NNN>-<slug>", "..."],
  "created_at": "2026-06-22T06:16:54Z"
}
```

## Script I/O (all JSON; deterministic; the executor only invokes + reads)

### scripts/exec_session.py
- args: `--run-id <id>`
- stdout: `{"session": "<name>", "origin": "reused|created"}` (or `{"error": ...}`)
- side effect: writes `<run_dir>/tmux_session`

### scripts/exec_dispatch.py
- stdin JSON: `{run_id, slug, plan_path, branch, repo_root, model?, prompt_path?, parent?}`
- stdout: `{"pane": "%NN", "plan_dir": "...", "log_path": ".../pane.log", "result_path": ".../agent.json", "started_at": "..."}`
- side effect: opens a new tmux window in the run's session and sends the
  sentinel-wrapped `cursor-agent` command. Never blocks.

### scripts/watch.sh
- args: `<abs path to pane.log>`
- run with `block_until_ms=0`; await via `AwaitShell` on the regex `__EXEC_DONE__ rc=`.
- exits 0 on the first sentinel line.

### scripts/exec_collect.py
- stdin JSON: `{run_id, slug, baseline_sha, repo_root}`
- stdout + `<plan_dir>/verdict.json`:
```json
{
  "status": "complete | incomplete",
  "rc": 0,
  "baseline_sha": "<sha at dispatch>",
  "head_sha": "<sha now>",
  "clean_tree": true,
  "committed": true,
  "chat_id": "<cursor-agent session_id, for --resume>",
  "collected_at": "2026-06-22T..."
}
```
- `committed` is the authoritative "done" signal: `head_sha != baseline_sha`
  AND `clean_tree`. `status` is `complete` only when `rc == 0` AND `committed`.

## cursor-agent JSON shape (observed, Cursor 3.8.x)
`--output-format json` emits one object:
`{type, subtype, is_error, duration_ms, result, session_id, request_id, usage{...}}`.
`session_id` is the chat id used for `cursor-agent --resume <session_id>`.
