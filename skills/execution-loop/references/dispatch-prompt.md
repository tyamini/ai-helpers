# Per-plan agent prompt

This is the prompt the executor writes to `<plan_dir>/prompt.txt` and passes to
the per-plan `cursor-agent` (Stage 3). It is the cursor-agent equivalent of the
old Task "Subagent prompt". Keep it tight: goal + harness referenced by file
path + related-plan status. Do not restate plan contents. Fill the bracketed
fields; drop harness lines the plan does not use.

Because this agent runs as a **top-level `cursor-agent` process** (not a Task
subagent), it may dispatch its own subagents if its skill does.

Telemetry needs nothing from this prompt: the agent's run-ledger node is parsed
from its `pane.log` by `exec_collect.py` when the plan finishes (no `init`, no
hook, no registration step).

```
Run the .ai/skills/common/implementation-loop/SKILL.md skill to execute the
entire plan at <PLAN_PATH>.
Use the plan's pass/acceptance criteria as your acceptance criteria (read them
from the plan). If the plan has no explicit, testable pass criteria, stop and
report it.

Read and follow these files:
- Build / unit / e2e harness: <harness doc path(s)>
- Coding conventions: <coding-rules doc path(s)>
- Commit conventions: .ai/skills/common/git-conventions/SKILL.md; commit on
  branch <BRANCH>.

Commit your work on <BRANCH> before finishing, with a clean tree; do not push
or open a PR.

Related plans: <none | "<plan> done (commit <sha>)" | "<plan> in progress">.

Only stop early for a real environment blocker (infra prevents using tools,
editing, compiling, or testing); report it verbatim. Otherwise keep working
through failures until the criteria pass and you have committed.
```
