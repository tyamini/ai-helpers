---
name: implementor
description: Implement an approved plan end-to-end in the cheetah repo — load the plan (file or Jira ticket), confirm parent branch and workspace (current, existing worktree, or new worktree), create a properly named branch, build with `dbuild` until green, implement and pass plan unit tests in the builder container, then commit, push, and open a PR. Use when the user asks to "implement a plan", "execute this plan", or hands over a plan/Jira ticket to be built and PR'd. Never runs e2e tests that need live DNOS service containers.
disable-model-invocation: true
---

# Implementor

## Goal
Take an approved plan and deliver it as a green, pushed PR on top of the
chosen parent branch — without ever running e2e tests that need live DNOS
service containers. Only unit tests that run inside the `dbuild` builder
container are exercised.

## Inputs
- **Plan source** — either a path under `.ai/plans/` (or any other file the
  user supplies) **or** a Jira ticket key (e.g. `SW-123456`). Exactly one
  is required.
- **Parent branch** — the branch the work will sit on top of and target with
  the PR. Either the **current** branch in the active worktree or **another**
  branch the user names.
- **Workspace** — one of:
  1. **current** — work in the currently-checked-out workspace.
  2. **existing worktree** — switch to a worktree the user already has.
  3. **new worktree** — create one via `using-git-worktree`.

## Companion skills (must read once, up front)
- `.ai/skills/common/git-conventions/SKILL.md` — branch name, commit
  message, and PR title rules. **All** branch creation, commits, and the
  PR opened by this skill must match those rules.
- `.ai/skills/common/using-git-worktree/SKILL.md` — only when the user picks
  the *new worktree* option.
- `.ai/skills/common/workflow-exec/SKILL.md` and
  `.ai/workflows/dbuild-usage.md` — for picking the right `dbuild` target.
- `.ai/skills/common/pr-labels/SKILL.md` — for selecting PR prefix labels.

## Workflow

### Stage 1: Load context and the plan

1. Read this skill's companion skills listed above.
2. Read `AGENTS.md` and `.ai/CONTRIBUTING.md` if not already in context.
3. Resolve the plan:
   - **File path:** read the file.
   - **Jira key:** fetch the issue with the DN MCP Jira tools
     (`atlassian_jira_get_issue`). Extract the plan/spec/requirements from
     the description, attached design docs, or linked `.ai/plans/*.md`
     references. If neither the description nor the linked artifacts give
     a self-contained plan, **stop and ask** the user to point at a plan
     file or describe the work.
4. Extract from the plan:
   - **Acceptance criteria** (what "done" means).
   - **Test plan** — which unit tests the plan calls for and where they
     live. Mark anything that is e2e / live-service explicitly out of scope
     for this skill (see [Scope: tests](#scope-tests)).
   - **Affected modules** — the rough set of files / packages to change.

**Gate:** Stop if the plan has no testable acceptance criteria or no
identifiable scope. Ask the user instead of guessing.

### Stage 2: Resolve parent branch

Ask the user explicitly:

> "Use the **current** branch as the parent for this work, or a
> **different** branch? If different, paste the branch name."

Then validate:

- For *current*: capture `git -C <repo> rev-parse --abbrev-ref HEAD`.
- For *another*: `git fetch` and verify with
  `git rev-parse --verify origin/<branch>` (or local).

If a Jira ticket was used as the plan source, **cross-check** the
chosen parent against the ticket's Fix version per
[Resolving BASE-BRANCH from Jira](/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md#resolving-base-branch-from-jira).
If they differ, surface the mismatch and let the user confirm or correct.

**Gate:** Parent branch is known and exists on the remote (or local, if
explicitly user-stated as untracked).

### Stage 3: Resolve workspace

Ask the user (use a structured choice if available):

> "Where should I implement this?
> 1. **Current** workspace at `<cwd>`
> 2. An **existing worktree** (paste path)
> 3. A **new worktree** (I'll create it via `using-git-worktree`)"

- **Current** — verify clean tree (`git status --porcelain`). If dirty,
  stop and ask whether to stash, commit elsewhere, or abort.
- **Existing worktree** — verify it exists with `git worktree list`, that
  the path is a worktree, and that it is clean. Switch context to that
  path for all subsequent commands.
- **New worktree** — invoke `using-git-worktree`. Pass the **branch we are
  about to create** (Stage 4) as `<branch>` so the worktree is created
  directly on it. Source `sandbox-env.sh` from the new worktree before
  any `dbuild` call. **Set `$CREATED_WORKTREE=1`** so Stage 8 knows it owns
  this worktree and may remove it on success.

**Gate:** A clean working directory is selected and recorded as
`$WORK_DIR` for the rest of the workflow. If a new worktree was created,
`$CREATED_WORKTREE=1` is also recorded.

### Stage 4: Create the work branch

1. Apply `git-conventions` Stage 1 to identify:
   - Developer name (from `git config user.email`).
   - Jira ticket id (from input or retrieval — high certainty required).
   - BASE-BRANCH (the parent branch from Stage 2, validated against Fix
     version when a ticket exists).
2. Compose the branch name following
   [Branch Naming](/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md#branch-naming).
3. Present the proposed branch name to the user and wait for confirmation
   if it deviates from the strict format (per `git-conventions` Stage 2).
4. Create the branch on top of the parent:
   - Current/existing worktree:
     `git checkout -b <branch> <parent>` (after `git fetch`).
   - New worktree: `using-git-worktree` already created the worktree on
     `<branch>`; verify with `git -C $WORK_DIR rev-parse --abbrev-ref HEAD`.
5. Confirm `git -C $WORK_DIR status` is clean and the branch sits on the
   intended parent (`git merge-base --is-ancestor <parent> HEAD`).

**Gate:** `$WORK_DIR` is checked out on a fresh branch named per
`git-conventions`, sitting on top of `<parent>`.

### Stage 5: Implementation loop (build until green)

Iterate the following until the build is clean.

1. **Implement** the smallest set of changes from the plan that moves
   acceptance criteria forward. Honor `AGENTS.md` change policy
   (minimal diff, no speculative work, do not touch unrelated code).
2. **Pick the build target** via `workflow-exec` +
   `.ai/workflows/dbuild-usage.md`. Record the chosen `dbuild make
   <target>` (and any `KEY=VALUE` overrides) in context for reuse — do
   not re-run `workflow-exec` every iteration.
3. **Build** with `dbuild make <target>` from `$WORK_DIR`.
   - On **success**: continue to Stage 6.
   - On **failure**: read the first actionable error, fix it, then rerun
     the **same** target. Do not silently widen scope.
4. After 3 consecutive failed iterations on the same root cause, **stop
   and ask** the user — do not loop indefinitely.

**Gate:** Latest `dbuild make <target>` exits 0 with the changes in place.

### Stage 6: Unit tests

Only handle **unit tests** that run inside the `dbuild` builder container.
See [Scope: tests](#scope-tests) for what is explicitly excluded.

1. **Identify the unit-test target(s)** named in the plan (e.g.
   `dbuild make <pkg>Tests`, `dbuild make rust_test_<crate>`,
   `dbuild make py_test_<pkg>`). If the plan only says "add unit tests"
   without naming a target, locate the matching test target via
   `workflow-exec` (search `.ai/workflows/dbuild-usage.md`, `README.md`,
   and the package's existing build files) and confirm with the user
   before running.
2. **Implement** the unit tests called for by the plan. New tests must
   live in the locations specified by the plan's *Test Plan* section.
3. **Compile + run** them via the chosen `dbuild make` target.
4. **Loop** on failures:
   - Test compilation error → fix the test or the code under test, rerun.
   - Test assertion failure → fix the implementation or the test (only
     when the test is genuinely wrong; never weaken assertions to make a
     real bug pass), rerun.
   - Same root cause failing 3 iterations in a row → **stop and ask**.
5. After unit tests pass, re-run the build target from Stage 5 once more
   to confirm nothing regressed.

**Gate:** Plan unit tests compile and pass; final build target is still
green.

### Stage 7: Commit, push, open PR

1. **Self-check the diff** with `git -C $WORK_DIR status` and
   `git -C $WORK_DIR diff --stat <parent>...HEAD`. Only include files
   the plan justifies.
2. **Compose commit message(s)** per
   [Commit Messages](/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md#commit-messages):
   - `SW-XXXXX: <short description> [AI generated]` when a ticket
     applies (this skill is AI-authored).
   - `<short description> [AI generated]` only if the user explicitly
     chose to continue without a Jira ticket (per `git-conventions`).
   - Body explains why; reference the plan path and any spec.
3. `git add` only the intended files; `git commit`.
4. `git push -u origin <branch>` (never push to a protected branch — see
   `git-conventions`).
5. **Compose the PR title** per
   [PR Titles](/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md#pr-titles):
   - Pick prefix labels using `pr-labels` (ask the user if unsure).
   - Include `[SW-XXXXX]` when a ticket applies.
   - Append `[AI generated]`.
   - Keep total title length under ~200 chars.
6. **Open the PR** targeting `<parent>` using the GitHub CLI:
   ```bash
   gh pr create --base <parent> --head <branch> \
       --title "<composed-title>" \
       --body "$(cat <<'EOF'
   ## Summary
   <1-3 bullets pulled from the plan>

   ## Plan
   <relative path to plan or Jira link>

   ## Test plan
   - dbuild make <build-target>: pass
   - dbuild make <unit-test-target>: pass
   - e2e/live-service tests: out of scope for this skill
   EOF
   )"
   ```
   Use `gh` rather than `mcp_web_fetch` for any PR action — see
   `AGENTS.md` (DN MCP / `gh` rule for private repos).
7. Print the PR URL.

**Gate:** PR is open against `<parent>`, title and branch match
`git-conventions`, and the PR body lists the unit-test target(s) that
passed and explicitly notes that e2e/live-service tests were not run.

### Stage 8: Clean up the worktree (only when this skill created it)

Run this stage **only when all** of the following are true:

- `$CREATED_WORKTREE=1` (Stage 3 created a new worktree for this run).
- Stage 7 finished and the PR is open. The branch is on the remote, so
  the local worktree no longer holds the only copy.
- Stages 5 and 6 reported `pass` for build and unit tests.

If any of those is false (failure mid-flow, halt, or the user supplied
their own current/existing workspace), **leave the worktree intact** so
the user can inspect or resume — Stage 8 does nothing in that case.

When all conditions hold:

1. **Leave the worktree** before removing it — `cd` back to the main
   cheetah repo (`$REPO_ROOT`), or invoke from there using `git -C
   $REPO_ROOT`. Removing a worktree while you are inside it leaves the
   shell on a deleted path.
2. **Run the cleanup helper:**
   ```bash
   bash "$REPO_ROOT/.ai/skills/common/using-git-worktree/scripts/remove-sandbox-worktree.sh" <branch>
   ```
   This:
   - Refuses to remove the main worktree (safety guard inside the script).
   - Force-removes when the only untracked file is the auto-generated
     `sandbox-env.sh` (expected — created by `sandbox-worktree.sh`).
   - Cleans `/tmp/dbuild-sandbox/<name>/`.
3. **If the helper exits non-zero** because the worktree has dirty files
   (modified or untracked beyond `sandbox-env.sh`):
   - Do **not** retry with `--force` silently.
   - Surface the file list verbatim to the user and ask: keep the
     worktree (skip cleanup), or `--force` it. Then act on the answer.
4. **Do not delete the local branch.** `git-conventions` requires explicit
   user confirmation to delete branches, and the work is now on the
   remote PR — the user may want to keep the branch checked out
   elsewhere. Removing the worktree only removes the working directory,
   not the branch.

**Gate:** Either the worktree was successfully removed (record
`worktree_cleanup: removed`), or it was preserved by user choice / due to
a halt earlier in the flow (record `worktree_cleanup: kept` with the
reason). If `$CREATED_WORKTREE=0`, record `worktree_cleanup:
not-applicable`.

## Scope: tests

This skill **only** runs unit-style tests that fit entirely inside the
`dbuild` builder container — i.e. compile + run with a `dbuild make
<test-target>` invocation, no orchestration of DNOS service containers,
no `dtest` suites that bring up full deployments.

Excluded from this skill (do **not** run them, even if the plan mentions
them):

- `dtest <suite>` against `tests/suites/**/dtest.yml` or
  `src/rust_packages/**/dtest.yml` (see `.ai/workflows/dtest-testing.md`).
- Any test that requires `dnos_*` service containers, ASIC simulators, a
  router lab, or `dnos.deb` install.
- Anything flagged `requires-prod-images: true` in `dtest.yml`.

If the plan demands such tests, list them in the PR body under
"Excluded tests — needs lab" and tell the user they must run separately.

## Halt conditions

Stop and surface the situation to the user instead of guessing whenever
any of these are true:

- Plan source cannot be loaded or has no testable acceptance criteria.
- Parent branch cannot be verified (does not exist, or Fix-version
  mismatch the user has not resolved).
- Workspace is dirty and the user has not chosen a stash/commit path.
- Build or unit tests fail 3 iterations in a row on the same root cause.
- Branch name, commit, or PR title cannot be composed within
  `git-conventions` (e.g. ticket id genuinely unknown).
- A test in the plan is e2e / live-service — never silently run it.

## Output format

End the run with a concise report:

```yaml
implementor_report:
  plan_source: <path-or-jira-key>
  parent_branch: <name>
  workspace: current|existing-worktree|new-worktree
  worktree_path: <path-when-applicable>
  branch: <created-branch-name>
  build:
    target: dbuild make <target>
    iterations: <count>
    final_status: pass
  unit_tests:
    target: dbuild make <target-or-targets>
    iterations: <count>
    final_status: pass|skipped-not-in-plan
  excluded_tests:
    - <test that requires live services, if any>
  commit:
    title: <commit title>
  pr:
    url: <pr-url>
    title: <pr-title>
    base: <parent-branch>
  worktree_cleanup: removed|kept|not-applicable
  worktree_cleanup_reason: <why kept, when applicable>
```

## Quality bar (self-check)

[ ] Plan loaded from a real file or Jira ticket; acceptance criteria
    captured.
[ ] User confirmed parent branch; for ticket-driven work the parent was
    cross-checked against Fix version per `git-conventions`.
[ ] User picked workspace mode; new worktrees were created via
    `using-git-worktree` and `sandbox-env.sh` was sourced before
    `dbuild`.
[ ] New branch sits directly on top of the chosen parent and follows
    `git-conventions` naming.
[ ] Build target identified once via `workflow-exec` and reused across
    iterations.
[ ] `dbuild make <build-target>` is green at the end.
[ ] Plan-defined unit tests were implemented (if any), compile, and
    pass via `dbuild make <test-target>`.
[ ] No e2e / live-service tests were executed; any such tests were
    listed as excluded in the PR body.
[ ] Commit title uses `SW-XXXXX:` (real ticket) or no `SW-` prefix
    (user chose no-ticket); `[AI generated]` appended.
[ ] Branch was pushed and PR opened against the parent with prefix
    labels and ticket reference per `git-conventions`.
[ ] No protected branch (`dev_v*`, `rel_v*`, `eng_v*`) was pushed to.
[ ] If this run created a new worktree (Stage 3) **and** the PR opened
    successfully (Stage 7), the worktree was removed in Stage 8 via
    `remove-sandbox-worktree.sh` from the main repo. On any halt or
    failure earlier in the flow, the worktree was left intact.
[ ] Worktrees the user supplied (current workspace or existing worktree)
    were never auto-removed.
[ ] Local branch was not deleted as part of cleanup (only the worktree
    directory and its sandbox dir).
[ ] Halt conditions were honored — no silent guessing on missing input.
