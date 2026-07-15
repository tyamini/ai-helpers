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
    <NNN>-<situation>/     # NNN = zero-padded cycle index; situation = passed|running|failed|behind|dirty|no-ci
      situation.json       # verbatim `pr_watchdog.py status` output for this cycle
      action.json          # what the watchdog did this cycle (below)
      rca/                 # only when a failure was investigated (subagent artifacts)
        summary.md
        evidence.json
        progress.md        # systematic-debugging subagent phase checklist (pre-test)
        repro-<n>.log      # systematic-debugging subagent local build/lint runs (pre-test)
      predict/             # only when Stage 2p ran (Feature 2 — pre-terminal coverage prediction)
        predict.json       # verbatim `predict_tests.py` output (per-item will-run/wont-run/cant-tell)
        summary.md         # subagent write-up (only when result == some-wont-run)
        suggested-fix.md   # subagent's concrete fix for the wont-run items (pr-prefix / marker / registration)
      tests-ran/           # only when Stage 2t ran (Feature 2 — post-CI coverage verify)
        summary.md
        evidence.json      # {added, ran, not_run:[{id,cause,suggested_fix}], inconclusive}
      suggested-fix.md     # non-trivial test fix (pr-failure-handler) OR a Stage 2t coverage fix
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
  "last_review": {"sha": "<SHA review was last requested for>", "posted": ["codex", "copilot"], "already": [], "skipped": "merge-commit | null"},
  "last_prediction": {"sha": "<HEAD Stage 2p predicted>", "result": "all-will-run | some-wont-run | cant-tell-only", "wont_run": ["<file::test>"]},
  "accepted_not_run": ["<file::test the user accepted as an intentional build-only run at Stage 2p>"],
  "last_tests_ran": {"sha": "<terminal-CI HEAD verified>", "result": "all-ran | some-not-run | inconclusive", "not_run": ["<file::test>"], "overall": "PASSED | FAILED"},
  "last_trigger": {"sha": "<HEAD sha a rebuild was requested for>", "at": "<ISO-8601>"},
  "watcher": {"pid": 12345, "started_at": "<ISO-8601>", "interval": 600, "max_runtime": 86400, "last_exit": "transition | maxruntime | fatal | null"},
  "last_escalation_notify": "sent | skipped | send-failed | null"
}
```

`watcher` records the backgrounded `pr_watchdog.py watch` job that owns the RUNNING-wait
(Stage 2). Its liveness is **independent of the agent turn**: the job keeps polling after the
turn ends and wakes the agent via the harness background-completion notification. `last_exit`
distinguishes a clean `WATCH_TRANSITION` (`transition`) from the cases that MUST notify
`watcher stopped` (`maxruntime`, `fatal`, or the job vanishing → recorded by the agent).

`is_cli_context` mirrors the headless-CLI-vs-IDE decision **owned by `cli-escalation-notify`**
(true in a headless CLI such as Claude Code or cursor-agent; false in an IDE agent panel). The
watchdog records the value that skill would use rather than re-implementing the detection.

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
  "merge_state_status": "CLEAN | BEHIND | DIRTY | BLOCKED | UNSTABLE | ...",
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

Base reconciliation: `behind` and a conflicted PR are **distinct** signals. GitHub reports a
conflicted PR as `merge_state_status == DIRTY` (`mergeable_state: dirty`), **not** `behind`.
The watchdog treats `needs-base-reconcile = behind == true OR merge_state_status == DIRTY`, but
this is **not** an action trigger on its own: a branch merely `behind` while a build is RUNNING
is benign and is left alone (never interrupt an in-progress build to chase a moving base). The
reconcile only rides along when the loop is already headed to a push/trigger — a
**PASSED-but-un-mergeable** PR, a `FAILED` remediation, or a `NO_CI`/idle trigger (CI must not
start on a behind/dirty HEAD). The one early exception is `conflict-early`: a `DIRTY` conflict
caught while the build has only just begun (nothing PASSED yet) is worth resolving + restarting
immediately, since a conflicted PR can't merge even if it goes green. All of these go to Stage 3
(base-merge, then Stage 3m conflict resolution if the merge conflicts).

## `action.json` — what the watchdog did this cycle

```json
{
  "cycle": 3,
  "situation": "failed",
  "decision": "wait | trigger | update-branch | resolve-conflict | prebuild-fix | investigate | request-review | predict-coverage | verify-tests-ran | escalate | done",
  "fix_kind": "branch-update | merge-conflict | prebuild | code-fix | coverage-fix | null",
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
