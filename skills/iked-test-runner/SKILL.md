---
name: iked-test-runner
description: Run a single iked / IPsec E2E test or whole suite via `test_ike.sh` inside a reused tmux pane, capture pass/fail with structured evidence, and — on failure — leave containers up and dispatch `iked-failure-rca` before returning. Use when the parent `iked-test-loop` (or a human) needs to execute one test/suite and get a deterministic verdict on disk.
disable-model-invocation: true
---

# iked Test Runner

## Goal
Execute exactly **one** test target (single test name or a whole suite) for the iked / IPsec E2E suites, in a tmux pane, with a parent-chosen `test_ike.sh` flag. Produce a deterministic verdict file. On failure, **do not destruct** the containers and dispatch `iked-failure-rca` to gather evidence before returning.

This skill is intentionally narrow: one invocation = one test/suite = one verdict.

## Inputs
- `target` (required) — a single test name (e.g. `test_ipsec_iked_tunnel_initiation`) or a suite name (`routing`, `cdnos`, `cli_tests`). The skill never runs more than one target per invocation.
- `flag` (required) — one of `"-c"`, `"-b"`, or `""` (empty string = no flag). Chosen by the parent `iked-test-loop` per its build-flag policy. The runner does **not** decide this.
- `suite_hint` (optional) — `routing` | `cdnos` | `cli_tests`. Passed through as `--suite=<hint>` to `test_ike.sh` to disambiguate when a test name lives in more than one suite. Omit when unambiguous.
- `run_dir` (required) — absolute path to the per-run directory, typically `~/.iked-runs/<run-id>/items/<seq>-<slug>/`. The runner writes all artifacts under this directory.
- `tmux_session` (required) — name of the tmux session the parent already owns (e.g. `iked-loop-<run-id>`). The runner attaches to this session; it does not create new sessions.

## Companion docs (read once per skill load)
- `/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc` — source of truth for tmux requirement, `test_ike.sh` invocation, container names (`e2e_R*_*`), trace file paths, and the "never sleep more than 30s" agent rule.

## Hard invariants
- Tests run **only** inside tmux — never in a plain shell.
- Polling intervals **never exceed 30 seconds** (per the iked-e2e rule).
- On failure, **never** invoke any cleanup command (`cleanAllDockers`, `docker rm`, `docker stop`, etc.). The next runner invocation will reset state via `test_ike.sh`'s own `cleanAllDockers` call.
- The runner **must not** pick its own flag. The flag arrives from the parent. If the input is missing or invalid, halt with `blocker: bad-flag`.

## Workflow

### Stage 1: Validate inputs and prepare run dir
1. Verify `run_dir` exists or create it (`mkdir -p`).
2. Verify `target` is a non-empty string. Halt with `blocker: bad-target` if empty.
3. Verify `flag` is one of `-c`, `-b`, or empty. Halt with `blocker: bad-flag` otherwise.
4. Snapshot the chosen tmux session — `tmux has-session -t <tmux_session>`. Halt with `blocker: missing-tmux-session` if it does not exist. The parent owns session creation; the runner does not create one.

**Gate:** `run_dir` is writable; `tmux_session` exists; inputs are well-formed.

### Stage 2: Pick the tmux pane (reuse, never create new)
Per the iked-e2e rule: prefer reusing a pane that previously ran a test.

1. List panes: `tmux list-panes -t <tmux_session> -F '#{pane_index} #{pane_current_command} #{pane_pid}'`.
2. Pick a pane in this order:
   - A pane whose recent capture (`tmux capture-pane -p -t <session>.<idx>`) contains a previous `test_ike.sh` invocation — prefer it.
   - Otherwise, the first pane in the session.
3. Record the chosen `<session>.<pane>` as `$PANE` in this run's context.

Do **not** open new panes. The session has whatever the parent set up; we adapt to it.

**Gate:** `$PANE` is bound to a single existing pane.

### Stage 3: Compose the command line
Build the exact command to send to the pane:

```
~/cheetah/services/control/quagga/iked/scripts/test_ike.sh <flag> [--suite=<suite_hint>] <target>
```

Rules:
- If `flag` is empty, omit it entirely (do not pass `""`).
- If `suite_hint` is provided, insert `--suite=<suite_hint>` between the flag and `target`.
- `target` is the last positional arg.
- Wrap the whole invocation with a sentinel so we can deterministically detect completion in pane output:

```
( ~/cheetah/services/control/quagga/iked/scripts/test_ike.sh <flag> [--suite=...] <target>; echo "__IKED_RUN_DONE__ rc=$?" ) 2>&1 | tee <run_dir>/runner.log
```

The `tee` to `runner.log` captures the raw output for the RCA skill regardless of pane scrollback. The `__IKED_RUN_DONE__ rc=<n>` sentinel is the completion marker the runner polls for.

**Gate:** Command line is composed and `runner.log` path is known.

### Stage 4: Send the command and poll for completion
1. Send to the pane:
   ```
   tmux send-keys -t $PANE "<composed-command>" C-m
   ```
2. Poll loop:
   - Sleep ≤ 30s between checks.
   - Each iteration:
     - `grep -m1 "__IKED_RUN_DONE__ rc=" <run_dir>/runner.log` → if present, parse `rc=<n>` and exit the loop.
     - Otherwise check the tail of `runner.log` for early-fatal signals (`ERROR cleanAllDockers`, container-missing-image, `FAILED:` immediately after launch). If found, still wait for the sentinel — the script always prints it via the subshell.
   - Total wall-clock cap: **120 minutes**. If exceeded, halt with `blocker: runner-timeout` and leave the pane untouched (no kill — preserves state for the user / RCA).

**Gate:** Either the sentinel was seen (with `rc`) or `blocker: runner-timeout`.

### Stage 5: Write the verdict file
On clean completion (sentinel observed), write `<run_dir>/verdict.json`:

```json
{
  "target": "<target>",
  "suite_hint": "<suite_hint or null>",
  "flag_used": "-c|-b|",
  "returncode": <n>,
  "status": "passed|failed",
  "started_at": "<ISO-8601>",
  "ended_at": "<ISO-8601>",
  "duration_seconds": <float>,
  "tmux_pane": "<session>.<idx>",
  "runner_log": "<absolute path to runner.log>",
  "containers_state": "live-failed | torn-down-passed | torn-down-by-script"
}
```

`containers_state` is decided as follows:
- `returncode == 0` → `torn-down-passed` (containers may or may not be up; from the loop's perspective treat them as disposable — the next run will `cleanAllDockers`).
- `returncode != 0` and the failure happened **after** `cleanAllDockers` and **before** the script returned (typical pytest failure) → `live-failed`. This is the case where containers are up and reachable for RCA.
- `returncode != 0` but the failure happened **before** containers came up (build failure, image-missing, infra setup failure) → `torn-down-by-script`. No live containers; RCA will only have `runner.log` and pytest excerpt to work with.

A simple heuristic to pick between `live-failed` and `torn-down-by-script`: parse `runner.log` for the line `==> Running ` (the script prints this after `cleanAllDockers`/`clearSpace` and before `make`). If it appears, containers were brought up → `live-failed`. If not, → `torn-down-by-script`.

**Gate:** `verdict.json` exists at `<run_dir>/verdict.json` and is valid JSON.

### Stage 6: On failure, dispatch `iked-failure-rca`
Skip this stage entirely on `status: passed` and return.

When `status: failed`:

1. Dispatch the `iked-failure-rca` skill as a sub-agent with inputs:
   - `run_dir` (same as ours)
   - `target` (same)
   - `verdict_path` = `<run_dir>/verdict.json`
   - `runner_log` = `<run_dir>/runner.log`
   - `containers_state` (from the verdict)
2. Wait for it to return. It must produce `<run_dir>/rca/summary.md` and `<run_dir>/rca/evidence.json`. If it does not, append `rca_status: "missing"` to `verdict.json`; otherwise append `rca_status: "ready"` plus `rca_summary: "<absolute path>"`.

**Gate:** On failure, `rca_status` is recorded in `verdict.json` (either `ready` or `missing`).

### Stage 7: Return
Return a tiny structured summary to the caller:

```yaml
runner_result:
  run_dir: <abs path>
  status: passed|failed|blocker
  blocker: <reason or null>
  returncode: <n>
  verdict_path: <run_dir>/verdict.json
  rca_path: <run_dir>/rca/summary.md   # only when status=failed and rca_status=ready
```

Do **not** print pages of test output to the parent — that lives in `runner.log` and the RCA report. Keep this return tight.

## Halt conditions
Stop and return a `blocker:` instead of guessing:
- `bad-target`, `bad-flag`, `missing-tmux-session` — input validation failures (Stage 1).
- `runner-timeout` — 120 min wall-clock exceeded (Stage 4). Leaves the pane and containers alone.
- The pane gets closed mid-run (`tmux list-panes` no longer shows our pane) → `blocker: pane-vanished`.

## Output format
The `verdict.json` schema above plus the `runner_result` YAML in Stage 7. Nothing else is written to stdout the caller relies on.

## Quality bar (self-check)
[ ] Exactly one target ran in this invocation.
[ ] The pane was an existing pane in the parent's session — no new sessions or panes were created.
[ ] The exact `test_ike.sh` flag came from the parent's input and was passed verbatim.
[ ] Polling intervals stayed under 30 seconds (per iked-e2e rule).
[ ] `runner.log` captures full stdout+stderr of the run.
[ ] `verdict.json` records the flag used, return code, and accurate `containers_state`.
[ ] On failure, no cleanup commands were issued; `iked-failure-rca` was dispatched and `rca_status` is recorded.
[ ] On success, the runner returned without invoking RCA.
[ ] On timeout, the pane and containers were left untouched for the user.
