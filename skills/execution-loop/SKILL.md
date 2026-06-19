---
name: execution-loop
description: Execute one or more already-written plans end-to-end by dispatching a dedicated subagent per plan, each running via the /implementation-loop skill. First validates that every plan is understood, that all files they reference exist and are reachable, and that every harness they need is present (compile, unit-test, e2e, git-conventions, coding-conventions). Then runs the plans strictly one at a time — one subagent per plan, each told to drive its whole plan until all of its pass criteria pass and to commit when done — waiting for each to finish before starting the next. Fires cli-escalation-notify on every event (plan start, plan finish, problems) and pushes the branch at the end. Use when the user says "execute these plans", "run the execution-loop", "execute plan X and Y", or hands over a set of plan files to be carried out.
---

# Execution Loop

## Goal
Carry a set of approved plans to completion: validate they are executable
up front, then dispatch one subagent per plan (strictly sequential) that
drives its entire plan until every pass criterion is met and commits the
result. The executor itself orchestrates, notifies, helps subagents past
genuine blockers, and pushes the branch at the end. It does **not** write
plan code itself — the per-plan subagents do. While a subagent runs the
executor **waits idle** — it does **not** read the subagent's output or
transcript (that would burn its context for no benefit) — doing nothing until
the subagent finishes or the user intervenes. On a user intervention it relays
an explicitly-marked directive into the running subagent via the Task tool's
`resume`, steering the same subagent without losing its context (see
[Directive injection](#directive-injection)).

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
- `.ai/skills/common/implementation-loop/SKILL.md` — each per-plan subagent runs this skill to drive its
  plan (implement → review → fix) until exit criteria are met.
- `.ai/skills/common/git-conventions/SKILL.md` — branch name, commit message,
  and PR/push rules. The work branch and every subagent commit must match it.
- `.agents/skills/cli-escalation-notify/SKILL.md` — fired on every event (plan start, plan finish,
  problems). Pushes a Slack DM in CLI context, no-ops in the IDE, never fatal.

## Hard invariants
- **No plan authoring here.** The executor orchestrates and pushes. All
  implementation, compiling, and testing happen inside the per-plan subagents.
- **Validation is delegated and read-only.** Stage 1 runs in a single `explore`
  subagent that reads the plans and probes the filesystem and returns a compact
  report; the main loop never reads plans or checks files itself — it works from
  the report alone. The one-subagent-per-plan / never-specialized-type rule
  below governs the **execution** subagents only.
- **One subagent per plan; one plan at a time.** Each subagent runs the
  `.ai/skills/common/implementation-loop/SKILL.md` skill. Dispatch it via the Task tool with
  `subagent_type: generalPurpose` (the general agent able to run the full
  implement → review → fix → test loop), passing the whole
  [Subagent prompt](#subagent-prompt) verbatim as the Task `prompt`. **Never**
  use a specialized subagent type (`implementer-*`, `reviewer-*`, `explore`,
  etc.) — they are narrower than the loop needs and can drop/hide the prompt.
  Dispatch it with `run_in_background: true` and capture its agent id
  immediately (needed to resume/steer the same subagent). The executor then
  **waits idle without reading the subagent's output/transcript** — see
  [Directive injection](#directive-injection). Still strictly one plan at a
  time: do not start the next plan's subagent until the current one has finished
  and its commit is confirmed by git evidence. Parallel only on explicit user
  opt-in.
- **Never re-scope a plan.** One plan file = exactly one subagent, regardless
  of how many files, tests, or sections (including a phase-2 refactor) it
  spans. The executor does not reclassify a plan as an "epic", split it into
  stages, or implement it itself — plan size is irrelevant to the
  one-subagent-per-plan rule.
- **Short prompts; reference the harness by file.** The subagent prompt states
  the goal, **points to** the harness/coding/commit docs it must read and follow
  (as a bare `label: path` — never transcribe their commands and never add a
  parenthetical describing a doc's contents), and the status of related plans —
  nothing more. **Do not restate, summarize, or re-enumerate anything already in
  the plan or the harness docs** (pass criteria, phases, non-goals, steps,
  commands, internal mechanics), and **do not list example blockers or example
  commands**. No step-by-step micromanagement.
- **Only environment issues are blockers.** A real blocker is an environment
  problem that stops an agent from using tools, editing code, compiling, or
  testing. Anything else (failing logic, wrong test, unclear step) is normal
  work the subagent — with the executor's help — must push through.
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

**Gate:** No open questions; the work branch is checked out on the confirmed
parent and the tree is clean.

### Stage 3: Execute — one subagent per plan
For each plan in order (sequential unless the user opted into parallel):

1. **Notify start** — dispatch `.agents/skills/cli-escalation-notify/SKILL.md`
   (`title: execution-loop — starting plan <name>`).
2. **Record the baseline** — capture the branch HEAD with `git rev-parse HEAD`
   so the subagent's commit can be confirmed objectively afterwards.
3. **Dispatch one subagent** via the Task tool with
   `subagent_type: generalPurpose` and `run_in_background: true` that runs
   the `.ai/skills/common/implementation-loop/SKILL.md` skill, passing the
   [Subagent prompt](#subagent-prompt) verbatim as the Task `prompt`. Do
   **not** pick a specialized subagent type. Reference only the harness that
   *this* plan needs, and state which related plans are already done (with
   commit sha) or in progress. **Capture the returned agent id** — it is needed
   to resume/steer the same subagent (see
   [Directive injection](#directive-injection) and Blocker policy).
4. **Wait idle for completion.** Do nothing until the subagent finishes or the
   user intervenes with a directive — do **not** read the subagent's
   output/transcript while it runs (conserve the executor's context); just await
   its completion (see [Directive injection](#directive-injection)). Do not
   start the next plan until the current subagent has finished.
5. **On finish — confirm by evidence, not by self-report.** Check that
   `git status --porcelain` is empty and that HEAD advanced past the baseline
   from step 2; capture the new sha with `git rev-parse HEAD`. If the tree is
   dirty or HEAD did not move, the plan is **not** done — resume the same
   subagent (see Blocker policy) to commit/finish; do not advance. Once
   confirmed, dispatch `.agents/skills/cli-escalation-notify/SKILL.md`
   (`title: execution-loop — finished plan <name>`).
6. **On a reported problem** — apply the [Blocker policy](#blocker-policy).

**Gate:** The working tree is clean and HEAD advanced past the baseline (a real
commit landed) for the current plan before the next plan's subagent is
dispatched.

### Stage 4: Finalize
1. After every plan has passed and committed, the executor **pushes** the work
   branch: `git push -u origin <branch>` (never a protected branch — see
   `.ai/skills/common/git-conventions/SKILL.md`).
2. Dispatch `.agents/skills/cli-escalation-notify/SKILL.md` (`title: execution-loop — run complete`).
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

## Subagent prompt
Dispatch with the Task tool, `subagent_type: generalPurpose`, and pass the text
below as the Task `prompt` (never leave it empty; never use a specialized
subagent type). Keep it tight. Fill the bracketed fields from Stage 1/3; drop
harness lines that this plan does not use.

```
Run the .ai/skills/common/implementation-loop/SKILL.md skill to execute the
entire plan at <PLAN_PATH>.
Use the plan's pass/acceptance criteria as your acceptance criteria (read them
from the plan). If the plan has no explicit, testable pass criteria, stop and
report it.

Read and follow these files:
- Build / unit / e2e harness: <harness doc path(s)>
- Coding conventions: <coding-rules doc path(s)>
- Commit conventions: .ai/skills/common/git-conventions/SKILL.md; commit on
  branch <BRANCH>.

Commit your work on <BRANCH> before finishing, with a clean tree; do not push
or open a PR.

Related plans: <none | "<plan> done (commit <sha>)" | "<plan> in progress">.

Only stop early for a real environment blocker (infra prevents using tools,
editing, compiling, or testing); report it verbatim. Otherwise keep working
through failures until the criteria pass and you have committed.
```

## Directive injection
While a per-plan subagent runs in the background, the executor can steer it by
sending an additional instruction to the **current** subagent mid-run via the
Task tool's `resume: <captured agent id>` (Stage 3 step 3). This is the same
subagent with its full context preserved — **never a fresh dispatch** for
steering, and never a specialized subagent type.

- **Trigger (user only, during the run):** the executor does **not** watch the
  subagent while it runs — it waits idle and reads nothing. Mid-run injection
  therefore happens only when the **user** types a message to the main loop
  while a subagent is running. (The executor still resumes the same subagent
  after it finishes when the evidence gate or a reported problem requires it —
  see Stage 3 step 5 and Blocker policy — but that is post-finish, not from
  watching the transcript.)
- **User routing (explicit marker only):** relay a user message to the subagent
  **only** when it is explicitly addressed to the subagent — it starts with
  `subagent:` or "tell the subagent ...". Any other user message is guidance to
  the executor itself; handle it normally and **do not** forward it.
- **Severity-based delivery:**
  - **High severity** (course-correction, stop, "wrong direction", anything
    that would otherwise waste significant work): `resume` with
    `interrupt: true` to preempt the subagent and deliver the directive
    immediately.
  - **Low severity** (optional hint or extra context): **hold** it and deliver
    it via a plain `resume` (no `interrupt`) when the subagent next completes or
    pauses. Do not preempt for low-severity work, and do not attempt a plain
    `resume` against a still-running subagent — it fails; only `interrupt: true`
    reaches a running subagent. If the subagent finishes the plan before a held
    hint is delivered, the hint is moot — drop it.
- **Notify:** on each injection, dispatch
  `.agents/skills/cli-escalation-notify/SKILL.md`
  (`title: execution-loop — directive injected: <plan>`).
- **Evidence still rules:** injection never substitutes for completion
  evidence. After injecting, resume waiting idle; the plan is "done" only on a
  clean tree + HEAD advanced past the baseline (Stage 3 step 5).

## Blocker policy
When a subagent reports a problem, the executor decides:
- **Not an environment issue** (failing logic, flaky test, unclear step): this
  is normal work. Help the subagent — clarify, point at the right harness/doc,
  or resume **the same subagent** via the Task tool's `resume` (the agent id
  kept in Stage 3 step 3), with `interrupt` per the severity rules in
  [Directive injection](#directive-injection), never a fresh dispatch — and let
  it keep going until the pass criteria are met. This is not a blocker.
- **Real environment issue** the subagent and the executor cannot fix (tools
  unavailable, cannot compile, cannot test, infra down): dispatch
  `.agents/skills/cli-escalation-notify/SKILL.md` (`title: execution-loop — blocked: <reason>`), then **pause
  the loop and ask the user**. Do not skip the plan or fabricate success.

## Halt conditions
Stop and surface to the user (with a CLI notify) when:
- A plan cannot be loaded or has no testable pass criteria.
- A referenced file is unreachable, a required harness is absent, or a
  companion skill cannot be resolved to a readable `SKILL.md` (Stage 1).
- The workspace is dirty and the user has not chosen how to proceed.
- A real environment blocker hits a subagent and cannot be fixed.

## Output format
End the run with a concise report:

```yaml
execution_loop_report:
  branch: <work-branch>
  parent: <parent-branch>
  parallel: false|true
  plans:
    - plan: <path>
      pass_criteria: met|blocked
      commit: <sha-or-none>
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
[ ] Exactly one subagent was dispatched per plan via the Task tool with
    `subagent_type: generalPurpose` (no specialized types), the full prompt
    passed verbatim, each running the `.ai/skills/common/implementation-loop/SKILL.md` skill,
    `run_in_background: true` with the agent id captured up front, strictly
    sequential (parallel only on explicit user opt-in).
[ ] No plan was reclassified as an "epic", split into stages, or implemented
    by the executor itself — one plan file mapped to exactly one subagent.
[ ] Each subagent prompt was short: goal + harness referenced **by file path**
    (bare `label: path`, never transcribed commands, no content-describing
    parentheticals) + related-plan status; nothing already in the plan or docs
    was restated (pass criteria, phases, non-goals, steps); no example blockers
    or commands; no step-by-step micromanagement.
[ ] The executor waited for each subagent and confirmed by git evidence (clean
    tree + HEAD advanced past the recorded baseline) that a real commit landed
    before dispatching the next — not by trusting the subagent's report.
[ ] Execution order matched the user-supplied order; no implicit reordering (a
    different order or a parallel run happened only on explicit user request).
[ ] `.agents/skills/cli-escalation-notify/SKILL.md` fired on plan start, plan finish, and every
    problem.
[ ] While each subagent ran, the executor waited idle and did **not** read the
    subagent's output/transcript — it acted only on the subagent finishing or a
    user intervention.
[ ] Directives were injected into the **same** subagent via the Task tool's
    `resume` (the captured agent id), never a fresh dispatch; mid-run injection
    was user-triggered and relayed only on the explicit marker (`subagent:` /
    "tell the subagent ..."); delivery was severity-based (`interrupt: true` to
    preempt vs. held and delivered via plain `resume` at the next
    completion/pause); each injection fired
    `.agents/skills/cli-escalation-notify/SKILL.md`.
[ ] Only environment issues were treated as blockers; everything else was
    pushed through with the executor's help.
[ ] On environment blockers the loop paused and asked the user — no skipping,
    no fabricated success.
[ ] The executor pushed the branch once at the end; no protected branch was
    pushed; no PR opened unless the user asked.
