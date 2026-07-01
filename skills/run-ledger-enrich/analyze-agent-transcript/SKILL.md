---
name: analyze-agent-transcript
description: Analyze one execution-loop agent's distilled transcript and write a curated finding note. Used by run-ledger-enrich, once per agent.
---

# analyze-agent-transcript

Input (from the orchestrator): `run_id`, plan `slug`, and the paths
`~/.exec-runs/<run_id>/enrich/<slug>.distilled.md` (distilled transcript) and
`~/.exec-runs/<run_id>/enrich/manifest.json` (the agent's row: prompt, directives,
final_summary, errors, harness, verdict, tool_counts, timing, model).

## Goal
Produce a readable, self-contained **finding note** for this agent that survives
transcript deletion: a human postmortem plus verbatim key excerpts.

## Steps
1. Read the agent's distilled transcript and its manifest row. Do **not** read the
   raw `pane.log` (it is intentionally huge/noisy).
2. Write a finding note to `~/.exec-runs/<run_id>/enrich/<slug>.finding.md` with
   EXACTLY these sections:

   ```markdown
   # <slug> — <role/model> — <verdict.status>

   ## What happened
   <2–6 sentence narrative of the agent's arc, grounded in the transcript.>

   ## What went well
   <bullets>

   ## What went wrong
   <bullets: failures, retries, injected directives and why they were needed,
   dead-ends. If none, say "Nothing notable.">

   ## Harness used
   <copy the manifest row's `harness` as three short bullet lists — Skills,
   Rules, Scripts — of what this agent actually used; write "None" where empty.
   Do not editorialize; this is an inventory.>

   ## Key excerpts
   ### Input prompt
   <the manifest `prompt` verbatim in a fenced block>
   ### Final summary / loop_report
   <the manifest `final_summary` verbatim in a fenced block>
   ### Injected directives
   <each manifest `directives` entry verbatim, or "None">
   ### Errors
   <the manifest `errors` entries verbatim, or "None">
   ### Pivotal moments
   <2–5 short verbatim quotes YOU pick from the distilled transcript that are not
   already captured above, each with its timestamp. Keep it tight — no padding.>
   ```
3. Push it: `python3 ~/.drivenets/cheetah/AI/v2/private/tools/run-ledger/client/enrich.py put-finding --run-id <run_id> --slug <slug> --file ~/.exec-runs/<run_id>/enrich/<slug>.finding.md`

## Rules
- Verbatim excerpts come from the manifest (deterministically extracted) — copy
  them, don't paraphrase.
- Narrative must be grounded in the transcript; do not invent events.
- Keep it lean: no restating the prompt in prose, no filler. Signal only.
