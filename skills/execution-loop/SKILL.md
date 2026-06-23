---
name: execution-loop
description: Execute one or more already-written plans end-to-end by dispatching a dedicated subagent per plan, each running via the /implementation-loop skill. First validates that every plan is understood, that all files they reference exist and are reachable, and that every harness they need is present (compile, unit-test, e2e, git-conventions, coding-conventions). Then runs the plans strictly one at a time — one subagent per plan, each told to drive its whole plan until all of its pass criteria pass and to commit when done — waiting for each to finish before starting the next. Fires cli-escalation-notify on every event (plan start, plan finish, problems) and pushes the branch at the end. Use when the user says "execute these plans", "run the execution-loop", "execute plan X and Y", or hands over a set of plan files to be carried out.
---

# Execution Loop

## Goal
Carry a set of approved plans to completion: validate they are executable
up front, then dispatch one **top-level `cursor-agent` process per plan**
(strictly sequential) that drives its entire plan until every pass criterion is
met and commits the result. The executor itself orchestrates, notifies, helps
agents past genuine blockers, and pushes the branch at the end. It does **not**
write plan code itself — the per-plan agents do.

Each per-plan agent is launched via deterministic scripts (JSON in/out) into a
tmux pane running `cursor-agent`. Launching it as a **top-level** process —
rather than a Task subagent — is deliberate: a Task subagent cannot spawn its
own subagents, so a per-plan Task subagent is forced to implement+review inline.
A top-level `cursor-agent` is free to dispatch implementation-loop's own
implementer/reviewer subagents, giving a real depth-3 tree
(executor → per-plan agent → implementer/reviewer) that the run-ledger captures.

**The executor does not block while a per-plan agent runs.** It launches the
agent and a background completion watcher, then **ends its turn** so it stays
free for user (or other-agent) input. It is re-woken by the watcher's background
completion notification the moment the agent finishes (not after any timeout) —
it does **not** read the agent's output/transcript while it runs (that would
burn its context for no benefit). At any time the executor (being free) can act
on user input: relay an explicitly-marked directive into the agent's chat via
`cursor-agent --resume <chat_id>`, or interrupt the run outright
(see [Directive injection](#directive-injection)). Run state lives in
`~/.exec-runs/<run_id>/`, so the loop resumes cleanly across turn boundaries.

## Inputs
- **Plans** — one or more plan files . At least one is required. Order matters: later plans
  may depend on earlier ones.
- **Parent branch** (optional) — the branch the work sits on top of. Defaults
  to the current branch; confirm with the user during validation.
- **Parallelism** — sequential by default. Run plans in parallel **only**
  when the user explicitly says so. Never assume parallelism is safe; if the
  user opts in, the consequences (e.g. concurrent commits to the same branch)
  are the user's responsibility, not something this skill works around.

## Companion skills (read once, up front)
- `.ai/skills/common/implementation-loop/SKILL.md` — each per-plan agent runs this skill to drive its
  plan (implement → review → fix) until exit criteria are met.
- `.ai/skills/common/git-conventions/SKILL.md` — branch name, commit message,
  and PR/push rules. The work branch and every commit must match it.
- `.agents/skills/cli-escalation-notify/SKILL.md` — fired on every event (plan start, plan finish,
  problems). Pushes a Slack DM in CLI context, no-ops in the IDE, never fatal.

## Companion scripts and references (in this skill dir)
- `scripts/exec_session.py` — resolve/create the run's tmux session (JSON out).
- `scripts/exec_dispatch.py` — launch ONE per-plan `cursor-agent` in a tmux window (JSON in/out). Run, do not read.
- `scripts/watch.sh` — completion-sentinel watcher (`__EXEC_DONE__ rc=`), run as a **background** shell (`block_until_ms: 0`); its completion notification re-wakes the executor. Run, do not read.
- `scripts/exec_collect.py` — parse `pane.log` (stream-json) + git evidence into `verdict.json` (JSON in/out).
- `references/run-state.md` — the `~/.exec-runs/<run_id>/` tree and every script's JSON contract.
- `references/dispatch-prompt.md` — the per-plan agent prompt.
- The `cursor-agent` CLI must be on `PATH`, and `CURSOR_API_KEY` (or a logged-in session) available to launched processes.

## Hard invariants
- **No plan authoring here.** The executor orchestrates and pushes. All
  implementation, compiling, and testing happen inside the per-plan agents.
- **Deterministic scripts own all mechanical actions.** tmux session
  resolution, agent launch, completion watching, and result collection happen in
  the `scripts/` above with JSON in/out. The executor invokes them and consumes
  their JSON — it never hand-rolls tmux/`cursor-agent` commands inline and never
  eyeballs pane contents for control flow.
- **Validation is delegated and read-only.** Stage 1 runs in a single `explore`
  Task subagent that reads the plans and probes the filesystem and returns a
  compact report; the main loop never reads plans or checks files itself — it
  works from the report alone. (Stage 1 is the one place a Task subagent is still
  used; everything else is `cursor-agent` processes.)
- **One top-level `cursor-agent` per plan; one plan at a time.** Each per-plan
  agent runs the `.ai/skills/common/implementation-loop/SKILL.md` skill and is
  launched via `scripts/exec_dispatch.py` (a top-level `cursor-agent -p` process
  in its own tmux pane), **not** the Task tool — that is what lets it dispatch
  its own implementer/reviewer subagents. Capture the returned `pane`. The
  executor then starts the background completion watcher and **ends its turn** —
  it does **not** block (no `AwaitShell` wait) and does not read the agent's
  output/transcript; it is re-woken by the watcher's completion notification and
  stays free for input meanwhile — see [Directive injection](#directive-injection).
  Strictly one plan at a time: do not start the next plan until the current one
  has finished and its commit is confirmed by git evidence. Parallel only on
  explicit user opt-in (separate panes make it trivial, but the sequential
  default stands).
- **Never re-scope a plan.** One plan file = exactly one per-plan agent,
  regardless of how many files, tests, or sections (including a phase-2
  refactor) it spans. The executor does not reclassify a plan as an "epic",
  split it into stages, or implement it itself — plan size is irrelevant to the
  one-agent-per-plan rule.
- **Short prompts; reference the harness by file.** The per-plan agent prompt
  (`references/dispatch-prompt.md`) states the goal, **points to** the
  harness/coding/commit docs it must read and follow (as a bare `label: path` —
  never transcribe their commands and never add a parenthetical describing a
  doc's contents), and the status of related plans — nothing more. **Do not
  restate, summarize, or re-enumerate anything already in the plan or the harness
  docs** (pass criteria, phases, non-goals, steps, commands, internal
  mechanics), and **do not list example blockers or example commands**. No
  step-by-step micromanagement.
- **Only environment issues are blockers.** A real blocker is an environment
  problem that stops an agent from using tools, editing code, compiling, or
  testing. Anything else (failing logic, wrong test, unclear step) is normal
  work the agent — with the executor's help — must push through.
- **Notify, never author content twice.** The executor delegates all Slack to
  `.agents/skills/cli-escalation-notify/SKILL.md`; it never composes Slack itself.

## Workflow

### Stage 1: Analyze and validate (delegated to a subagent)
Validation is pure read-only work, so it does **not** run in the main loop —
reading every plan and probing the filesystem here would burn the orchestrator's
context for no implementation benefit. Instead dispatch **one** read-only
validation subagent (Task tool, `subagent_type: explore`, blocking; wait for it)
that performs every check below and returns only the compact report defined in
[Validation subagent](#validation-subagent). The main loop consumes that report
and reads nothing else in this stage.

The subagent must, for **every** plan, in the user-supplied order:
1. Read the plan in full and extract its **pass / acceptance criteria**, every
   **file / path it references**, the **harness** it needs (build / unit-test /
   e2e, coding conventions), and any **dependency** on the other plans.
2. **Files reachable:** verify each referenced file/path exists and is readable;
   list any missing.
3. **Harness present:** identify the **minimal** harness reference doc(s) the
   plan needs plus the commit conventions
   (`.ai/skills/common/git-conventions/SKILL.md`), taken **only** from the
   user's input for this run or from the plan (never from repo
   workflow/conventions indexes), and confirm each exists and is reachable.
   Record **paths** only — no transcribed commands.
4. **Companion skills reachable:** resolve every skill listed under
   [Companion skills](#companion-skills-read-once-up-front) to its real file —
   **following symlinks** (a symlinked skill dir is installed, not missing; do
   not judge presence with a glob that skips symlinks) — and flag any that
   cannot be located. This includes
   `.agents/skills/cli-escalation-notify/SKILL.md`: being installed is a
   separate fact from its run-time "never fatal" send behavior, so one that
   cannot be located is a reported problem, not a silent best-effort skip.
5. **Ordering:** keep the user-supplied order verbatim. Do **not** infer or
   reorder based on dependencies; record any dependency as information only.

The subagent returns the compact validation report — a short per-plan summary,
or a `problems` list if anything failed. It never transcribes plan contents,
criteria text, or commands.

**Gate:** The validation subagent returned its report; every plan is understood;
all referenced files and required harness docs are reachable (paths only — never
transcribed commands); every companion skill resolved to a readable `SKILL.md`;
the order equals the user-supplied order (unchanged unless the user specified
otherwise). If the report lists any problem, proceed to Stage 2 to clarify or
halt; otherwise the main loop holds only the per-plan harness paths, order, and
dependency notes.

### Stage 2: Clarify and set up
1. **Clarify gaps (should be rare).** If the validation report lists any problem
   — unreachable files, an unavailable harness, an unresolvable companion skill,
   a plan with no testable pass criteria, or a real knowledge gap — present a single
   consolidated set of questions to the user and wait. A missing companion
   skill is never silently best-effort skipped; surface it and let the user
   decide. In CLI context, first
   dispatch `.agents/skills/cli-escalation-notify/SKILL.md` (`title: execution-loop — clarification needed`)
   as a heads-up; the local question remains the answer channel.
2. **Work branch.** Confirm the parent branch with the user (default: current),
   then create the run's work branch per `.ai/skills/common/git-conventions/SKILL.md`. Verify the
   workspace is clean (`git status --porcelain`); if dirty, stop and ask.
3. **Register the run (telemetry).** Generate a run id
   (`run_id="$(date +%Y%m%d-%H%M%S)-$(openssl rand -hex 3)"`). Let
   `L=~/.drivenets/cheetah/AI/v2/private/tools/run-ledger/client/run_ledger.py`.
   Emit run-start (fire-and-forget, non-fatal — swallow errors):
   ```
   "$L" record --source notify --event run-start \
     --run-id "$run_id" --field role=executor --field branch=<work-branch>
   ```
   This event carries `run_id` and no `session_id`, so the ledger routes it (and
   every later milestone) to a single **run-root note keyed by `run_id`** — the
   executor never needs to discover its own session id. Telemetry is hook-free
   and deterministic: `exec_dispatch.py` emits `plan_start`, and
   `exec_collect.py` parses each finished agent's `pane.log` into its node
   (keyed by the agent's `session_id`, parented to `run_id`). There is **no**
   live registry, `init`/`resolve`, or `active.json`.
4. **Resolve the tmux session.** Run `scripts/exec_session.py --run-id "$run_id"`.
   It reuses the current session when inside tmux (`$TMUX` set), else creates a
   dedicated `exec-loop-<run_id>` session, and records the name. Keep the
   returned `{session, origin}`. If it returns an `error`, halt and surface it.
   Write `meta.json` (`references/run-state.md`) with the run_id, branch, parent,
   repo_root, session, origin, model, and context.

**Gate:** No open questions; the work branch is checked out on the confirmed
parent, the tree is clean, the run-start record was emitted, and the tmux
session is resolved with `meta.json` written.

### Stage 3: Execute — one cursor-agent per plan
For each plan in order (sequential unless the user opted into parallel). `slug`
is `<NNN>-<sanitized-plan-name>` (NNN = zero-padded plan index).

1. **Notify start** — dispatch `.agents/skills/cli-escalation-notify/SKILL.md`
   (`title: execution-loop — starting plan <name>`), passing `run_id: <run_id>`
   in its `run_context`; the milestone records to the run-root note (keyed by
   `run_id`).
2. **Record the baseline** — capture the branch HEAD with `git rev-parse HEAD`;
   this is the per-plan agent's commit evidence baseline.
3. **Write the prompt.** Fill `references/dispatch-prompt.md` for this plan
   (goal + only the harness paths this plan needs + related-plan status) and save
   it to `~/.exec-runs/<run_id>/plans/<slug>/prompt.txt`. The prompt carries no
   telemetry step — the agent's node is parsed from its `pane.log` at collect time.
4. **Dispatch one top-level `cursor-agent`.** Pipe JSON to
   `scripts/exec_dispatch.py`: `{run_id, slug, plan_path, branch, repo_root,
   model, prompt_path}`. It splits a tmux pane and launches `cursor-agent`.
   Keep the returned `{pane, log_path}`. Do **not** use the Task tool here.
5. **Start the completion watcher in the background, then free the turn.** Run
   `scripts/watch.sh <log_path>` via the Shell tool with `block_until_ms: 0`
   (background — capture its shell id) so it does **not** block the turn. Then
   **end the turn**: the executor stays free for user/other-agent input while the
   per-plan agent runs in its pane. The background watcher completes the instant
   the `__EXEC_DONE__ rc=` sentinel appears (≈1s after the agent finishes, not at
   any cap), and its **completion notification re-wakes the executor** to run
   step 6. Do **not** `AwaitShell`-block on it, and do **not** read the agent's
   pane/transcript while it runs. (If the user sends a directive meanwhile, see
   [Directive injection](#directive-injection); resume waiting after.) Do not
   start the next plan until this one has finished.
6. **On the watcher's completion — collect + confirm by evidence, not
   self-report.** Pipe
   `{run_id, slug, baseline_sha, repo_root}` to `scripts/exec_collect.py`. It
   writes `verdict.json`, records the per-plan agent's run-ledger node from
   `pane.log` (deterministic, fail-open), and returns
   `{status, rc, committed, head_sha, chat_id, ...}`.
   The plan is done **only** when `committed` is true (HEAD advanced past the
   baseline AND clean tree). If not committed, the plan is **not** done — resume
   the same agent via `cursor-agent --resume <chat_id>` (see Blocker policy) to
   commit/finish; do not advance. Once confirmed, dispatch
   `.agents/skills/cli-escalation-notify/SKILL.md`
   (`title: execution-loop — finished plan <name>`).
7. **On a reported problem** — apply the [Blocker policy](#blocker-policy).

**Gate:** `verdict.json.committed` is true (a real commit landed past the
recorded baseline, clean tree) for the current plan before the next plan's agent
is dispatched.

### Stage 4: Finalize
1. After every plan has passed and committed, the executor **pushes** the work
   branch: `git push -u origin <branch>` (never a protected branch — see
   `.ai/skills/common/git-conventions/SKILL.md`).
2. Dispatch `.agents/skills/cli-escalation-notify/SKILL.md` (`title: execution-loop — run complete`),
   again passing `run_id` in `run_context`.
3. Print the run report (see Output format). Do **not** open a PR unless the
   user asked for one.

**Gate:** The branch is pushed and the run report is printed.

## Validation subagent
Dispatch with the Task tool, `subagent_type: explore` (read-only — it must not
edit anything), blocking. This is the only place the plans are read in full; the
main loop relies entirely on the returned report. Pass the text below verbatim
as the Task `prompt`, filling the bracketed fields.

```
Validate these execution-loop plans (read-only — do not edit anything).

Plans, in this exact order:
<one plan path per line>

For every plan:
- Read it in full and confirm it has explicit, testable pass/acceptance criteria.
- Verify every file/path it references exists and is readable.
- Identify the minimal harness reference doc(s) it needs (build / unit-test /
  e2e, coding conventions) plus commit conventions, taken only from the plan or
  the inputs below (never from repo indexes), and confirm each exists and is
  readable. Record paths only; never transcribe commands.
- Note any dependency on the other plans (information only; do not reorder).

Resolve each companion skill below to a readable SKILL.md, following symlinks,
and flag any that cannot be located:
- .ai/skills/common/implementation-loop/SKILL.md
- .ai/skills/common/git-conventions/SKILL.md
- .agents/skills/cli-escalation-notify/SKILL.md

Harness / coding-rules inputs for this run: <paths, or "none — take from the plans">

Return ONLY this compact report — no plan text, no criteria prose, no commands:

validation_report:
  plans:
    - plan: <path>
      pass_criteria: testable|missing
      harness: [<doc path(s)>]
      coding_rules: [<doc path(s)>]
      depends_on: [<plan path(s)>]   # info only
  order: [<plan paths, user-supplied order>]
  companion_skills: resolved | [<path>: unresolved]
  problems: []   # list every missing file, absent harness, unresolved skill, or plan lacking testable criteria
```

## Per-plan agent prompt
The prompt the executor writes to `<plan_dir>/prompt.txt` (Stage 3 step 3) lives
in `references/dispatch-prompt.md`. Fill its bracketed fields per plan; keep it
tight (goal + only this plan's harness paths + related-plan status). Do not
duplicate it here.

## Directive injection
While a per-plan agent runs, the executor can steer it by resuming its chat via
`cursor-agent --resume <chat_id> -p "<directive>"` — the same conversation with
its context preserved, **never a fresh launch** for steering. The `chat_id` is
the cursor-agent `session_id`, available from `verdict.json` after the agent
finishes (and, mid-run, from `pane.log` — the stream-json `system/init` line emits it first).

- **Behavior change vs Task subagents — no mid-run preemption.** A headless
  `cursor-agent -p` runs its turn to completion; you cannot interrupt it mid-turn
  the way Task `resume --interrupt` did. So:
  - **High severity** (wrong direction / must stop now / would waste significant
    work): kill the run — `tmux send-keys -t <pane> C-c` then, if needed, kill
    the pane/process — and relaunch the plan via `scripts/exec_dispatch.py` with
    a corrected prompt (fresh baseline). This is the only way to stop wasted work
    immediately.
  - **Low severity** (optional hint/extra context): **hold** it and deliver it
    after the agent finishes its turn via `cursor-agent --resume <chat_id> -p
    "<directive>"`. If the plan already committed (evidence gate passed) before
    you deliver it, the hint is moot — drop it.
- **Trigger (user only, during the run):** the executor's turn has **ended**
  while the agent runs (it is free), so it does not watch the agent. Mid-run
  injection happens when the **user** (or another agent) sends a message to the
  main loop while a per-plan agent is still running; that input re-engages the
  free executor, which then injects/interrupts and goes back to waiting for the
  completion notification.
- **User routing (explicit marker only):** relay a user message to the agent
  **only** when explicitly addressed to it — it starts with `subagent:` /
  `agent:` or "tell the agent ...". Any other user message is guidance to the
  executor itself; handle it normally and **do not** forward it.
- **Notify:** on each injection, dispatch
  `.agents/skills/cli-escalation-notify/SKILL.md`
  (`title: execution-loop — directive injected: <plan>`).
- **Evidence still rules:** injection never substitutes for completion evidence.
  After injecting, resume waiting; the plan is "done" only when
  `verdict.json.committed` is true (Stage 3 step 6).

## Blocker policy
When a per-plan agent reports a problem (or finishes without committing), the
executor decides:
- **Not an environment issue** (failing logic, flaky test, unclear step,
  forgot to commit): normal work. Help the agent — clarify, point at the right
  harness/doc, or resume it via `cursor-agent --resume <chat_id> -p "<directive>"`
  (kill+relaunch only for high-severity course corrections, per
  [Directive injection](#directive-injection)) — and let it keep going until the
  pass criteria are met and it has committed. This is not a blocker.
- **Real environment issue** the agent and the executor cannot fix (tools
  unavailable, cannot compile, cannot test, infra down, `cursor-agent` cannot
  launch/auth): dispatch `.agents/skills/cli-escalation-notify/SKILL.md`
  (`title: execution-loop — blocked: <reason>`), then **pause the loop and ask
  the user**. Do not skip the plan or fabricate success.

## Halt conditions
Stop and surface to the user (with a CLI notify) when:
- A plan cannot be loaded or has no testable pass criteria.
- A referenced file is unreachable, a required harness is absent, or a
  companion skill cannot be resolved to a readable `SKILL.md` (Stage 1).
- The workspace is dirty and the user has not chosen how to proceed.
- The tmux session cannot be resolved, or `cursor-agent` cannot launch/auth
  (`scripts/exec_session.py` / `scripts/exec_dispatch.py` returns an `error`).
- A real environment blocker hits a per-plan agent and cannot be fixed.

## Output format
End the run with a concise report:

```yaml
execution_loop_report:
  run_id: <run-id>
  branch: <work-branch>
  parent: <parent-branch>
  tmux_session: <session> (<created|reused>)
  parallel: false|true
  plans:
    - plan: <path>
      pass_criteria: met|blocked
      commit: <sha-or-none>
      chat_id: <cursor-agent session_id>
      notes: <1-line>
  push: pushed|skipped
  blockers:
    - plan: <path>
      reason: <env blocker, if any>
```

## Quality bar (self-check)
[ ] Stage 1 ran in one read-only `explore` validation subagent that read every
    plan in full and returned the compact validation report (pass criteria,
    referenced files, needed harness, inter-plan dependencies); the main loop
    consumed only that report and never read the plans or probed files itself.
[ ] Per the report, all referenced files were reachable and every required
    harness doc/file path (taken only from the user's input or the plan, minimal
    set) was identified and reachable before any execution (paths only — commands
    are never transcribed).
[ ] Clarification questions (if any) were consolidated into one ask; the
    common case asked nothing.
[ ] A single work branch was created per `.ai/skills/common/git-conventions/SKILL.md` on the confirmed
    parent; the tree was clean before starting.
[ ] All mechanical actions went through the deterministic `scripts/` with JSON
    in/out (session resolve, dispatch, watch, collect); no inline tmux /
    `cursor-agent` commands and no eyeballing pane contents for control flow.
[ ] The tmux session was reused (inside tmux) or created (`exec-loop-<run_id>`)
    via `scripts/exec_session.py`, and `meta.json` was written.
[ ] Exactly one top-level `cursor-agent` was launched per plan via
    `scripts/exec_dispatch.py` (NOT the Task tool), each running the
    `.ai/skills/common/implementation-loop/SKILL.md` skill, with `pane`/`chat_id`
    captured, strictly sequential (parallel only on explicit user opt-in).
[ ] No plan was reclassified as an "epic", split into stages, or implemented
    by the executor itself — one plan file mapped to exactly one per-plan agent.
[ ] Each per-plan agent prompt (`references/dispatch-prompt.md`) was short: goal
    + harness referenced **by file path** (bare `label: path`, never transcribed
    commands, no content-describing parentheticals) + related-plan status;
    nothing already in the plan or docs was restated; no step-by-step
    micromanagement.
[ ] The executor did **not** block while a per-plan agent ran: it started
    `watch.sh` as a background shell (`block_until_ms: 0`) and ended its turn
    (free for user/other-agent input), then resumed on the watcher's completion
    notification. It confirmed by `exec_collect.py` evidence
    (`verdict.json.committed`: clean tree + HEAD advanced past the recorded
    baseline) before dispatching the next — never by trusting the agent's
    self-report, never by reading its transcript mid-run.
[ ] Execution order matched the user-supplied order; no implicit reordering (a
    different order or a parallel run happened only on explicit user request).
[ ] `.agents/skills/cli-escalation-notify/SKILL.md` fired on plan start, plan finish, and every
    problem.
[ ] Directives were injected into the **same** agent via
    `cursor-agent --resume <chat_id>` (never a fresh launch for steering);
    mid-run injection was user-triggered and relayed only on the explicit marker
    (`subagent:` / `agent:` / "tell the agent ..."); high-severity used
    kill+relaunch (no mid-turn preemption exists), low-severity was held and
    delivered after the turn; each injection fired
    `.agents/skills/cli-escalation-notify/SKILL.md`.
[ ] Only environment issues were treated as blockers; everything else was
    pushed through with the executor's help.
[ ] On environment blockers the loop paused and asked the user — no skipping,
    no fabricated success.
[ ] The executor pushed the branch once at the end; no protected branch was
    pushed; no PR opened unless the user asked.
