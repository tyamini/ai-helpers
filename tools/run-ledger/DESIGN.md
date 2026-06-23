# run-ledger — design & implementation plan

Deterministic, hook-free observability for orchestration runs. Producers turn a
run's **own artifacts** into events; the existing spool → central HTTP service →
Markdown/Obsidian vault path files them into one note per node.

This document supersedes the Cursor-hook mechanism. It records the agreed design
(Approach A) and the implementation plan to get there.

---

## 1. Goal & scope

**Goal:** see an orchestration run as a timeline in the vault — the plan list,
the executor → per-plan-agent → (depth-3) subagent tree, timing, machine, model,
and tool usage — produced **deterministically**, with no Cursor hooks.

**In scope (v1):** the CLI `execution-loop` (tmux + `cursor-agent`), which writes
deterministic artifacts per run:
- `~/.exec-runs/<run_id>/meta.json` — run_id, branch, host, model, plans.
- `~/.exec-runs/<run_id>/plans/<slug>/pane.log` — the per-plan agent's full
  `cursor-agent` stream-json (session_id, model, tool calls, timestamps, usage)
  plus the `__EXEC_DONE__ rc=<n>` sentinel.
- `~/.exec-runs/<run_id>/plans/<slug>/verdict.json` — status, committed, chat_id.

**Out of scope (v1):** loops that run only as in-IDE Task subagents (no stream
artifact). They can still get the Tier-1 timeline (see §8) but not the
stream-derived per-agent detail.

**Kept unchanged:** `server/app.py`, the spool→flush→`POST /events` transport,
the vault layout, the enrichment endpoint, and `run_ledger.py timeline`.

---

## 2. Why hooks are being removed

The Cursor-hook source has three structural problems:
1. Hooks fire for **every** Cursor session, so scoping a hook event to a run
   required a contraption: an `init` marker tool-call, a second hook observing
   it, digging out a hidden `session_id`, and writing a machine-local registry
   (`var/live/<session_id>.json`) that later hooks are matched against.
2. The hook **payload schema is undocumented** — parent/child agent linkage was
   guesswork (a standing "KNOWN UNKNOWN" in the README).
3. Hooks are **silent and out-of-band** — they cannot be run by hand and watched,
   so failures are hard to debug.

The key realization: the executor **already launches the agents and already
captures their full output to disk**, so the facts hooks tried to recover already
exist deterministically in `pane.log`/`meta.json`/`verdict.json`. We read those
instead of relying on Cursor to tell us.

---

## 3. Design (Approach A)

### 3.1 Event model & vault keying

The vault stays "one note per node," with simpler keys:

- **Run-root / executor note → keyed by `run_id`.** Milestone events
  (`run_start`, `plan_start`, `plan_finish`, `run_complete`, `blocked`) carry
  `run_id` and **no** `session_id`. `Vault.record`'s existing rule
  `key = session_id or run_id` already routes them to one run-root note. *This is
  why the executor no longer needs to discover its own session id.*
- **Per-plan agent note → keyed by its `chat_id`** (the cursor-agent
  `session_id`, read from `pane.log`/`verdict.json`). Frontmatter
  `parent: [[<host>/<run_id>]]` links it to the run root.
- **Depth-3 subagents** (implementer/reviewer) → listed on the per-plan agent
  node as a `subagents` summary when the stream exposes their Task calls;
  promotion to their own notes is future work (§7).

Tool usage is recorded as a **per-agent summary** (`tool_counts`, e.g.
`Shell=12 Task=3 MCP=1`), not one event per call.

### 3.2 Producers and who emits each event

Two deterministic producers, both reusing the existing `record` → spool → flush
→ central → vault path. The split below is deliberate: mechanical events ride
**inside scripts the executor already runs**, so the LLM authors nothing.

| Event | Emitted by | Trigger point | LLM role |
| --- | --- | --- | --- |
| `run_start` | executor (verbatim skill line) | Stage 2 (bootstrap, no script yet) | runs one fixed line; only `$run_id`/`$branch` vary |
| `plan_start` / `plan_finish` / `run_complete` | executor via `cli-escalation-notify` step-0 | Stage 3/4 milestones it already notifies on | fires notify with a title; recorder maps title → closed event name |
| **agent node** (`subagent_start` / `subagent_stop`) | `run_ledger.py ingest-pane`, called by `exec_collect.py` | Stage 3 collect (script already parses `pane.log`) | none |
| `blocked` / `directive_injected` / `clarification` | executor via `cli-escalation-notify` | on genuine judgment | picks a human title; recorder maps title → closed event name |

**No duplication.** The plan/run milestones already flow through the executor's
existing `cli-escalation-notify` calls (step-0 records them deterministically in
both IDE and CLI). So the scripts add **only** the genuinely-missing piece — the
per-agent node — rather than re-emitting milestones. This is also why
`exec_dispatch.py` needs no telemetry change: `plan_start` is the notify
"starting plan" milestone.

**Closed vocabulary.** Event names are fixed in the scripts/recorder. The LLM
never invents an event name and never scopes an event; `run_id` is generated once
per run and threaded through (read from `meta.json` by the scripts).

> Note: `run_start`/`run_complete` sit at the run's outer edges where no script
> runs, so v1 keeps them as two fixed executor-run lines. Folding them into a
> tiny start/finalize script (zero LLM-typed events) is a trivial later
> tightening; not required for v1.

### 3.3 What is deleted vs kept

**Deleted (hook machinery, all of it):**
- `hooks/run-ledger.sh`, `hooks/hooks.json.example`, the `hooks/` dir.
- In `client/run_ledger.py`: `cmd_hook`, `_HOOK_EVENT_MAP`, `_parse_init_command`,
  `cmd_init`, `cmd_resolve`, and the live registry
  (`_register`/`_lookup`/`_scan_live`/`_end_run`/`_prune`, `_live_dir`/`_live_path`),
  plus the `init`/`resolve`/`hook` subcommands.
- Runtime `var/live/` (no longer written).

**Kept / lightly tweaked:**
- `client/run_ledger.py`: `record`, `flush`, `timeline`, spool helpers — unchanged.
- `lib/vault.py`: one small additive change — merge an event's `tool_counts`
  into the agent note's frontmatter `counts` (instead of only bumping per
  `tool_use` event). `run_id` keying + parent wikilink already work.
- `server/app.py`, spool/flush transport, enrichment endpoint — unchanged.

---

## 4. `ingest-pane` parser contract

A new deterministic subcommand of `client/run_ledger.py`. It re-reads the
finished artifacts for one plan and emits that plan's events. It is **fail-open**
(never raises, exit 0) like the rest of the client.

```
run_ledger.py ingest-pane --run-id <id> --slug <slug> [--repo <repo_root>]
```

Resolves paths from `~/.exec-runs/<run_id>/plans/<slug>/`:
- reads `pane.log` (stream-json) and `verdict.json`.

Parses (defensively — see §6) and emits, via the existing spool, the agent node
(milestones are not emitted here — see §3.2 "No duplication"):
1. `subagent_start` — fields: `session_id=<chat_id>`, `parent_session_id=<run_id>`
   (→ parent wikilink to the run root), `role=subagent`, `plan=<slug>`, `model`,
   `ts=<first stream ts>`. (`host` is stamped by the recorder.)
2. `subagent_stop` — fields: `session_id=<chat_id>`, `ts=<last stream ts>`,
   `status=<from verdict>`, `tool_counts={<tool>: <count>}` (dict, merged into
   frontmatter), `tools="Edit=.. Read=.. ..."` (human timeline string),
   `subagents="<observed Task subagent_types>"` (when present).

Notes:
- Timestamps come from the stream, **not** `now()` — `ts` is passed explicitly so
  the agent node reflects real start/end (the event's `ts` field overrides the
  recorder default).
- Tool counts: tally stream tool-call lines by tool name.
- Idempotent end-to-end: each emitted event has a fresh `event_uuid`; the server
  dedupes by `event_uuid`, and re-running `ingest-pane` for the same plan
  produces new uuids but the same note content (append is deterministic; a second
  run would duplicate lines, so `exec_collect.py` calls it exactly once per
  collect — the collect step itself runs once per plan).

---

## 5. `execution-loop` integration (exact changes)

**`scripts/exec_dispatch.py`** — no telemetry emit (milestones come from notify).
Only the obsolete "prompt's first step runs `run_ledger.py init` / hook
registers" comments are removed.

**`scripts/exec_collect.py`** — after writing `verdict.json`, invoke
`run_ledger.py ingest-pane --run-id <run_id> --slug <slug> --repo <repo>` (one
`subprocess.run`, output swallowed, non-fatal). This is the only functional
addition; the existing parse stays.

**`references/dispatch-prompt.md`** — delete the mandatory `init` first-line block
(lines describing `run_ledger.py init --run-id ... --role subagent --parent ...`)
and the surrounding explanation. The per-plan agent registers nothing now.

**`SKILL.md`:**
- Stage 2 step 3 ("Register the run"): replace the `init --role executor` +
  `resolve` block with a single fixed `run-start` record line:
  ```
  "$L" record --source notify --event run-start \
    --run-id "$run_id" --field branch="$work_branch"
  ```
  Drop all mention of `resolve`/`exec_sid`/the live registry.
- Stage 3 step 3: remove the instruction to put the `init` first-step in the
  per-plan prompt.
- Stage 4 step 3: remove `init --end` (no registry to deregister); keep the
  `run complete` notify (which already records `run_complete`).
- "Telemetry" hard-invariant + quality-bar lines: reword from "registry/init/
  resolve" to "events are emitted from the run's artifacts by the dispatch/collect
  scripts; the root note is keyed by run_id."

**`references/run-state.md`** — rewrite the "Telemetry: live registry + per-agent
vault" section to: run-root note keyed by `run_id` (milestones via notify);
per-plan agent note keyed by `chat_id` with `parent → run_id`, produced by
`exec_collect.py` → `ingest-pane`; **no** `var/live`, **no** `init`/`resolve`.

**`README.md` / `.gitignore`** — drop the hooks/registry sections and the
`active.json` mention; document `ingest-pane` and run_id keying.

---

## 6. Known unknowns & verification

**Known unknown — cursor-agent stream-json shape.** The exact per-line schema
(field names for tool calls, whether nested Task subagent events appear) is not
formally documented. Mitigations:
- The parser is **defensive**: it probes multiple field-name variants (mirroring
  the existing `_extract_chat_id`) and degrades gracefully (missing tool detail →
  empty `tool_counts`, never a crash).
- A **captured real `pane.log` fixture** is committed as a test input; the parser
  is unit-tested against it for expected events. Unlike hooks, the input is a
  plain file we can inspect and replay.

**Verification:**
1. Unit: `ingest-pane` on a synthetic real-shaped `pane.log` →
   `subagent_start`/`subagent_stop` with the right session_id, parent, model,
   timestamps, tool counts, and depth-3 subagents
   (`tests/test_ingest_pane.py`). Also validated against real captured
   `~/.exec-runs/*/plans/*/pane.log` files.
2. Integration: the test feeds emitted events (plus a milestone) through
   `lib.vault.Vault.record` and asserts the per-agent note (model, timing,
   tools, parent wikilink) and the run-root note.
3. End-to-end: a real `execution-loop` run produces the full tree in the vault
   with **no** `var/live/` created and **no** hooks installed.

---

## 7. Future features (mentioned, not in v1)

- **Higher-level orchestration** (a meta-loop spawning `execution-loop` runs):
  the model is recursive — a run can be the parent of other runs. Cheap
  provision: let `run_start` accept an optional `parent_run_id` and give the
  run-root note a `parent_run` wikilink (empty for top-level). Propagation rule:
  whoever spawns a sub-run passes its own `run_id` down as `parent_run_id`. The
  central service already aggregates cross-machine, so a meta-run spanning
  machines forms one tree.
- **Enrichment tool** (`enrich.py`): a separate, post-hoc, LLM-powered pass —
  deliberately *not* part of the deterministic pipeline. Reads a run's transcript
  (the `pane.log` stream is the transcript), extracts timestamped "thoughts"
  (decisions, dead-ends, blocker resolutions), and `PUT`s them to the existing
  `/runs/{host}/{run_id}/enrichment/{type}` endpoint as linked notes anchored by
  timestamp to the matching timeline events. Enrichment only *adds* linked notes;
  it never edits the deterministic timeline.
- **Transcript sidecar:** to let enrichment run centrally days later, `ingest-pane`
  could also ship the raw/distilled stream to the server as a sidecar (since
  `~/.exec-runs/` is machine-local and transient).

---

## 8. Portability to other orchestration skills

- **Tier 1 (one line, anywhere):** any skill that generates a `run_id` and emits
  `run_ledger.py record --source notify --event <milestone> --field run_id=<id>`
  at its milestones gets the vault timeline (plans, timing, machine). Skills that
  already fire `cli-escalation-notify` get this **for free** (its step 0 records).
- **Tier 2 (per-agent tree + tools):** any skill that dispatches `cursor-agent`
  and captures its stream-json to a file adds one `ingest-pane <log>` call where
  it collects the agent. Pure Task-subagent skills (no stream file) stay Tier 1.

---

## 9. Implementation plan (phased)

### Phase 1 — run-ledger client + vault
- `client/run_ledger.py`: delete hook/registry/init/resolve machinery (§3.3);
  add `cmd_ingest_pane` + `ingest-pane` subparser (§4).
- `lib/vault.py`: merge `tool_counts` into frontmatter `counts`; confirm
  run_id-keyed root note + `parent → run_id` wikilink for agent nodes.
- Delete `hooks/` dir.
- **Verify:** unit test `ingest-pane` against a committed `pane.log` fixture;
  round-trip `record→flush→server→vault`; `timeline` renders the tree.

### Phase 2 — execution-loop integration
- `scripts/exec_dispatch.py`: remove init/hook comments (no emit needed).
- `scripts/exec_collect.py`: call `ingest-pane` after `verdict.json`.
- `references/dispatch-prompt.md`: remove the `init` first-line.
- `SKILL.md`: Stage 2/3/4 edits + invariant/quality-bar rewording (§5).
- `references/run-state.md`: rewrite telemetry section (§5).
- **Verify:** a real `execution-loop` run yields the full vault tree; no
  `var/live/` created; no hooks installed.

### Phase 3 — docs & cleanup
- `README.md`: remove hooks/registry sections; document `ingest-pane` + run_id
  keying + the Tier-1/Tier-2 portability story.
- `.gitignore`: drop the `active.json` mention.
- **Verify:** README reflects the shipped mechanism; no dangling references to
  hooks/`init`/`resolve`/`var/live` anywhere in the tool or the skill.
