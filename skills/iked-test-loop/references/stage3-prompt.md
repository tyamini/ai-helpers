# Stage 3 prompt template — iked-test-loop

This is the **local interactive prompt** the loop presents to the user on a non-trivial escalation. It runs after Stage 3's `cli-escalation-notify` dispatch (which, in CLI context, has already pushed a Slack DM with the same information).

Render via `AskQuestion` when available so the user sees a structured choice; otherwise emit the markdown form below.

## Markdown form

```
Non-trivial failure on target <target> (iteration <N>).
Reason: <non_trivial_reason>
Handler report: <run_dir>/rca/summary.md
Suggested fix:  <run_dir>/suggested-fix.md

Accumulated trivial fixes since last anchor (<git short-sha of anchor>):
  <list applied_fixes between current_anchor_sha and HEAD as:
   "iter N — <target> — <one-line rationale>">

How should I proceed?
  (a) Apply the suggested fix and AGGREGATE it into the current accumulated diff. Then re-queue this target.
  (b) COMMIT the current accumulated diff first, then apply the suggested fix on top of a fresh anchor. Then re-queue this target.
  (c) HAND OFF — stop the loop here. The accumulated diff stays in the working tree for you to inspect.
  (d) SKIP this target — leave it as failed in the report and move to the next plan item.
```

## AskQuestion shape

- `id`: `iked_loop_escalation`
- `prompt`: first paragraph of the markdown above (the three "Non-trivial …" lines + the suggested-fix path).
- Options:
  - `aggregate` — *Apply suggested fix and aggregate into current diff. Re-queue target.*
  - `commit_and_continue` — *Commit current diff, then apply suggested fix on top. Re-queue target.*
  - `handoff` — *Hand off — stop the loop, leave diff for inspection.*
  - `skip` — *Skip this target — mark failed and move to next plan item.*

## Branch behaviour after the user picks

- **(a) Aggregate.** Apply the candidate diff from `suggested-fix.md` to the working tree (`git apply <derived patch>` or hand-apply if the report only contains a pseudo-diff and the user provided guidance). Update `last_touched_paths` to the changed file set. Re-queue the same target. Go to Stage 2 with the next iteration.
- **(b) Commit and continue.** Run the commit using `git-conventions`:
  - Stage everything between `current_anchor_sha` and HEAD plus the new fix — see Safety note below.
  - Compose a commit message via `git-conventions` Stage X (commit-messages). The body summarizes the `applied_fixes` between `current_anchor_sha` and HEAD ("Trivial fixes from iked-test-loop run <run_id>: …"). Append `[AI generated]`.
  - `git commit -m "<composed>"`.
  - Update `current_anchor_sha = git rev-parse HEAD`. Reset `applied_fixes` log for the next batch.
  - Then apply the suggested fix to the working tree (becomes the start of the **next** accumulated diff).
  - Re-queue the same target. Go to Stage 2.
- **(c) Hand off.** Mark item `status: handed-off`, write the Stage 4 final summary, exit. Do not commit, do not clean tmux.
- **(d) Skip.** Mark item `status: skipped-failed`, set `last_touched_paths = []` (no new code), advance to the next plan item.

## Safety on `git add` for option (b)

Never run `git add -A` blindly. Only stage files that:

- Are inside the accumulated diff (`git diff --name-only <current_anchor_sha>`), OR
- Were the target of a handler trivial patch (recorded in `applied_fixes`), OR
- Are the file modified by the just-presented suggested fix.

Anything else in the working tree (e.g. stray untracked files the user dropped while inspecting) must be confirmed with the user before staging.
