# Per-task prompt templates

These are the prompts the kanban-execution-loop skill composes (SKILL.md Stage 3
step 1) and passes to `scripts/build_pipeline.py` as each task's `prompt`. Every
task runs with the **Claude** agent, in its own detached worktree off the shared
work branch.

Keep every prompt tight: goal + only this task's Claude harness paths + the plan
path. Fill the bracketed fields per task and **drop harness lines the task does
not use**.

Every task is created with `--auto-review-mode commit` — the deployed Kanban CLI
has no `done` mode. Tasks that produce changes get committed and cherry-picked
onto the work branch; a verification-only task that ends with zero working changes
is auto-moved to Done by commit mode, which advances the chain.

Rules that apply to all templates:
- **Reference the harness by bare `label: path`.** Never transcribe its commands
  and never add a parenthetical describing what a doc contains.
- **Do NOT tell any task to commit or push** (except `finalize`, which pushes and
  opens the PR). Auto-review owns commits: commit-mode tasks are committed and
  cherry-picked onto the work branch automatically.
- **Do not restate the plan.** Point at it; the agent reads it.
- **Only a real environment blocker** (infra prevents using tools, editing,
  compiling, or testing) stops a task early; it reports that verbatim.

---

## phase-1-validate  (auto-review mode: `commit`; ends with no changes -> auto-done)

```
Read-only validation for a kanban-execution-loop pipeline. Do NOT edit, commit,
or change anything in the repo.

Confirm the pipeline is executable:
- Every plan below exists, is readable, and has explicit, testable pass criteria:
  <one plan path per line>
- Every file/path those plans reference exists in this worktree (i.e. is committed
  on this branch), not just in someone's uncommitted working tree.
- The Claude harness docs this pipeline relies on exist and are readable:
  - Build / unit / e2e harness: <harness doc path(s)>
  - Coding conventions: <coding-rules doc path(s)>

Report a concise PASS/FAIL summary listing any missing plan, uncommitted or
unreachable referenced file, missing harness doc, or plan lacking testable
criteria. Make no changes; this task exists only to gate the pipeline.
```

---

## plan-NN-exec  (auto-review mode: `commit`)

```
Run the implementation-loop skill to execute the entire plan at <PLAN_PATH>.
Use the plan's own pass/acceptance criteria as your acceptance criteria (read
them from the plan). If the plan has no explicit, testable pass criteria, stop
and report it.

Read and follow these files:
- Build / unit / e2e harness: <harness doc path(s)>
- Coding conventions: <coding-rules doc path(s)>

Do not commit or push — the board commits your work for you when the task
finishes. Keep working through failures until the plan's criteria pass. Stop
early only for a real environment blocker; report it verbatim.

Related plans: <none | "<plan> already done on this branch">.
```

---

## plan-NN-validate  (auto-review mode: `commit`)

```
Independently verify that the plan at <PLAN_PATH> is actually done on the current
branch — do not assume the previous task finished it correctly.

Check every one of the plan's pass/acceptance criteria against the current state,
running the harness to prove it:
- Build / unit / e2e harness: <harness doc path(s)>
- Coding conventions: <coding-rules doc path(s)>

If every criterion is met, make no changes and report PASS (the board will
complete this task). If any criterion is not met, fix it until all criteria pass
— do not commit or push, the board commits your fixes for you. Stop early only
for a real environment blocker; report it verbatim.
```

---

## finalize  (auto-review mode: `commit`; pushes/opens PR itself, no changes -> auto-done)

```
The plans for this pipeline have all been executed and validated on this work
branch. Finish the run:
- Do any small cleanup the plans call for (leftover scratch files, TODOs opened
  by the pipeline). Do not start new feature work.
- Push the work branch and open a PR, following:
  - Git / PR conventions: .ai/skills/common/git-conventions/SKILL.md

Report the pushed branch and the PR URL. Stop early only for a real environment
blocker; report it verbatim.
```
