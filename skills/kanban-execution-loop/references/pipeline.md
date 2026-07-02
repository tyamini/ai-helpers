# kanban-execution-loop pipeline model

How the linked board pipeline is shaped, why the auto-review modes are what they
are, and the branch/worktree behavior the whole design rests on. This is the
reference behind SKILL.md; read it once when the pipeline's behavior is unclear.

## The chain

For N plans the skill builds a single linear chain of board cards. Every task
uses `--auto-review-mode commit`:

```
phase-1-validate
      -> plan-01-exec
      -> plan-01-validate
      -> plan-02-exec
      -> plan-02-validate
      -> ...
      -> plan-NN-exec
      -> plan-NN-validate
      -> finalize
```

Each edge is a Kanban dependency where the **waiter waits on the prerequisite**:
`kanban task link --task-id <waiter> --linked-task-id <prereq>`. When the
prerequisite finishes review and moves to Done, the waiting backlog task
auto-starts. **The skill starts nothing** — every task is created into the
backlog and left there; the user starts `phase-1-validate` to kick off the run,
and everything after it starts itself as its predecessor completes.

Slugs: `phase-1-validate`, `plan-NN-exec`, `plan-NN-validate` (NN = zero-padded
plan index), `finalize`.

## Auto-review mode and why

The deployed Kanban CLI only accepts auto-review modes `commit` and `pr` —
**there is no `done` mode**. So every task is created with `--auto-review-enabled
true --auto-review-mode commit`. The board's auto-review is what advances the
chain, and commit mode covers both task shapes:

| Task | What commit mode does |
|------|-----------------------|
| phase-1-validate | Read-only; ends with **zero working changes**, and commit mode auto-moves a clean review straight to Done (which auto-starts plan-01-exec). |
| plan-NN-exec | Produces code. Commit mode commits the worktree changes and cherry-picks them onto the work branch, then (on the next clean review) moves to Done. |
| plan-NN-validate | May fix gaps (then commit+cherry-pick). If it changed nothing, commit mode still auto-moves the clean review to Done, so a pure PASS also advances. |
| finalize | Its prompt pushes the branch and opens the PR itself; it makes no working-tree changes, so commit mode auto-moves the clean review to Done. |

The **zero-change auto-done** behavior is specific to commit mode (a clean review
under commit mode is treated as "nothing to commit -> Done"); pr mode is
intentionally excluded from it, which is why verification-only tasks use commit,
not pr.

Key consequence: **there is no pass/fail gate on the board.** Moving a card to
Done always advances the chain. That is why validate tasks must independently
verify and drive to green themselves rather than relying on the board to stop —
see the validate template in `task-prompts.md`.

## Branch / worktree model (the crux)

Derived from the Kanban runtime source (`kanban-src`):

- A task runs in a **detached-HEAD git worktree** created from its stored
  `baseRef`: `git worktree add --detach <path> <baseRef^{commit}>`. Kanban does
  **not** create a named per-task branch. (`src/workspace/task-worktree.ts`)
- A linked successor that auto-starts uses its **own stored `baseRef`**. There is
  no branch inheritance, no stacking on the prerequisite's commit or PR head —
  only the prompt handoff can carry upstream git metadata, and that does not
  affect worktree creation. (`src/core/task-board-mutations.ts`,
  `web-ui/src/hooks/use-linked-backlog-task-actions.ts`)
- `commit`-mode auto-review does not merge anything server-side. It sends the
  task's Claude agent a prompt to commit in the detached worktree and then
  **cherry-pick that commit onto the `{{base_ref}}` branch**, found in whichever
  worktree has it checked out. (`src/config/runtime-config.ts`)
- An existing worktree is never rebased when its base advances; only missing
  worktrees are created, and a successor's worktree is created **at auto-start
  time** (after the prerequisite reached Done). (`src/workspace/task-worktree.ts`)

Putting those together, the design makes plans accumulate like this:

1. **All tasks share ONE work branch as `--base-ref`.** The skill creates that
   branch in Stage 2.
2. Each commit-mode task cherry-picks its commit onto the shared work branch.
3. The next task's worktree is created only after its predecessor finished, so it
   is detached at the (now-advanced) work-branch tip and contains all prior work.
4. `finalize` pushes that single accumulated branch and opens one PR.

## Gotchas (encode these when running the skill)

- **Plans + harness docs must be committed to the base branch** (or be absolute
  paths outside the repo). A fresh worktree only contains what is committed on the
  branch — uncommitted working-tree plan files will be invisible to the tasks. The
  phase-1-validate task exists to catch this; the skill itself does not verify it.
- **Leave the main repo checked out on the work branch.** `commit` mode looks for
  a worktree where `{{base_ref}}` is checked out to cherry-pick into; the main
  repo checkout is the natural target. If the work branch is checked out nowhere,
  the cherry-pick step has no destination.
- **Do not tell exec/validate tasks to commit.** This is the opposite of
  `execution-loop`, where the per-plan agent commits itself. Here auto-review
  `commit` mode owns the commit + cherry-pick; a task that also commits/pushes on
  its own can conflict with it.
- **Sequential only.** The chain is linear and relies on each task's cherry-pick
  landing before the next worktree is created. Do not fan out plan tasks in
  parallel onto the same work branch.
- **This skill only builds the board.** Per-task telemetry and progress come from
  the board tasks themselves; the skill's own `cli-escalation-notify` is limited
  to pipeline-built and halts.
