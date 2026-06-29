---
name: pr-watchdog
description: Continuously watch over a GitHub PR's CI in the cheetah repo and keep it moving toward green. Resolves the PR from --pr or the local branch, triggers a build when none is running, and auto-applies the one safe deterministic fix (branch behind base) in an isolated worktree, pushing it automatically. Pre-build/build failures are always handed to a generic systematic-debugging subagent; test failures to the pr-failure-handler subagent; their code fixes halt and ask the user before pushing. In cursor-agent CLI context it sends a Slack message for every PR event and every analysis. Use when asked to watch/babysit/guard a PR, drive a PR to green, or auto-fix CI for a PR. Triggers on phrases like "watch this PR", "watchdog my PR", "drive PR to green", "keep my PR green", "auto-fix CI for PR".
disable-model-invocation: true
---

# PR Watchdog

## Goal

Keep one PR moving toward a green, mergeable state with minimal user interruption:
observe CI each cycle, trigger a build when none is running, auto-apply the one safe
deterministic fix (branch-update) and push it, and investigate every build/test failure via
a subagent — halting only for code fixes or non-trivial decisions. End with the PR green or a
clean summary; a watchdog-created worktree is removed on finish, while the run dir always
persists for inspection.

This skill owns the **orchestration**. Deterministic flows are scripts in `scripts/`.
Failure *investigation* is delegated to a subagent. It dispatches sub-skills only on
specific events:

- a **generic `systematic-debugging` subagent** — for a **pre-test** failure (build /
  pre-build: compile, link, lint, validate, codegen, packaging), dispatched via the Task
  tool with an inline prompt that tells it to run the `systematic-debugging` skill against
  the relevant local build/lint command and loop until it passes (Stage 3a). There is no
  dedicated skill for this — the prompt carries all the context.
- `pr-failure-handler` — for a **test-stage** failure, investigates the Jenkins logs and
  failed tests, classifies (trivial / non-trivial / flaky), applies a trivial code fix in
  the worktree, or writes a Suggested Fix on non-trivial.
- `worktree` — owns **all worktree actions**. The watchdog never runs `git worktree` itself;
  it invokes this skill (Workflow B) to create/reuse the fix worktree synced to
  `origin/<branch>` and to handle the already-checked-out conflict (main-worktree /
  dedicated-branch / handoff). It returns `worktree`, `worktree_mode`, `dedicated_branch`,
  `push_target`.
- `cli-escalation-notify` — fires for **every PR event and every analysis**; pushes a
  Slack DM in CLI context, no-ops in the IDE.
- `git-conventions` — composes commit messages whenever the loop commits a fix.
- `pr-labels` — consulted only if the loop ever needs to retitle the PR (rare; usually it
  does not touch the title).

## Inputs

- `pr` (optional) — PR number (e.g. `91682`). When omitted, resolved from the current
  local branch via `scripts/pr_watchdog.py resolve`. HALT `no-unique-branch-pr` if the
  branch maps to zero or multiple open PRs.
- `interval` (optional, default `600` seconds / 10 min) — poll cadence between cycles
  while a build is running or pending.
- `base_branch` (optional) — overrides the base branch detected from the PR (rare).

## Companion docs

- `references/state-schema.md` — `meta.json` / `situation.json` / `action.json` shapes
  and the `~/.pr-watchdog-runs/<run_id>/` tree.
- `references/stage-prompts.md` — event-notification bodies, the escalation prompt, and
  the final/halt summary templates.
- `scripts/pr_watchdog.py` is **self-contained** (stdlib + `gh` + `git`): **all GitHub I/O
  is via the `gh` CLI** and Jenkins detail is read over tokenless HTTP. It has **no
  dependency on `pr_driver.py`** and reads no PAT/`mcp.json`/MCP. (`pr_driver.py` remains a
  useful manual reference, but the watchdog does not import or call it.)

## Companion scripts (run, do not read)

All GitHub access is through the **`gh` CLI** (no PAT, no `~/.cursor/mcp.json` token, no
GitHub MCP) — `gh` must be authenticated (`gh auth status`). Jenkins detail is read over
tokenless HTTP.

- `scripts/pr_watchdog.py status [--pr N]` — emit one `situation.json` (see schema). The
  loop's single source of truth for CI state each cycle.
- `scripts/pr_watchdog.py watch --pr N [--interval S] [--max-runtime S]` — **block-poll**
  until CI transitions (PASSED / FAILED / behind) or the wall-clock cap, then exit. Run it
  as a **backgrounded Shell job** (`block_until_ms: 0`); its completion notification wakes
  the agent **even after the turn ends**, so a turn ending can't silently kill the watch.
  Streams `WATCH_POLL` heartbeats; exits `0` with `WATCH_TRANSITION <reason> <json>`,
  `10` with `WATCH_MAXRUNTIME <json>`, or `1` with `WATCH_FATAL`. This replaces the old
  in-turn `AwaitShell`-sleep poll loop.
- `scripts/pr_watchdog.py resolve` — resolve a unique PR from the local branch.
- `scripts/pr_watchdog.py trigger --pr N [--full] [--server SLUG]` — post the Jenkins rebuild
  request. **For a fresh HEAD (a new commit, a base-merge, or a PR-prefix/title change) use
  `--full`**: it posts the single global `pipeline please rebuild` comment — no host/server
  slug, no `failed` qualifier — and **always posts** (a brand-new HEAD has no statuses to
  discover, so it never bails with `no-ci-yet`). Without `--full` it rebuilds only the failed
  servers (`pipeline please rebuild failed <slug>` per discovered server, or just `--server`
  ones) — use that form only to retry a specific flaky/failed server.
- `scripts/update_branch.sh --check|--apply <wt> <base> [--push] [--keep-conflict]` — detect /
  fix "branch behind base" **and surface base↔branch merge conflicts**. `--apply` merges
  `origin/<base>` (pushes on `--push`). A clean merge (`ACTION=merged`) is the one
  auto-pushable reconcile; on conflict (`ACTION=conflict`, exit 3) it normally aborts, but
  with `--keep-conflict` it **leaves the conflicted merge in the worktree** and prints
  `CONFLICT_FILES=...` so the Stage 3m merge-conflict subagent can resolve it instead of the
  loop halting.
- `scripts/fix_lint.sh <wt> <category>` — run the repo's auto-formatter/validator for
  `rust|yang|python|generic` and report whether files changed. **Run by the Stage 3a
  `systematic-debugging` subagent** as a candidate lint/format fix, not by the loop itself.

## Hard invariants

- **One PR per run.** The watchdog watches exactly one PR end to end.
- **Watch liveness is independent of the agent turn.** The RUNNING-wait is NOT an in-turn
  sequence of `AwaitShell` sleeps (a turn ending — model stop, harness turn cap,
  interruption — would silently kill it, as happened in run `20260616-162322`). Instead run
  `pr_watchdog.py watch` as a **backgrounded Shell job** that exits on a real CI transition;
  the harness's background-completion notification wakes the agent to act. **Silence is never
  success** — any watcher exit that is NOT a `WATCH_TRANSITION` (max-runtime, fatal, or the
  job vanishing) MUST notify the user via `cli-escalation-notify`; never assume green because
  no event arrived.
- **GitHub via `gh` only.** All GitHub reads/writes use the authenticated `gh` CLI — never
  a PAT, `~/.cursor/mcp.json`, or the GitHub MCP. (Jenkins detail is tokenless HTTP; `git`
  push/merge use the normal git remote.)
- **Worktree actions are delegated to the `worktree` skill.** The watchdog never runs
  `git worktree`/`make_worktree.sh` itself — it invokes the `worktree` skill (Stage 1 step 4)
  and uses what it returns.
- **All fixes happen in the worktree**, never in the user's main checkout — *except* when
  the PR branch is already checked out in the main repo and the user (via the `worktree`
  skill's recovery) opts to either run fixes in that main checkout (`worktree_mode = main`)
  or in a dedicated-branch worktree based on `origin/<branch>` that pushes to the PR branch
  (`worktree_mode = dedicated`). The default worktree tracks the PR branch and is synced to
  `origin/<branch>` before each fix. **Never `--force`** a worktree onto a branch checked out
  elsewhere; only HALT `worktree-conflict` if the user declines both recovery options.
- **Batch fixes; one push per remediation cycle.** Apply ALL currently-known fixes to the
  worktree first — base-merge (behind), pre-build/lint fix, approved code fix — **then push
  once at the end** and trigger a single rebuild. NEVER push a partial fix (e.g. the
  base-merge on its own) that would burn a rebuild before the failure is actually fixed, and
  would also push the last CI-bearing commit out of reach.
- **Order within the batch depends on the failure type:**
  - **Build / pre-build failure (or reconcile-only):** base-merge **first** (resolving any
    conflict via Stage 3m), then the build fix — so the fix builds on the updated base
    (build/lint verification doesn't depend on a Jenkins image).
  - **Test failure:** fix the **test first**, then base-merge. Any local verification the
    handler does (unit/GTest only) runs against the **current HEAD** under test; merging base
    first would change the worktree source out from under that verification. Merge only after
    the test fix is in.
- **Reconcile with base before any push or rebuild.** A branch that is behind base **or
  conflicted with it** is NOT mergeable and must be reconciled before the watchdog pushes a
  fix or triggers a build. `behind` and `dirty` are **distinct** GitHub states: a conflicted
  PR reports `merge_state_status == DIRTY` (`mergeable_state` ∈ {`behind`,`dirty`}), **not**
  `behind` — so NEVER gate base reconciliation on `behind` alone (doing so misses conflicted
  PRs, as happened on PR 96575). Treat `needs-base-reconcile = behind == true OR
  merge_state_status == DIRTY`. The base-merge (and, on conflict, the resolution) is part of
  the Stage 3 batch and lands in the single Stage 3c push; never push/trigger an unreconciled
  branch.
- **Merge conflicts are resolved, not halted-on.** A base-merge conflict is handed to the
  **merge-conflict subagent** (Stage 3m), never resolved by the loop itself. **Trivial**,
  intent-preserving conflicts (both-added includes/decls/registrations, adjacent
  non-overlapping hunks, import/whitespace ordering, generated files/lockfiles) are resolved
  in the worktree and ride along with the base-merge into the Stage 3c push **without a
  separate gate** (they are part of the safe reconcile). **Non-trivial** conflicts
  (overlapping edits to the same logic, semantic divergence, a design call) are escalated via
  the **regular Stage 4 gate**. HALT `branch-merge-conflict` ONLY when a non-trivial conflict
  is declined (Stage 4 handoff) or the subagent is blocked — never on the mere existence of a
  conflict.
- **Auto-push only the safe deterministic fix** — a clean base merge (branch-update) by
  `update_branch.sh`, plus any **trivial** conflict resolution applied during that merge by
  the Stage 3m subagent. Every other source fix (lint/format, pre-build, validation, test,
  and any **non-trivial** conflict resolution) is produced by a subagent and is **never**
  auto-pushed by default; it goes through the Stage 4 escalation gate (the pre-build
  auto-push opt-in in Notes is the only exception).
- **Never edit CI config to make a failure pass.** No touching workflows, deselect lists,
  the suite registry, or test-stage selection just to go green.
- **Investigation/fixing is always a subagent; the loop never edits source files itself.**
  The only deterministic flow the loop runs directly is `update_branch.sh` (base-merge). Every
  source fix is produced by a subagent: the generic `systematic-debugging` subagent for
  pre-build (which may run `fix_lint.sh` as a candidate fix) / `pr-failure-handler` for tests.
  The loop never attempts an inline pre-build/lint fix.
- **Notify on events, not every poll.** Send a Slack event only on a *state transition*
  or an *action*, not on every identical RUNNING cycle. Every analysis (handler result)
  always notifies.
- **Out-of-band notification is delegated.** The loop never composes Slack itself; it
  calls `cli-escalation-notify`, which pushes in CLI and no-ops in the IDE, non-fatally.

## Workflow

### Stage 1: Initialize the run

1. `run_id = YYYYMMDD-HHMMSS-<6-hex>`. Create `~/.pr-watchdog-runs/<run_id>/cycles/`.
   Verify GitHub access: `gh auth status` must succeed (all GitHub I/O is via `gh`). HALT
   `gh-not-authenticated` otherwise — do not fall back to a PAT/MCP.
2. Resolve the PR: use `pr` if given; else `scripts/pr_watchdog.py resolve`. HALT
   `no-unique-branch-pr` on a null result.
3. Run `scripts/pr_watchdog.py status --pr <pr>` once to capture `branch`, `base_branch`,
   `url`, `title`, `draft`. Honor a `base_branch` override if provided.
4. Create the fix worktree by **invoking the `worktree` skill (Workflow B)** with
   `repo_root = <repo_root>`, `branch = <branch>`, `worktree_path =
   ~/.pr-watchdog-runs/<run_id>/worktree`. The watchdog does **not** run `git worktree` or
   `make_worktree.sh` itself. The skill creates/reuses the worktree synced to
   `origin/<branch>` and, if the PR branch is already checked out elsewhere (its
   `make_worktree.sh` exit 3),    surfaces the recovery options (main-worktree /
   dedicated-branch / handoff) and acts on the user's choice. Record what it returns into
   `meta.json`: `worktree`, `worktree_mode` (`worktree` | `main` | `dedicated`),
   `worktree_created` (true only when it made a NEW worktree this run — drives Stage 5
   cleanup), `dedicated_branch`, `push_target`. If the skill reports the user chose
   **handoff** → HALT `worktree-conflict`. On success, write `meta.json.worktree` and the
   `worktree` file.
5. Detect CLI context (`$CURSOR_AGENT` set AND `$CURSOR_LAYOUT` unset; do **not** gate on
   `$VSCODE_AGENT_FOLDER`). Record `is_cli_context`.
6. Write `meta.json` (schema in `references/state-schema.md`).

**Gate:** `meta.json` exists, the PR is resolved, the worktree is ready and synced to
`origin/<branch>`, CLI context is recorded.

### Stage 2: Watch loop (continuous, via the backgrounded `watch` process)

A cycle is one **observation → branch** step. The observation comes from one of two
sources, both `pr_watchdog.py`:

- **Active checks** (a quick `status --pr <pr>` snapshot) when you need an immediate read —
  at Stage 1's end, right after a `trigger`, or to confirm a build registered.
- **Blocking wait** via the backgrounded `watch --pr <pr> --interval <interval>` process
  while a build is RUNNING. **Do not** sit in-turn on `AwaitShell` sleeps re-polling — that
  loop dies if the turn ends and silently stops watching. Instead:
  1. Launch `watch` as a background Shell job (`block_until_ms: 0`); record its
     `meta.json.watcher = {pid, started_at, interval, max_runtime}`. End the turn — the
     watcher keeps polling on its own.
  2. When the background job **completes**, the harness notifies you. Read the tail of its
     terminal output and branch on the **last `WATCH_*` line**:
     - `WATCH_TRANSITION <reason> <json>` → use that situation as this cycle's observation
       (`reason` ∈ `passed|failed|behind`) and branch below.
     - `WATCH_MAXRUNTIME <json>` → HALT `max-runtime` (notify; the build never finished).
     - `WATCH_FATAL <msg>` → the watcher hit repeated poll errors → notify
       `watcher stopped`, do one `status` snapshot, and either relaunch `watch` or HALT
       `pr-driver-error` if `status` also fails.
  3. **Silence is never success:** if the background job ends with no `WATCH_TRANSITION`
     (vanished, killed, exit you didn't expect), notify `watcher stopped` and re-observe with
     `status` before deciding — never assume green.

For each cycle write `cycles/<NNN>-<situation>/situation.json`, then branch on the situation.
Each observation, union the `servers[].slug` into `meta.json.known_server_slugs` (so a later
trigger can fall back to them after a base-merge clears HEAD's statuses). Notify via
`cli-escalation-notify` only when this cycle's classification differs from
`meta.json.last_overall` or an action is taken (use the titles in `stage-prompts.md`).

  Also compute `needs-base-reconcile = behind == true OR merge_state_status == DIRTY`
  (`mergeable_state` ∈ {`behind`,`dirty`}) from this cycle's `situation.json` — it gates the
  branches below. A conflicted PR is `dirty`, **not** `behind`, so check both.

- **`PASSED` and not `needs-base-reconcile`** → notify "PR is green", go to Stage 5.
- **`PASSED` but `needs-base-reconcile`** → the PR is green but **un-mergeable** (behind or
  conflicted with base) → go to **Stage 3 (Remediate)** to reconcile (base-merge + conflict
  resolution) before it can merge. Do not treat green-but-dirty as done.
- **`needs-base-reconcile` or `FAILED`** (with a build present) → go to **Stage 3
  (Remediate)**, which batches the base-merge (+ conflict resolution if the merge conflicts)
  and any failure fix locally and pushes once. Do **not** merge-and-push the base here on its
  own — that is the partial-push mistake the batching invariant forbids.
- **`NO_CI`** or (`FAILED`/idle and not `build_running`) with nothing to fix → run
  `scripts/pr_watchdog.py trigger --pr <pr> --full` (subject to the re-trigger guard below).
  This is the path for **any fresh HEAD — a new commit, a base-merge, or a PR-prefix/title
  change — whether you pushed it or the watchdog pushed a fix** (branch-update, lint/validate,
  or an approved code fix). **This CI does NOT auto-build on push** — there is no
  webhook/branch-scan auto-trigger, so a new HEAD always reports `NO_CI` /
  `build_running:false` and must be triggered explicitly. `--full` posts the single global
  `pipeline please rebuild` (no host/slug, no `failed`) and **always posts** — so there is no
  server discovery and no `no-ci-yet` to work around. Notify "build triggered"; next cycle.
  (Only use the per-server `trigger --server <slug>` form to retry one specific flaky/failed
  server, never for a fresh HEAD.)

**Re-trigger guard (avoid duplicate rebuilds).** Record each trigger in `meta.json`
(`last_trigger = {sha, at}`). Do **not** re-`trigger` the same HEAD `sha` again until either
a build registers for it (`build_running`/statuses appear) or a cooldown elapses
(`>= interval`). This prevents posting a second `pipeline please rebuild` while the first
request is still propagating (statuses can take a minute or two to appear after triggering).
- **`RUNNING`** / `build_running` → **launch (or keep) the backgrounded `watch` process**
  and end the turn; resume when it completes (per the two sources above). Do NOT poll in-turn
  with `AwaitShell` sleeps. No notify unless this is the first transition into RUNNING. If a
  quick `status` right after a `trigger` still shows `NO_CI` (statuses not yet registered),
  do a short bounded wait for them to appear before launching `watch`, then launch it.

(The `behind`/`FAILED` → Stage 3 route is the second bullet above.)

Record `action.json` each cycle.

**Gate:** `situation.json` + `action.json` written; while a build is RUNNING the wait is
owned by a **backgrounded `watch` process** (not in-turn `AwaitShell` sleeps), and the loop
either advanced on a `WATCH_TRANSITION`, took a deterministic action, escalated, or notified
`watcher stopped` on any non-transition exit (silence is never treated as success).

### Stage 3: Remediate (batch local fixes, then one push + rebuild)

Entered when `needs-base-reconcile` (behind or dirty) or `overall == FAILED`. Assemble
**all** currently-known fixes into the worktree **without pushing**, then land them with a
single push + rebuild (Stage 3c). Per the batching invariant, nothing is pushed until the
batch is complete. The **base-merge** is `scripts/update_branch.sh --apply <wt> <base>
--keep-conflict` — **without `--push`** (merges `origin/<base>` locally only): clean
(`ACTION=merged`) → continue; conflict (`ACTION=conflict`, exit 3) → the conflicted merge is
**left in the worktree** with its `CONFLICT_FILES`, which go to **Stage 3m (merge-conflict
resolution)** — NOT a halt.

#### Stage 3 Step 0: classify the failure (if FAILED)

Read `situation.json` and classify the failing stage as **pre-test** vs **test** — this
decides the order of merge vs fix:

- **pre-test** (build/pre-build) or **reconcile-only** (behind/dirty, no failure) →
  **base-merge first, then fix** (Step 1A).
- **test** → **fix the test first, then base-merge** (Step 1B), because the local test
  verification needs the Jenkins image that matches the current CI commit.

#### Stage 3 Step 1: apply fixes in the type-dependent order

**1A — build / pre-build failure, or reconcile-only:**
1. If `needs-base-reconcile` (behind or dirty), run the base-merge now (no `--push`); on a
   conflict resolve it via **Stage 3m** before continuing. If this is a reconcile-only cycle
   (no `FAILED`), skip to Stage 3c once the merge is clean/resolved.
2. Then fix the pre-build/build failure locally (routing below), building on the merged base.

**1B — test failure:**
1. Fix/verify the test **first** (dispatch `pr-failure-handler`, Stage 3b — it does log-based
   RCA, reproducing locally only for unit/GTest targets, never e2e/system tests). Do **not**
   merge base yet.
2. **After** the test fix is in the worktree, if `needs-base-reconcile`, run the base-merge
   (no `--push`) on top of it; on a conflict resolve it via **Stage 3m**.

Routing for the fix itself (used by 1A/1B):

- **pre-test** = build / pre-build (compile, link, configure, `lint`, `ruff`, `validate`,
  `pyang`, codegen, packaging). Heuristic: the failing stage name does **not** match the
  test keywords (`TEST`, `SANITY`, `REGRESSION`, `SYSTEM`, `SMOKE`, `ARTIFACT`) and there
  are no `failed_tests` rows for it. `lint_validate_failures` is always pre-test.
- **test** = a stage with `failed_tests`, or whose name matches a test keyword.

Route accordingly:

1. **Pre-test failure** → **always** dispatch the generic `systematic-debugging` subagent
   (Stage 3a) to reproduce and loop locally until the stage passes. The watchdog does **not**
   attempt any inline fix (no inline `fix_lint.sh`, no other inline edits) — the subagent owns
   the investigation and the fix (it may use `fix_lint.sh` itself as a candidate fix; see
   Stage 3a).
2. **Test failure** → dispatch `pr-failure-handler` (Stage 3b).

Notify "CI failed, investigating" on entry to either subagent. **All fixes stay in the
worktree (unpushed) until Stage 3c.**

#### Stage 3a: Pre-test failure — generic `systematic-debugging` subagent

Resolve the local command for `failing_stage` from `.ai/CONTRIBUTING.md` / `AGENTS.md`
(builds run via `dbuild`; e.g. `dbuild make mega_lint`, `ruff check`, `make lint_rust` /
`make rust_fmt`, `dbuild make ninja_build`, `dbuild make quagga`, `pyang`,
`dbuild make validate-yangs`). Then dispatch a **generalPurpose Task subagent** (no
dedicated skill) with an inline prompt built from this template:

```
Run the `systematic-debugging` skill and follow its four phases verbatim (Iron Law: no fix
before root cause; Phase 4.5: stop and report after 3 failed cycles — do not attempt a 4th).

Context (general):
- Work ONLY inside this git worktree: <worktree>  (PR branch <branch>, base <base_branch>).
  Never touch the main checkout, never `git push`, never `git commit` — I (the parent) own
  all commits and pushes.
- This is cheetah PR-<pr>; CI failed in the pre-test stage "<failing_stage>".
- Write your artifacts under: <cycle_dir>/rca/  (progress.md, repro-<n>.log, summary.md,
  evidence.json) and, on escalation, <cycle_dir>/suggested-fix.md (a ready-to-apply diff).

Your task:
- The failing stage maps to this local command: `<resolved command>`.
- This is a build / pre-build issue: do NOT run any test (unit or system) — that is only for
  test-stage failures. You only need the local build/lint command.
- Phase 1: run it in the worktree and REPRODUCE the same failure (capture to
  rca/repro-0.log) BEFORE editing anything. If it doesn't reproduce, stop and report
  "could-not-reproduce-locally" (likely infra/flaky) — do not guess a fix.
- Then find the root cause (use `git -C <worktree> diff <base_branch>...HEAD` for recent
  changes; trace deep errors backward), apply ONE minimal fix in the worktree, and re-run
  the command. Loop until it exits 0 ("fixed-locally") or you hit the 3-cycle cap
  ("needs-escalation").
- For a lint/format/validate stage, the smallest correct fix is often the repo's OWN
  auto-formatter — run `<pr-watchdog>/scripts/fix_lint.sh <worktree> <rust|yang|python|generic>`,
  then re-run the command to confirm it now passes. Use it as a candidate fix; this is NOT a
  bypass (it's the sanctioned formatter), unlike disabling/narrowing rules which is.
- NEVER bypass: no disabling/narrowing lint rules, `# noqa`/blanket `#[allow]`, deleting or
  xfail-ing the target, editing CI config/stage selection, or hand-editing generated files.
  If passing requires a bypass, that is "needs-escalation".
- A repo's OWN sanctioned mechanism is NOT a bypass. If the failing tool itself points you at
  a maintained registry/allow-list as the resolution (e.g. its output says "add the missing
  commands to known_missing_commands.py" or similar), treat using it as a legitimate fix. If
  the tool says to do so only after human/sys-arch sign-off, do NOT auto-apply — but it is
  still the recommended fix (see next bullet), not a dead end.
- ALWAYS produce a concrete recommendation, even on escalation. On `needs-escalation`, write
  `<cycle_dir>/suggested-fix.md` containing the smallest root-cause fix as a **ready-to-apply
  diff** (e.g. the exact lines to add to the registry, or the exact files/edits to author),
  so the parent can present a one-click apply at the user gate. Quote the tool's own message.
  Then revert your tentative edits so the worktree is clean (the user, as sign-off, applies
  the suggested diff). Never escalate with prose alone when the concrete change is known.
- On `fixed-locally`, leave the fix applied in the worktree.

Return: result (fixed-locally | needs-escalation | blocker), the exact command + its final
exit status, root_cause (one sentence), touched_paths, suggested_fix_path (set on
needs-escalation), and paths to your rca/ artifacts.
```

Wait for the subagent. **Always notify** "analysis complete" (and, on success, the
"pre-build fixed locally" event) with the root cause, result, and report path.

Branch on the returned result:
- **`fixed-locally`** → the fix is verified green locally and stays in the worktree. It is a
  code change, so by default it needs the Stage 4 gate (the prompt carries the local-pass
  evidence so approval is low-risk); on approval (or under the auto-push opt-in, see Notes)
  it lands via the single **Stage 3c** push together with any base-merge.
- **`needs-escalation`** → the worktree is clean and `suggested-fix.md` holds a ready-to-apply
  diff. Go to Stage 4 and present it as a one-click **`apply_push`** option (e.g. "add these
  N lines to `known_missing_commands.py`") — do **not** escalate with prose only when a
  concrete fix is known. On approval, apply the diff in the worktree and land it via Stage 3c.
- **`could-not-reproduce-locally` / blocker** → likely infra/flaky → no local fix; land any
  base-merge from Step 0 via Stage 3c, else re-trigger the build (one retry max; second
  occurrence → Stage 4).

#### Stage 3b: Test failure — investigation subagent

Dispatch `pr-failure-handler` with: `cycle_dir`, `pr`, `repo_root`, `worktree`, `branch`,
`base_branch`, `situation`, and the failing `servers`/`failed_tests`. **The handler does not
run image-based system/E2E tests** — its RCA is log-based, and it may reproduce locally
**only** for unit/GTest targets (via `dbuild`, no image), per its "Reproducing locally"
section. The watchdog does **not** pre-resolve any image-import command. The handler does RCA
(via `systematic-debugging`) + classification in one shot; on `trivial` it applies a code
patch **in the worktree** (saves `patch.diff`); on `non-trivial` it writes `suggested-fix.md`;
on `flaky` it does nothing.

Wait for it. Read `cycle_dir/rca/evidence.json` for `classification`, `relatedness`,
`root_cause`, `touched_paths`, `patch_path`, `suggested_fix_path`. **Always notify**
"analysis complete" with the root cause, classification, relatedness, and report path.

Branch:
- **`flaky`** → no local fix; re-trigger this server's build via
  `scripts/pr_watchdog.py trigger --server <slug>` (one retry max per server; a second flaky
  on the same server → escalate as non-trivial).
- **`trivial` or `non-trivial`** → both are code fixes → Stage 4 gate; on approval they land
  via the single **Stage 3c** push (the fix-landing policy forbids auto-pushing any
  investigation-derived code change).

**Gate:** the relevant subagent's artifacts are present; an analysis event was notified;
fixes (if any) are applied in the worktree, unpushed, awaiting Stage 3c / the Stage 4 gate.

#### Stage 3m: Merge-conflict resolution (subagent)

Entered when the Step 1 base-merge returns `ACTION=conflict` (run with `--keep-conflict`, so
the conflicted merge is left in the worktree). The loop **never resolves conflicts itself** —
dispatch a **generalPurpose Task subagent** to resolve them in the worktree and classify each.

Pass: `worktree`, `branch`, `base_branch`, the `CONFLICT_FILES` list, and `cycle_dir` (write
artifacts under `<cycle_dir>/merge/`). Instruct it to:
- For each conflicted hunk, decide **trivial** (mechanical, intent-preserving: both-sides-added
  includes/declarations/registrations, adjacent non-overlapping hunks, import/whitespace
  ordering, regenerated/generated files, lockfiles) vs **non-trivial** (overlapping edits to
  the same logic, semantic divergence, anything needing a design call).
- Resolve **all trivial** conflicts in the worktree keeping **both** sides' intent, `git add`
  the resolved files. Do **not** commit or push (the loop owns that via Stage 3c).
- If **any** conflict is non-trivial, do not guess: leave those files conflicted (resolve only
  the trivial ones) and write `<cycle_dir>/merge/suggested-resolution.md` (the conflicting
  hunks + recommended resolution as a ready-to-apply diff).
- **Never** resolve by deleting a side's feature, reverting the PR's commits, `--ours`/`--theirs`
  blanket-picking, or editing CI/test selection to dodge the conflict.

Return: per-file classification, `all_trivial` (bool), `touched_paths`, and
`suggested_resolution_path` (set when any non-trivial). **Always notify** "analysis complete".

Branch on the result:
- **`all_trivial`** → the resolved base-merge rides the safe reconcile: it joins the batch and
  lands via **Stage 3c with no separate gate** (per the conflict invariant — trivial,
  intent-preserving resolution is part of the base reconcile). Notify "merge conflicts
  resolved".
- **any non-trivial** → **Stage 4 gate** (regular escalation): present the suggested-resolution
  (`apply_push` / `commit_and_push` / `handoff` / `skip`). On approval, apply the diff in the
  worktree, `git add`, and land via Stage 3c. On `handoff` → HALT `branch-merge-conflict`. On
  `skip` → abort the in-progress merge (`git -C <wt> merge --abort`) and continue; note the PR
  stays un-mergeable (cannot reach green-and-mergeable until reconciled).

**Gate:** either the merge is fully resolved (all trivial, or non-trivial approved) and staged
for the Stage 3c push, or it was escalated/halted; the worktree is never left with stray
conflict markers heading into a push.

#### Stage 3c: Land the batch — one push + rebuild

After Step 0 and Step 1 have applied everything actionable to the worktree (and any required
Stage 4 gate has passed):

1. **Push once.** If the worktree is ahead of `origin/<branch>` (base-merge and/or fixes):
   commit anything uncommitted via `git-conventions` (`[AI generated]`), then a single
   `git -C <wt> push origin HEAD:<branch>`. Record the whole batch as one `meta.json.pushes`
   entry (list the `kinds`: e.g. `branch-update` + `prebuild`). Never `git add -A` blindly
   (see `stage-prompts.md` git-add safety).
2. **Trigger the rebuild — always.** The push does **not** auto-build CI, and the new HEAD is
   a fresh commit/merge, so immediately run `scripts/pr_watchdog.py trigger --pr <pr> --full`:
   the single global `pipeline please rebuild` (no host/slug, no `failed`) that always posts.
   Record `last_trigger`. Notify once (e.g. "fixes pushed & rebuild triggered").
3. **Continue.** Back to Stage 2; the next poll should show `RUNNING` once statuses appear
   (give it ~1–2 min). If it still shows `NO_CI`, the trigger didn't take — retry once per
   the re-trigger guard before waiting the full `interval`.

If nothing was applied (e.g. `needs-escalation` with a clean worktree, or all fixes were
skipped at the gate) → no push; continue per the gate outcome.

**Gate:** at most one push happened this cycle; a rebuild is running or was requested for
the new HEAD; `action.json` records the batch.

### Stage 4: Code-fix escalation (halt and ask)

A code fix is never auto-pushed. Present the escalation prompt from
`references/stage-prompts.md`:

1. Dispatch `cli-escalation-notify` (title `pr-watchdog — analysis complete` already fired
   in Stage 3a; here add the decision heads-up with the four choices).
2. Present the local interactive prompt (`AskQuestion` when available). Wait for the user.
3. Act on the choice per `stage-prompts.md` (apply_push / commit_and_push / handoff /
   skip), honoring the `git add` safety rules. On approval the fix joins the batch and is
   landed by the single **Stage 3c** push (together with any Step 0 base-merge) — not a
   separate push. `skip` leaves it unpushed; `handoff` stops the loop.

**Gate:** the user chose a route; the worktree/push state is consistent with their choice.

### Stage 5: Final summary & worktree cleanup

When CI is green, the user hands off, or a halt fires:

1. **Clean up the worktree the watchdog created.** If the watchdog generated a **new**
   worktree this run (`worktree_mode` is `worktree` or `dedicated` AND the `worktree` skill
   reported `created = true`), remove it via the `worktree` skill (Workflow B cleanup) —
   `remove_worktree.sh <repo_root> <worktree> [dedicated_branch]`, which also deletes the
   dedicated branch. Do **not** remove it when:
   - `worktree_mode == main` (it's the user's own checkout), or
   - the watchdog **reused** a pre-existing worktree (`created = false`), or
   - the finish is a **handoff** (the user is taking over — leave it for them), or
   - the worktree has uncommitted or unpushed changes — surface them and ask before removing,
     never silently discard.
2. Print the summary from `references/stage-prompts.md` (or the halt variant), stating
   whether the worktree was removed or preserved (and where).
3. Never push uncommitted worktree changes or delete the run dir
   (`~/.pr-watchdog-runs/<run_id>/` always persists for inspection).

**Gate:** summary printed; a watchdog-created worktree was removed (or explicitly preserved
per the rules above); loop returns.

## Halt conditions

The loop stops and surfaces the situation when one fires. In CLI context, dispatch
`cli-escalation-notify` (`title: pr-watchdog — halted: <halt_code>`) for halts that need
the user (`branch-merge-conflict`, `worktree-conflict`, handler `blocker`, user `handoff`,
`max-runtime`, `watcher-stopped`). Skip notifying the Stage-1-only halts (`no-unique-branch-pr`).

- `gh-not-authenticated` — `gh auth status` failed; GitHub access is `gh`-only.
- `no-unique-branch-pr` — branch maps to zero/multiple open PRs.
- `worktree-conflict` — the `worktree` skill reported the PR branch is already checked out
  elsewhere (its `make_worktree.sh` exit 3) and the user declined both recovery options
  (main-worktree / dedicated-branch), choosing handoff. HALT only in that case.
- `branch-merge-conflict` — a base-merge conflict could **not** be auto-resolved: the Stage 3m
  merge-conflict subagent found a **non-trivial** conflict and the user chose `handoff` at the
  Stage 4 gate, or the subagent returned a `blocker`. A merely *detected* conflict is NOT a
  halt — trivial conflicts are resolved inline and non-trivial ones are escalated first.
- `pr-driver-error` — `pr_watchdog.py status` exits 1 (e.g. bad creds, PR not found).
- Handler returned `blocker:` (e.g. log fetch failed).
- User chose **(c) Hand off** at a Stage 4 gate.
- `max-runtime` — the backgrounded `watch` exited `WATCH_MAXRUNTIME` (wall-clock cap, default
  24h) before the build ever finished. Notify, then HALT.
- `watcher-stopped` — the backgrounded `watch` ended without a `WATCH_TRANSITION`
  (`WATCH_FATAL`, killed, or vanished). This is **not** silently ignored: notify
  `watcher stopped`, re-observe via `status`, and either relaunch `watch` or HALT
  `pr-driver-error` if `status` also fails.

In every halt: persist state to `meta.json`, print the halt-summary variant, and run the
Stage 5 worktree cleanup (remove the worktree only if the watchdog created it this run and it
has no pending changes — see Stage 5; `worktree-conflict`/`handoff` and `main`/reused modes
leave it intact). Never revert or force-push; the run dir always persists.

## Notes

- **Auto-push opt-in for pre-test fixes.** By default a `fixed-locally` pre-test fix from
  the `systematic-debugging` subagent still passes through the Stage 4 user gate (the prompt
  shows it is verified green locally, so approval is low-risk). If the user explicitly asks to
  auto-push locally-verified build/pre-build fixes (env `PR_WATCHDOG_AUTOPUSH_PREBUILD=1`
  or a stated preference at run start), push them directly in Stage 3a without the gate.
  Test-stage code fixes from `pr-failure-handler` are **never** auto-pushed regardless.

## Output format

A markdown summary (Stage 5 shape) plus the `~/.pr-watchdog-runs/<run_id>/` tree.

## Quality bar (self-check)
- [ ] The PR was resolved (from `--pr` or the unique branch PR); a single PR was watched end to end.
- [ ] Every fix landed in the worktree, never the user's main checkout.
- [ ] Only the clean base-merge (branch-update) was auto-pushed without a gate; every source fix (lint/validate/pre-build/test) came from a subagent and went through the Stage 4 user gate (unless the pre-build auto-push opt-in was set).
- [ ] The branch was reconciled with base before any push/trigger: `needs-base-reconcile` (behind OR `merge_state_status == DIRTY`) routed to Stage 3 — a green-but-dirty PR was never treated as done. Base-merge conflicts went to the Stage 3m subagent (trivial resolved inline + landed with the merge, non-trivial escalated via the Stage 4 gate); a conflict was never silently halted-on or pushed unreconciled.
- [ ] Pre-build failures were ALWAYS handed to the `systematic-debugging` subagent; the loop never attempted an inline pre-build/lint fix.
- [ ] CI config / workflows / suite registry / test-stage selection were never edited to force green.
- [ ] Deterministic flows ran via the scripts; pre-test failures went to a generic `systematic-debugging` subagent (local fix-until-pass) and test failures to `pr-failure-handler`.
- [ ] The RUNNING-wait used the backgrounded `pr_watchdog.py watch` process (not in-turn `AwaitShell` sleeps); the agent resumed on its completion notification. Every non-`WATCH_TRANSITION` watcher exit (max-runtime / fatal / vanished) produced a `watcher stopped` notification — silence was never treated as green.
- [ ] In CLI context, a Slack event fired for each PR event and each analysis (state transitions/actions, not every identical poll), via `cli-escalation-notify`; status recorded in `meta.json`.
- [ ] Commits used `git-conventions` (with `[AI generated]`); no `git add -A`; protected branches never pushed to directly.
- [ ] On finish, a watchdog-created worktree (`worktree_created`, non-`main`, no pending changes) was removed via the `worktree` skill; `main`/reused/handoff/dirty worktrees were left intact. The run dir always persisted; nothing was force-pushed or reverted.
