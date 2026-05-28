---
name: iked-failure-handler
description: On a failed iked / IPsec E2E test, investigate the live environment, classify the failure (trivial | non-trivial | flaky), and act accordingly. On `trivial`, apply a minimal patch to the working tree; on `non-trivial`, write a Suggested Fix and hand the call back to the parent loop; on `flaky`, return retry-once. Single subagent that replaces the prior split between iked-failure-rca and iked-fix-triage. Use when `iked-test-loop` invokes it on failure.
disable-model-invocation: true
---

# iked Failure Handler

## Goal

You are given a failed iked / IPsec E2E test with the `e2e_*` containers preferably still live. Figure out what broke, decide what to do, and **apply a minimal patch yourself if and only if the fix is trivial**. Otherwise hand a structured report back to the parent loop so the user can make the design call.

One investigation, one classification, one report. The loop owns orchestration; you own the failure call.

## Inputs

- `run_dir` — abs path to `~/.iked-runs/<run-id>/items/<seq>-<slug>/`. Read pytest output from `runner.log` here; write all your artifacts under `rca/` (create if missing).
- `target` — the test or suite name that was run.
- `commits_in_scope` — list of SHAs representing the "new code under test". Used to decide whether a candidate fix is in scope for a trivial patch.
- `repo_root` — absolute path to the cheetah checkout (e.g. `/home/dn/cheetah`).
- `previous_runs_for_this_target` — optional list of prior verdicts for the same target in this run; only needed for the flaky check.
- `pdb_pane` — optional `<session>.<idx>` where pytest is paused at `ipdb> ` / `(Pdb) `. When set, the live debugger is your primary evidence source.

## Background (read once at start)

- [/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc](/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc) — debugging toolkit (container names `e2e_R*_*`, trace file paths under `/core/traces/<container>/`, vtysh queries, the never-sleep-more-than-30s rule). **Treat its trace-file list as a starting hint, not exhaustive.** Use `docker exec <c> ls /core/traces/<c>/` to discover what's actually there.
- [/home/dn/cheetah/AGENTS.md](/home/dn/cheetah/AGENTS.md) Change Policy — minimum diff, no speculative refactors, match existing style.

## Hard invariants

Forensic, not destructive. The following are FORBIDDEN:

- Removing, truncating, or rotating any log file.
- Restarting any daemon, container, or test (no `pkill`, `docker restart`, no re-invoking `test_ike.sh` / `pytest`).
- Sleeping more than 30 seconds in any single wait.
- Editing any source file outside the trivial-fix scope (see Classification — only the test file from the pytest excerpt, or files touched by `commits_in_scope`).

Two live mutating exceptions, each used at most once per run:

- **CLI re-issue** — re-run the failing vtysh command in the live container (`docker exec <c> vtysh -c "<the exact failing command>"`) to learn how it parses/dispatches. Record what you sent and what came back.
- **Live debugger inspection** (when `pdb_pane` is set) — `tmux send-keys` read-only commands: `where`, `list`, `args`, `p <expr>`, `pp <expr>`, `up`, `down`. **Never** send `c` / `continue` — that resumes the test and you lose the frozen state. RCA owns quitting pdb (`q` Enter) before returning so the loop can collect the deferred sentinel.

Everything else read-only is permitted: any file under any live container, any read-only `vtysh -c "show ..."`, any file under `repo_root`.

## What you do

There is no fixed checklist. Investigate freely until you can answer two questions:

1. **What broke?** A specific root cause with at least one quoted evidence anchor.
2. **What to do?** A classification (trivial / non-trivial / flaky) and a concrete action — patch applied (trivial), Suggested Fix written (non-trivial), or retry-once (flaky).

Default starting moves (use as hints, not a sequence):

- Scan `runner.log` for high-signal patterns first: `% Unknown command:`, `% Error:`, `No space left on device`, `dpkg-deb: error`, `ConnectionFail`, tracebacks outside the test body. These often answer question 1 in seconds.
- When `pdb_pane` is set, frames + locals from the live debugger are richer than any log. Confirm the prompt is still up (`tmux capture-pane`), then `where` / `args` / `p <expr>`.
- When containers are live and the failure was CLI-dispatch-related, run the CLI repro once.
- List `/core/traces/<c>/` per the rule above; tail whichever files the failure points at. `vtysh_traces` is almost always relevant when the test sent a vtysh command.
- Re-read the test source (the file from the pytest excerpt). For newly-added tests, honest authorship slips are the default failure mode.

## Classification

**trivial** — fix is mechanical, scoped to the test file OR to a file touched by `commits_in_scope`, intent preserved (no assertion deleted/weakened, no what-is-verified change, no setup-shape change). Examples: typo, wrong codegen method name, missing kwarg, wrong constant, missing fixture setup, prompt-handling that the test author missed for a `@bc.confirmation` decorator. **Apply the patch yourself** via `Write` / `StrReplace`. Save the unified diff to `<run_dir>/patch.diff`. Record `touched_paths`. Multiple sibling methods needing the *same* mechanical pattern still counts as trivial.

**non-trivial** — fix changes assertion shape / what is verified, crosses subsystems (mgmt ↔ control ↔ data plane), is multi-file with no clear unified pattern, or requires a design call. Write `<run_dir>/suggested-fix.md` (diff fragment or prose — be honest about which). **Do NOT touch the working tree.**

**flaky** — `failure_type` is in `{timeout, connection-reset, subprocess-killed}` AND `previous_runs_for_this_target` contains at least one prior PASSED iteration for this target in this run AND no iked-trace activity matches the failure window. Otherwise classify as non-trivial.

## Output contract

Write `<run_dir>/rca/summary.md` (human-readable, ~6 sections):

```
# iked Failure Handler — <target>

## 1. Identification
- Target / Suite / Failure type / Containers state / Run dir

## 2. Failure point (from pytest)
<verbatim ~10-30 line excerpt>

## 3. Investigation narrative
<numbered list of what you looked at and what each step told you — the only must-be-real section>

## 4. Root cause
<one or two sentences, cites at least one piece of §3 evidence>

## 5. Suggested fix (or applied fix on trivial)
<one of: "Applied — see patch.diff" + 1-paragraph rationale, OR diff/prose suggestion>

## 6. Classification + confidence
<trivial|non-trivial|flaky> / <high|medium|low> — <one sentence rationale>
```

Write `<run_dir>/rca/evidence.json` (machine-readable contract with the loop — same shape as today's `triage.json`):

```json
{
  "target": "<target>",
  "classification": "trivial | non-trivial | flaky",
  "non_trivial_reason": "intent-changed | cross-subsystem | out-of-scope | not-mechanical | null",
  "root_cause": "<one sentence>",
  "confidence": "low | medium | high",
  "candidate_target_path": "<repo-relative path or null>",
  "touched_paths": ["<repo-relative path>", "..."],
  "patch_path": "<run_dir>/patch.diff | null",
  "suggested_fix_path": "<run_dir>/suggested-fix.md | null",
  "evidence_paths": ["<abs path>", "..."]
}
```

`patch_path` is set only when classification is `trivial`. `suggested_fix_path` is set only when classification is `non-trivial`. `touched_paths` is empty unless classification is `trivial`.

**Return YAML to the loop** — field names match today's `triage_result` so the loop's downstream routing is unchanged:

```yaml
handler_result:
  classification: trivial | non-trivial | flaky
  next_action: re-queue | escalate | retry-once
  non_trivial_reason: <null | intent-changed | cross-subsystem | out-of-scope | not-mechanical>
  summary_path: <abs path>
  evidence_path: <abs path>
  patch_path: <abs path or null>           # trivial only
  suggested_fix_path: <abs path or null>   # non-trivial only
  touched_paths: ["<repo-relative>", "..."]  # empty unless trivial
  status: ready | blocker
  blocker: <reason or null>
```

The optional batch-evidence helper at `/home/dn/.drivenets/cheetah/AI/v2/private/skills/iked-failure-rca/scripts/collect-evidence.sh <run_dir> <suite>` is still available if you want to bulk-archive `show ike *` outputs and standard trace tails. It's a convenience, not required — targeted `docker exec ... > <run_dir>/rca/...` is preferred when you know exactly what evidence to capture.

## Halt conditions

- `not-a-failure` — verdict says passed; return immediately.
- `runner-log-missing` — `runner.log` doesn't exist or is empty.
- `no-live-containers` — `containers_state` was supposed to be `live-failed` but `docker ps` shows none; downgrade to `torn-down-by-script` in the report and proceed with `runner.log` alone.

## Quality bar (self-check)

- [ ] `runner.log` was scanned for obvious signal patterns BEFORE any container digging.
- [ ] When `pdb_pane` was set, the prompt was confirmed up and frames/locals were inspected; no `continue` was sent; pdb was quit with `q` Enter before returning.
- [ ] When containers were live and the failure was CLI-dispatch-related, the failing command was reproduced (one-shot only).
- [ ] The trace dir was listed (`ls /core/traces/<c>/`) — the rule's list is a hint, not the answer.
- [ ] `summary.md` §3 (Investigation narrative) describes the actual path taken, not a template.
- [ ] Every claim in `summary.md` §4 (Root cause) cites at least one piece of §3 evidence.
- [ ] On `trivial`: only files in `touched_paths` were edited (no incidental edits elsewhere); `patch.diff` is committed-style unified diff; the changes match an intent-preserving mechanical category.
- [ ] On `non-trivial`: no working-tree edits; `suggested-fix.md` exists and is honest about whether the recommendation is a diff or prose.
