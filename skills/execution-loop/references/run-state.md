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
  via `tee`). No telemetry env — the agent's node is parsed from `pane.log` at
  collect time. Never blocks.

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
  "exit_reason": "met-criteria | blocked | ... | null",
  "verification": "pass | fail | not-run | blocked | null",
  "loop_report_found": true,
  "green": true,
  "chat_id": "<cursor-agent session_id, for --resume>",
  "collected_at": "2026-06-22T..."
}
```
- `committed` (git evidence: `head_sha != baseline_sha` AND `clean_tree`) is
  necessary but **not** sufficient. `exit_reason`/`verification` come from the
  agent's loop_report (parsed from the terminal `result` event; `null` if the
  report is missing/unparsed). `green` = `committed` AND
  `exit_reason == met-criteria` AND `verification == pass` — this is the "done"
  signal the executor advances on. `status` is `complete` only when `rc == 0`
  AND `green`; a committed-but-not-green plan is `incomplete` (→ Blocker policy).

## Telemetry: deterministic, hook-free, artifact-derived
Telemetry lives in the run-ledger and is produced from this run's own artifacts —
there is **no** Cursor hook, live registry, `init`/`resolve`, or `active.json`:

- **Run-root note → keyed by `run_id`.** Milestone events (`run_start` at
  Stage 2; `plan_start`/`plan_finish`/`run_complete` via the executor's
  cli-escalation-notify calls) carry `run_id` and no `session_id`, so the ledger
  routes them all to one run-root note. The executor never needs its own
  session id.
- **Per-plan agent note → keyed by its `chat_id`.** At collect time,
  `exec_collect.py` invokes `run_ledger.py ingest-pane --run-id <id> --slug
  <slug>`, which parses the finished `pane.log` (cursor-agent stream-json) +
  `verdict.json` into `subagent_start`/`subagent_stop` events: model, machine,
  real start/end timestamps, a per-tool summary, and any depth-3 Task subagents
  observed. Frontmatter `parent → [[<host>/<run_id>]]` links it to the run root.
- **Central vault (tyamini-dev):** one note per node at
  `agents/<host>/<key>.md` (`<key>` = `session_id`, or `run_id` for the root).
  A run = the run-root note plus the agent notes sharing its `run_id`.

## cursor-agent output shape (observed, Cursor 3.8.x)
Dispatch uses `--output-format stream-json --stream-partial-output`, which emits
one JSON object per line, live: a `system/init` line
(`{type:"system",subtype:"init",session_id,model,...}`), `assistant` text-delta
lines, tool-call lines, and a final
`{type:"result",subtype:"success",result,session_id,usage{...}}`. Every line
carries `session_id` — the chat id for `cursor-agent --resume <session_id>`.
`pane.log` is this stream plus the trailing `__EXEC_DONE__ rc=<n>` sentinel.
