---
name: iked-test-loop
description: Orchestrate end-to-end iked / IPsec E2E test execution for a list of new + regression tests against a set of in-scope commits. Owns the tmux session, the `test_ike.sh` build-flag policy, the per-iteration runner→handler cycle, and aggregation of trivial fixes into a single working-tree diff. On non-trivial findings, surfaces a Suggested Fix to the user and asks whether to aggregate into the current diff or commit-and-continue. Use when the user asks to "run the iked test plan", "test these iked changes", "run iked regression + new tests against PR X", or similar.
---

# iked Test Loop

## Goal

Take a queue of iked / IPsec E2E targets (single tests and/or whole suites) plus the set of commits that represent the "new code under test", and drive them to completion. Aggregate trivial fixes silently. Halt on non-trivial findings, present the Suggested Fix, and let the user choose how to proceed (aggregate or commit-and-continue). End with a clean summary and an uncommitted accumulated diff the user can inspect.

This skill owns the **orchestration**. It runs each test target itself (inline, in Stage 2c) and dispatches sub-skills only on specific events:

- `iked-failure-handler` — investigates failures, classifies them (trivial / non-trivial / flaky), applies trivial patches in-place, writes a Suggested Fix on non-trivial.
- `cli-escalation-notify` — fires at every non-trivial gate (Stage 3); pushes a Slack DM in CLI context, no-ops in the IDE.

Stage 2c uses a watcher-shell pattern (`tail -F | grep -m1 <sentinel>` + `AwaitShell` on the regex) so the loop returns within ~1s of each test finishing instead of polling on fixed sleep windows.

## Inputs

- `plan` (required) — ordered list of targets. Each item is either a single test name (e.g. `test_ipsec_iked_tunnel_initiation`) or a bare suite name (`routing`, `cdnos`, `cli_tests`). The order is honored. By convention the user front-loads new-feature tests before regression sweeps.
- `commits` (required) — list of commit SHAs in this checkout that represent the "new code under test". Used by `iked-failure-handler` to scope its trivial-fix gate.
- `suite_hint` (optional, per-item) — `--suite=<routing|cdnos|cli_tests>` when a test name is ambiguous across suites.
- `save_plan` (optional, default `false`) — when `true`, also mirror plan/state under `/home/dn/cheetah/.ai/plans/iked-loop-<run-id>/`. Otherwise state stays under `~/.iked-runs/<run-id>/`.

## Companion docs

- `/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc` — tmux, `test_ike.sh`, trace files. The loop respects this rule end-to-end.
- `/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md` — composes commit messages when the user picks "commit and continue" at a Stage 3 gate.

## Companion files in this skill

- `scripts/watcher.sh` — dual-detector watcher (sentinel + pdb-prompt poll). Run, do not read.
- `references/state-schema.md` — full `meta.json` / `verdict.json` / `kind` shapes and the run-state directory tree.
- `references/stage3-prompt.md` — Stage 3 user prompt template, branch behaviour, and `git add` safety rules.
- `references/stage4-summary.md` — Stage 4 final-summary template (and halt-summary variant).

## Hard invariants

- **Sequential.** Tests share the `e2e_*` containers; `test_ike.sh` calls `cleanAllDockers` at start. One target at a time, end to end. No parallelism.
- **One tmux session, reuse preferred.** Per the iked-e2e rule: zero existing sessions → create `iked-loop-<run_id>`; exactly one → reuse it; two or more → HALT `multiple-sessions-found`. The chosen session is reused for every iteration.
- **Aggregated diff, never auto-committed.** Trivial fixes accumulate in the working tree. The loop only runs `git commit` when the user explicitly picks "commit and continue" at a Stage 3 gate. Loop completion does **not** commit.
- **No retry budget on trivial fixes.** A test may iterate trivially as many times as needed; the handler's intent gate is the safety mechanism, not a counter.
- **Flaky → one retry max.** A second flaky failure on the same target is escalated as non-trivial.
- **No code generation outside the handler.** The loop never edits source files. Only `iked-failure-handler` does (and only when classification is `trivial`).
- **Out-of-band notification is delegated.** The loop never composes Slack messages itself. Every Stage 3 gate invokes `cli-escalation-notify` which decides whether to push (CLI) or no-op (IDE) and fails non-fatally either way.

## Target-kind policy

Each plan item is classified at Stage 2b and passed to `iked-failure-handler` so it can tune its trivial-fix gate (the handler loosens for `new` targets). Rule: bare `routing|cdnos|cli_tests` → `regression`; anything else → `new`. The user can override per item with a `<target>@new` / `<target>@regression` suffix; the loop strips the suffix when launching the test.

## Build-flag policy

First iteration → `-c`. After that, `-b` iff `last_touched_paths` includes any path under `services/control/`. Otherwise no flag.

`last_touched_paths` is the `touched_paths` field from the most recent `evidence.json` (empty for flaky retries; populated for trivial fixes; computed from `git diff --name-only <anchor_sha>` when the user manually aggregated a fix).

## Run state layout

```
~/.iked-runs/<run-id>/
  meta.json                # run-level state — schema in references/state-schema.md
  plan.yml                 # queue with live status per item
  tmux_session             # one-line file with the chosen tmux session name
  items/<NNN>-<slug>/      # NNN = zero-padded global iteration index
    kind                   # one line: new | regression
    verdict.json           # per-iteration result — schema in references/state-schema.md
    runner.log             # full test_ike.sh stdout+stderr
    rca/                   # only on failure (handler artifacts)
    patch.diff             # trivial only (handler-applied)
    suggested-fix.md       # non-trivial only (handler-emitted)
```

Full schemas: `references/state-schema.md`.

## Workflow

### Stage 1: Initialize the run

1. `run_id = YYYYMMDD-HHMMSS-<6-hex>`. Create `~/.iked-runs/<run_id>/items/`.
2. `git -C <repo_root> status --porcelain` — if dirty, list dirty files and ask the user: keep them as the starting accumulated diff or stash first? Wait for the answer.
3. Record `start_sha = git rev-parse HEAD`, `current_anchor_sha = start_sha`.
4. Verify each input commit: `git cat-file -e <sha>` per commit. HALT `bad-commit` if any fail.
5. **Tmux session selection.** `tmux list-sessions -F '#{session_name}'` (treat non-zero exit as "no sessions"). Apply the cases above. On reuse, verify the chosen session has at least one pane whose `pane_current_command` is a plain shell (`bash|zsh|fish|sh|dash`); if none, HALT `no-idle-pane`. Write the chosen name to `tmux_session`. Record `session_origin` (`created` or `reused-sole`).
6. **Detect runtime context for `meta.json`.** `is_cli_context == true` iff `$CURSOR_AGENT` set AND both `$VSCODE_AGENT_FOLDER` and `$CURSOR_LAYOUT` unset (use `printenv VAR` per variable). Record it. Slack target resolution is delegated entirely to `cli-escalation-notify` at Stage 3 — the loop does not look up handles itself.
7. Write `meta.json` and `plan.yml` (every item `status: queued`). If `save_plan == true`, mirror to `/home/dn/cheetah/.ai/plans/iked-loop-<run_id>/`.

**Gate:** `meta.json` exists, the tmux session is identified with at least one idle shell pane, all input commits are valid, the user has decided about a dirty tree.

### Stage 2: For each plan item — run loop

For each item in `plan` (in order), and within an item for each retry iteration:

#### 2a. Pick flag

Compute `flag` per the build-flag policy above. `global_iteration_index` counts every runner dispatch across the whole loop (first dispatch = 0). If the user resumed from a non-trivial gate without an applied fix, treat the next iteration as `last_touched_paths = []`.

#### 2b. Allocate item dir and classify

`item_dir = ~/.iked-runs/<run_id>/items/<NNN>-<slug>/` where `NNN` is the global iteration index (3 digits) and `slug` is the sanitized target name. Apply the target-kind rule (suffix overrides if present). Write the result to `<item_dir>/kind` and pass it to the handler on failure.

#### 2c. Run the target inline

The loop runs the test directly — no sub-agent dispatch — using the dual-detector watcher. On failure, dispatches `iked-failure-handler`.

1. **Pick the pane.** `tmux list-panes -t <tmux_session> -F '#{pane_index} #{pane_current_command} #{pane_pid} #{pane_current_path}'`. Filter to idle shell panes (`bash|zsh|fish|sh|dash`) — never send-keys into a busy pane. Among idle panes prefer in order: a pane whose `tmux capture-pane -p -S -200` shows a previous `test_ike.sh` invocation; a pane whose `pane_current_path` is under `<repo_root>`; otherwise the first idle pane. If none idle → HALT `no-idle-pane`. Record the chosen `<session>.<idx>` as `$PANE` and the wall-clock as `started_at`.

2. **Compose the command** with a completion sentinel:

   ```
   CMD='( <repo_root>/services/control/quagga/iked/scripts/test_ike.sh <flag> [--suite=<suite_hint>] <target>; \
          echo "__IKED_RUN_DONE__ rc=$?" ) 2>&1 | tee <item_dir>/runner.log'
   ```

   Omit `<flag>` entirely when empty (do not pass `""`). Insert `--suite=<suite_hint>` between flag and target when provided. `tee` truncates by default — each iteration's `runner.log` is fresh.

3. **Start the watcher** (sentinel + pdb-prompt detector, runs concurrently; whichever fires first wins; never quits the debugger):

   ```
   bash <skill_dir>/scripts/watcher.sh "<session>.<idx>" "<item_dir>/runner.log"
   ```

   `<skill_dir>` is the directory of this SKILL.md (resolve via the symlink: `readlink -f <repo>/.claude/skills/iked-test-loop` or use the absolute private path `/home/dn/.drivenets/cheetah/AI/v2/private/skills/iked-test-loop`). Run via the Shell tool with `block_until_ms=0`. Capture the shell id as `$WATCHER`. Watcher exits 0 on sentinel (line `__IKED_RUN_DONE__ rc=<n>`), 2 on debugger (line `IPDB_ACTIVE: <last-pane-line>`). Start it **before** sending the test command so an early sentinel is not missed.

4. **Send the command:** `tmux send-keys -t $PANE "$CMD" C-m` (returns immediately).

5. **Wait for the watcher:** `AwaitShell shell_id=$WATCHER pattern="(__IKED_RUN_DONE__ rc=|IPDB_ACTIVE:)" block_until_ms=7200000` (2-hour cap). Cap exceeded → HALT `runner-timeout`. `tmux list-panes -t <tmux_session>` no longer shows `$PANE` → HALT `pane-vanished`.

6. **Branch on which detector fired** (read the watcher's terminal output):

   - **Sentinel path** (`__IKED_RUN_DONE__ rc=` present):
     1. `rc = grep -oE '__IKED_RUN_DONE__ rc=[0-9]+' <item_dir>/runner.log | tail -1 | sed 's/.*rc=//'`.
     2. `status = passed if rc == 0 else failed`.
     3. Write `verdict.json` with the full schema (see `references/state-schema.md`), `pdb_state: "none"`. `containers_state` per the heuristic in that reference.

   - **Debugger path** (`IPDB_ACTIVE:` present):
     1. The test failed and pytest is paused at `ipdb> ` / `(Pdb) ` on `$PANE`. The pdb session holds live frames and locals — DO NOT quit it. The loop hands the live session to `iked-failure-handler` for richer-than-logs investigation.
     2. Write a partial `verdict.json`: `status: "failed"`, `containers_state: "live-failed"`, `pdb_state: "active"`, `pdb_pane`, `pdb_prompt: "<verbatim IPDB_ACTIVE: line>"`. Leave `returncode` / `ended_at` / `duration_seconds` null until step 8.
     3. The watcher is dead (it exited 2). Do NOT restart it yet.

7. **Dispatch `iked-failure-handler`** when `status == failed` (both paths).

   Inputs: `run_dir = <item_dir>`, `target`, `target_kind` (from `<item_dir>/kind`), `commits_in_scope = commits`, `repo_root`, `previous_runs_for_this_target` (extracted from `plan.yml`); plus `pdb_pane` and `pdb_prompt` on the debugger path.

   The handler does RCA *and* triage in a single dispatch. On `trivial` it applies the patch itself (saves to `<item_dir>/patch.diff`). On `non-trivial` it writes `<item_dir>/suggested-fix.md`. On `flaky` it does nothing. When `pdb_pane` is set, the handler is **responsible for quitting pdb** (`tmux send-keys -t <pdb_pane> "q" Enter`) before returning.

   Wait for the handler. Read `<item_dir>/rca/evidence.json` for `classification`, `next_action`, `non_trivial_reason`, `touched_paths`, `patch_path`, `suggested_fix_path`. Append `handler_status` and (when ready) `handler_summary` to `verdict.json`.

8. **Post-handler finalization** — debugger path only:

   1. Start a sentinel-only watcher: `bash -c 'tail -F <item_dir>/runner.log 2>/dev/null | grep -m1 "__IKED_RUN_DONE__ rc="'` with `block_until_ms=0`; capture as `$WATCHER2`.
   2. `AwaitShell shell_id=$WATCHER2 pattern="__IKED_RUN_DONE__ rc=" block_until_ms=600000` (10-min cap for teardown).
   3. **Sentinel within cap** → parse rc; update `verdict.json` with `returncode`, `ended_at`, `duration_seconds`, `pdb_state: "quit-by-handler"`.
   4. **Cap exceeded** → safety net: log a warning, `tmux send-keys -t $PANE "q" Enter` once, restart the watcher with a 60s cap. If the sentinel arrives → `pdb_state: "quit-by-loop-safety-net"`. Otherwise → HALT `pdb-teardown-timeout` (the pane is left in pdb for the user to inspect).

9. **Branch on `status`:** `passed` → mark plan item passed, advance to next item. `failed` → continue to Stage 2d. Any HALT above bypasses 2d.

**Gate:** `verdict.json` is fully populated, handler artifacts present iff failure, pdb is no longer active when control passes onward.

#### 2d. Act on the handler classification

Read `<item_dir>/rca/evidence.json` and branch:

- **`trivial`** — handler already applied the patch. Append a row to in-memory `applied_fixes`: `{ iteration, target, patch_path, touched_paths, rationale }` (rationale = `evidence.json.root_cause`). Set `last_touched_paths = evidence.json.touched_paths`. Re-queue the **same target** for the next iteration. Increment `global_iteration_index`. Go to 2a.
- **`flaky`** — if this target has not been flaky-retried yet in this run, set `last_touched_paths = []` and re-queue. Otherwise treat as non-trivial.
- **`non-trivial`** — escalate to Stage 3.

**Gate:** Either the loop continues with the same target or jumps to Stage 3.

### Stage 3: Non-trivial escalation

The handler has already written `<item_dir>/suggested-fix.md`.

1. **Push out-of-band notification.** Dispatch `cli-escalation-notify` with:

   - `title`: `iked-test-loop escalation — non-trivial failure on <target>`
   - `run_context`:
     - `run_id: <run_id>`
     - `repo: <repo_root>` `host: <hostname>`
     - `target: <target>` (kind `<new|regression>`)
     - `iteration: <N>`
     - `flag chain so far: <c, c, b, ...>`
     - `tmux session: <tmux_session>`
   - `body_md`:
     - **Root cause** (one sentence from `evidence.json.root_cause`).
     - **Reason** (`evidence.json.non_trivial_reason`).
     - Path to handler report: `<item_dir>/rca/summary.md`.
     - Path to suggested fix: `<item_dir>/suggested-fix.md`, followed by the first ~40 lines of that file in a Slack code block (triple-backtick). Truncate longer fixes with `... (truncated — see file)`.
     - The 4 choices (a–d) verbatim so the user knows what the loop is waiting on.

   The notify skill returns `sent` (CLI → DM delivered), `skipped` (IDE or no handle), or `send-failed` (Slack API blip). All three statuses are equivalent here — proceed regardless. Record the returned status under `meta.json.last_escalation_notify` for audit.

2. **Present the local interactive prompt** per `references/stage3-prompt.md`. Wait for the user's choice. The Slack DM (if any) is a heads-up, not the answer channel.

3. **Branch on the user's answer** per `references/stage3-prompt.md` (aggregate / commit-and-continue / hand off / skip), including the `git add` safety rules.

**Gate:** User has chosen a route and the loop has acted on it. The accumulated diff state is consistent.

### Stage 4: Final summary

When the plan is exhausted (every item is `passed`, `skipped-failed`, or `handed-off`):

1. Compute the final accumulated diff: `git -C <repo_root> diff <current_anchor_sha>`.
2. Print the markdown summary per `references/stage4-summary.md`.
3. Do **not** kill the tmux session, do **not** commit the accumulated diff, do **not** clean `~/.iked-runs/<run_id>/`.

**Gate:** Summary printed; loop returns.

## Halt conditions

The loop stops mid-flow and surfaces the situation when one of these fires:

- `bad-commit` — input commit not reachable.
- `dirty-tree-unresolved` — Stage 1 step 2; user did not pick a path.
- `multiple-sessions-found` — Stage 1 step 5; more than one tmux session exists.
- `no-idle-pane` — Stage 1 step 5 / Stage 2c step 1; no plain-shell pane available.
- `runner-timeout` — Stage 2c step 5; AwaitShell exceeded the 2-hour cap.
- `pane-vanished` — Stage 2c step 5; pane is gone after AwaitShell returned.
- `pdb-teardown-timeout` — Stage 2c step 8; handler did not quit pdb and the safety net failed.
- Handler returned `blocker:` (e.g. `runner-log-missing`, `evidence-collection-failed`).
- User chose **(c) Hand off** at any Stage 3 gate.

In every halt case:

1. Persist whatever state exists to `meta.json` and `plan.yml`.
2. Do **not** commit, do **not** clean tmux, do **not** clean `~/.iked-runs/<run_id>/`.
3. Print the halt-summary variant from `references/stage4-summary.md`.
4. Where the halt is meaningful for the user (Stage 3 hand-off, `runner-timeout`, `pdb-teardown-timeout`, handler blocker), dispatch `cli-escalation-notify` with `title: iked-test-loop halted — <halt_code>` so a CLI user is told without watching the terminal. `bad-commit` / `multiple-sessions-found` / `no-idle-pane` / `dirty-tree-unresolved` happen at Stage 1 before any work, so a notification adds noise — skip those.

## Output format

A markdown summary (Stage 4 shape) plus the file tree under `~/.iked-runs/<run_id>/`. No YAML required — the loop is user-facing.

## End-of-run checklist (compressed)

Before returning, sanity-check:

- [ ] One tmux session was selected at start and reused for every iteration; no busy panes were hijacked.
- [ ] Trivial fixes accumulated in the working tree; nothing was auto-committed; `current_anchor_sha` was updated only on explicit `commit-and-continue`.
- [ ] Every Stage 3 escalation went through `cli-escalation-notify` (status recorded in `meta.json`) and the local interactive prompt fired regardless of notify outcome.
- [ ] On the debugger path, the loop did not quit pdb itself; the handler did, with the safety net only firing on handler failure.
- [ ] On halt, the working tree was not reverted, no commits were created behind the user's back, and the tmux session was left intact.
