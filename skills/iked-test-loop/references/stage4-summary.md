# Stage 4 final summary template — iked-test-loop

Printed when the plan is exhausted (every item is `passed`, `skipped-failed`, or `handed-off`). Computed from `meta.json`, `plan.yml`, and `git diff <current_anchor_sha>`.

```
# iked-test-loop summary — <run_id>

Plan: <N items> — <P passed, S skipped, H handed-off>
Commits in scope: <list>
Tmux session: <tmux_session> (left running)

## Per-item results
- [PASS]    <target> — iter <K>, flag chain: <c, c, b, c, ...>
- [SKIP]    <target> — non-trivial: <reason> — see <item_dir>/suggested-fix.md
- [HANDOFF] <target> — non-trivial: <reason>

## Commits created during the loop
- <sha> <subject>          (or "none — all fixes still uncommitted")

## Accumulated diff still in the working tree
<git diff --stat <current_anchor_sha>>

## Where to look
- Full per-item artifacts: ~/.iked-runs/<run_id>/items/
- Handler reports:         ~/.iked-runs/<run_id>/items/*/rca/summary.md
- Suggested fixes:         ~/.iked-runs/<run_id>/items/*/suggested-fix.md
- Applied trivial patches: ~/.iked-runs/<run_id>/items/*/patch.diff
```

## Post-summary invariants

After printing the summary, the loop:

- Does **not** kill the tmux session (the user may want to inspect panes or re-run manually).
- Does **not** commit the accumulated diff.
- Does **not** clean `~/.iked-runs/<run_id>/`.

## Halt-summary variant

When the loop exits via a halt condition (see SKILL.md `## Halt conditions`), print the same skeleton but replace the `## Per-item results` block with:

```
## Halt
Blocker: <halt_code>
Last item: <target> (iter <N>) at Stage <2c step | 1 | 3>
Pointing to: <item_dir or run_dir>
```

Same post-summary invariants apply — nothing is reverted, committed, or cleaned.
