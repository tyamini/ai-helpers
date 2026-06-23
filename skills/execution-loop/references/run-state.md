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
      agent.err        # cursor-agent stderr
      pane.log         # live tee of the pane: cursor-agent stream-json events + the completion sentinel
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
- stdin JSON: `{run_id, slug, plan_path, branch, repo_root, model?, prompt_path?}`
- stdout: `{"pane": "%NN", "plan_dir": "...", "log_path": ".../pane.log", "started_at": "..."}`
- side effect: splits a new tmux pane in the run's session and sends the
  sentinel-wrapped `cursor-agent` command (stream-json, piped live to the pane
  via `tee`). No telemetry env — the prompt's first step runs `run_ledger.py
  init`, and the hook registers the agent's session. Never blocks.

### scripts/watch.sh
- args: `<abs path to pane.log>`
- run as a **background** shell (`block_until_ms: 0`); the executor ends its turn
  and is re-woken by the background completion notification (no `AwaitShell`
  block). Exits 0 on the first `__EXEC_DONE__ rc=` sentinel line.

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

## Telemetry: live registry + per-agent vault
Telemetry scoping is decentralized and lives in the run-ledger, not here:

- **Live registry (machine-local):** `run-ledger/var/live/<session_id>.json` =
  `{session_id, run_id, role, parent_session_id, registered_at}`. Written by the
  hook when it observes a `run_ledger.py init` tool call. A hook event is
  recorded **only** if its session is registered. Keyed by `session_id`, so
  multiple runs can be live on one machine concurrently. There is **no**
  `active.json`. The executor registers itself (`init --role executor`) and
  learns its own id via `resolve`; each per-plan agent self-registers via the
  `init` first-step in its prompt (`--role subagent --parent <exec_sid>`); the
  run is deregistered at Stage 4 (`init --end`) with a TTL backstop.
- **Central vault (tyamini-dev):** one note per agent at
  `agents/<host>/<session_id>.md` (frontmatter `run_id`/`role`/`parent` wikilink
  + that agent's timeline). A run = the agent notes sharing a `run_id`.

## cursor-agent output shape (observed, Cursor 3.8.x)
Dispatch uses `--output-format stream-json --stream-partial-output`, which emits
one JSON object per line, live: a `system/init` line
(`{type:"system",subtype:"init",session_id,model,...}`), `assistant` text-delta
lines, tool-call lines, and a final
`{type:"result",subtype:"success",result,session_id,usage{...}}`. Every line
carries `session_id` — the chat id for `cursor-agent --resume <session_id>`.
`pane.log` is this stream plus the trailing `__EXEC_DONE__ rc=<n>` sentinel.
