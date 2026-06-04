# State schema — pr-watchdog

The watchdog persists per-run state under `~/.pr-watchdog-runs/<run_id>/`. SKILL.md
references field names only; the full shapes live here so they don't bloat the
always-loaded prompt.

## Run-state directory tree

```
~/.pr-watchdog-runs/<run_id>/
  meta.json                # run-level state (below)
  worktree                 # one-line file: abs path to the fix worktree
  cycles/
    <NNN>-<situation>/     # NNN = zero-padded cycle index; situation = passed|running|failed|behind|no-ci
      situation.json       # verbatim `pr_watchdog.py status` output for this cycle
      action.json          # what the watchdog did this cycle (below)
      rca/                 # only when a failure was investigated (subagent artifacts)
        summary.md
        evidence.json
        progress.md        # systematic-debugging subagent phase checklist (pre-test)
        repro-<n>.log      # systematic-debugging subagent local build/lint runs (pre-test)
      suggested-fix.md     # non-trivial test fix only (pr-failure-handler)
      patch.diff           # applied fix diff (lint/validate/prebuild/code), when present
```

## `meta.json` — run-level state

Written at Stage 1; updated at every push / escalation / event.

```json
{
  "run_id": "<YYYYMMDD-HHMMSS-rand>",
  "started_at": "<ISO-8601>",
  "repo_root": "/home/dn/cheetah",
  "pr": 91682,
  "url": "https://github.com/drivenets/cheetah/pull/91682",
  "branch": "<head branch>",
  "base_branch": "<base branch>",
  "worktree": "<abs path to fix worktree, or <repo_root> when worktree_mode == main>",
  "worktree_mode": "worktree | main | dedicated",
  "worktree_created": "<bool — true if the watchdog created a NEW worktree this run (make_worktree.sh CREATED); drives Stage 5 cleanup>",
  "dedicated_branch": "<dedicated branch name, only when worktree_mode == dedicated, else null>",
  "push_target": "<branch fixes are pushed to; == branch (dedicated mode makes this explicit)>",
  "interval_seconds": 600,
  "is_cli_context": true,
  "last_overall": "NO_CI | RUNNING | PASSED | FAILED",
  "last_event_notified": "<short string of the last Slack-notified event>",
  "pushes": [
    {"cycle": 3, "kinds": ["branch-update", "prebuild"], "sha": "<sha>", "files": ["..."]}
  ],
  "known_server_slugs": ["israel1", "israel3", "aws5", "aws6", "aws7", "aws8"],
  "last_trigger": {"sha": "<HEAD sha a rebuild was requested for>", "at": "<ISO-8601>"},
  "last_escalation_notify": "sent | skipped | send-failed | null"
}
```

`is_cli_context == true` iff `$CURSOR_AGENT` is set AND `$CURSOR_LAYOUT` is unset
(matches `cli-escalation-notify`). Do NOT also gate on `$VSCODE_AGENT_FOLDER`.

`worktree_mode` is `worktree` by default (an isolated worktree checked out on the PR branch).
On a `worktree-conflict` (PR branch already checked out in the main repo) the user may opt
into `main` (fixes applied in `<repo_root>`, which is recorded as `worktree`; never
`reset --hard`) or `dedicated` (a worktree on `dedicated_branch`, based on `origin/<branch>`,
pushed to `push_target == branch`). See Stage 1 step 4.

## `situation.json` — per-cycle observation

Verbatim stdout of `scripts/pr_watchdog.py status --pr <pr>`:

```json
{
  "pr": 91682,
  "url": "<github pr url>",
  "title": "<pr title>",
  "branch": "<head branch>",
  "base_branch": "<base branch>",
  "sha": "<head sha>",
  "overall": "NO_CI | RUNNING | PASSED | FAILED",
  "build_running": true,
  "behind": false,
  "mergeable_state": "clean | behind | dirty | blocked | unstable | ...",
  "draft": false,
  "servers": [{"name": "Israel-1", "state": "FAILED", "build": "123", "duration": "", "stage": "Lint & Validate"}],
  "failed_tests": [{"server": "...", "stage": "...", "test": "...", "file": "...", "count": 2, "jira": null, "jira_fix_versions": null}],
  "lint_validate_failures": [{"server": "...", "stage": "Lint & Validate"}]
}
```

`overall` semantics (from pr_driver):
- `NO_CI` — no Jenkins checks on HEAD (e.g. CI never ran for this commit).
- `RUNNING` — at least one server RUNNING / PENDING_RETRY / UNKNOWN.
- `PASSED` — every discovered server PASSED.
- `FAILED` — no server running and at least one FAILED.

## `action.json` — what the watchdog did this cycle

```json
{
  "cycle": 3,
  "situation": "failed",
  "decision": "wait | trigger | update-branch | prebuild-fix | investigate | escalate | done",
  "fix_kind": "branch-update | prebuild | code-fix | null",
  "prebuild_result": "fixed-locally | needs-escalation | null",
  "cycles_used": 0,
  "auto_pushed": false,
  "pushed_sha": "<sha or null>",
  "escalation": "aggregate | commit-and-continue | handoff | skip | null",
  "notes": "<one line>"
}
```

## Fix-landing policy (batched — one push per remediation cycle)

All fixes are applied to the worktree first and landed by a **single push** at Stage 3c.
Never push a partial fix (e.g. the base-merge alone). A `meta.json.pushes[]` entry records
the whole batch (`kinds` may list several, e.g. `["branch-update", "prebuild"]`).

What may be in a batch and whether it needs the user gate before the push:
- **Safe / deterministic, no gate:** `branch-update` only (clean base-merge by
  `update_branch.sh`, applied with NO `--push`). This is the single fix that ships in the
  cycle's push without asking.
- **Pre-test `fixed-locally` (systematic-debugging subagent) → Stage 4 gate by default**
  (verified green locally; low-risk approval). This covers lint/format/validate too — the
  subagent owns those fixes (it may run `fix_lint.sh`); the loop never applies them inline.
  Auto-push opt-in: `PR_WATCHDOG_AUTOPUSH_PREBUILD=1`. On approval it joins the same batch push.
- **Test-stage code fixes (`pr-failure-handler`) → always Stage 4 gate.** On
  `apply_push`/`commit_and_push` they join the batch; on `skip` they stay unpushed.

`known_server_slugs` is the union of server slugs seen in any poll; the trigger uses it as a
fallback when a fresh HEAD (e.g. after a base-merge) has no statuses and the history lookback
can't rediscover them.
