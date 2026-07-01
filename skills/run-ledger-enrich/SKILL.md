---
name: run-ledger-enrich
description: Post-hoc enrichment of a completed execution-loop run. Renames the run's vault notes to meaningful deterministic names and writes curated per-agent findings, a run-level synthesis, a harness inventory (skills/rules/scripts used), and a distilled-transcript archive into the vault, so the vault stays complete after the transient ~/.exec-runs transcripts are deleted. Use when asked to "enrich a run", "summarize/postmortem an execution-loop run into the ledger", or given a run_id to enrich.
---

# run-ledger-enrich

## Goal
Turn one completed `execution-loop` run's transcripts into a durable, browsable
enrichment layer in the vault: meaningful note names + per-agent findings + a
run synthesis + a harness inventory + a distilled-transcript archive. Additive
only — the sole edit to existing notes is the rename (old name kept as an alias
so links still resolve).

## Inputs
- `run_id` (required). Resolve `host` = this machine's hostname.

## Preconditions
- The run is **complete** (check `python3 <tool>/client/run_ledger.py timeline <run_id>`;
  do not enrich a `running` run — rename could race the recorder).
- `~/.exec-runs/<run_id>/` exists on this machine (that is where `pane.log` lives).
- Let `TOOL=~/.drivenets/cheetah/AI/v2/private/tools/run-ledger`.

## Workflow
1. **Prep (deterministic):** `python3 $TOOL/client/enrich.py prep --run-id <run_id>`.
   Reads `~/.exec-runs/<run_id>/enrich/manifest.json` afterward for the agent list
   (roles, slugs, new_names, per-agent extracts + harness).
2. **Rename (deterministic):** `python3 $TOOL/client/enrich.py rename --run-id <run_id>`.
3. **Archive (deterministic):** `python3 $TOOL/client/enrich.py put-archive --run-id <run_id>`.
4. **Per-agent findings:** for each `subagent` entry in the manifest, run the
   `analyze-agent-transcript` subskill (passing `run_id` + `slug`). It writes and
   pushes that agent's finding note.
5. **Run synthesis:** write `~/.exec-runs/<run_id>/enrich/synthesis.md` — a
   readable run-level postmortem: overall narrative, cross-agent conclusions,
   lessons for the future, and a **Harness used (run-wide)** section listing the
   aggregated `harness` (skills / rules / scripts) from `manifest.json`
   (human-oriented, grounded in the per-agent findings and the manifest; do NOT
   restate each finding). Push it:
   `python3 $TOOL/client/enrich.py put-synthesis --run-id <run_id> --file ~/.exec-runs/<run_id>/enrich/synthesis.md`.
6. **Report:** print the vault locations written (renamed notes + `enrichment/<host>__<run_id>__*`).

## Rules
- The deterministic steps (1–3, and the push in 4–5) go through `enrich.py`/the
  server API — never hand-edit vault files and never rename over SSH.
- Only run on a complete run. Re-running is safe (idempotent rename; enrichment
  PUTs overwrite).
- Findings are narrative + verbatim excerpts; keep them lean (no filler). The
  archive already preserves the full distilled transcript.
- Linking is via `[[host/<note>]]` wikilinks in enrichment bodies (auto-backlinks);
  do not edit agent-note bodies.

## Quality bar
[ ] Ran on a complete run; prep/rename/archive all succeeded.
[ ] Every subagent got a finding note; a synthesis note was written.
[ ] Notes renamed to `<run_id>__<slug>__subagent` / `<run_id>__executor`, old
    names preserved as aliases (existing links still resolve).
[ ] Each finding + the synthesis records the harness (skills/rules/scripts) used.
[ ] No agent-note body was edited; only rename + additive enrichment notes.
