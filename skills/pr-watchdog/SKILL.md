---
name: pr-watchdog
description: Continuously watch over a GitHub PR's CI in the cheetah repo and keep it moving toward green. Resolves the PR from --pr or the local branch, triggers a build when none is running, and auto-fixes the deterministic failure classes (branch behind base, lint, pre-build validation) in an isolated worktree, pushing safe fixes automatically. Real CI build/test failures are investigated by the pr-failure-handler subagent; non-trivial or code fixes halt and ask the user before pushing. In cursor-agent CLI context it sends a Slack message for every PR event and every analysis. Use when asked to watch/babysit/guard a PR, drive a PR to green, or auto-fix CI for a PR. Triggers on phrases like "watch this PR", "watchdog my PR", "drive PR to green", "keep my PR green", "auto-fix CI for PR".
disable-model-invocation: true
---

# PR Watchdog

## Goal

Keep one PR moving toward a green, mergeable state with minimal user interruption:
observe CI each cycle, trigger a build when none is running, auto-apply the safe
deterministic fixes (branch-update, lint, pre-build validation) and push them, and
investigate real build/test failures via a subagent — halting only for code fixes or
non-trivial decisions. End with the PR green or a clean summary plus an inspectable
worktree.

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
- `scripts/pr_watchdog.py resolve` — resolve a unique PR from the local branch.
- `scripts/pr_watchdog.py trigger --pr N [--server SLUG]` — post the Jenkins rebuild request
  (the `pipeline please rebuild failed <slug>` comment) for every discovered server, or just
  `--server` ones.
- `scripts/pr_watchdog.py jmc --pr N [--server SLUG] [--run]` — resolve the relevant **Israel**
  Jenkins build URL and (with `--run`) run `script/jenkins_make_config.sh` to **import the
  latest images** — the precondition for running any system/E2E test locally.
- `scripts/make_worktree.sh <repo> <branch> <wt>` — create/reuse the isolated fix
  worktree, synced to `origin/<branch>`.
- `scripts/update_branch.sh --check|--apply <wt> <base> [--push]` — detect / fix
  "branch behind base" (merges `origin/<base>`; pushes on `--apply --push`).
- `scripts/fix_lint.sh <wt> <category>` — run the repo's auto-formatter/validator for
  `rust|yang|python|generic` and report whether files changed.

## Hard invariants

- **One PR per run.** The watchdog watches exactly one PR end to end.
- **GitHub via `gh` only.** All GitHub reads/writes use the authenticated `gh` CLI — never
  a PAT, `~/.cursor/mcp.json`, or the GitHub MCP. (Jenkins detail is tokenless HTTP; `git`
  push/merge use the normal git remote.)
- **All fixes happen in the worktree**, never in the user's main checkout. The worktree
  tracks the PR branch; `make_worktree.sh` syncs it to `origin/<branch>` before each fix. If
  the PR branch is already checked out elsewhere, `make_worktree.sh` exits 3 →
  `worktree-conflict` halt (escalate and let the user choose; never `--force`).
- **Batch fixes; one push per remediation cycle.** Apply ALL currently-known fixes to the
  worktree first — base-merge (behind), pre-build/lint fix, approved code fix — **then push
  once at the end** and trigger a single rebuild. NEVER push a partial fix (e.g. the
  base-merge on its own) that would burn a rebuild before the failure is actually fixed, and
  would also push the last CI-bearing commit out of reach.
- **Order within the batch depends on the failure type:**
  - **Build / pre-build failure (or behind-only):** base-merge **first**, then the build fix
    — so the fix builds on the updated base (build/lint verification doesn't depend on a
    Jenkins image).
  - **Test failure:** fix the **test first**, then base-merge. Verifying a test locally
    imports the Jenkins image for the **current CI commit** (`jenkins_make_config`); merging
    base first would make the worktree source no longer match that image. Merge only after
    the test fix is in.
- **Auto-push only safe deterministic fixes** — clean base merge (branch-update), lint
  auto-format, pre-build validation regeneration. Code fixes from failure investigation
  are **never** auto-pushed; they go through the Stage 4 escalation gate.
- **Never edit CI config to make a failure pass.** No touching workflows, deselect lists,
  the suite registry, or test-stage selection just to go green.
- **Deterministic flows are scripts; investigation/fixing is a subagent.** The loop never
  edits source files itself — `fix_lint.sh`/`update_branch.sh` (deterministic), or the
  generic `systematic-debugging` subagent (local build/lint loop) / `pr-failure-handler`
  (test investigation) do.
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
4. Create the fix worktree: `scripts/make_worktree.sh <repo_root> <branch>
   ~/.pr-watchdog-runs/<run_id>/worktree`. HALT `worktree-conflict` on exit code 3 (the PR
   branch is already checked out elsewhere — typically the user's main repo — or the path is
   occupied). On that halt, **escalate and let the user choose** (e.g. switch their checkout
   to another branch then re-run, run observe-only with no worktree, or hand off) — do not
   silently work around it. Write the path to `meta.json.worktree` and the `worktree` file.
5. Detect CLI context (`$CURSOR_AGENT` set AND `$CURSOR_LAYOUT` unset; do **not** gate on
   `$VSCODE_AGENT_FOLDER`). Record `is_cli_context`.
6. Write `meta.json` (schema in `references/state-schema.md`).

**Gate:** `meta.json` exists, the PR is resolved, the worktree is ready and synced to
`origin/<branch>`, CLI context is recorded.

### Stage 2: Watch loop (continuous)

For each cycle (index `NNN`, starting 0): run `scripts/pr_watchdog.py status --pr <pr>`,
write `cycles/<NNN>-<situation>/situation.json`, then branch on the situation. Each poll,
union the `servers[].slug` into `meta.json.known_server_slugs` (so a later trigger can fall
back to them after a base-merge clears HEAD's statuses). Notify via `cli-escalation-notify`
only when this cycle's classification differs from `meta.json.last_overall` or an action is
taken (use the titles in `stage-prompts.md`).

- **`PASSED`** → notify "PR is green", go to Stage 5.
- **`behind == true` or `FAILED`** (with a build present) → go to **Stage 3 (Remediate)**,
  which batches the base-merge (if behind) and the failure fix locally and pushes once. Do
  **not** merge-and-push the base here on its own — that is the partial-push mistake the
  batching invariant forbids.
- **`NO_CI`** or (`FAILED`/idle and not `build_running`) with nothing to fix → run
  `scripts/pr_watchdog.py trigger --pr <pr>` (subject to the re-trigger guard below). This is
  the path for **any new commit — whether you pushed it or the watchdog pushed a fix**
  (branch-update, lint/validate, or an approved code fix). **This CI does NOT auto-build on
  push** — there is no webhook/branch-scan auto-trigger, so a new HEAD always reports
  `NO_CI` / `build_running:false` and must be triggered explicitly. `trigger` sources the
  server slugs from the last commit that had CI when HEAD has none yet (`catalog_source`
  shows `head` vs `history:<sha>`). If it returns `triggered:false, reason:no-ci-yet` **but**
  `meta.json.known_server_slugs` is non-empty (a base-merge can push the last CI commit out
  of the history lookback), retry `trigger --pr <pr> --server <slug>` for each known slug
  (the `explicit-slugs` fallback). Only a PR that has **never** had CI returns `no-ci-yet`
  with no cached slugs. Notify "build triggered"; next cycle.

**Re-trigger guard (avoid duplicate rebuilds).** Record each trigger in `meta.json`
(`last_trigger = {sha, at}`). Do **not** re-`trigger` the same HEAD `sha` again until either
a build registers for it (`build_running`/statuses appear) or a cooldown elapses
(`>= interval`). This prevents posting a second `pipeline please rebuild` while the first
request is still propagating (statuses can take a minute or two to appear after triggering).
- **`RUNNING`** / `build_running` → wait `interval` (use `AwaitShell` with no shell_id to
  sleep), then next cycle. No notify unless this is the first transition into RUNNING.

(The `behind`/`FAILED` → Stage 3 route is the second bullet above.)

Record `action.json` each cycle.

**Gate:** `situation.json` + `action.json` written; the loop either advanced, slept, took
a deterministic action, or escalated.

### Stage 3: Remediate (batch local fixes, then one push + rebuild)

Entered when `behind == true` or `overall == FAILED`. Assemble **all** currently-known
fixes into the worktree **without pushing**, then land them with a single push + rebuild
(Stage 3c). Per the batching invariant, nothing is pushed until the batch is complete. The
**base-merge** is `scripts/update_branch.sh --apply <wt> <base>` — **without `--push`**
(merges `origin/<base>` locally only; clean → continue, exit 3 → HALT
`branch-merge-conflict`).

#### Stage 3 Step 0: classify the failure (if FAILED)

Read `situation.json` and classify the failing stage as **pre-test** vs **test** — this
decides the order of merge vs fix:

- **pre-test** (build/pre-build) or **behind-only** → **base-merge first, then fix** (Step 1A).
- **test** → **fix the test first, then base-merge** (Step 1B), because the local test
  verification needs the Jenkins image that matches the current CI commit.

#### Stage 3 Step 1: apply fixes in the type-dependent order

**1A — build / pre-build failure, or behind-only:**
1. If `behind`, run the base-merge now (no `--push`). If behind-only (no `FAILED`), skip to
   Stage 3c.
2. Then fix the pre-build/build failure locally (routing below), building on the merged base.

**1B — test failure:**
1. Fix/verify the test **first** (dispatch `pr-failure-handler`, Stage 3b — it imports the
   Jenkins image for the current HEAD and works against it). Do **not** merge base yet.
2. **After** the test fix is in the worktree, if `behind`, run the base-merge (no `--push`)
   on top of it.

Routing for the fix itself (used by 1A/1B):

- **pre-test** = build / pre-build (compile, link, configure, `lint`, `ruff`, `validate`,
  `pyang`, codegen, packaging). Heuristic: the failing stage name does **not** match the
  test keywords (`TEST`, `SANITY`, `REGRESSION`, `SYSTEM`, `SMOKE`, `ARTIFACT`) and there
  are no `failed_tests` rows for it. `lint_validate_failures` is always pre-test.
- **test** = a stage with `failed_tests`, or whose name matches a test keyword.

Route accordingly:

1. **Pre-test failure** → first try the cheap deterministic auto-format: pick the category
   (`rust|yang|python|generic`) and run `fix_lint.sh <wt> <category>`. If `CHANGED=true`
   AND a quick local re-run of that command now passes, it's a SAFE fix — **leave it applied
   in the worktree** (it lands via Stage 3c). Otherwise (no change, fixer errored, or still
   failing) → dispatch the generic `systematic-debugging` subagent (Stage 3a) to reproduce
   and loop locally until the stage passes.
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
- This is a build / pre-build issue: do NOT import images and do NOT run jenkins_make_config
  — those are only for running system/E2E tests. You only need the local build/lint command.
- Phase 1: run it in the worktree and REPRODUCE the same failure (capture to
  rca/repro-0.log) BEFORE editing anything. If it doesn't reproduce, stop and report
  "could-not-reproduce-locally" (likely infra/flaky) — do not guess a fix.
- Then find the root cause (use `git -C <worktree> diff <base_branch>...HEAD` for recent
  changes; trace deep errors backward), apply ONE minimal fix in the worktree, and re-run
  the command. Loop until it exits 0 ("fixed-locally") or you hit the 3-cycle cap
  ("needs-escalation").
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

Resolve the image-import command first: run `scripts/pr_watchdog.py jmc --pr <pr>` and pass
its `jenkins_url` as `israel_jenkins_url` and its `command` as `jmc_command`. Then dispatch
`pr-failure-handler` with: `cycle_dir`, `pr`, `repo_root`, `worktree`, `branch`,
`base_branch`, `situation`, the failing `servers`/`failed_tests`, and `israel_jenkins_url`
/ `jmc_command`. **If the handler needs to run a test locally (reproduce/verify), it must
first run `jenkins_make_config` via that command to import the latest images** (see the
handler's "Running a test locally" section). The handler does RCA + classification in one
shot; on `trivial` it applies a code patch **in the worktree** (saves `patch.diff`); on
`non-trivial` it writes `suggested-fix.md`; on `flaky` it does nothing.

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

#### Stage 3c: Land the batch — one push + rebuild

After Step 0 and Step 1 have applied everything actionable to the worktree (and any required
Stage 4 gate has passed):

1. **Push once.** If the worktree is ahead of `origin/<branch>` (base-merge and/or fixes):
   commit anything uncommitted via `git-conventions` (`[AI generated]`), then a single
   `git -C <wt> push origin HEAD:<branch>`. Record the whole batch as one `meta.json.pushes`
   entry (list the `kinds`: e.g. `branch-update` + `prebuild`). Never `git add -A` blindly
   (see `stage-prompts.md` git-add safety).
2. **Trigger the rebuild — always.** The push does **not** auto-build CI, so immediately run
   `scripts/pr_watchdog.py trigger --pr <pr>` (with the `explicit-slugs` fallback to
   `meta.json.known_server_slugs` after a base-merge clears HEAD's statuses). Record
   `last_trigger`. Notify once (e.g. "fixes pushed & rebuild triggered").
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

### Stage 5: Final summary

When CI is green, the user hands off, or a halt fires:

1. Print the summary from `references/stage-prompts.md` (or the halt variant).
2. Do **not** remove the worktree, push uncommitted worktree changes, or delete the run
   dir.

**Gate:** summary printed; loop returns.

## Halt conditions

The loop stops and surfaces the situation when one fires. In CLI context, dispatch
`cli-escalation-notify` (`title: pr-watchdog — halted: <halt_code>`) for halts that need
the user (`branch-merge-conflict`, `worktree-conflict`, handler `blocker`, user `handoff`,
`max-runtime`). Skip notifying the Stage-1-only halts (`no-unique-branch-pr`).

- `gh-not-authenticated` — `gh auth status` failed; GitHub access is `gh`-only.
- `no-unique-branch-pr` — branch maps to zero/multiple open PRs.
- `worktree-conflict` — `make_worktree.sh` exit 3 (PR branch already checked out elsewhere,
  e.g. the user's main repo, or the path is occupied). Escalate and let the user choose.
- `branch-merge-conflict` — `update_branch.sh --apply` exit 3 (base merge conflicts).
- `pr-driver-error` — `pr_watchdog.py status` exits 1 (e.g. bad creds, PR not found).
- Handler returned `blocker:` (e.g. log fetch failed).
- User chose **(c) Hand off** at a Stage 4 gate.
- `max-runtime` — exceeded a sensible wall-clock cap (default 24h, mirroring pr_driver).

In every halt: persist state to `meta.json`, print the halt-summary variant, and leave the
worktree and run dir intact (nothing reverted or force-pushed).

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
- [ ] Only deterministic fixes (branch-update / lint / validate) were auto-pushed; every investigation-derived code fix went through the Stage 4 user gate.
- [ ] CI config / workflows / suite registry / test-stage selection were never edited to force green.
- [ ] Deterministic flows ran via the scripts; pre-test failures went to a generic `systematic-debugging` subagent (local fix-until-pass) and test failures to `pr-failure-handler`.
- [ ] In CLI context, a Slack event fired for each PR event and each analysis (state transitions/actions, not every identical poll), via `cli-escalation-notify`; status recorded in `meta.json`.
- [ ] Commits used `git-conventions` (with `[AI generated]`); no `git add -A`; protected branches never pushed to directly.
- [ ] On halt, the worktree and run dir were left intact; nothing was force-pushed or reverted.
