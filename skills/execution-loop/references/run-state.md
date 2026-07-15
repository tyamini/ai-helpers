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
      agent            # one line: the agent CLI used ("cursor" | "claude")
      agent.err        # agent CLI stderr
      agent.pid        # the launched agent CLI PID (watcher liveness + collect reap)
      pane             # the tmux pane id (so collect can close it after the plan)
      pane.log         # live tee of the pane: agent CLI stream-json events + the completion sentinel
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
  "agent": "cursor | claude",
  "model": "<model or null for the agent CLI's default>",
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
- stdin JSON: `{run_id, slug, plan_path, branch, repo_root, agent?, model?, prompt_path?}`
  (`agent` = `cursor` (default) | `claude`; falls back to `$EXEC_LOOP_AGENT`)
- stdout: `{"pane": "%NN", "plan_dir": "...", "log_path": ".../pane.log", "pid_path": ".../agent.pid", "agent": "...", "started_at": "..."}`
  (or `{"error": "agent-not-available"|"unknown-agent"|...}`)
- side effect: splits a new tmux pane in the run's session and sends the
  sentinel-wrapped agent-CLI command (`cursor-agent` or `claude`, stream-json,
  piped live to the pane via `tee`). The agent CLI is backgrounded inside the
  subshell so its real PID is written to `agent.pid`; the pane id is written to
  `pane` and the chosen agent to `agent`. No telemetry env — the agent's node is
  parsed from `pane.log` at collect time. Never blocks.

### scripts/exec_resume.py
- stdin JSON: `{run_id, slug, directive, agent?, model?, repo_root?, chat_id?}`
  (`agent`/`chat_id` default to what dispatch recorded for the plan — the `agent`
  file and the first `session_id` in `pane.log` / `verdict.json`)
- stdout: `{"pane": "%NN", "log_path": ".../pane.log", "pid_path": ".../agent.pid", "chat_id": "...", "agent": "...", "started_at": "..."}` (or `{"error": ...}`)
- side effect: resumes the plan's chat with the same agent CLI
  (`--resume <chat_id>`), reusing the plan's pane (splits a fresh one if it was
  reaped), wrapped exactly like dispatch — stream appended to the SAME
  `pane.log`, `agent.pid` rewritten with the new PID, trailing `__EXEC_DONE__`
  sentinel — so the same `watch.sh`/`exec_collect.py` apply. Never blocks; start
  a fresh `watch.sh` after it.

### scripts/watch.sh
- args: `<abs path to pane.log> [pidfile] [idle_secs]` (pass the `pid_path` from
  dispatch as `pidfile`; `idle_secs` defaults to 1200)
- run as a **background** shell (`block_until_ms: 0`); the executor ends its turn
  and is re-woken by the background completion notification (no `AwaitShell`
  block). Exits 0 — printing a `__EXEC_DONE__ rc=<n|reason>` line — on the FIRST
  of any reliable done signal: the anchored `^__EXEC_DONE__ rc=` sentinel, a
  terminal stream-json `"type":"result"` event (turn ended even if the process
  hangs on exit), the agent PID dying without a sentinel (`proc-exit`), or the
  log going idle for `idle_secs` (`idle-timeout`). The result-event and idle
  signals are what prevent a hung-but-idle agent CLI from stranding the
  executor; a slightly-early wake is safe because collect re-checks by evidence.
  Agent-agnostic: both `cursor-agent` and `claude` emit the `"type":"result"`
  terminal event and write the same sentinel.

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
  "chat_id": "<agent CLI session_id, for --resume>",
  "agent": "cursor | claude",
  "metrics": {
    "invocations": 2,       // result events with num_turns>0 (dispatch + resumes)
    "turns": 138,           // summed num_turns
    "output_tokens": 218269,
    "cache_read_tokens": 38221248,
    "api_minutes": 47.8,    // summed duration_api_ms
    "cost_usd": 33.18       // ~approximate (CLI cost fields can disagree)
  },
  "collected_at": "2026-06-22T..."
}
```
- `committed` (git evidence: `head_sha != baseline_sha` AND `clean_tree`) is
  necessary but **not** sufficient. `exit_reason`/`verification` come from the
  agent's loop_report (parsed from the terminal `result` event; `null` if the
  report is missing/unparsed). `green` = `committed` AND
  `exit_reason == met-criteria` AND `verification == pass` — this is the "done"
  signal the executor advances on. `status` is `complete` when `green`, else
  `incomplete` (→ Blocker policy). `rc` is recorded for diagnostics only and is
  no longer required to be 0, since the watcher may wake the executor on the
  result event or idle backstop before the process-exit sentinel lands. `agent`
  echoes the CLI recorded in the plan's `agent` file (used by `exec_resume.py`).
  When `green`, collect reaps the plan's agent CLI (via `agent.pid`) and closes
  its `pane`, so a finished-but-hung agent cannot leak.
- `metrics` aggregates trustworthy counters (from the agent CLI's own `result`
  events) as cumulative plan totals across dispatch + every resume — for the
  run-report counts table. It deliberately omits the self-reported per-turn wall
  `duration_ms` (unreliable under the backgrounded `-p` invocation); wall-clock
  belongs in the timeline table, built from the executor's own recorded phase
  timestamps (each `started_at` from dispatch/resume, each `collected_at`).

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
  <slug>`, which parses the finished `pane.log` stream-json +
  `verdict.json` into `subagent_start`/`subagent_stop` events: model, machine,
  real start/end timestamps, a per-tool summary, and any depth-3 Task subagents
  observed. Frontmatter `parent → [[<host>/<run_id>]]` links it to the run root.
  The parser is tuned to the cursor-agent stream shape; `session_id` and `model`
  are captured for both CLIs, but the per-tool/depth-3 detail is sparse for
  `claude` (whose tool calls are `assistant`/`tool_use` content blocks, not
  top-level `tool_call` events). This is telemetry richness only — `ingest-pane`
  is fail-open and never affects a verdict or the run.
- **Central vault (tyamini-dev):** one note per node at
  `agents/<host>/<key>.md` (`<key>` = `session_id`, or `run_id` for the root).
  A run = the run-root note plus the agent notes sharing its `run_id`.

## Agent-CLI output shape (stream-json)
Both agent CLIs emit `--output-format stream-json` — one JSON object per line,
live — with the same signals the watcher and collector rely on: a `system/init`
line carrying `session_id` and `model`, `assistant` lines, tool events, and a
terminal `{type:"result",subtype:"success",result,session_id,...}`. Every line
carries `session_id` — the chat id for `--resume <session_id>`. `pane.log` is
this stream plus the trailing `__EXEC_DONE__ rc=<n>` sentinel.

- **cursor** (`cursor-agent`, observed Cursor 3.8.x): dispatched with
  `--force --trust --output-format stream-json --stream-partial-output --workspace <root>`;
  emits `system/init`, `assistant` text-delta lines, top-level `tool_call` lines,
  and the final `result` event.
- **claude** (Claude Code): dispatched with
  `-p --output-format stream-json --verbose` (cwd = repo_root).
  `--dangerously-skip-permissions` is added **only when the executor itself runs
  with it** — the per-plan child inherits the main Claude agent's permission mode
  (auto-detected by walking the process tree for an ancestor `claude` launched
  with the flag; overridable via `$EXEC_LOOP_SKIP_PERMISSIONS=1|0`). It emits
  `{type:"system",subtype:"init",...}`, `assistant`
  messages whose `message.content[]` holds `text` and `tool_use` blocks, and the
  final `{type:"result",subtype:"success",result,session_id,total_cost_usd,...}`.
  The loop_report the collector reads lands in the `assistant` text / `result`.
