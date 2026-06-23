# run-ledger

Deterministic, **hook-free** observability for orchestration loops
(`execution-loop`, …). Producers turn a run's own artifacts into events and
record them to a local spool that forwards to a **central HTTP service on
`tyamini-dev`**, which writes a **Markdown/Obsidian vault** — one note per node.
Obsidian and a deterministic CLI read it today; a web dashboard can later read
the same API.

See `DESIGN.md` for the full design and rationale (why hooks were removed).

```
producers (any machine)                              central (tyamini-dev)
  milestones (run/plan) ─┐                              HTTP service ── Markdown vault
  via record ────────────┤─▶ run_ledger.py record ─▶ spool.jsonl ─(flush)─▶ POST /events ─▶ agents/<host>/<key>.md
  per-agent node ────────┘     (ingest-pane parses                                           ▲
   from pane.log               a finished pane.log)                                          │
  run_ledger.py timeline / Obsidian / dashboard ─────────────────────────────────────────────┘
```

Two producers, both deterministic and fail-open:
- **Milestones** (`run_start`/`plan_start`/`plan_finish`/`run_complete`/`blocked`)
  — `run_ledger.py record`, carrying a `run_id` (no `session_id`) → one
  **run-root note keyed by `run_id`**. In `execution-loop` these flow through the
  Stage-2 run-start line + the executor's `cli-escalation-notify` calls.
- **Per-agent node** — `run_ledger.py ingest-pane` parses a finished plan's
  `pane.log` (cursor-agent stream-json) + `verdict.json` into
  `subagent_start`/`subagent_stop` (model, machine, real start/end, per-tool
  summary, depth-3 Task subagents), keyed by the agent's `session_id` and
  parented to the `run_id`.

## Layout

| Path | Role |
| --- | --- |
| `lib/vault.py` | Shared Markdown-vault helpers (frontmatter + append-only timeline). Imported by both server and client. |
| `server/app.py` | Central ingest+query HTTP service (stdlib `http.server`, single-threaded → serialised writes). Runs on `tyamini-dev`. |
| `server/run-ledger.service` | User systemd unit for the service. |
| `client/run_ledger.py` | Per-machine CLI: `record` / `ingest-pane` / `flush` / `timeline`. |
| `tests/test_ingest_pane.py` | Parser + vault round-trip test (`python3 tests/test_ingest_pane.py`). |
| `var/` | Runtime data (gitignored): central `vault/` on tyamini-dev; `spool.jsonl` per machine. |

## Config (env)

| Var | Default | Used by |
| --- | --- | --- |
| `RUN_LEDGER_URL` | `http://tyamini-dev:8723` | client |
| `RUN_LEDGER_TOKEN` | _(empty = no auth)_ | client + server |
| `RUN_LEDGER_VAR` | `<tool>/var` | client + server |
| `RUN_LEDGER_VAULT` | `<var>/vault` | server (point at an Obsidian vault if desired) |
| `RUN_LEDGER_PORT` / `RUN_LEDGER_BIND` | `8723` / `0.0.0.0` | server |

## Install

**Central service (tyamini-dev only):**
```
mkdir -p ~/.config/systemd/user
ln -sf ~/.drivenets/cheetah/AI/v2/private/tools/run-ledger/server/run-ledger.service \
       ~/.config/systemd/user/run-ledger.service
systemctl --user edit run-ledger      # add Environment=RUN_LEDGER_TOKEN=<token>
systemctl --user daemon-reload && systemctl --user enable --now run-ledger.service
loginctl enable-linger "$USER"
```

**Each machine (producers):** no per-machine install or Cursor config — set the
endpoint and (optional) token in the environment available to the loop:
```
export RUN_LEDGER_URL=http://tyamini-dev:8723
export RUN_LEDGER_TOKEN=<token>
```

## CLI

```
run_ledger.py record --source notify --event plan-finish --run-id R1 --field plan=001-foo
run_ledger.py ingest-pane --run-id 20260621-104902-ab12cd --slug 001-foo
run_ledger.py flush
run_ledger.py timeline tyamini-dev2/20260621-104902-ab12cd
```

## Guarantees

- **Fail-open:** `record` and `ingest-pane` never raise and always exit 0; a
  telemetry problem can never block a loop.
- **No hooks:** events come only from a run's own artifacts (`pane.log`,
  `verdict.json`) and explicit milestone records. Ad-hoc chats record nothing
  because nothing emits for them.
- **No duplicates:** flush retries are idempotent — the server skips any
  `event_uuid` already applied (`.index/<host>__<key>.seen`).
- **Schemaless / append-only:** unknown fields are preserved as `key=value` on
  the timeline line and verbatim in `.index/<host>__<key>.events.jsonl`;
  enrichment is added as separate linked notes, never by editing a run note.
