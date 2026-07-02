---
name: kanban-execution-loop
description: Turn a set of already-written plans plus a Claude harness reference into a linked Kanban task pipeline on the board — instead of executing the plans inline like execution-loop. This skill only builds the building blocks: it sets up one shared work branch and creates a chain of linked board tasks — phase-1 harness/plan validation, then per plan an execute task and a validate task, then a finalize task that cleans up and opens the PR — all running with the Claude agent, all sharing that work branch, wired so finishing one task auto-starts the next. It does NOT verify the harness/plans itself (the phase-1 task does that) and it starts nothing — every task is left ready in the backlog for the user to kick off. Use when the user says "build a kanban pipeline for these plans", "run the kanban-execution-loop", "put these plans on the board and link them", or hands over a set of plan files to be carried out on the Kanban board rather than in tmux.
---

# Kanban Execution Loop

## Goal
Take the **same inputs** as `execution-loop` (one or more approved plans + a
harness reference) but, instead of dispatching a `cursor-agent` per plan, **build
a linked Kanban task pipeline** on the board. The end result is a chain of board
cards: a phase-1 harness/plan validation task, then for each plan an **execute**
task and a **validate** task, then a **finalize** task that cleans up and opens
the PR — every card linked so finishing one auto-starts the next.

This skill **only builds the building blocks, then stops.** It sets up the shared
work branch and creates + links the tasks. It does **not** do the tasks' work: it
does not read/verify the plans or the harness (that is the phase-1 task's job),
and it **starts nothing** — every task is left ready in the backlog for the user
to start. All board tasks run with the **Claude** agent (`--agent-id claude`) and
reference the **Claude** harness — never `cursor-agent`.

## Inputs
- **Plans** — a plans directory or an explicit ordered plan list. At least one is
  required. Order matters: later plans may depend on earlier ones. For the board
  tasks to see them, the plans must be reachable inside a fresh worktree of the
  work branch (see [Branch / worktree model](#branch--worktree-model)) — i.e.
  committed to that branch, or absolute paths outside the repo. The **phase-1
  task** checks this; this skill does not.
- **Harness reference (Claude)** — the build / unit / e2e harness doc(s) plus
  coding-conventions doc(s) the tasks must read and follow. Referenced **by path**
  only, never transcribed. Must be the Claude harness, not a cursor one. This
  skill only threads the paths into the task prompts; it does not open or verify
  them.
- **Parent branch** (optional) — the branch the work sits on top of. Defaults to
  the current branch; confirm during setup.
- **Project path** (optional) — the Kanban project (main repo) the board belongs
  to. Defaults to the current repo (`git rev-parse --path-format=absolute
  --git-common-dir` with the trailing `/.git` stripped).
- **Model / agent** — default `--agent-id claude`.

## Companion skills (referenced by the tasks, not run here)
- `.ai/skills/common/implementation-loop/SKILL.md` — each **execute** task runs
  this skill (via the Claude agent) to drive its plan until the plan's pass
  criteria are met.
- `.ai/skills/common/git-conventions/SKILL.md` — work-branch name and PR rules.
  The shared work branch and the finalize PR must match it.
- `.ai/skills/common/plan-validator/SKILL.md` — reference for what a plan's pass
  criteria look like; the **validate** tasks verify against them.
- `~/.drivenets/cheetah/AI/v2/private/skills/cli-escalation-notify/SKILL.md` —
  fired only on pipeline-built and on a setup halt. Per-task telemetry comes from
  the board tasks themselves, so keep this skill's own notify light.

## Companion scripts and references (in this skill dir)
- `scripts/build_pipeline.py` — the one mechanical action: given the fully
  composed task list + link edges (JSON in), it creates every task and links them
  (it can optionally start one, but this skill does not use that), and emits the
  chain (JSON out). Run, do not hand-roll `kanban` commands inline.
- `references/pipeline.md` — the chain shape, auto-review modes, the shared
  work-branch / worktree model, and the gotchas.
- `references/task-prompts.md` — the per-task Claude prompt templates.

## Hard invariants
- **No plan authoring and no doing the tasks' work here.** This skill sets up a
  work branch and builds the board. It does **not** read or validate the plans,
  open or verify the harness, implement, compile, test, or commit — all of that
  happens inside the board tasks (Claude agents) later. The **phase-1 task** is
  the one that verifies the harness and plans.
- **Start nothing.** Every task is created into the backlog and left there. The
  skill never starts a task; the user kicks off the pipeline by starting the
  first task. Do not pass `start_slug` to the build script.
- **Deterministic script owns all board mutations.** Task creation and linking
  happen in `scripts/build_pipeline.py` with JSON in/out. The skill invokes it and
  consumes its JSON — it never hand-rolls `kanban task create` / `kanban task
  link` inline.
- **One shared work branch for the whole pipeline.** Every task is created with
  `--base-ref <work-branch>`. This is what makes the plans accumulate — see
  [Branch / worktree model](#branch--worktree-model). Never give tasks different
  base refs.
- **Auto-review, not explicit commits.** The deployed Kanban CLI only accepts
  auto-review modes `commit` and `pr` — **there is no `done` mode**. Every task in
  this pipeline uses `--auto-review-mode commit`: execute/validate tasks that
  produce changes get committed and cherry-picked onto the work branch, and a
  verification-only task (phase-1, finalize) that ends with **zero working
  changes** is auto-moved to Done by commit mode, which advances the chain. **Do
  not** tell any task to commit in its prompt — that is the opposite of
  `execution-loop`, where the agent commits itself.
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
  branch** — committed to it, or absolute paths outside the repo. The phase-1 task
  verifies this at run time.

See `references/pipeline.md` for the full rationale and the source behavior it is
derived from.

## Pipeline shape
For N plans the skill builds this chain (each edge = "waiter waits on
prerequisite"; the prerequisite finishing auto-starts the waiter). Every task uses
`--auto-review-mode commit`:

```
phase-1-validate           verify harness + plans (no changes -> auto-done)
      -> plan-01-exec       run implementation-loop on plan 01
      -> plan-01-validate   verify plan 01 is actually done; fix if not
      -> plan-02-exec
      -> plan-02-validate
      -> ...
      -> finalize           cleanup + push work branch + open PR
```

- **phase-1-validate** — verifies the harness and that every plan is
  reachable/committed with testable criteria; read-only, so it ends with zero
  changes and commit mode auto-moves it to Done (which auto-starts plan-01-exec
  once the user has started the pipeline).
- **plan-NN-exec** — runs the Claude `implementation-loop` skill on that one plan;
  auto-review commits + cherry-picks onto the work branch.
- **plan-NN-validate** — independently verifies plan NN is actually done on the
  accumulated branch (the board analog of `execution-loop`'s `exec_collect.py`
  green gate); fixes and commits if not. If it changed nothing, commit mode still
  auto-moves the clean review to Done, so a pure pass also advances.
- **finalize** — cleanup + `git push` the work branch + open the PR per
  git-conventions (its prompt performs the push/PR; it makes no working-tree
  changes, so commit mode auto-moves it to Done).

## Workflow

### Stage 1: Gather inputs (no reading, no verifying)
Resolve only what is needed to compose the tasks — do **not** read the plans or
check the harness (the phase-1 task does that):
1. The ordered list of plan paths (expand a plans directory into an ordered list;
   keep an explicit list verbatim).
2. The Claude harness / coding-conventions doc path(s) to thread into the prompts.
3. The parent branch (default: current) and the project path (default: current
   repo).

**Gate:** at least one plan path, the harness path(s), and the parent/project are
known. If the user gave none of the harness paths and no plan clearly implies
them, ask once (see clarify note in Stage 2). Otherwise proceed.

### Stage 2: Set up the work branch
1. **Confirm the parent branch** (default: current), then **create the shared work
   branch** per `.ai/skills/common/git-conventions/SKILL.md` off that parent, in
   the main repo. Verify the tree is clean (`git status --porcelain`); if dirty,
   stop and ask.
2. **Leave the main repo checked out on the work branch** (do not switch away) so
   commit-mode tasks have a worktree to cherry-pick into.
3. **Clarify only if truly blocked** (missing harness paths, ambiguous plan order,
   dirty tree with no chosen resolution): present a single consolidated set of
   questions and wait. In CLI context, first fire `cli-escalation-notify`
   (`title: kanban-execution-loop — clarification needed`) as a heads-up; the
   local question remains the answer channel.

**Gate:** the work branch is checked out on the confirmed parent in the main repo
and the tree is clean.

### Stage 3: Build the pipeline
1. Compose the task list for phase-1-validate, each plan's exec + validate, and
   finalize, filling the templates in `references/task-prompts.md` (goal + Claude
   harness paths + plan path). Assign slugs: `phase-1-validate`, `plan-NN-exec`,
   `plan-NN-validate` (NN = zero-padded plan index), `finalize`. Set every task's
   `auto_review_mode` to `commit`.
2. Build the link edges as `[waiter_slug, prereq_slug]` in chain order.
3. Invoke `scripts/build_pipeline.py` **once**, piping JSON:
   `{project_path, work_branch, agent_id: "claude", model, tasks, links}` —
   **omit `start_slug`** so nothing is started. It creates every task, links them,
   and returns the chain. If it returns `ok: false`, halt and surface the error.

**Gate:** the script returned `ok: true` with an id for every task and a
dependency id for every link, and all tasks are in the backlog (none started).

### Stage 4: Report
1. Print the created chain (slugs, task ids, links, work branch). Note that all
   tasks are in the backlog and the user starts `phase-1-validate` to run the
   pipeline.
2. Fire `cli-escalation-notify` (`title: kanban-execution-loop — pipeline built`,
   body = the chain summary + work branch). Do not open a PR here — the finalize
   task does that when the pipeline completes.

**Gate:** the chain report is printed.

## Per-task prompts
The prompts the skill composes (Stage 3 step 1) live in
`references/task-prompts.md`. Fill their bracketed fields per task; keep them
tight (goal + only this task's Claude harness paths + plan path). Never restate
plan contents, never tell a task to commit.

## Halt conditions
Stop and surface to the user (with a CLI notify) when:
- No harness paths were given and none can be inferred, or the plan order is
  ambiguous, and the user has not resolved it.
- The workspace is dirty and the user has not chosen how to proceed.
- `scripts/build_pipeline.py` returns `ok: false` (a `kanban` create/link failed,
  e.g. the runtime is unavailable).

## Output format
End with a concise report:

```yaml
kanban_execution_loop_report:
  work_branch: <work-branch>
  parent: <parent-branch>
  project_path: <main repo>
  agent: claude
  auto_review_mode: commit   # all tasks
  tasks:
    - slug: phase-1-validate
      id: <task-id>
      column: backlog
    - slug: plan-01-exec
      id: <task-id>
      column: backlog
    # ...
    - slug: finalize
      id: <task-id>
      column: backlog
  links:
    - waiter: plan-01-exec
      prereq: phase-1-validate
      dependency_id: <dep-id>
    # ...
  started: none   # all tasks left in backlog; user starts phase-1-validate
```

## Quality bar (self-check)
[ ] The skill did NOT read or validate the plans or the harness, and did not
    implement/compile/test/commit — it only set up the branch and built the board.
    (Harness/plan verification is left to the phase-1 task.)
[ ] Clarification (if any) was one consolidated ask; the common case asked nothing.
[ ] One shared work branch was created per git-conventions on the confirmed
    parent, the tree was clean, and the main repo was left checked out on it.
[ ] The board was built solely through `scripts/build_pipeline.py` (JSON in/out);
    no inline `kanban task create` / `link`.
[ ] Every task was created with `--agent-id claude`, `--base-ref <work-branch>`
    (one shared branch), and `--auto-review-mode commit` (the CLI has no `done`
    mode; zero-change tasks auto-done under commit).
[ ] No task prompt told the agent to commit; prompts referenced the Claude harness
    by bare `label: path` and restated nothing from the plans/docs.
[ ] The chain was linked phase-1-validate -> per-plan exec -> per-plan validate ->
    finalize (waiter waits on prereq).
[ ] Nothing was started — `start_slug` was omitted and every task is in the
    backlog for the user to kick off.
[ ] Validators were told to independently verify and drive to green themselves
    (no reliance on the board to stop on failure).
[ ] `cli-escalation-notify` fired on pipeline-built and on any halt; no PR was
    opened by this skill (the finalize task owns the PR).
