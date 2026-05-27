---
name: iked-test-loop
description: Orchestrate end-to-end iked / IPsec E2E test execution for a list of new + regression tests against a set of in-scope commits. Owns the tmux session, the `test_ike.sh` build-flag policy, the per-iteration runner→RCA→triage cycle, and aggregation of trivial fixes into a single working-tree diff. On non-trivial findings, surfaces a Suggested Fix to the user and asks whether to aggregate into the current diff or commit-and-continue. Use when the user asks to "run the iked test plan", "test these iked changes", "run iked regression + new tests against PR X", or similar.
---

# iked Test Loop

## Goal
Take a queue of iked / IPsec E2E targets (single tests and/or whole suites) plus the set of commits that represent the "new code under test", and drive them to completion. Aggregate trivial fixes silently. Halt on non-trivial findings, present the Suggested Fix, and let the user choose how to proceed (aggregate or commit-and-continue). End with a clean summary and an uncommitted accumulated diff the user can inspect.

This skill owns the **orchestration**. It dispatches three sub-skills (one per role) and never does their work directly:

- `iked-test-runner` — runs a single target.
- `iked-failure-rca` — gathers evidence (called by the runner on failure; the loop does not call it directly, but reads its output).
- `iked-fix-triage` — classifies a failure and either applies a trivial patch or emits a Suggested Fix.

## Inputs
- `plan` (required) — ordered list of targets to run. Each item is either a single test name (e.g. `test_ipsec_iked_tunnel_initiation`) or a suite name (`routing`, `cdnos`, `cli_tests`). The order is honored.
- `commits` (required) — list of commit SHAs in this checkout that represent the "new code under test". Used by `iked-fix-triage` to scope the trivial-fix gate. May be a single SHA.
- `suite_hint` (optional, per-item) — when a test name is ambiguous across suites, pass `--suite=<routing|cdnos|cli_tests>` via this hint.
- `save_plan` (optional, default `false`) — when `true`, also write the plan and state under `/home/dn/cheetah/.ai/plans/iked-loop-<run-id>/`. Otherwise run state stays ephemeral under `~/.iked-runs/<run-id>/`.

## Companion docs
- `/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc` — tmux, `test_ike.sh`, trace files. The loop respects this rule end-to-end.
- `/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md` — used when the user picks "commit and continue" on a non-trivial escalation. The loop uses `git-conventions` to compose the commit message.

## Hard invariants
- **Sequential.** Tests share the `e2e_*` containers; the runner calls `cleanAllDockers` at start. The loop runs one target at a time, end to end. No parallelism.
- **One tmux session for the whole loop.** Created once at start, reused for every runner dispatch. Per the iked-e2e rule, the runner reuses a pane inside it.
- **Aggregated diff, never auto-committed.** Trivial fixes land in the working tree and accumulate. The loop only ever runs `git commit` when the user explicitly picks "commit and continue" at a non-trivial gate. Loop completion does **not** commit.
- **Build-flag policy is fixed.** First run `-c`; subsequent runs `-b` if the previous fix touched `services/control/**`, no flag otherwise. The runner never picks the flag.
- **No retry budget on trivial fixes.** A test may iterate trivially as many times as needed, provided each fix passes the `iked-fix-triage` intent gate. The intent gate is the safety mechanism, not a counter.
- **Flaky → one retry max.** A `flaky` triage classification gets exactly one retry. A second failure in the same target with `failure_type` again in the flaky set is treated as non-trivial (`escalate`).
- **No code generation outside of triage.** The loop itself never edits source files. Only `iked-fix-triage` does.

## Build-flag policy (encoded as a function in the loop)

```
def pick_flag(iteration_n: int, last_touched_paths: list[str]) -> str:
    if iteration_n == 0:
        return "-c"  # first run; build infra + quagga as needed
    if any(p.startswith("services/control/") for p in last_touched_paths):
        return "-b"  # routing/iked C/C++ changed → rebuild quagga
    return ""        # only test/python changed (or flaky retry, no fix) → no flag
```

`last_touched_paths` is the `touched_paths` field from the most recent `triage.json` (empty for flaky retries, populated for trivial fixes, empty when the user just aggregated a fix manually — in that case the loop computes it from `git diff --name-only <anchor_sha>` since the last runner invocation).

## Run state layout

```
~/.iked-runs/<run-id>/
  meta.json                # run-level state (see schema below)
  plan.yml                 # queue with live status per item
  tmux_session             # one-line file with the tmux session name
  items/
    <NNN>-<slug>/          # NNN = zero-padded iteration index, slug = sanitized target
      verdict.json         # from iked-test-runner
      runner.log
      rca/
        summary.md
        evidence.json
        pytest_excerpt.txt
        shows/...
        traces/...
      triage.json          # from iked-fix-triage
      patch.diff           # trivial only
      suggested-fix.md     # non-trivial only
```

`meta.json` schema:

```json
{
  "run_id": "<YYYYMMDD-HHMMSS-rand>",
  "started_at": "<ISO-8601>",
  "repo_root": "/home/dn/cheetah",
  "commits": ["<sha>", "..."],
  "start_sha": "<HEAD at loop start>",
  "current_anchor_sha": "<HEAD or last-commit sha — accumulated diff is git diff <this>>",
  "tmux_session": "iked-loop-<run-id>",
  "plan_save_path": "<.ai/plans/... or null>"
}
```

## Workflow

### Stage 1: Initialize the run
1. Generate `run_id = YYYYMMDD-HHMMSS-<6-hex>`.
2. Create `~/.iked-runs/<run_id>/items/`.
3. `git -C <repo_root> status --porcelain` — if the working tree is dirty:
   - List the dirty files.
   - Ask the user: "The working tree has uncommitted changes. Treat them as the starting accumulated diff (the loop will keep them and add to them), or stash them first?" Wait for answer.
4. Record `start_sha = git rev-parse HEAD`, `current_anchor_sha = start_sha`.
5. Verify each input commit exists: `git cat-file -e <sha>` per commit. Halt with `blocker: bad-commit` if any fail.
6. Create tmux session: `tmux new-session -d -s iked-loop-<run_id>`. Write the name to `tmux_session` file.
7. Write `meta.json` and `plan.yml` (initial state, every item `status: queued`).
8. If `save_plan == true`, also `cp meta.json plan.yml` under `/home/dn/cheetah/.ai/plans/iked-loop-<run_id>/`.

**Gate:** `meta.json` and tmux session exist; commits are valid; user is OK with the starting working-tree state.

### Stage 2: For each plan item — run loop
For each item in `plan` (in order), and within an item for each retry iteration:

#### 2a. Pick flag
- Compute `flag = pick_flag(global_iteration_index, last_touched_paths)`. `global_iteration_index` counts every runner dispatch across the whole loop (first dispatch = 0).
- If the user is resuming from a non-trivial escalation **without** an applied fix (e.g. they chose to inspect manually), treat the next iteration as `last_touched_paths = []` → no flag.

#### 2b. Allocate item dir
- `item_dir = ~/.iked-runs/<run_id>/items/<NNN>-<slug>/`. `NNN` is the global iteration index, padded to 3 digits. `slug` is the test/suite name sanitized.

#### 2c. Dispatch the runner
Invoke `iked-test-runner` as a sub-agent with:
- `target`, `flag`, optional `suite_hint`, `run_dir = <item_dir>`, `tmux_session = iked-loop-<run_id>`.

Wait for it to return `runner_result`. Update `plan.yml`:
- `passed` → mark item `status: passed`, move to next plan item.
- `failed` → continue to 2d.
- `blocker` → halt the whole loop, surface to the user, do not move on.

#### 2d. Read triage (runner already triggered RCA)
The runner dispatches RCA on failure. The triage sub-skill is **not** auto-called by the runner — the loop dispatches it now:

Invoke `iked-fix-triage` with:
- `run_dir = <item_dir>`, `evidence_path`, `summary_path`, `commits`, `repo_root`, `previous_runs_for_this_target` (extracted from `plan.yml`).

Wait for `triage_result`.

#### 2e. Act on the triage classification

**`classification: trivial` → loop continues silently.**
- The patch is already applied to the working tree.
- Append a row to the in-memory `applied_fixes` list: `{ iteration, target, patch_path, touched_paths, rationale }`.
- Set `last_touched_paths = touched_paths`.
- Re-queue the **same target** for the next iteration. Increment global iteration index. Go to 2a.

**`classification: flaky` → one retry.**
- If this target has not been flaky-retried before in this run, set `last_touched_paths = []`, re-queue. Otherwise treat as non-trivial-escalate (Stage 3).

**`classification: non-trivial` → escalate to Stage 3.**

**Gate:** Either the loop continues with the same target (trivial / flaky-first-time) or jumps to Stage 3 (non-trivial / flaky-second-time).

### Stage 3: Non-trivial escalation — ask the user
The triage skill has already written `<item_dir>/suggested-fix.md`. The loop:

1. Prints a short prompt:

   ```
   Non-trivial failure on target <target> (iteration <N>).
   Reason: <non_trivial_reason>
   RCA: <run_dir>/rca/summary.md
   Suggested fix: <run_dir>/suggested-fix.md

   Accumulated trivial fixes since last anchor (<git short-sha of anchor>):
     <list applied_fixes between current_anchor_sha and HEAD as: "iter N — <target> — <one-line rationale>">

   How should I proceed?
     (a) Apply the suggested fix and AGGREGATE it into the current accumulated diff. Then re-queue this target.
     (b) COMMIT the current accumulated diff first, then apply the suggested fix on top of a fresh anchor. Then re-queue this target.
     (c) HAND OFF — stop the loop here. The accumulated diff stays in the working tree for you to inspect.
     (d) SKIP this target — leave it as failed in the report and move to the next plan item.
   ```

   (Use `AskQuestion` when available so the user sees a structured choice.)

2. Wait for the answer.

3. Branch on the answer:

   - **(a) Aggregate.** Apply the candidate diff from `suggested-fix.md` to the working tree (run `git apply <derived patch>` or hand-apply if the report only contains a pseudo-diff and the user provided guidance). Update `last_touched_paths` to the changed file set. Re-queue the same target. Go to Stage 2 with the next iteration.
   - **(b) Commit and continue.** Run the commit using `git-conventions`:
     - Stage everything between `current_anchor_sha` and HEAD plus the new fix: `git add -A` (scoped — see safety note below).
     - Compose a commit message via `git-conventions` Stage X (commit-messages). The body summarizes the `applied_fixes` between `current_anchor_sha` and HEAD ("Trivial fixes from iked-test-loop run <run_id>: …"). Append `[AI generated]`.
     - `git commit -m "<composed>"`.
     - Update `current_anchor_sha = git rev-parse HEAD`. Reset `applied_fixes` log for the next batch.
     - Then apply the suggested fix to the working tree (becomes the start of the **next** accumulated diff).
     - Re-queue the same target. Go to Stage 2.
   - **(c) Hand off.** Mark item `status: handed-off`, write a final summary (Stage 4), exit. Do not commit, do not clean tmux.
   - **(d) Skip.** Mark item `status: skipped-failed`, set `last_touched_paths = []` (no new code), advance to the next plan item.

**Safety on `git add` for option (b):** never run `git add -A` blindly — only stage files that:
- Are inside the accumulated diff (`git diff --name-only <current_anchor_sha>`), OR
- Were the target of an `iked-fix-triage` trivial patch (recorded in `applied_fixes`), OR
- Are the file modified by the just-presented suggested fix.

Anything else in the working tree (e.g. stray untracked files the user dropped while inspecting) must be confirmed with the user before staging.

**Gate:** User has chosen a route and the loop has acted on it. The accumulated diff state is consistent.

### Stage 4: Final summary
When the plan is exhausted (every item is `passed`, `skipped-failed`, or `handed-off`):

1. Compute the final accumulated diff: `git -C <repo_root> diff <current_anchor_sha>`.
2. Print a markdown summary:

   ```
   # iked-test-loop summary — <run_id>

   Plan: <N items> — <P passed, S skipped, H handed-off>
   Commits in scope: <list>
   Tmux session: iked-loop-<run_id> (left running)

   ## Per-item results
   - [PASS] <target> — iter <K>, flag chain: <c, c, b, c, ...>
   - [SKIP] <target> — non-trivial: <reason> — see <item_dir>/suggested-fix.md
   - [HANDOFF] <target> — non-trivial: <reason>

   ## Commits created during the loop
   - <sha> <subject>  (or "none — all fixes still uncommitted")

   ## Accumulated diff still in the working tree
   <git diff --stat <current_anchor_sha>>

   ## Where to look
   - Full per-item artifacts: ~/.iked-runs/<run_id>/items/
   - RCA reports: ~/.iked-runs/<run_id>/items/*/rca/summary.md
   - Suggested fixes (non-trivial items): ~/.iked-runs/<run_id>/items/*/suggested-fix.md
   ```

3. **Do not** kill the tmux session (the user may want to inspect panes or re-run manually). **Do not** commit the accumulated diff. **Do not** clean `~/.iked-runs/<run_id>/`.

**Gate:** Summary printed; loop returns.

## Halt conditions
The loop stops mid-flow and surfaces the situation when:

- `bad-commit` — an input commit is not reachable in the repo.
- `dirty-tree-unresolved` — the working tree was dirty at start and the user did not choose a path.
- Runner returned `blocker: <reason>` (e.g. `runner-timeout`, `pane-vanished`).
- RCA returned `blocker:` (e.g. `evidence-collection-failed`).
- Triage returned `blocker:` (e.g. `test-file-unresolved`).
- The user chose **(c) Hand off** at any non-trivial gate.

In all halt cases the loop:
1. Writes whatever state it has so far to `meta.json` and `plan.yml`.
2. Does **not** commit, does **not** clean the tmux session, does **not** clean `~/.iked-runs/<run_id>/`.
3. Prints a halt summary identifying the blocker and pointing at the relevant `<item_dir>` for inspection.

## Output format
A markdown summary (Stage 4 shape) plus the file tree under `~/.iked-runs/<run_id>/`. No YAML required — the loop is user-facing.

## Quality bar (self-check)
[ ] Exactly one tmux session was created at start; it was reused for every runner dispatch.
[ ] The build flag for each iteration came from the policy table: first iteration `-c`, then `-b` iff `last_touched_paths` includes `services/control/**`, otherwise no flag.
[ ] Trivial fixes accumulated in the working tree; the loop never auto-committed.
[ ] Non-trivial escalations always presented the Suggested Fix to the user and waited for one of the 4 documented choices before continuing.
[ ] `commit-and-continue` used `git-conventions` to compose the message and only staged files inside the accumulated diff (no blind `git add -A`).
[ ] `current_anchor_sha` was updated after every commit; "accumulated diff" always means `git diff <current_anchor_sha>`.
[ ] Plan items were attempted in input order; tests never ran in parallel.
[ ] Flaky classifications got at most one retry per target.
[ ] On any halt or hand-off, no working-tree changes were reverted, no commits were created behind the user's back, and the tmux session was left intact.
[ ] Final summary lists per-item status, commits created (if any), and the path to artifacts.
