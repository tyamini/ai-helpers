---
name: kanban-execution-loop
description: Turn a set of already-written plans plus a Claude harness reference into a linked, auto-advancing Kanban task pipeline on the board — instead of executing the plans inline like execution-loop. First validates (read-only) that every plan is understood, reachable, and committed to the base branch, and that the Claude harness docs exist. Then creates one chain of linked board tasks — phase-1 harness/plan validation, then per plan an execute task and a validate task, then a finalize task that cleans up and opens the PR — all running with the Claude agent, all sharing one work branch, wired so each task auto-completes and auto-starts the next. Use when the user says "build a kanban pipeline for these plans", "run the kanban-execution-loop", "put these plans on the board and link them", or hands over a set of plan files to be carried out on the Kanban board rather than in tmux.
---

# Kanban Execution Loop

## Goal
Take the **same inputs** as `execution-loop` (one or more approved plans + a
harness reference) but, instead of dispatching a `cursor-agent` per plan, **build
a linked Kanban task pipeline** that the board runs autonomously. The end result
is a chain of board cards: a phase-1 harness/plan validation task, then for each
plan an **execute** task and a **validate** task, then a **finalize** task that
cleans up and opens the PR — every card linked so finishing one auto-starts the
next.

This skill **authors and wires the board, then stops.** It does not babysit the
run: once the chain is built and the first task started, the board's auto-review
+ linked-auto-start machinery drives the whole pipeline. All board tasks run with
the **Claude** agent (`--agent-id claude`) and reference the **Claude** harness —
never `cursor-agent`.

## Inputs
- **Plans** — a plans directory or an explicit ordered plan list. At least one is
  required. Order matters: later plans may depend on earlier ones. The plans must
  be reachable inside a fresh worktree of the work branch (see
  [Branch / worktree model](#branch--worktree-model)) — i.e. committed to that
  branch, or absolute paths outside the repo.
- **Harness reference (Claude)** — the build / unit / e2e harness doc(s) plus
  coding-conventions doc(s) the tasks must read and follow. Referenced **by path**
  only, never transcribed. Must be the Claude harness, not a cursor one.
- **Parent branch** (optional) — the branch the work sits on top of. Defaults to
  the current branch; confirm during setup.
- **Project path** (optional) — the Kanban project (main repo) the board belongs
  to. Defaults to the current repo (`git rev-parse --path-format=absolute
  --git-common-dir` with the trailing `/.git` stripped).
- **Model / agent** — default `--agent-id claude`.

## Companion skills (read once, up front)
- `.ai/skills/common/implementation-loop/SKILL.md` — each **execute** task runs
  this skill (via the Claude agent) to drive its plan until the plan's pass
  criteria are met.
- `.ai/skills/common/git-conventions/SKILL.md` — work-branch name and PR rules.
  The shared work branch and the finalize PR must match it.
- `.ai/skills/common/plan-validator/SKILL.md` — reference for what a plan's pass
  criteria look like; the **validate** tasks verify against them.
- `~/.drivenets/cheetah/AI/v2/private/skills/cli-escalation-notify/SKILL.md` —
  fired only on pipeline-built and on a Stage-1/2 halt. Per-task telemetry comes
  from the board tasks themselves, so keep this skill's own notify light.

## Companion scripts and references (in this skill dir)
- `scripts/build_pipeline.py` — the one mechanical action: given the fully
  composed task list + link edges (JSON in), it creates every task, links them,
  optionally starts the first, and emits the chain (JSON out). Run, do not
  hand-roll `kanban` commands inline.
- `references/pipeline.md` — the chain shape, auto-review modes, the shared
  work-branch / worktree model, and the gotchas.
- `references/task-prompts.md` — the per-task Claude prompt templates.

## Hard invariants
- **No plan authoring, no inline execution here.** This skill validates, sets up
  a work branch, and builds the board. All implementing, compiling, testing, and
  committing happen inside the board tasks (Claude agents), later, on their own.
- **Deterministic script owns all board mutations.** Task creation, linking, and
  the initial start happen in `scripts/build_pipeline.py` with JSON in/out. The
  skill invokes it and consumes its JSON — it never hand-rolls `kanban task
  create` / `kanban task link` inline.
- **Validation is delegated and read-only.** Stage 1 runs in a single `explore`
  Task subagent that reads the plans and probes the filesystem and returns a
  compact report; the main loop never reads plans or checks files itself.
- **One shared work branch for the whole pipeline.** Every task is created with
  `--base-ref <work-branch>`. This is what makes the plans accumulate — see
  [Branch / worktree model](#branch--worktree-model). Never give tasks different
  base refs.
- **Auto-review, not explicit commits.** Execute and validate tasks use
  `--auto-review-mode commit`; auto-review commits their work and cherry-picks it
  onto the work branch. Verification-only tasks (phase-1 validate, finalize) use
  `--auto-review-mode done`. **Do not** tell any task to commit in its prompt —
  that is the opposite of `execution-loop`, where the agent commits itself.
- **Short prompts; reference the harness by file.** Each task prompt states the
  goal, **points to** the Claude harness / coding-conventions docs it must read
  (bare `label: path`, never transcribed commands, no content-describing
  parentheticals), and nothing already in the plan or those docs. No step-by-step
  micromanagement.
- **Validators drive to green themselves.** The board has no pass/fail gate —
  moving a card to Done always advances the chain. So a validate task must
  independently confirm its plan is actually done and, if not, fix and commit
  until green; it escalates only on a real environment blocker. Never rely on the
  board to "stop on failure."
- **Notify, never author content twice.** All Slack goes through
  `cli-escalation-notify`; this skill never composes Slack itself.

## Branch / worktree model
Kanban tasks each run in a **detached-HEAD worktree off their stored `baseRef`**.
A linked successor auto-starts using its **own** `baseRef` (there is no branch
inheritance), and commit-mode auto-review makes the task's Claude agent
**cherry-pick its commit onto the `base_ref` branch**. Therefore, to make the
plans accumulate into one PR:

- **All tasks share ONE work branch as `--base-ref`.** Each task's commit lands
  on that branch; the next task's worktree — created only after the previous task
  finishes — is detached at the branch tip and so includes all prior work.
- **The main repo is left checked out on the work branch** so commit mode's "find
  where `{{base_ref}}` is checked out" step has a target worktree to cherry-pick
  into.
- **Plans + harness docs must be reachable inside a fresh worktree of the work
  branch** — committed to it, or absolute paths outside the repo. Phase-1 validate
  enforces this.

See `references/pipeline.md` for the full rationale and the source behavior it is
derived from.

## Pipeline shape
For N plans the skill builds this chain (each edge = "waiter waits on
prerequisite"; the prerequisite finishing auto-starts the waiter):

```
phase-1-validate           (done)    read-only: plans reachable + harness present
      -> plan-01-exec      (commit)  run implementation-loop on plan 01
      -> plan-01-validate  (commit)  verify plan 01 is actually done; fix if not
      -> plan-02-exec      (commit)
      -> plan-02-validate  (commit)
      -> ...
      -> finalize          (done)    cleanup + push work branch + open PR
```

- **phase-1-validate** — read-only; makes no git changes → `done` mode (auto-
  completes and auto-starts plan-01-exec).
- **plan-NN-exec** — runs the Claude `implementation-loop` skill on that one plan
  → `commit` mode (auto-review commits + cherry-picks onto the work branch).
- **plan-NN-validate** — independently verifies plan NN is actually done on the
  accumulated branch (the board analog of `execution-loop`'s `exec_collect.py`
  green gate); fixes and commits if not → `commit` mode (which also auto-dones on
  zero changes, so a clean pass still advances).
- **finalize** — cleanup + `git push` the work branch + open the PR per
  git-conventions → `done` mode (its prompt performs the push/PR).

## Workflow

### Stage 1: Analyze and validate (delegated to a subagent)
Dispatch **one** read-only validation subagent (Task tool, `subagent_type:
explore`, blocking; wait for it) that performs every check below and returns only
the compact report in [Validation subagent](#validation-subagent). The main loop
consumes that report and reads nothing else here.

The subagent must, for **every** plan, in the user-supplied order:
1. Read the plan in full and extract its **pass / acceptance criteria**, the
   **files / paths it references**, the **Claude harness** it needs, and any
   **dependency** on the other plans.
2. **Files reachable and committed:** verify each plan file and referenced path
   exists, is readable, and is **committed to the intended base branch** (or is an
   absolute path outside the repo) so a fresh worktree will contain it; list any
   that are missing or only present as uncommitted working-tree files.
3. **Harness present:** confirm the Claude harness / coding-conventions doc(s) —
   taken only from the user's input for this run or from the plan — exist and are
   reachable. Record **paths** only.
4. **Companion skills reachable:** resolve every skill under
   [Companion skills](#companion-skills-read-once-up-front) to its real
   `SKILL.md`, following symlinks; flag any that cannot be located.
5. **Ordering:** keep the user-supplied order verbatim; record dependencies as
   information only.

**Gate:** every plan is understood; all referenced files and harness docs are
reachable and committed to the base branch; every companion skill resolved; order
unchanged. If the report lists any problem, proceed to Stage 2 to clarify or
halt.

### Stage 2: Clarify and set up
1. **Clarify gaps (should be rare).** If the report lists any problem —
   unreachable/uncommitted files, an absent harness, an unresolvable companion
   skill, or a plan with no testable pass criteria — present a single consolidated
   set of questions and wait. In CLI context, first fire `cli-escalation-notify`
   (`title: kanban-execution-loop — clarification needed`) as a heads-up; the
   local question remains the answer channel.
2. **Confirm the parent branch** (default: current), then **create the shared work
   branch** per `.ai/skills/common/git-conventions/SKILL.md` off that parent, in
   the main repo. Verify the tree is clean (`git status --porcelain`); if dirty,
   stop and ask.
3. **Leave the main repo checked out on the work branch** (do not switch away)
   so commit-mode tasks have a worktree to cherry-pick into.

**Gate:** no open questions; the work branch is checked out on the confirmed
parent in the main repo, and the tree is clean.

### Stage 3: Build the pipeline
1. Compose the task list for phase-1-validate, each plan's exec + validate, and
   finalize, filling the templates in `references/task-prompts.md` (goal + Claude
   harness paths + plan path). Assign slugs: `phase-1-validate`,
   `plan-NN-exec`, `plan-NN-validate` (NN = zero-padded plan index), `finalize`.
2. Build the link edges as `[waiter_slug, prereq_slug]` in chain order.
3. Invoke `scripts/build_pipeline.py` **once**, piping JSON:
   `{project_path, work_branch, agent_id: "claude", model, tasks, links,
   start_slug: "phase-1-validate"}`. It creates every task, links them, starts
   phase-1-validate, and returns the chain. If it returns `ok: false`, halt and
   surface the error.

**Gate:** the script returned `ok: true` with an id for every task and a
dependency id for every link, and phase-1-validate is started (in progress).

### Stage 4: Report
1. Print the created chain (slugs, task ids, links, auto-review modes, work
   branch, which task was started).
2. Fire `cli-escalation-notify` (`title: kanban-execution-loop — pipeline built`,
   body = the chain summary + work branch). Do not open a PR here — the finalize
   task does that when the pipeline completes.

**Gate:** the chain report is printed.

## Validation subagent
Dispatch with the Task tool, `subagent_type: explore` (read-only), blocking. Pass
the text below verbatim, filling the bracketed fields.

```
Validate these kanban-execution-loop plans (read-only — do not edit anything).

Plans, in this exact order:
<one plan path per line>

Intended base branch for the pipeline worktrees: <parent branch, or "current">

For every plan:
- Read it in full and confirm it has explicit, testable pass/acceptance criteria.
- Verify every file/path it references exists, is readable, and is committed to
  the intended base branch (or is an absolute path outside the repo) so a fresh
  git worktree of that branch will contain it. Flag anything missing or present
  only as an uncommitted working-tree file.
- Identify the minimal Claude harness / coding-conventions doc(s) it needs, taken
  only from the plan or the inputs below (never from repo indexes), and confirm
  each exists and is readable. Record paths only; never transcribe commands.
- Note any dependency on the other plans (information only; do not reorder).

Resolve each companion skill below to a readable SKILL.md, following symlinks,
and flag any that cannot be located:
- .ai/skills/common/implementation-loop/SKILL.md
- .ai/skills/common/git-conventions/SKILL.md
- .ai/skills/common/plan-validator/SKILL.md

Claude harness / coding-rules inputs for this run: <paths, or "none — take from the plans">

Return ONLY this compact report — no plan text, no criteria prose, no commands:

validation_report:
  plans:
    - plan: <path>
      pass_criteria: testable|missing
      committed_to_base: yes|no
      harness: [<doc path(s)>]
      coding_rules: [<doc path(s)>]
      depends_on: [<plan path(s)>]   # info only
  order: [<plan paths, user-supplied order>]
  companion_skills: resolved | [<path>: unresolved]
  problems: []   # list every missing/uncommitted file, absent harness, unresolved skill, or plan lacking testable criteria
```

## Per-task prompts
The prompts the skill composes (Stage 3 step 1) live in
`references/task-prompts.md`. Fill their bracketed fields per task; keep them
tight (goal + only this task's Claude harness paths + plan path). Never restate
plan contents, never tell a task to commit.

## Halt conditions
Stop and surface to the user (with a CLI notify) when:
- A plan cannot be loaded or has no testable pass criteria.
- A referenced file is unreachable or not committed to the base branch, or a
  required harness doc is absent, or a companion skill cannot be resolved.
- The workspace is dirty and the user has not chosen how to proceed.
- `scripts/build_pipeline.py` returns `ok: false` (a `kanban` create/link/start
  failed, e.g. the runtime is unavailable).

## Output format
End with a concise report:

```yaml
kanban_execution_loop_report:
  work_branch: <work-branch>
  parent: <parent-branch>
  project_path: <main repo>
  agent: claude
  tasks:
    - slug: phase-1-validate
      id: <task-id>
      auto_review_mode: done
      column: in_progress|backlog
    - slug: plan-01-exec
      id: <task-id>
      auto_review_mode: commit
      column: backlog
    # ...
    - slug: finalize
      id: <task-id>
      auto_review_mode: done
      column: backlog
  links:
    - waiter: plan-01-exec
      prereq: phase-1-validate
      dependency_id: <dep-id>
    # ...
  started: phase-1-validate
```

## Quality bar (self-check)
[ ] Stage 1 ran in one read-only `explore` subagent that read every plan and
    returned the compact report (pass criteria, referenced files + committed-to-
    base status, needed Claude harness, inter-plan dependencies); the main loop
    consumed only that report.
[ ] All referenced files and harness docs were reachable AND committed to the base
    branch (paths only, no transcribed commands) before building.
[ ] Clarification (if any) was one consolidated ask; the common case asked nothing.
[ ] One shared work branch was created per git-conventions on the confirmed
    parent, the tree was clean, and the main repo was left checked out on it.
[ ] The board was built solely through `scripts/build_pipeline.py` (JSON in/out);
    no inline `kanban task create` / `link` / `start`.
[ ] Every task was created with `--agent-id claude` and `--base-ref <work-branch>`
    (one shared branch); exec/validate used `--auto-review-mode commit`, phase-1-
    validate and finalize used `--auto-review-mode done`.
[ ] No task prompt told the agent to commit; prompts referenced the Claude harness
    by bare `label: path` and restated nothing from the plans/docs.
[ ] The chain was linked phase-1-validate -> per-plan exec -> per-plan validate ->
    finalize (waiter waits on prereq), and phase-1-validate was started.
[ ] Validators were told to independently verify and drive to green themselves
    (no reliance on the board to stop on failure).
[ ] `cli-escalation-notify` fired on pipeline-built and on any halt; no PR was
    opened by this skill (the finalize task owns the PR).
