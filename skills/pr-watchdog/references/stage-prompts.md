# Prompts & summary templates — pr-watchdog

## Event notification (Stage 2 / Stage 3) — body for `cli-escalation-notify`

Fired in CLI context for **each PR event** and **each analysis**. The orchestrator
dispatches `cli-escalation-notify` (it no-ops in the IDE). Keep `body_md` short and
actionable — the user may be on a phone.

`run_context` lines to pass every time:

```
run_id: <run_id>
repo: <repo_root>   host: <hostname>
pr: <pr> — <title>
branch: <branch>  (base <base_branch>)
url: <pr url>
```

Event titles (one per event class):

- `pr-watchdog — build triggered (PR <pr>)`
- `pr-watchdog — branch updated to base (PR <pr>)`
- `pr-watchdog — merge conflicts resolved (PR <pr>)`  ← Stage 3m subagent resolved trivial base↔branch conflicts
- `pr-watchdog — lint/validate auto-fixed & pushed (PR <pr>)`
- `pr-watchdog — review requested @codex/@copilot (PR <pr>)`  ← Stage 2r posted a review request for a new functional commit (Feature 1)
- `pr-watchdog — coverage prediction: test may not run (PR <pr>)`  ← Stage 2p advisory early warning; build keeps running (Feature 2 early)
- `pr-watchdog — CI failed, investigating (PR <pr>)`
- `pr-watchdog — pre-build fixed locally, cycles <n>/3 (PR <pr>)`  ← systematic-debugging subagent fixed-locally
- `pr-watchdog — test coverage verified (PR <pr>)`  ← Stage 2t subagent checked PR-added tests ran in CI (Feature 2)
- `pr-watchdog — analysis complete (PR <pr>)`  ← every subagent result
- `pr-watchdog — PR is green (PR <pr>)`
- `pr-watchdog — watcher stopped (PR <pr>)`  ← background `watch` exited WITHOUT a transition
- `pr-watchdog — halted: <halt_code> (PR <pr>)`

### Background `watch` process (Stage 2)

The RUNNING-wait is a backgrounded `pr_watchdog.py watch` job (`Bash` with
`run_in_background: true`), not in-turn foreground `sleep`s (a turn ending would otherwise
silently kill the watch — the failure mode from run `20260616-162322`; foreground `sleep` is
also blocked in this harness). Its stdout (captured in the background terminal file) is a
stream of one-line markers; branch on the **last** one when the job completes:

- `WATCH_POLL <iso> overall=.. running=.. behind=..` — one heartbeat per poll (progress only).
  `behind=True` here is informational only — it does NOT end the watch (a branch behind base is
  benign and never interrupts a running build).
- `WATCH_TRANSITION <reason> <situation-json>` — exit 0; `reason` ∈ `passed|failed|conflict-early`.
  Use the JSON as the cycle's observation and run the Stage 2 branch. There is deliberately no
  `behind` reason; `conflict-early` fires only for a base conflict (DIRTY) caught while the
  build has only just begun (nothing has passed yet).
- `WATCH_MAXRUNTIME <situation-json>` — exit 10; HALT `max-runtime` (notify).
- `WATCH_FATAL <msg>` — exit 1 (repeated poll errors); notify `watcher stopped`, re-observe
  with `status`, relaunch or HALT.

**`watcher stopped` body** — include: how it ended (`WATCH_FATAL` / max-runtime / vanished),
the last `WATCH_POLL` line seen, and that watching is paused until you relaunch `watch` or
the user re-engages. Silence must never be read as green.

For an **analysis** event, include in `body_md`: root cause (one line), classification (or
`fixed-locally`/`needs-escalation` for pre-build), relatedness to the PR's own changes (test
failures), and the path to `rca/summary.md`. For a `fixed-locally` pre-build result, state
that the local build/lint command now passes (cite `repro-<n>.log`).

## Worktree-conflict options (Stage 1)

The worktree-conflict recovery prompt (PR branch already checked out elsewhere →
main-worktree / dedicated-branch / handoff) is **owned by the `worktree` skill** (Workflow
B). The watchdog invokes that skill at Stage 1 step 4 and records the returned
`worktree_mode` / `dedicated_branch` / `push_target`; it does not present the prompt itself.

## Code-fix escalation prompt (Stage 4)

A code fix from a subagent is gated before push. For a **test-stage** fix
(`pr-failure-handler`) this gate always applies. For a **pre-test** fix (the
`systematic-debugging` subagent, `fixed-locally`) the gate also applies by default but the fix is already verified green on
the real local command — note that so approval is low-risk (skip this gate only when the
user opted into auto-push). Present this prompt and wait. In CLI context,
`cli-escalation-notify` has already pushed the same content as a heads-up.

Render via `AskUserQuestion` when available; otherwise emit the markdown form.

```
PR-<pr> CI failure on <server> / <stage> (cycle <N>).
Source: <systematic-debugging subagent | pr-failure-handler>
Classification/result: <fixed-locally | trivial | non-trivial | needs-escalation> — <reason>
Local verification: <"build/lint command passes locally (repro-<n>.log)" for pre-build, else "n/a">
Root cause: <one sentence>
Relatedness: <caused-by-this-pr | unrelated | inconclusive | n/a>
Report:        <cycle_dir>/rca/summary.md
Candidate fix: <cycle_dir>/suggested-fix.md  (or applied in worktree: <cycle_dir>/patch.diff)

How should I proceed?
  (a) APPLY the fix in the worktree and PUSH it to the PR branch, then re-watch CI.
  (b) COMMIT current accumulated worktree changes first, then apply this fix on top and push.
  (c) HAND OFF — stop the watchdog. The worktree and run state stay for you to inspect.
  (d) SKIP this failure — leave it failed in the report and keep watching the rest.
```

`AskUserQuestion` shape:
- `header`: `PR fix` (chip)
- `id` (bookkeeping): `pr_watchdog_escalation`
- Options: `apply_push`, `commit_and_push`, `handoff`, `skip`.

### Branch behaviour after the user picks

- **(a) apply_push** — apply the candidate fix in the worktree (handler already applied
  it on `trivial`; for `non-trivial` apply the diff from `suggested-fix.md`), commit via
  `git-conventions` (append `[AI generated]`), `git push origin HEAD:<branch>`. Record the
  push in `meta.json.pushes[]`. The push re-triggers CI; continue the watch loop.
- **(b) commit_and_push** — commit any already-applied worktree changes first (one commit),
  then apply + commit this fix, then push. Same recording.
- **(c) handoff** — write the final summary, exit. Do not push, do not remove the worktree.
- **(d) skip** — mark the failure skipped in the report, continue watching other servers.

### `git add` safety

Never `git add -A` blindly. Stage only: files the handler patch touched
(`evidence.json.touched_paths`), files changed by the just-presented fix, and files already
in the accumulated worktree diff. Confirm anything else with the user before staging.

## Early coverage-prediction question (Stage 2p / Feature 2 early)

Presented **after the build was already triggered** when a `general-purpose` subagent predicts an
added test won't run under the current CI selection. This is a **non-blocking warning** — CI keeps
running while the user decides; if they never answer, the terminal Stage 2t gate is the backstop.
Never interrupt the running build for this. Render via `AskUserQuestion` (header `Coverage`);
otherwise the markdown form.

```
⚠️ Heads-up (build is RUNNING — not blocked): I predict <k> test(s) ADDED by this PR won't run
under PR prefix "<current>".
Won't-run: <file::test> — <cause, e.g. its marker <m> maps to stage <S>, which "<current>" doesn't run>
Suggested fix: <e.g. use prefix "<recommended>" (pr-labels) | add marker <m> | register suite>
Prediction: <cycle_dir>/predict/summary.md   (static marker/prefix analysis — not the real run)

The build keeps running regardless. How do you want to handle coverage?
  (a) FIX NOW — apply the fix so the test runs. Note: this makes a fresh HEAD that SUPERSEDES the
      current build (a new CI starts).
  (b) BUILD ONLY — I intended these tests not to run; keep the current build and don't ask again.
  (c) DECIDE AFTER CI — keep the build; if the test still didn't run when CI ends I'll re-raise it.
```

`AskUserQuestion` options: `fix_now`, `accept_build_only`, `decide_after_ci`.

- **(a) fix_now** — apply the suggested change (pr-labels prefix/title, or a worktree
  marker/registration edit). It supersedes the in-flight build → re-trigger and keep watching.
- **(b) accept_build_only** — add the test ids to `meta.json.accepted_not_run`; leave the build
  running; Stage 2t later records them as accepted coverage gaps (no halt). Don't re-ask this HEAD.
- **(c) decide_after_ci** — no action now; the authoritative Stage 2t gate re-raises at terminal.

## Test-coverage gate prompt (Stage 2t / Feature 2)

Presented only when CI is **green** but a test ADDED by the PR never ran in CI. This is a
coverage gate, not a failure fix — the PR is not declared green until it is resolved. In CLI
context `cli-escalation-notify` has already fired the heads-up. Render via `AskUserQuestion`
(header `Test coverage`); otherwise the markdown form.

```
PR-<pr> CI is GREEN, but <k> test(s) ADDED by this PR never ran in CI.
Never-ran: <file::test>, ...
Likely cause: <e.g. PR-prefix "<current>" does not select the <suite/stage> that runs these>
Suggested fix: <e.g. retitle PR prefix to "<recommended>" (pr-labels) | add marker <m> | register suite>
Report:        <cycle_dir>/tests-ran/summary.md
Candidate fix: <cycle_dir>/suggested-fix.md

An added test that never runs is untested code merging green. How should I proceed?
  (a) APPLY the coverage fix (PR-prefix/title via pr-labels, and/or a worktree marker/registration
      edit), then re-trigger CI so the test runs.
  (b) HAND OFF — stop the watchdog; the report and suggested fix stay for you.
  (c) ACCEPT AS-IS — declare the PR green anyway (records the uncovered added test in the summary).
```

`AskUserQuestion` options: `apply_fix`, `handoff`, `accept_as_is`.

- **(a) apply_fix** — apply the suggested change. A `pr-labels` prefix/title change re-triggers
  CI as a fresh HEAD (Stage 2 `NO_CI`/idle trigger path); a worktree marker/registration edit
  joins the Stage 3c batch (code fix → it is itself a functional commit, so Stage 2r fires). Keep
  watching until the added test shows up in a later `tests-ran` catalog.
- **(b) handoff** — HALT `tests-not-run`; write the final summary, leave the worktree/run dir.
- **(c) accept_as_is** — the user knowingly accepts the coverage hole; declare green (Stage 5) and
  record the uncovered added test under "Open coverage gaps" in the summary. Not the default.

## Final / halt summary

Printed when CI goes green, the user hands off, or a halt fires.

```
# pr-watchdog summary — <run_id>

PR-<pr>: <title>
Final CI: <PASSED | FAILED | RUNNING(handed-off)>
Branch: <branch> (base <base_branch>)
Worktree: <worktree path> (<removed — created by this run | preserved: main checkout | preserved: reused | preserved: handoff | preserved: pending changes>)

## Pushes made this run
- cycle <N> — <kind> — <short sha> — <files>     (or "none")

## Open failures (if any)
- <server>/<stage> — <classification> — see <cycle_dir>/suggested-fix.md

## Open coverage gaps (if any — Feature 2 accept_as_is)
- <file::test> — added by this PR, never ran in CI — see <cycle_dir>/tests-ran/summary.md

## Where to look
- Per-cycle state: ~/.pr-watchdog-runs/<run_id>/cycles/
- Handler reports: ~/.pr-watchdog-runs/<run_id>/cycles/*/rca/summary.md
- Coverage reports: ~/.pr-watchdog-runs/<run_id>/cycles/*/tests-ran/summary.md
```

### Halt variant

Replace `## Pushes` with:

```
## Halt
Blocker: <halt_code>
At cycle <N>, situation <overall>
Pointing to: <cycle_dir>
```

Post-summary invariants: remove the worktree **only** when the watchdog created it this run
and it has no pending changes (Stage 5 cleanup) — otherwise leave it intact (`main`/reused/
handoff/dirty). Never push uncommitted worktree changes; never delete
`~/.pr-watchdog-runs/<run_id>/`.
