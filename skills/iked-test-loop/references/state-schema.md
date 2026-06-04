# State Schemas — iked-test-loop

The loop persists per-run state under `~/.iked-runs/<run_id>/`. This file documents the JSON shapes the loop reads/writes. SKILL.md only references field names; full schemas live here so they don't bloat the always-loaded prompt.

## `meta.json` — run-level state

Written at Stage 1 and updated at every commit / Slack-resolution / anchor change.

```json
{
  "run_id": "<YYYYMMDD-HHMMSS-rand>",
  "started_at": "<ISO-8601>",
  "repo_root": "/home/dn/cheetah",
  "commits": ["<sha>", "..."],
  "start_sha": "<HEAD at loop start>",
  "current_anchor_sha": "<HEAD or last-commit sha — accumulated diff is `git diff <this>`>",
  "tmux_session": "iked-loop-<run_id>",
  "session_origin": "created | reused-sole",
  "known_suites": ["routing", "cdnos", "cli_tests", "cli_infra", "..."],
  "known_suites_source": "<abs path to captured `test_ike.sh --list` output>",
  "plan_save_path": "<.ai/plans/iked-loop-<run_id>/ or null>",
  "is_cli_context": true,
  "slack_target": "<Slack user ID, e.g. U01ABC2DEF, or null>",
  "slack_target_handle": "<the handle/email used to look the user up — for audit; null when not resolved>",
  "save_plan": false
}
```

`known_suites` is populated from `test_ike.sh --list` at Stage 1 step 6 (parse the `Suites:` block). It is the only place the loop looks up valid suite names — `target_kind()` consults this list, never a hardcoded literal. The example above shows the 2026-05 registry; the actual contents are whatever `test_ike.sh --list` reports at run time.

## `verdict.json` — per-iteration test result

Written at Stage 2c step 6 and updated at step 8 on the debugger path.

```json
{
  "target": "<target>",
  "suite_hint": "<suite_hint or null>",
  "flag_used": "-c | -b | \"\"",
  "status": "passed | failed",
  "started_at": "<ISO-8601 from step 1>",
  "tmux_pane": "<session>.<idx>",
  "runner_log": "<abs path>",
  "containers_state": "live-failed | torn-down-passed | torn-down-by-script",
  "pdb_state": "none | active | quit-by-handler | quit-by-loop-safety-net",
  "pdb_pane": "<session>.<idx> | null",
  "pdb_prompt": "<verbatim IPDB_ACTIVE: line, or null>",
  "returncode": "<int or null until sentinel arrives>",
  "ended_at": "<ISO-8601 or null>",
  "duration_seconds": "<float or null>",
  "handler_status": "ready | missing | null",
  "handler_summary": "<abs path or null>"
}
```

`containers_state` heuristic on the sentinel path:

- `rc == 0` → `torn-down-passed`.
- Else if `runner.log` contains `==> Running ` (the marker `test_ike.sh` prints right before `make`) → `live-failed`.
- Else → `torn-down-by-script` (typically a build / image-missing / infra failure).

On the debugger path, the initial write has `status: failed`, `containers_state: live-failed`, `pdb_state: active`, and `returncode`/`ended_at`/`duration_seconds` set to `null` until the post-handler sentinel arrives.

## `<item_dir>/kind` — one-line target classification

Written at Stage 2b. Contents: `new` or `regression` (no newline ok). The value is read by Stage 2c when dispatching `iked-failure-handler`.

## `<item_dir>/rca/evidence.json`

Owned by `iked-failure-handler`; see that skill's "Output contract". The loop reads `classification`, `next_action`, `non_trivial_reason`, `relatedness`, `touched_paths`, `patch_path`, `suggested_fix_path`, `root_cause`.

## Run-state directory tree

```
~/.iked-runs/<run-id>/
  meta.json
  plan.yml                 # queue with live status per item
  tmux_session             # one-line file with the tmux session name
  items/
    <NNN>-<slug>/          # NNN = zero-padded global iteration index
      kind                 # one line: new | regression
      verdict.json
      runner.log           # full test_ike.sh output (tee'd from the pane)
      rca/                 # only on failure (handler artifacts)
        summary.md
        evidence.json
        pytest_excerpt.txt
        shows/...
        traces/...
      patch.diff           # trivial only (handler-applied)
      suggested-fix.md     # non-trivial only (handler-emitted)
```
