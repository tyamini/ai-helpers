---
name: iked-test-loop
description: Orchestrate end-to-end iked / IPsec E2E test execution for a list of new + regression tests against a set of in-scope commits. Owns the tmux session, the `test_ike.sh` build-flag policy, the per-iteration runner→handler cycle, and aggregation of trivial fixes into a single working-tree diff. On non-trivial findings, surfaces a Suggested Fix to the user and asks whether to aggregate into the current diff or commit-and-continue. Use when the user asks to "run the iked test plan", "test these iked changes", "run iked regression + new tests against PR X", or similar.
---

# iked Test Loop

## Goal
Take a queue of iked / IPsec E2E targets (single tests and/or whole suites) plus the set of commits that represent the "new code under test", and drive them to completion. Aggregate trivial fixes silently. Halt on non-trivial findings, present the Suggested Fix, and let the user choose how to proceed (aggregate or commit-and-continue). End with a clean summary and an uncommitted accumulated diff the user can inspect.

This skill owns the **orchestration**. It runs each test target itself (inline, in Stage 2c) and dispatches one sub-skill only on failure:

- `iked-failure-handler` — investigates the failure, classifies it (trivial / non-trivial / flaky), and either applies a trivial patch in-place or writes a Suggested Fix for the loop to surface.

The inline runner step uses a watcher-shell pattern (`tail -F | grep -m1 <sentinel>` + `AwaitShell` on the regex) so the loop returns within ~1s of each test finishing instead of polling on fixed sleep windows.

## Inputs
- `plan` (required) — ordered list of targets to run. Each item is either a single test name (e.g. `test_ipsec_iked_tunnel_initiation`) or a suite name (`routing`, `cdnos`, `cli_tests`). The order is honored. By convention the user front-loads **new-feature tests** (single-test items) before **regression** runs (bare suite items); the loop encodes this convention as `target_kind` (see below).
- `commits` (required) — list of commit SHAs in this checkout that represent the "new code under test". Used by `iked-failure-handler` to scope the trivial-fix gate. May be a single SHA.
- `suite_hint` (optional, per-item) — when a test name is ambiguous across suites, pass `--suite=<routing|cdnos|cli_tests>` via this hint.
- `save_plan` (optional, default `false`) — when `true`, also write the plan and state under `/home/dn/cheetah/.ai/plans/iked-loop-<run-id>/`. Otherwise run state stays ephemeral under `~/.iked-runs/<run-id>/`.

## Companion docs
- `/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc` — tmux, `test_ike.sh`, trace files. The loop respects this rule end-to-end.
- `/home/dn/cheetah/.ai/skills/common/git-conventions/SKILL.md` — used when the user picks "commit and continue" on a non-trivial escalation. The loop uses `git-conventions` to compose the commit message.

## Hard invariants
- **Sequential.** Tests share the `e2e_*` containers; `test_ike.sh` calls `cleanAllDockers` at start. The loop runs one target at a time, end to end. No parallelism.
- **One tmux session for the whole loop, reuse preferred.** Per the iked-e2e rule, the loop prefers an existing tmux session (any name) over creating its own. Selection logic: zero existing sessions → create `iked-loop-<run_id>`; exactly one → reuse it; two or more → HALT with `multiple-sessions-found` and let the user pick. The chosen session is then reused for every test iteration, and the loop reuses an idle pane inside it.
- **Aggregated diff, never auto-committed.** Trivial fixes land in the working tree and accumulate. The loop only ever runs `git commit` when the user explicitly picks "commit and continue" at a non-trivial gate. Loop completion does **not** commit.
- **Build-flag policy is fixed.** First run `-c`; subsequent runs `-b` if the previous fix touched `services/control/**`, no flag otherwise. Stage 2c never picks the flag — it gets it from Stage 2a's `pick_flag(...)`.
- **No retry budget on trivial fixes.** A test may iterate trivially as many times as needed, provided each fix passes the handler's intent gate. The intent gate is the safety mechanism, not a counter.
- **Flaky → one retry max.** A `flaky` handler classification gets exactly one retry. A second failure in the same target with `failure_type` again in the flaky set is treated as non-trivial (`escalate`).
- **No code generation outside the handler.** The loop itself never edits source files. Only `iked-failure-handler` does (and only when it classifies the failure as `trivial`).
- **Push escalations to Slack only in CLI context.** When `is_cli_context == true` and `slack_target` resolved at Stage 1, every Stage 3 escalation pushes a DM with the run/target/iteration, the handler's root-cause, and the suggested fix excerpt to `slack_target` **before** the interactive prompt fires. The IDE path (`is_cli_context == false`) never pushes — the user is already in front of the chat panel. Slack send failures are non-fatal; the loop always falls back to the local interactive prompt.

## Target-kind policy (encoded as a function in the loop)

Each plan item is classified as `new` or `regression` at item-allocation time (Stage 2b). The classification is passed to `iked-failure-handler` so it can tune its strictness — the handler loosens its trivial-fix gate for `new` targets (see that skill).

```
def target_kind(target: str) -> str:
    # Bare suite names are full regression sweeps by definition.
    if target in {"routing", "cdnos", "cli_tests"}:
        return "regression"
    # Anything else is a specific test method/file — by convention these
    # are the new-feature tests the user queues at the front of the plan.
    return "new"
```

This rule is intentionally syntactic (target shape, not git history). If the user wants to override on a per-item basis they can pass the item as `<target>@regression` or `<target>@new`; the loop strips the suffix when launching the test and uses the explicit kind. Absent a suffix, the function above decides.

## Build-flag policy (encoded as a function in the loop)

```
def pick_flag(iteration_n: int, last_touched_paths: list[str]) -> str:
    if iteration_n == 0:
        return "-c"  # first run; build infra + quagga as needed
    if any(p.startswith("services/control/") for p in last_touched_paths):
        return "-b"  # routing/iked C/C++ changed → rebuild quagga
    return ""        # only test/python changed (or flaky retry, no fix) → no flag
```

`last_touched_paths` is the `touched_paths` field from the most recent `evidence.json` (empty for flaky retries, populated for trivial fixes, empty when the user just aggregated a fix manually — in that case the loop computes it from `git diff --name-only <anchor_sha>` since the last runner invocation).

## Run state layout

```
~/.iked-runs/<run-id>/
  meta.json                # run-level state (see schema below)
  plan.yml                 # queue with live status per item
  tmux_session             # one-line file with the tmux session name
  items/
    <NNN>-<slug>/          # NNN = zero-padded iteration index, slug = sanitized target
      verdict.json         # from the inline runner step (Stage 2c)
      runner.log           # full `test_ike.sh` stdout+stderr (tee'd from the pane)
      rca/                 # only on failure (handler artifacts)
        summary.md
        evidence.json      # machine-readable contract with the loop
        pytest_excerpt.txt
        shows/...
        traces/...
      patch.diff           # trivial only (handler-applied)
      suggested-fix.md     # non-trivial only (handler-emitted)
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
  "plan_save_path": "<.ai/plans/... or null>",
  "is_cli_context": true,
  "slack_target": "<Slack user ID (e.g. U01ABC2DEF) or null>",
  "slack_target_handle": "<the handle/email used to look the user up — for audit; null when not resolved>"
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
6. Select the tmux session per the iked-e2e rule's "prefer reuse" requirement:
   1. List existing sessions: `tmux list-sessions -F '#{session_name}'` (treat a non-zero exit as "no sessions" — tmux returns 1 when there is no server).
   2. Zero sessions → create one: `tmux new-session -d -s iked-loop-<run_id>`. Record `session_origin: created`.
   3. Exactly one session → reuse it verbatim. Record `session_origin: reused-sole`.
   4. Two or more sessions → HALT with `blocker: multiple-sessions-found`. Surface to the parent a list of `<session_name> (<n> windows / <m> panes, last activity <ISO-8601>)` lines so the user can pick. Resume on the next invocation with the chosen session name passed in (treat the picked name as if it were the sole session in step 3). Do **not** auto-pick.
   5. Verify the chosen session has at least one pane whose `pane_current_command` is a plain shell (`bash`, `zsh`, `fish`, `sh`, `dash`). If none, HALT with `blocker: no-idle-pane`; the runner cannot send-keys safely into a pane running a foreground program.
   6. Write the chosen session name to the `tmux_session` file.
7. Detect runtime context and resolve the Slack escalation target (used only at Stage 3 to push non-trivial findings out-of-band):
   1. `is_cli_context` is `true` iff `$CURSOR_AGENT` is set AND both `$VSCODE_AGENT_FOLDER` and `$CURSOR_LAYOUT` are unset. The Cursor IDE host always sets the latter two (`CURSOR_LAYOUT=unifiedAgent` for the agent panel); the standalone `cursor-agent` CLI does not. Use `printenv` per-variable, not a substring grep, to avoid false positives.
   2. When `is_cli_context == false` → set `slack_target = null`, `slack_target_handle = null`, skip the rest of step 7. The IDE user is already watching the chat panel; no Slack push is needed.
   3. When `is_cli_context == true`, resolve the Slack handle by checking, in order, the first non-empty value:
      - Env var `IKED_LOOP_SLACK_USER` (explicit override; pass through verbatim).
      - `git -C <repo_root> config user.email` → take the local-part (substring before `@`).
      - `git -C <repo_root> config user.name`.
      Record this as `slack_target_handle`. If all three are empty → set `slack_target = null` and log a warning that Stage 3 will fall back to interactive-only.
   4. With a non-null handle, look the Slack user up via `slackbot_slack_find_user(username_or_display_name=<handle>)`. On success record the returned Slack user ID as `slack_target`. On failure (no match / Slack error) → set `slack_target = null`, log a one-line warning identifying the handle that did not resolve, and continue. Do **not** halt the loop — Stage 3 must still work without Slack.
8. Write `meta.json` and `plan.yml` (initial state, every item `status: queued`). Include `session_origin`, `is_cli_context`, `slack_target`, and `slack_target_handle` in `meta.json` so subsequent runs and audits can reconstruct the choices.
9. If `save_plan == true`, also `cp meta.json plan.yml` under `/home/dn/cheetah/.ai/plans/iked-loop-<run_id>/`.

**Gate:** `meta.json` exists, the tmux session is identified (reused or created), it has at least one idle shell pane, commits are valid, the user is OK with the starting working-tree state, and `is_cli_context` plus `slack_target` are recorded (the latter may be `null` and that is OK).

### Stage 2: For each plan item — run loop
For each item in `plan` (in order), and within an item for each retry iteration:

#### 2a. Pick flag
- Compute `flag = pick_flag(global_iteration_index, last_touched_paths)`. `global_iteration_index` counts every runner dispatch across the whole loop (first dispatch = 0).
- If the user is resuming from a non-trivial escalation **without** an applied fix (e.g. they chose to inspect manually), treat the next iteration as `last_touched_paths = []` → no flag.

#### 2b. Allocate item dir and classify the target
- `item_dir = ~/.iked-runs/<run_id>/items/<NNN>-<slug>/`. `NNN` is the global iteration index, padded to 3 digits. `slug` is the test/suite name sanitized.
- Compute `kind = target_kind(target)` per the policy function above. Record it under `<item_dir>/kind` (one line: `new` or `regression`) so subsequent dispatches read it from disk and the run state is auditable. This value is passed to `iked-failure-handler` if the item fails.

#### 2c. Run the target inline

The loop runs the test directly — no sub-agent dispatch — using a watcher-shell pattern that returns within ~1s of the test finishing instead of polling on fixed sleep windows. On failure, dispatches `iked-failure-handler`.

1. **Pick the pane** in the chosen tmux session:
   - `tmux list-panes -t <tmux_session> -F '#{pane_index} #{pane_current_command} #{pane_pid} #{pane_current_path}'`.
   - Filter to **idle panes** (`pane_current_command` is one of `bash`, `zsh`, `fish`, `sh`, `dash`). Never send-keys into a busy pane — that hijacks the user's foreground work.
   - Among idle panes, prefer in order:
     - A pane whose recent capture (`tmux capture-pane -p -t <session>.<idx> -S -200`) contains a previous `test_ike.sh` invocation.
     - A pane whose `pane_current_path` resolves under `<repo_root>` (e.g. `/home/dn/cheetah` or a worktree thereof).
     - The first idle pane.
   - If no pane is idle → HALT with `blocker: no-idle-pane`.
   - Record the chosen `<session>.<idx>` as `$PANE` and the wall-clock as `started_at` (ISO-8601).

2. **Compose the command line** with a completion sentinel:

   ```
   CMD='( <repo_root>/services/control/quagga/iked/scripts/test_ike.sh <flag> [--suite=<suite_hint>] <target>; \
          echo "__IKED_RUN_DONE__ rc=$?" ) 2>&1 | tee <item_dir>/runner.log'
   ```

   - If `flag` is empty, omit it entirely (do not pass `""`).
   - If `suite_hint` is provided, insert `--suite=<suite_hint>` between the flag and `target`.
   - `target` is the last positional arg.
   - `tee` truncates by default — each iteration's `runner.log` is fresh.

3. **Start a watcher shell** that exits when EITHER the sentinel is written OR an interactive debugger prompt (`ipdb> ` / `(Pdb) `) appears in the pane. The two detectors run concurrently; whichever fires first wins. The watcher NEVER quits the debugger — pdb's live state is a high-value investigation artifact and the loop hands it off to `iked-failure-handler` intact:

   ```bash
   bash -c '
     PANE="<session>.<idx>"
     LOG="<item_dir>/runner.log"

     # Watcher A — event-driven sentinel. tail -F follows the file even
     # before it exists; grep -m1 exits on first match and SIGPIPEs tail.
     ( tail -F "$LOG" 2>/dev/null | grep -m1 "__IKED_RUN_DONE__ rc=" ) &
     WA=$!

     # Watcher B — polled (5s) detection of an interactive debugger
     # prompt as the LAST non-empty line in the pane. pdb prompts have
     # no trailing newline and may not flush through `tee`, so we read
     # the pane directly. We DO NOT send keys to dismiss — the handler
     # owns the live session.
     #
     # IMPORTANT: `tmux capture-pane -p` strips trailing whitespace from
     # each line, so the captured pdb prompt is literally `ipdb>` /
     # `(Pdb)` with NO trailing space — case patterns must match without
     # the space. The `*` after the prompt allows optional trailing chars.
     (
       while true; do
         sleep 5
         last=$(tmux capture-pane -t "$PANE" -p -S -3 2>/dev/null \
                | grep -v "^$" | tail -1)
         case "$last" in
           "ipdb>"*|"(Pdb)"*)
             echo "IPDB_ACTIVE: $last"
             exit 2
             ;;
         esac
       done
     ) &
     WB=$!

     wait -n $WA $WB
     RC=$?
     kill $WA $WB 2>/dev/null
     exit $RC
   '
   ```

   - Run via the Shell tool with `block_until_ms=0` (immediate background).
   - Capture the returned shell id as `$WATCHER`.
   - Outcomes:
     - **Sentinel fires** → Watcher A's `grep -m1` prints the matched line and exits 0; `wait -n` returns 0; the outer shell exits 0.
     - **Debugger fires** → Watcher B prints `IPDB_ACTIVE: <last-line>` and exits 2; `wait -n` returns 2; the outer shell exits 2.
   - Starting the watcher **before** `send-keys` avoids missing an early sentinel and ensures the debugger poll is already poised.
   - Requires bash 4.3+ for `wait -n` (Ubuntu default).

4. **Send the command to the pane**:

   ```
   tmux send-keys -t $PANE "$CMD" C-m
   ```

   Regular (foreground) Shell call — `send-keys` returns immediately after queuing the keystrokes.

5. **Wait for the watcher** via AwaitShell:

   - `AwaitShell shell_id=$WATCHER pattern="(__IKED_RUN_DONE__ rc=|IPDB_ACTIVE:)" block_until_ms=7200000` (2-hour wall-clock cap).
   - Returns within ~1s of sentinel write, within ~5s of a debugger appearing, or after the cap.
   - Cap exceeded → HALT with `blocker: runner-timeout`. Pane and `e2e_*` containers left untouched.
   - `tmux list-panes -t <tmux_session>` no longer shows `$PANE` → HALT with `blocker: pane-vanished`.

6. **Branch on which detector fired**. Read the watcher's terminal output to classify:

   - **6a — Sentinel path** (output contains `__IKED_RUN_DONE__ rc=`):
     1. Extract rc: `grep -oE '__IKED_RUN_DONE__ rc=[0-9]+' <item_dir>/runner.log | tail -1 | sed 's/.*rc=//'`.
     2. `status = passed if rc == 0 else failed`.
     3. `containers_state` heuristic:
        - `rc == 0` → `torn-down-passed`.
        - Else if `runner.log` contains `==> Running ` (the marker `test_ike.sh` prints right before `make`) → `live-failed`.
        - Else → `torn-down-by-script` (typically a build / image-missing / infra failure).
     4. Write `<item_dir>/verdict.json` with all fields, `pdb_state: "none"`.

   - **6b — Debugger path** (output contains `IPDB_ACTIVE:`):
     1. The test failed and pytest is paused in an interactive debugger on `$PANE`. The pdb session holds the test's live stack frames, locals, and source listing — DO NOT quit it. The loop hands the live session to `iked-failure-handler` for richer-than-logs investigation.
     2. Write `<item_dir>/verdict.json` with partial fields:
        - `status: "failed"`, `containers_state: "live-failed"`, `pdb_state: "active"`, `pdb_pane: "<session>.<idx>"`, `pdb_prompt: "<verbatim IPDB_ACTIVE: line>"`.
        - Omit `returncode` and `ended_at` (test hasn't returned yet; will be filled in step 8).
     3. The watcher is now dead (it exited 2). Do NOT restart it yet — wait for the handler in step 7 first.

   Common to both paths: `verdict.json` schema is:

   ```json
   {
     "target": "<target>",
     "suite_hint": "<suite_hint or null>",
     "flag_used": "-c|-b|",
     "status": "passed|failed",
     "started_at": "<ISO-8601 from step 1>",
     "tmux_pane": "<session>.<idx>",
     "runner_log": "<abs path>",
     "containers_state": "live-failed | torn-down-passed | torn-down-by-script",
     "pdb_state": "none | active | quit-by-rca | quit-by-loop-safety-net",
     "pdb_pane": "<session>.<idx> | null",
     "pdb_prompt": "<string or null>",
     "returncode": <n or null>,
     "ended_at": "<ISO-8601 or null>",
     "duration_seconds": <float or null>
   }
   ```

7. **Dispatch `iked-failure-handler`** when `status == failed` (both 6a and 6b paths):

   - Inputs: `run_dir = <item_dir>`, `target`, `target_kind` (from `<item_dir>/kind`), `commits_in_scope = commits`, `repo_root`, `previous_runs_for_this_target` (extracted from `plan.yml`), plus (when 6b) `pdb_pane` and `pdb_prompt`.
   - The handler does both RCA *and* triage in a single dispatch and, when classification is `trivial`, applies the patch to the working tree itself (saves to `<item_dir>/patch.diff`). On `non-trivial`, it writes `<item_dir>/suggested-fix.md` and leaves the working tree alone. On `flaky`, it does nothing.
   - When `pdb_pane` is present, the live debugger session is the **primary** investigation resource (interactive frame/locals inspection via `tmux send-keys` + `tmux capture-pane`), and the handler is **responsible for quitting pdb** (`tmux send-keys -t <pdb_pane> "q" Enter`) before returning so the loop can collect the final rc.
   - The handler enforces its own wall-clock budget (see that skill). The loop does NOT add a Task-level timeout; if the handler returns with `status: ready` but flagged confidence: low and `next_action: escalate`, surface it normally as a non-trivial escalation in Stage 3.
   - Wait for the handler. Read `<item_dir>/rca/evidence.json` for `classification`, `next_action`, `touched_paths`, `patch_path`, `suggested_fix_path`. Append `handler_status: "ready" | "missing"` and (when ready) `handler_summary: "<abs path>"` to `verdict.json`.

8. **Post-handler finalization** (debugger path only — sentinel path skips this):

   By now the handler should have quit pdb and pytest is running teardown. Wait for the (deferred) sentinel:

   1. Start a sentinel-only watcher:

      ```bash
      bash -c 'tail -F <item_dir>/runner.log 2>/dev/null | grep -m1 "__IKED_RUN_DONE__ rc="'
      ```

      `block_until_ms=0`; capture as `$WATCHER2`.
   2. `AwaitShell shell_id=$WATCHER2 pattern="__IKED_RUN_DONE__ rc=" block_until_ms=600000` (10-min cap for teardown).
   3. **Sentinel within cap** → parse rc; update `verdict.json` with `returncode`, `ended_at`, `duration_seconds`, `pdb_state: "quit-by-handler"`.
   4. **Cap exceeded** → safety net: log a warning, send `q` Enter to `$PANE` once, restart the watcher with a 60s cap. If sentinel arrives → update `verdict.json` with `pdb_state: "quit-by-loop-safety-net"`. If still no sentinel → HALT with `blocker: pdb-teardown-timeout` (handler did not quit pdb cleanly and the safety net failed).

9. **Branch on `status`**:

   - `passed` → mark plan item `status: passed`, move to next plan item (Stage 2a with the next item).
   - `failed` → continue to Stage 2d (Act on the handler classification).
   - HALT conditions above (no-idle-pane, runner-timeout, pane-vanished, pdb-teardown-timeout) skip Stage 2d entirely and surface to the user.

**Gate:** `verdict.json` exists with the appropriate fields (full schema on sentinel path; partial then completed on debugger path), handler artifacts present iff failure, pdb is no longer active when control passes to Stage 2d or to the next plan item.

#### 2d. Act on the handler classification

Read `<item_dir>/rca/evidence.json` (the handler's machine-readable output). Branch on `classification`:

**`classification: trivial` → loop continues silently.**
- The handler already applied the patch to the working tree (path recorded in `evidence.json.patch_path`).
- Append a row to the in-memory `applied_fixes` list: `{ iteration, target, patch_path, touched_paths, rationale }` — `touched_paths` and `patch_path` come from `evidence.json`; `rationale` is the handler's one-line root-cause from `evidence.json.root_cause`.
- Set `last_touched_paths = evidence.json.touched_paths`.
- Re-queue the **same target** for the next iteration. Increment global iteration index. Go to 2a.

**`classification: flaky` → one retry.**
- If this target has not been flaky-retried before in this run, set `last_touched_paths = []`, re-queue. Otherwise treat as non-trivial-escalate (Stage 3).

**`classification: non-trivial` → escalate to Stage 3.** The handler has already written `<item_dir>/suggested-fix.md`.

**Gate:** Either the loop continues with the same target (trivial / flaky-first-time) or jumps to Stage 3 (non-trivial / flaky-second-time).

### Stage 3: Non-trivial escalation — ask the user
The handler has already written `<item_dir>/suggested-fix.md`. The loop:

1. Prints a short prompt:

   ```
   Non-trivial failure on target <target> (iteration <N>).
   Reason: <non_trivial_reason>
   Handler report: <run_dir>/rca/summary.md
   Suggested fix:  <run_dir>/suggested-fix.md

   Accumulated trivial fixes since last anchor (<git short-sha of anchor>):
     <list applied_fixes between current_anchor_sha and HEAD as: "iter N — <target> — <one-line rationale>">

   How should I proceed?
     (a) Apply the suggested fix and AGGREGATE it into the current accumulated diff. Then re-queue this target.
     (b) COMMIT the current accumulated diff first, then apply the suggested fix on top of a fresh anchor. Then re-queue this target.
     (c) HAND OFF — stop the loop here. The accumulated diff stays in the working tree for you to inspect.
     (d) SKIP this target — leave it as failed in the report and move to the next plan item.
   ```

   (Use `AskQuestion` when available so the user sees a structured choice.)

2. **Push to Slack if in CLI context.** Read `is_cli_context` and `slack_target` from `meta.json`. When both `is_cli_context == true` AND `slack_target != null`, send a DM via `slackbot_slack_send_msg(channel=<slack_target>, message_content=<body>)` **before** waiting for the user's answer in step 3. The body must include, in this order:
   - One-line header: `:rotating_light: iked-test-loop escalation — run <run_id>` on `<repo_root>` (host `<hostname>`).
   - Agent info: `target: <target>` (kind `<new|regression>`), `iteration: <N>`, `flag chain so far: <c, c, b, ...>`, `tmux session: <tmux_session>`.
   - Problem description: the `root_cause` and `non_trivial_reason` strings from `<item_dir>/rca/evidence.json`, followed by the absolute path to the full handler report (`<item_dir>/rca/summary.md`).
   - Suggested fix: the absolute path (`<item_dir>/suggested-fix.md`) followed by the first ~40 lines of that file quoted inside a Slack code block (triple-backtick). Truncate longer fixes with `... (truncated — see file)`.
   - The 4 choices (a–d) verbatim so the user knows what the loop is waiting on.
   - A final line: `Reply locally via the agent prompt — this Slack message is a push notification, not the answer channel.`

   The DM is purely informational. Send failures (Slack API error, network blip, etc.) are non-fatal: log a one-line warning and continue to step 3 with the local interactive prompt as before. Do NOT retry, do NOT halt.

3. Wait for the answer.

4. Branch on the answer:

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
- Were the target of a handler trivial patch (recorded in `applied_fixes`), OR
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
   - Handler reports: ~/.iked-runs/<run_id>/items/*/rca/summary.md
   - Suggested fixes (non-trivial items): ~/.iked-runs/<run_id>/items/*/suggested-fix.md
   - Applied trivial patches: ~/.iked-runs/<run_id>/items/*/patch.diff
   ```

3. **Do not** kill the tmux session (the user may want to inspect panes or re-run manually). **Do not** commit the accumulated diff. **Do not** clean `~/.iked-runs/<run_id>/`.

**Gate:** Summary printed; loop returns.

## Halt conditions
The loop stops mid-flow and surfaces the situation when:

- `bad-commit` — an input commit is not reachable in the repo.
- `dirty-tree-unresolved` — the working tree was dirty at start and the user did not choose a path.
- `multiple-sessions-found` — Stage 1 step 6.4: more than one tmux session exists and the user has not yet chosen which to reuse. Surface the session list and wait for a pick.
- `no-idle-pane` — Stage 1 step 6.5 or Stage 2c step 1: the chosen session has no pane in a plain shell (`bash`/`zsh`/`fish`/`sh`/`dash`) at the moment a test needs to be sent. The user must free a pane or pick a different session.
- `runner-timeout` — Stage 2c step 5: AwaitShell on the dual-detector watcher exceeded the 2-hour wall-clock cap. Pane and containers are left untouched.
- `pane-vanished` — Stage 2c step 5: the chosen pane is no longer listed after AwaitShell returned.
- `pdb-teardown-timeout` — Stage 2c step 8 safety net: pdb was detected, the handler was dispatched, but the sentinel never appeared even after the loop sent a fallback `q` Enter. The pane is left as-is (likely still in pdb) so the user can inspect.
- Handler returned `blocker:` (e.g. `runner-log-missing`, `evidence-collection-failed`, `test-file-unresolved`).
- The user chose **(c) Hand off** at any non-trivial gate.

In all halt cases the loop:
1. Writes whatever state it has so far to `meta.json` and `plan.yml`.
2. Does **not** commit, does **not** clean the tmux session, does **not** clean `~/.iked-runs/<run_id>/`.
3. Prints a halt summary identifying the blocker and pointing at the relevant `<item_dir>` for inspection.

## Output format
A markdown summary (Stage 4 shape) plus the file tree under `~/.iked-runs/<run_id>/`. No YAML required — the loop is user-facing.

## Quality bar (self-check)
[ ] Exactly one tmux session was selected at start per the iked-e2e "prefer reuse" rule (zero existing → created; one → reused; multiple → halted with `multiple-sessions-found` for the user to pick). The chosen session was reused for every test iteration.
[ ] Per-iteration pane selection (Stage 2c step 1) only ever picked an idle shell pane, preferring `test_ike.sh`-tagged panes then repo-root-cwd panes. No pane running user work was hijacked.
[ ] Each test iteration used the dual-detector watcher (event-driven sentinel via `tail -F | grep -m1` + parallel 5s-poll pdb prompt detector via `tmux capture-pane`) — happy-path sentinel returns within ~1s, no fixed-interval polling of the test result itself.
[ ] On debugger path, the loop did **not** quit pdb itself — it handed the live `pdb_pane` to `iked-failure-handler` as the primary investigation resource. The handler quit pdb before returning. The loop's post-handler safety net only fires if the handler failed to quit.
[ ] Each plan item was classified at Stage 2b via the `target_kind()` function (single test → `new`, bare suite → `regression`, explicit `@new`/`@regression` suffix honored), recorded under `<item_dir>/kind`, and passed to `iked-failure-handler` on failure dispatches.
[ ] The build flag for each iteration came from the policy table: first iteration `-c`, then `-b` iff `last_touched_paths` includes `services/control/**`, otherwise no flag.
[ ] `containers_state` in `verdict.json` was set per the heuristic (`==> Running ` marker presence) on every failure, so `iked-failure-handler` knows whether containers are live.
[ ] Trivial fixes accumulated in the working tree; the loop never auto-committed.
[ ] At Stage 1 step 7, `is_cli_context` was set per the env-var rule (`CURSOR_AGENT` set AND `VSCODE_AGENT_FOLDER`/`CURSOR_LAYOUT` both unset), the Slack handle was resolved via the documented chain (`IKED_LOOP_SLACK_USER` → git `user.email` local-part → git `user.name`) and looked up via `slackbot_slack_find_user`, and both `is_cli_context` and `slack_target` (or `null`) were persisted in `meta.json`. A failed Slack lookup did not halt the loop.
[ ] Non-trivial escalations always presented the Suggested Fix to the user and waited for one of the 4 documented choices before continuing.
[ ] Whenever `is_cli_context == true` AND `slack_target != null`, every Stage 3 escalation pushed a DM via `slackbot_slack_send_msg` to `slack_target` (containing run/target/iteration, root-cause + non_trivial_reason, and the suggested-fix excerpt) **before** the interactive prompt fired. IDE-context runs (`is_cli_context == false`) never pushed. Slack send failures were logged and bypassed without halting.
[ ] `commit-and-continue` used `git-conventions` to compose the message and only staged files inside the accumulated diff (no blind `git add -A`).
[ ] `current_anchor_sha` was updated after every commit; "accumulated diff" always means `git diff <current_anchor_sha>`.
[ ] Plan items were attempted in input order; tests never ran in parallel.
[ ] Flaky classifications got at most one retry per target.
[ ] On any halt or hand-off, no working-tree changes were reverted, no commits were created behind the user's back, and the tmux session was left intact.
[ ] Final summary lists per-item status, commits created (if any), and the path to artifacts.
