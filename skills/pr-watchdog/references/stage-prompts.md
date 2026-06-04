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
- `pr-watchdog — lint/validate auto-fixed & pushed (PR <pr>)`
- `pr-watchdog — CI failed, investigating (PR <pr>)`
- `pr-watchdog — pre-build fixed locally, cycles <n>/3 (PR <pr>)`  ← systematic-debugging subagent fixed-locally
- `pr-watchdog — analysis complete (PR <pr>)`  ← every subagent result
- `pr-watchdog — PR is green (PR <pr>)`
- `pr-watchdog — halted: <halt_code> (PR <pr>)`

For an **analysis** event, include in `body_md`: root cause (one line), classification (or
`fixed-locally`/`needs-escalation` for pre-build), relatedness to the PR's own changes (test
failures), and the path to `rca/summary.md`. For a `fixed-locally` pre-build result, state
that the local build/lint command now passes (cite `repro-<n>.log`).

## Code-fix escalation prompt (Stage 4)

A code fix from a subagent is gated before push. For a **test-stage** fix
(`pr-failure-handler`) this gate always applies. For a **pre-test** fix (the
`systematic-debugging` subagent, `fixed-locally`) the gate also applies by default but the fix is already verified green on
the real local command — note that so approval is low-risk (skip this gate only when the
user opted into auto-push). Present this prompt and wait. In CLI context,
`cli-escalation-notify` has already pushed the same content as a heads-up.

Render via `AskQuestion` when available; otherwise emit the markdown form.

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

`AskQuestion` shape:
- `id`: `pr_watchdog_escalation`
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

## Final / halt summary

Printed when CI goes green, the user hands off, or a halt fires.

```
# pr-watchdog summary — <run_id>

PR-<pr>: <title>
Final CI: <PASSED | FAILED | RUNNING(handed-off)>
Branch: <branch> (base <base_branch>)
Worktree: <worktree path> (left in place)

## Pushes made this run
- cycle <N> — <kind> — <short sha> — <files>     (or "none")

## Open failures (if any)
- <server>/<stage> — <classification> — see <cycle_dir>/suggested-fix.md

## Where to look
- Per-cycle state: ~/.pr-watchdog-runs/<run_id>/cycles/
- Handler reports: ~/.pr-watchdog-runs/<run_id>/cycles/*/rca/summary.md
```

### Halt variant

Replace `## Pushes` with:

```
## Halt
Blocker: <halt_code>
At cycle <N>, situation <overall>
Pointing to: <cycle_dir>
```

Post-summary invariants (always): do **not** remove the worktree, do **not** push
uncommitted worktree changes, do **not** delete `~/.pr-watchdog-runs/<run_id>/`.
