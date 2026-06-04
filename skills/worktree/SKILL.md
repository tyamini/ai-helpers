---
name: worktree
description: Create and manage git worktrees for the cheetah repo. Two workflows — (1) a dbuild **sandbox** worktree so two branches can build/unit-test in parallel without clobbering each other (ROOT/prod_root + DN_QUAGGA_DEBUG_PATH isolation), and (2) an isolated **fix worktree** for PR automation, synced to origin/<branch>, with recovery when the branch is already checked out elsewhere (main-worktree / dedicated-branch). Owns all worktree actions for the private skills (pr-watchdog invokes this instead of doing worktree work itself). Triggers on "create worktree", "parallel build", "build in worktree", "fix worktree", "sandbox worktree".
disable-model-invocation: true
---

# Worktree

Single owner of git-worktree actions for the cheetah private skills. It covers two
workflows; pick by intent:

- **Workflow A — Sandbox worktree** (`scripts/sandbox-worktree.sh`): run two `dbuild`
  builds — or a build and a unit test — from two branches at the same time, without
  clobbering each other. This is the dbuild/quagga build-isolation use case.
- **Workflow B — Fix worktree for automation** (`scripts/make_worktree.sh`): create/reuse an
  isolated worktree checked out on a PR branch and synced to `origin/<branch>`, for applying
  and pushing fixes (used by `pr-watchdog`). Includes recovery when the PR branch is already
  checked out in another worktree (e.g. the user's main repo).

Scripts (run, do not read):

- `scripts/sandbox-worktree.sh [--allow-existing] <branch> [path]` — Workflow A setup.
- `scripts/remove-sandbox-worktree.sh [--force] <branch>` — Workflow A cleanup.
- `scripts/check-test-isolation.sh <test_file>` — Workflow A routing-test isolation check.
- `scripts/make_worktree.sh <repo> <branch> <wt> [dedicated_branch]` — Workflow B create/reuse.
- `scripts/remove_worktree.sh [--force] <repo> <wt> [dedicated_branch]` — Workflow B cleanup
  (refuses the main worktree; needs `--force` if the tree is dirty).

______________________________________________________________________

# Workflow A — Sandbox worktree (parallel dbuild)

Run two `dbuild` builds — or a build and a unit test — from two branches at
the same time, without clobbering each other.

The `scripts/sandbox-worktree.sh` helper handles two problems that plain
`git worktree add` doesn't solve:

1. **`ROOT` / `prod_root` override** — these are usually exported in your
   shell pointing to the main repo, and `dbuild` honours the inherited
   value. Without overriding them, `dbuild` invoked from a worktree builds
   the main repo's code. This applies to **any** `dbuild` target.
2. **`/tmp/debs` collision (quagga only)** — the `.deb` produced by
   `dbuild make quagga` lands in `/tmp/debs`. The helper exports
   `DN_QUAGGA_DEBUG_PATH` so two quagga builds (and the tests that consume
   their debs) don't overwrite each other. For non-quagga targets this
   export is harmless.

Everything else — `ccache`, `GOCACHE`, sccache redis, `/var/log/dn` — is
either concurrency-safe by design or per-worktree already.

## Parallelism matrix in different worktrees

| Combo | Supported |
|-------|-----------|
| 2× `dbuild <target>` | ✅ |
| build + unit test (e.g. `dbuild make isisTests`) | ✅ |
| build **or** unit test + one running dev test setup (dtest or make test_*) | ✅ |
| 2× dev test setups at the same time | ❌ don't do this |

## Quick start

```bash
# 1. Create the worktree + env file (default path: ~/worktrees/<branch>)
bash .claude/skills/worktree/scripts/sandbox-worktree.sh <branch>

# 2. Enter and activate
cd ~/worktrees/<branch>
source sandbox-env.sh

# 3. Build — dbuild now targets this worktree's code
dbuild make <target>
```

For `dbuild make quagga`, the `.deb` lands in `$DN_QUAGGA_DEBUG_PATH`
(→ `/tmp/dbuild-sandbox/<name>/debs/`) instead of `/tmp/debs`.

## Reusing an existing worktree

If a linked worktree for `<branch>` already exists, the script errors out by
default and tells you to remove it or pass `--allow-existing`. Adopting an
existing worktree skips `git worktree add` and just regenerates the sandbox
debs dir and `sandbox-env.sh` against the discovered path:

```bash
bash .claude/skills/worktree/scripts/sandbox-worktree.sh --allow-existing <branch>
```

The `[path]` argument is **ignored** when `--allow-existing` adopts a
pre-existing worktree (a note is printed if one was provided). If no worktree
for the branch exists yet, the flag is a no-op and the script creates one
normally.

The script refuses to "adopt" the **main** worktree even with
`--allow-existing` — sandboxing requires a separate linked worktree.

## Running quagga routing tests from the sandbox

Routing/quagga tests (`make test_routing.*`, `make test_quagga.*`, etc.)
read debs from `$DN_QUAGGA_DEBUG_PATH` and fall back to `/tmp/debs` when
unset, so the main worktree's workflow is unchanged.

## Old-version branches

The `DN_QUAGGA_DEBUG_PATH` plumbing only exists on branches that already
contain `.ai/skills/common/using-git-worktree/`. If the branch you check
out predates that change, **its dbuild and routing-test code paths still
hard-code `/tmp/debs`** — the sandbox env file is generated, but nothing on
that branch reads it. Effects:

- `dbuild make quagga` in the old-version worktree writes to `/tmp/debs`,
  same as the main worktree.
- A `make quagga` on either side can overwrite the `.deb` a routing test
  on the other side is consuming, mid-run.

The `sandbox-worktree.sh` script prints a `WARNING:` when it detects this.
There is no automatic fix — either rebuild on each worktree before its own
test, or serialize quagga work between the two worktrees.

## Cleanup

Use the helper:

```bash
bash .claude/skills/worktree/scripts/remove-sandbox-worktree.sh <branch>
```

The helper:
- Looks up the worktree for `<branch>` and refuses to touch the main worktree.
- Removes the worktree. If the only untracked file is the auto-generated
  `sandbox-env.sh`, it force-removes safely; otherwise it requires `--force`.
- Removes `/tmp/dbuild-sandbox/<name>/` (the script falls back to a
  `sudo rm -rf` hint if it can't delete the dir).

Equivalent manual commands:

```bash
git worktree remove ~/worktrees/<branch>
sudo rm -rf /tmp/dbuild-sandbox/<branch>
```

## Pitfalls

- **Don't run `docker-rm` or `clear_containers` while a sibling build is live.**
- **Don't run two dev test setups at once.**
- **`Permission denied` on sandbox debs dir** after a CI/Jenkins build wrote
  there as root: `sudo chown -R dn:dn "$DN_QUAGGA_DEBUG_PATH"`.

## Where artifacts land

| Context | `dbuild make quagga` deb path |
|---------|--------------------------------|
| Main repo | `/tmp/debs/*.deb` |
| Worktree (after `source sandbox-env.sh`) | `"$DN_QUAGGA_DEBUG_PATH"/*.deb` → `/tmp/dbuild-sandbox/<name>/debs/*.deb` |

Non-quagga `dbuild` targets write their output under `${ROOT}/...` (e.g.
`${ROOT}/bin`, `${ROOT}/src/wbox/build`), which is per-worktree.

## For agents

### When to trigger Workflow A

Trigger on either an explicit request or a situation that implies parallel
work — you don't need a direct user instruction.

- Explicit: user asks to create a worktree, set up a sandbox, or build/test
  in parallel.
- Implicit: the task requires building or testing a second branch while the
  main worktree already has a build or test running, or an agent needs an
  isolated tree to avoid disturbing the user's current state.

### Steps

**Stage 1 — Gather parameters.**
- `branch` (required) — branch or commit to check out.
- `path` (optional) — default `~/worktrees/<branch>`.

Verify the branch exists (`git rev-parse --verify <branch>`).

**Stage 2 — Create.**
Run `bash .claude/skills/worktree/scripts/sandbox-worktree.sh <branch> [path]` from the main repo root.
The script:
1. `git worktree add`.
2. Creates `/tmp/dbuild-sandbox/<name>/debs/`.
3. Writes `sandbox-env.sh` exporting `ROOT`, `prod_root` and `DN_QUAGGA_DEBUG_PATH`.

If the script fails with `Error: a worktree for branch '<branch>' already
exists at <path>`, do **not** retry blindly. Stop and ask the user whether to
reuse the existing worktree or remove it:
- Reuse → re-run with `--allow-existing` (the `[path]` arg, if any, will be
  ignored and the discovered worktree path will be used).
- Remove → run `git worktree remove <path>` first, then re-run without the flag.

If the script fails with `Error: branch '<branch>' is checked out in the main
worktree`, the branch cannot be sandboxed — ask the user to check out the
branch in a different worktree first or pick a different branch.

**Stage 3 — Verify.**
- `ls <path>/.git` exists.
- `grep ROOT <path>/sandbox-env.sh` shows the worktree path.
- `ls /tmp/dbuild-sandbox/<name>/debs` exists.
- `test -d <path>/.ai/skills/common/using-git-worktree` — if missing, the
  branch is an **old version** (see "Old-version branches" above). Surface
  the script's `WARNING:` line to the user verbatim and remember it for
  this session: `/tmp/debs` is shared between this worktree and the main
  worktree on this branch.

**Stage 4 — Report.**
Print the quick-start commands, where artifacts land, and the cleanup snippet.

**Stage 5 — Cleanup (agent-initiated removal).**

The agent may remove a worktree silently **only when BOTH** of the following
are true:
1. The worktree was created by this agent in the current session for its own
   internal purposes (not at the user's request, not for the user's work).
2. All tasks completed without errors **and** no output or results were
   produced that the user might want to keep.

If either condition is not met — always ask first:
> "About to remove the worktree for `<branch>` at `<path>` and its sandbox
> dir `/tmp/dbuild-sandbox/<name>/`. Proceed?"

Only run the remove script after the user confirms:
```bash
bash .claude/skills/worktree/scripts/remove-sandbox-worktree.sh <branch>
```

If the remove script fails because the worktree has modified files (exit 1
with a dirty-files list), surface those files to the user and ask whether to
`--force` or abort — do not retry with `--force` silently.

### Routing test rules
**Before running any routing test from a sandbox worktree:**

1. **Old-version branch** (Stage 3 flagged no `.ai/skills/common/using-git-worktree/`):
   if the user asks to run a routing/quagga test **without rebuilding first**,
   check `ls /tmp/debs`. If non-empty, stop and warn: the debs may be from
   a different worktree and may not match this branch. Ask whether to proceed
   anyway, rebuild quagga first, or clear `/tmp/debs`. Only run after the
   user confirms. Re-warn on every subsequent request for the lifetime of
   the session.

2. For **new-version branch** run the isolation check:
   ```bash
   bash .claude/skills/worktree/scripts/check-test-isolation.sh <test_file>
   ```
   - Exit 0: safe to proceed.
   - Exit 1: parse the script's output lines and present to the user as:
     > **`<COMPOSE>`** still hardcodes `/tmp/debs` — the test will not use
     > the worktree deb at `<WORKTREE_DEB>`.
     >
     > Options:
     > 1. Copy the worktree deb into `/tmp/debs` (overwrites — affects any parallel worktree):
     >    `<OPTION_1_CMD>`
     > 2. Proceed anyway using whatever `.deb` is already in `/tmp/debs`.
     >
     > To fix permanently: update `<FIX_TARGET>` — replace
     > `- /tmp/debs:/home/debug_pkg` with
     > `- ${DN_QUAGGA_DEBUG_PATH:-/tmp/debs}:/home/debug_pkg`.

     Wait for the user to choose before running the test.

______________________________________________________________________

# Workflow B — Fix worktree for automation

Create or reuse an **isolated worktree on a PR branch, synced to
`origin/<branch>`**, so an automation skill (e.g. `pr-watchdog`) can apply and
push fixes without touching the user's main checkout. This workflow owns
`scripts/make_worktree.sh` and the recovery when the PR branch is already
checked out elsewhere.

## Inputs

- `repo_root` (required) — the cheetah checkout (e.g. `/home/dn/cheetah`).
- `branch` (required) — the PR head branch to isolate.
- `worktree_path` (required) — where to create the worktree (the caller
  usually passes `~/.pr-watchdog-runs/<run_id>/worktree`).

## Create / reuse

```bash
.claude/skills/worktree/scripts/make_worktree.sh <repo_root> <branch> <worktree_path>
```

The script creates (or reuses) the worktree on `<branch>` and, when the
worktree is clean, `reset --hard`s it to `origin/<branch>` so fixes land on
the remote tip. It emits `WORKTREE`, `BRANCH`, `HEAD`, `CREATED`, `SYNCED`.

**Exit codes:** `0` ready · `3` the branch is already checked out elsewhere /
the path is occupied · `1` other git error. **Never `--force`.**

## Recovery on exit 3 (branch checked out elsewhere)

Git forbids the same branch in two worktrees. When `make_worktree.sh` exits 3,
**do not work around it silently** — surface the conflict and let the user pick
how to still apply fixes (`AskQuestion` when available):

```
Can't create the fix worktree: PR branch <branch> is already checked out at <other path>
(git won't check the same branch out twice). How should I apply fixes?

  (a) MAIN WORKTREE — apply fixes directly in that checkout (<repo_root>). Only if it's
      clean and on <branch>; I won't reset --hard it, and base-merges/fixes land there.
  (b) DEDICATED BRANCH — make a worktree on <branch>-wd-<run_id> (based on origin/<branch>)
      and push fixes to the PR with `HEAD:<branch>`. Your main checkout is untouched.
  (c) HAND OFF — stop; nothing is changed.
```

`AskQuestion` id `worktree_conflict`; options `main_worktree`, `dedicated_branch`, `handoff`.

Act on the choice and return the result to the caller:

- **(a) main_worktree** — verify `<repo_root>` is clean and on `<branch>`; if not, say so
  and re-ask. Return `worktree = <repo_root>`, `worktree_mode = main`. Do **not**
  `reset --hard` the main checkout (no silent discard of the user's work).
- **(b) dedicated_branch** — re-run with a dedicated branch name:
  ```bash
  .claude/skills/worktree/scripts/make_worktree.sh <repo_root> <branch> <worktree_path> <branch>-wd-<run_id>
  ```
  The script bases a NEW branch on `origin/<branch>` (so the checkout doesn't collide) and
  emits `DEDICATED_BRANCH` and `PUSH_TARGET=<branch>`. Return `worktree_mode = dedicated`,
  `dedicated_branch`, and `push_target = <branch>`. The caller commits there and pushes with
  `git push origin HEAD:<branch>`, so fixes still land on the PR.
- **(c) handoff** — signal the caller to halt (`worktree-conflict`); change nothing.

## Returns (to the invoking skill)

`worktree` (abs path), `worktree_mode` (`worktree` default | `main` | `dedicated`),
`created` (bool — `make_worktree.sh` `CREATED`: true when a NEW worktree was made this
invocation, false when an existing one was reused or in `main` mode), `dedicated_branch`
(dedicated only, else null), `push_target` (`== branch`; explicit in dedicated mode),
`synced` (bool). Default `worktree_mode` is `worktree` (an isolated worktree on the PR
branch, synced to `origin/<branch>`).

## Cleanup (remove a worktree this skill created)

Remove the fix worktree when the caller is done — **but only one it created** (`created ==
true`, modes `worktree`/`dedicated`). Run:

```bash
.claude/skills/worktree/scripts/remove_worktree.sh <repo_root> <worktree_path> [dedicated_branch]
```

The script refuses to remove the **main** worktree (exit 2), prunes stale entries, and — for
a dedicated branch — deletes that local branch afterward (its commits are already on the PR).
If the worktree has uncommitted/untracked changes it exits 1 without removing; surface those
files to the caller/user and only re-run with `--force` after confirming nothing is needed.

**Never** remove a worktree the skill did not create this run: `main` mode (the user's
checkout) or a reused worktree (`created == false`) must be left intact.

## Quality bar (self-check)

- [ ] Workflow A: `<path>/.git` exists and `git -C <path> branch --show-current` matches;
      `sandbox-env.sh` exports `ROOT`, `prod_root`, `DN_QUAGGA_DEBUG_PATH`;
      `/tmp/dbuild-sandbox/<name>/debs/` exists; user shown artifacts + cleanup.
- [ ] Workflow A: on "already exists" failure, stopped and asked before `--allow-existing` or
      removing; on cleanup, only skipped confirmation for an agent-internal worktree with no
      user-visible output, and surfaced dirty files before any `--force`.
- [ ] Workflow A: before a routing test, ran `check-test-isolation.sh` (new-version) or
      checked `/tmp/debs` and warned (old-version), and waited for the user's choice on exit 1.
- [ ] Workflow B: never `--force`d a worktree onto a branch checked out elsewhere; on exit 3
      offered main-worktree / dedicated-branch / handoff and returned the chosen mode/path.
- [ ] Workflow B: the worktree was synced to `origin/<branch>` (default mode); main mode was
      never `reset --hard`; dedicated mode pushes `HEAD:<branch>`.
