# run-ledger

Deterministic, non-LLM observability for orchestration loops (`execution-loop`,
`implementation-loop`). Producers on any machine record events to a local spool
that forwards to a **central HTTP service on `tyamini-dev`**, which writes a
**Markdown/Obsidian vault** — one note per run. Obsidian and a deterministic CLI
read it today; a web dashboard can later read the same API.

```
producers (any machine)                         central (tyamini-dev)
  Cursor hooks ─┐                                  HTTP service ── Markdown vault
  notify wrap ──┤─▶ run_ledger.py record ─▶ spool.jsonl ─(flush)─▶ POST /events ─▶ runs/<host>/<run_id>.md
                                                                                   ▲
  run_ledger.py timeline / Obsidian / dashboard ──────────────────────────────────┘
```

## Layout

| Path | Role |
| --- | --- |
| `lib/vault.py` | Shared Markdown-vault helpers (frontmatter + append-only timeline). Imported by both server and client. |
| `server/app.py` | Central ingest+query HTTP service (stdlib `http.server`, single-threaded → serialised writes). Runs on `tyamini-dev`. |
| `server/run-ledger.service` | User systemd unit for the service. |
| `client/run_ledger.py` | Per-machine CLI: `record` / `hook` / `flush` / `timeline`. |
| `hooks/run-ledger.sh` | Dumb Cursor-hook entrypoint → `run_ledger.py hook`. |
| `hooks/hooks.json.example` | Example user hook registration (copy to `~/.cursor/hooks.json`). |
| `var/` | Runtime data (gitignored): central `vault/` on tyamini-dev; `spool.jsonl` + `active.json` per machine. |

## Config (env)

| Var | Default | Used by |
| --- | --- | --- |
| `RUN_LEDGER_URL` | `http://tyamini-dev:8723` | client |
| `RUN_LEDGER_TOKEN` | _(empty = no auth)_ | client + server |
| `RUN_LEDGER_VAR` | `<tool>/var` | client + server |
| `RUN_LEDGER_VAULT` | `<var>/vault` | server (point at an Obsidian vault if desired) |
| `RUN_LEDGER_PORT` / `RUN_LEDGER_BIND` | `8723` / `0.0.0.0` | server |
| `RUN_LEDGER_MAX_AGE` | `86400` (24h) | client (active-run staleness TTL) |
| `RUN_LEDGER_HOOK_DEBUG` | _(unset)_ | hook (dump raw payloads to `var/hook-raw.jsonl`) |

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

**Each machine (producers):**
```
cp tools/run-ledger/hooks/hooks.json.example ~/.cursor/hooks.json   # then restart Cursor
export RUN_LEDGER_URL=http://tyamini-dev:8723
export RUN_LEDGER_TOKEN=<token>
```

> First-run note (KNOWN UNKNOWN): the exact Cursor hook payload schema — in
> particular whether a parent/root agent id is present — is undocumented. Set
> `RUN_LEDGER_HOOK_DEBUG=1` for one session and inspect `var/hook-raw.jsonl` to
> confirm field names. With a parent/root id the full executor→subagent tree is
> reconstructable; without it, subagents are recorded as a flat list under the
> run (documented fallback).

## CLI

```
run_ledger.py record --source notify --event plan_finish --field run_id=R1 --field sha=abc123
run_ledger.py flush
run_ledger.py timeline tyamini-dev2/20260621-104902-ab12cd
```

## Guarantees

- **Fail-open:** `record` never raises and always exits 0; a telemetry problem
  can never block a loop.
- **No unrelated entries:** hook events are recorded only inside the
  `[run-start, run-complete]` window of a live `active.json` (with lineage
  scoping when agent ids are exposed); ad-hoc chats record nothing.
- **No duplicates:** flush retries are idempotent — the server skips any
  `event_uuid` already applied to a run (`.index/<host>__<run_id>.seen`).
- **Schemaless / append-only:** unknown fields are preserved as `key=value` on
  the timeline line and verbatim in `.index/<host>__<run_id>.events.jsonl`;
  enrichment is added as separate linked notes, never by editing a run note.
