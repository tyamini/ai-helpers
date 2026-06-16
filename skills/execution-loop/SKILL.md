---
name: execution-loop
description: Execute one or more already-written plans end-to-end by dispatching a dedicated subagent per plan, each running via the /implementation-loop skill. First validates that every plan is understood, that all files they reference exist and are reachable, and that every harness they need is present (compile, unit-test, e2e, git-conventions, coding-conventions). Then runs the plans strictly one at a time — one subagent per plan, each told to drive its whole plan until all of its pass criteria pass and to commit when done — waiting for each to finish before starting the next. Fires cli-escalation-notify on every event (plan start, plan finish, problems) and pushes the branch at the end. Use when the user says "execute these plans", "run the execution-loop", "execute plan X and Y", or hands over a set of plan files to be carried out.
disable-model-invocation: true
---

# Execution Loop

## Goal
Carry a set of approved plans to completion: validate they are executable
up front, then dispatch one subagent per plan (strictly sequential) that
drives its entire plan until every pass criterion is met and commits the
result. The executor itself orchestrates, notifies, helps subagents past
genuine blockers, and pushes the branch at the end. It does **not** write
plan code itself — the per-plan subagents do.

## Inputs
- **Plans** — one or more plan files (paths under `.ai/plans/` or any file
  the user supplies). At least one is required. Order matters: later plans
  may depend on earlier ones.
- **Parent branch** (optional) — the branch the work sits on top of. Defaults
  to the current branch; confirm with the user during validation.
- **Parallelism** — sequential by default. Run plans in parallel **only**
  when the user explicitly says so. Never assume parallelism is safe; if the
  user opts in, the consequences (e.g. concurrent commits to the same branch)
  are the user's responsibility, not something this skill works around.

## Companion skills (read once, up front)
- `implementation-loop` — each per-plan subagent runs this skill to drive its
  plan (implement → review → fix) until exit criteria are met.
- `.ai/skills/common/git-conventions/SKILL.md` — branch name, commit message,
  and PR/push rules. The work branch and every subagent commit must match it.
- `cli-escalation-notify` — fired on every event (plan start, plan finish,
  problems). Pushes a Slack DM in CLI context, no-ops in the IDE, never fatal.

## Hard invariants
- **No plan authoring here.** The executor reads, validates, orchestrates, and
  pushes. All implementation, compiling, and testing happen inside the
  per-plan subagents.
- **One subagent per plan; one plan at a time.** Each subagent runs the
  `/implementation-loop` skill. Dispatch it via the Task tool with
  `subagent_type: generalPurpose` (the general agent able to run the full
  implement → review → fix → test loop), passing the whole
  [Subagent prompt](#subagent-prompt) verbatim as the Task `prompt`. **Never**
  use a specialized subagent type (`implementer-*`, `reviewer-*`, `explore`,
  etc.) — they are narrower than the loop needs and can drop/hide the prompt.
  Dispatch it blocking (wait for completion). Do not start the next plan's
  subagent until the current one has finished. Parallel only on explicit user
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
  `cli-escalation-notify`; it never composes Slack itself.

## Workflow

### Stage 1: Analyze and validate
For **every** plan, before any execution:
1. Read the plan in full. Extract: its **pass / acceptance criteria**, every
   **file / path it references**, the **harness** it needs (compile, unit
   tests, e2e, coding conventions), and any **dependency** on other plans in
   the set.
2. **Files reachable:** verify each referenced file/path exists and is
   readable. Record any missing ones.
3. **Harness present:** identify the harness **reference doc(s)** each plan
   needs (build / unit-test / e2e, coding conventions) and confirm they exist
   and are reachable, plus the commit conventions (`git-conventions`). Take the
   harness docs **only from the user's input for this run or from the plan** —
   do not discover them from repo workflow/conventions indexes. Reference the
   **minimal** set: when a subsystem-specific doc already covers a harness, do
   not add a generic one on top. Record the **paths** only — not transcribed
   commands. The subagent reads them itself.
4. **Companion skills reachable:** resolve every skill listed under
   [Companion skills](#companion-skills-read-once-up-front) to its real file —
   **follow symlinks** (a symlinked skill dir is installed, not missing; do not
   judge presence with a glob that skips symlinks) — and confirm each
   `SKILL.md` exists and is readable. This includes `cli-escalation-notify`:
   being installed is a separate fact from its run-time "never fatal" send
   behavior, so a companion skill that cannot be located up front is a Stage 1
   finding, not a silent best-effort skip.
5. **Ordering:** the execution order is exactly the order the user supplied the
   plans. Do **not** infer or reorder based on dependencies. Record any
   dependency you notice as information only; use a different order solely when
   the user explicitly asked for one (or explicitly allowed parallel).

**Gate:** Every plan understood; all referenced files exist and are reachable;
each required harness doc/file is identified and reachable (paths only — never
transcribed commands); every companion skill resolves to a readable `SKILL.md`;
the execution order equals the user-supplied order (unchanged unless the user
specified otherwise).

### Stage 2: Clarify and set up
1. **Clarify gaps (should be rare).** If Stage 1 found anything missing or
   ambiguous — unreachable files, an unavailable harness, a plan with no
   testable pass criteria, or a real knowledge gap — present a single
   consolidated set of questions to the user and wait. In CLI context, first
   dispatch `cli-escalation-notify` (`title: execution-loop — clarification needed`)
   as a heads-up; the local question remains the answer channel.
2. **Work branch.** Confirm the parent branch with the user (default: current),
   then create the run's work branch per `git-conventions`. Verify the
   workspace is clean (`git status --porcelain`); if dirty, stop and ask.

**Gate:** No open questions; the work branch is checked out on the confirmed
parent and the tree is clean.

### Stage 3: Execute — one subagent per plan
For each plan in order (sequential unless the user opted into parallel):

1. **Notify start** — dispatch `cli-escalation-notify`
   (`title: execution-loop — starting plan <name>`).
2. **Record the baseline** — capture the branch HEAD with `git rev-parse HEAD`
   so the subagent's commit can be confirmed objectively afterwards.
3. **Dispatch one subagent** via the Task tool with
   `subagent_type: generalPurpose` (blocking; wait for completion) that runs
   the `/implementation-loop` skill, passing the
   [Subagent prompt](#subagent-prompt) verbatim as the Task `prompt`. Do
   **not** pick a specialized subagent type. Reference only the harness that
   *this* plan needs, and state which related plans are already done (with
   commit sha) or in progress. Keep the subagent's agent id — you may need it
   to resume the same subagent (see Blocker policy).
4. **Wait** for the subagent to finish. Do not start the next plan until it
   returns.
5. **On finish — confirm by evidence, not by self-report.** Check that
   `git status --porcelain` is empty and that HEAD advanced past the baseline
   from step 2; capture the new sha with `git rev-parse HEAD`. If the tree is
   dirty or HEAD did not move, the plan is **not** done — resume the same
   subagent (see Blocker policy) to commit/finish; do not advance. Once
   confirmed, dispatch `cli-escalation-notify`
   (`title: execution-loop — finished plan <name>`).
6. **On a reported problem** — apply the [Blocker policy](#blocker-policy).

**Gate:** The working tree is clean and HEAD advanced past the baseline (a real
commit landed) for the current plan before the next plan's subagent is
dispatched.

### Stage 4: Finalize
1. After every plan has passed and committed, the executor **pushes** the work
   branch: `git push -u origin <branch>` (never a protected branch — see
   `git-conventions`).
2. Dispatch `cli-escalation-notify` (`title: execution-loop — run complete`).
3. Print the run report (see Output format). Do **not** open a PR unless the
   user asked for one.

**Gate:** The branch is pushed and the run report is printed.

## Subagent prompt
Dispatch with the Task tool, `subagent_type: generalPurpose`, and pass the text
below as the Task `prompt` (never leave it empty; never use a specialized
subagent type). Keep it tight. Fill the bracketed fields from Stage 1/3; drop
harness lines that this plan does not use.

```
Run the /implementation-loop skill to execute the entire plan at <PLAN_PATH>.
Use the plan's pass/acceptance criteria as your acceptance criteria (read them
from the plan). If the plan has no explicit, testable pass criteria, stop and
report it.

Read and follow these files:
- Build / unit / e2e harness: <harness doc path(s)>
- Coding conventions: <coding-rules doc path(s)>
- Commit conventions: <git-conventions path>; commit on branch <BRANCH>.

Commit your work on <BRANCH> before finishing, with a clean tree; do not push
or open a PR.

Related plans: <none | "<plan> done (commit <sha>)" | "<plan> in progress">.

Only stop early for a real environment blocker (infra prevents using tools,
editing, compiling, or testing); report it verbatim. Otherwise keep working
through failures until the criteria pass and you have committed.
```

## Blocker policy
When a subagent reports a problem, the executor decides:
- **Not an environment issue** (failing logic, flaky test, unclear step): this
  is normal work. Help the subagent — clarify, point at the right harness/doc,
  or resume **the same subagent** via the Task tool's `resume` (the agent id
  kept in Stage 3 step 3), never a fresh dispatch — and let it keep going until
  the pass criteria are met. This is not a blocker.
- **Real environment issue** the subagent and the executor cannot fix (tools
  unavailable, cannot compile, cannot test, infra down): dispatch
  `cli-escalation-notify` (`title: execution-loop — blocked: <reason>`), then **pause
  the loop and ask the user**. Do not skip the plan or fabricate success.

## Halt conditions
Stop and surface to the user (with a CLI notify) when:
- A plan cannot be loaded or has no testable pass criteria.
- A referenced file is unreachable or a required harness is absent (Stage 1).
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
[ ] Every plan was read in full; pass criteria, referenced files, needed
    harness, and inter-plan dependencies were extracted.
[ ] All referenced files were verified reachable; every required harness
    doc/file path (taken only from the user's input or the plan, minimal set)
    was identified and verified reachable before any execution (paths only —
    commands are never transcribed).
[ ] Clarification questions (if any) were consolidated into one ask; the
    common case asked nothing.
[ ] A single work branch was created per `git-conventions` on the confirmed
    parent; the tree was clean before starting.
[ ] Exactly one subagent was dispatched per plan via the Task tool with
    `subagent_type: generalPurpose` (no specialized types), the full prompt
    passed verbatim, each running the `/implementation-loop` skill, blocking,
    strictly sequential (parallel only on explicit user opt-in).
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
[ ] `cli-escalation-notify` fired on plan start, plan finish, and every
    problem.
[ ] Only environment issues were treated as blockers; everything else was
    pushed through with the executor's help.
[ ] On environment blockers the loop paused and asked the user — no skipping,
    no fabricated success.
[ ] The executor pushed the branch once at the end; no protected branch was
    pushed; no PR opened unless the user asked.
