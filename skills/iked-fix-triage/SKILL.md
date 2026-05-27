---
name: iked-fix-triage
description: Classify an iked / IPsec E2E failure RCA as `trivial`, `non-trivial`, or `flaky`, then either apply a minimal patch (trivial) or emit a "Suggested Fix" report for the user (non-trivial). Includes a hard intent-preservation gate — any candidate fix that would change WHAT the test verifies is automatically non-trivial regardless of mechanical simplicity. Use when the parent `iked-test-loop` has an RCA bundle and needs a routing decision.
disable-model-invocation: true
---

# iked Fix Triage

## Goal
Read an RCA bundle plus the in-scope commits and the test source, then decide:

- **trivial** → apply a minimal patch to the working tree, return `next_action: re-queue`.
- **non-trivial** → emit a `suggested-fix.md` report, return `next_action: escalate`. The loop will surface this to the user.
- **flaky** → no patch, return `next_action: retry-once`.

The intent-preservation gate is non-negotiable: any candidate fix that would change what the test asserts or which behavior it exercises is **non-trivial**, even if the change is one line.

## Inputs
- `run_dir` (required) — the per-item run dir, contains `rca/summary.md` and `rca/evidence.json`.
- `evidence_path` (required) — `<run_dir>/rca/evidence.json` (the RCA's machine-readable output).
- `summary_path` (required) — `<run_dir>/rca/summary.md`.
- `commits` (required) — list of SHAs that represent the "new code under test". The parent passes these in. Used to compute the in-scope file set.
- `repo_root` (required) — absolute path to the cheetah checkout (e.g. `/home/dn/cheetah`).
- `previous_runs_for_this_target` (optional) — list of prior verdicts for the same target in this run; used by the flaky-detection branch.

## Hard invariants
- **No new abstractions, no opportunistic refactors.** Only the minimal diff that addresses the cited hypothesis. Adheres to AGENTS.md "Touch only what you must."
- **Intent gate beats trivial-fix-hint.** Even if `evidence.json.trivial_fix_hint == true`, the intent gate can still flip the classification to non-trivial.
- **Scope gate.** A change is in-scope-trivial only if it lives inside:
  - the test file from the pytest excerpt, OR
  - the union of files touched by the input `commits` (`git show --pretty= --name-only <sha>` per commit).
  Anything outside both is non-trivial by default.
- **Never modify a path outside scope** when applying a trivial fix. If the agent feels the right fix is elsewhere, that is itself the signal for non-trivial.

## Workflow

### Stage 1: Load context
1. Read `evidence.json`. Capture: `target`, `suite`, `failure_type`, `hypothesis`, `suspected_paths`, `confidence`, `trivial_fix_hint`.
2. Read `summary.md`. Capture the verbatim "Failure point" (section 2) and "Trace evidence" (section 4) — needed when emitting the suggested-fix report.
3. Locate the test source file:
   - From the pytest excerpt: `<file>::<class>::<method>` → resolve `<file>` relative to `repo_root` (typically `src/tests/{routing,cdnos,cli_tests}/tests/test_*.py`).
   - Read the entire test method (function body + decorators + class-level fixtures it depends on).
4. Compute the **in-scope file set**:
   - `git -C <repo_root> show --pretty= --name-only <sha>` for each sha in `commits`.
   - Add the test file path.
   - This is `scope_paths`.

**Gate:** RCA loaded, test source loaded, `scope_paths` computed.

### Stage 2: Extract test intent
The intent is the test's contract — what it claims to verify. Derive it from the source, **not** from the failure:

1. **Function name** — token-split the test name to get an intent phrase (`test_ipsec_iked_tunnel_initiation` → "tunnel initiation").
2. **Docstring** — if present, treat it as the canonical intent.
3. **Assertion messages and conditions** — list every `assert <cond>, "<msg>"`, `pytest.raises(...)`, `assert_<helper>(...)` in the method body. Each one is a sub-claim.
4. **Setup steps that gate the assertions** — `wait_for_*`, `wait_until_*`, fixture choices that set proposals / lifetimes / keys. These define WHAT the test is exercising.

Synthesize into 1–4 bullets: "this test verifies that …".

**Gate:** `intent_bullets` is a non-empty list.

### Stage 3: Propose a candidate fix
From the RCA hypothesis and the test source, draft the smallest diff that would plausibly make this run pass. Express it as:

- `candidate.target_path` — single file (multi-file candidates are by definition non-trivial; jump straight to Stage 4 escalate).
- `candidate.diff` — unified diff fragment.
- `candidate.rationale` — 1 sentence connecting the diff to the hypothesis.

Do **not** apply the diff yet.

### Stage 4: Intent-preservation gate
Walk these checks **in order**. Any single failure flips classification to `non-trivial` and skips to Stage 6.

1. **Assertion-shape preservation.** Does the candidate diff delete, weaken, comment out, or rewrite an assertion? (Examples of weakening: replacing `== N` with `>= 1`, widening a tolerance, replacing strict equality with `in`, removing an `assert`, replacing `wait_for(X, timeout=10)` with `wait_for(X, timeout=120)` **when** the long timeout would let a previously-caught race pass silently.)
2. **What-is-verified preservation.** Does the candidate diff change which conditions are checked (e.g. swap one CLI command for another, change the peer / SA being inspected, change which router is asserted on)?
3. **Setup-shape preservation.** Does the candidate diff alter the configuration the test pushes (different cipher, different lifetime, different topology, different routing) in a way that changes the scenario rather than fixing a typo?
4. **Cross-subsystem reach.** Does the suspected hypothesis or candidate diff implicate a subsystem outside the in-scope file set (mgmt ↔ control ↔ data plane crossing, new YANG, new RPC, new state machine state)?

If all four pass, the candidate **preserves intent**. Continue to Stage 5. Otherwise jump to Stage 6.

**Gate:** Classification is either "intent-preserved" (continue) or `non-trivial:intent-changed | non-trivial:cross-subsystem` (Stage 6).

### Stage 5: Trivial scope and apply
With intent preserved, run the scope check:

1. `candidate.target_path` must be inside `scope_paths` from Stage 1.
2. The diff must be **mechanical** — one of:
   - typo or symbol-rename follow-up
   - missing CLI line that a new commit clearly requires
   - error-string update matching an intentional message change in the diff under test
   - wait/poll tuning that **keeps** the original strictness (e.g. adjusting a wait helper to match a renamed CLI but with the same effective timeout)
   - fixture wiring (passing a missing argument, importing a new helper)

If either check fails → classification `non-trivial:out-of-scope` or `non-trivial:not-mechanical` → Stage 6.

If both pass:

1. Apply the diff to `candidate.target_path` in-place (this lands in the working tree; the loop will leave it uncommitted as part of the accumulated diff).
2. Save the applied patch to `<run_dir>/patch.diff` (`git -C <repo_root> diff -- <candidate.target_path> > <run_dir>/patch.diff`).
3. Compute `touched_paths = [candidate.target_path]`. The loop uses this to pick `-c` vs `-b` on the next runner invocation.
4. Write `<run_dir>/triage.json`:
   ```json
   {
     "classification": "trivial",
     "next_action": "re-queue",
     "intent_bullets": ["..."],
     "candidate_target_path": "<repo-relative path>",
     "touched_paths": ["<repo-relative path>"],
     "patch_path": "<run_dir>/patch.diff",
     "rationale": "<one sentence>"
   }
   ```

**Gate:** Patch landed in the working tree, `triage.json` written with `classification: trivial`.

### Stage 6: Non-trivial → emit Suggested Fix report
Write `<run_dir>/suggested-fix.md`. This file is what the loop shows the user verbatim. Structure:

```
# Suggested Fix — <target>

## Why this is non-trivial
<one of: intent-changed | cross-subsystem | out-of-scope | not-mechanical>
<1–2 sentences on which gate tripped and why>

## What the test was verifying (intent)
- <intent bullet 1>
- <intent bullet 2>

## RCA hypothesis
<copy from evidence.json>

## Evidence anchors
- Pytest excerpt: <run_dir>/rca/pytest_excerpt.txt
- Trace snippets: <run_dir>/rca/summary.md §4
- Live container state: <run_dir>/rca/summary.md §3

## Candidate fix (for user review — NOT applied)
File: <candidate.target_path or "TBD — see hypothesis">

```diff
<unified diff fragment, or pseudo-diff if multi-file>
```

## Blast radius
- Files implicated: <list>
- Subsystems crossed: <none | mgmt-control | control-data | yang | rpc>
- Risk of breaking other iked tests: <low|medium|high — one sentence>

## Recommended next step
<one of:
- "Apply this candidate fix to the accumulated diff and re-run the test."
- "Investigate <area> before deciding — the candidate above is a guess."
- "This fix changes test intent. Confirm the new intent is what you want before applying."
>
```

Then write `<run_dir>/triage.json`:

```json
{
  "classification": "non-trivial",
  "next_action": "escalate",
  "intent_bullets": ["..."],
  "non_trivial_reason": "intent-changed|cross-subsystem|out-of-scope|not-mechanical",
  "suggested_fix_path": "<run_dir>/suggested-fix.md",
  "candidate_target_path": "<path or null>",
  "touched_paths": []
}
```

**Gate:** `suggested-fix.md` and `triage.json` exist; no working-tree changes were applied.

### Stage 6b: Flaky shortcut (checked before Stages 3–6)
Run this check between Stage 2 and Stage 3 as an early exit:

- `failure_type` in `{"timeout", "connection-reset", "subprocess-killed"}` AND
- No `iked_traces` evidence of FSM-level activity around the failure window in `summary.md` §4 AND
- `previous_runs_for_this_target` contains at least one prior `status: passed` for this target in this run.

If all true → write `<run_dir>/triage.json`:

```json
{
  "classification": "flaky",
  "next_action": "retry-once",
  "intent_bullets": ["..."],
  "touched_paths": []
}
```

Return without producing a patch or suggested-fix report. (If the retry also fails, the next triage round will not see "this run had a prior pass" any more and will classify normally.)

### Stage 7: Return
Return:

```yaml
triage_result:
  run_dir: <abs path>
  classification: trivial|non-trivial|flaky
  next_action: re-queue|escalate|retry-once
  non_trivial_reason: <one of: null|intent-changed|cross-subsystem|out-of-scope|not-mechanical>
  triage_path: <run_dir>/triage.json
  suggested_fix_path: <run_dir>/suggested-fix.md   # only when non-trivial
  patch_path: <run_dir>/patch.diff                  # only when trivial
  touched_paths: ["<repo-relative>", "..."]         # empty unless trivial
```

## Halt conditions
- `rca-missing` — `evidence.json` or `summary.md` not found.
- `no-commits` — `commits` input is empty (the scope gate has nothing to compare against). In that case treat **every** non-test-file candidate as out-of-scope (still legal; not a halt) — the halt only fires if the test file itself cannot be resolved either.
- `test-file-unresolved` — pytest excerpt did not yield a `<file>::<class>::<method>` triple the agent can map to a real file under `<repo_root>/src/tests/`.
- `multi-file-candidate-with-no-clear-primary` — the agent cannot honestly point at a single file as the primary fix site. This is itself a signal for non-trivial; do not silently pick one.

## Output format
The `triage_result` YAML in Stage 7, plus `triage.json` and either `patch.diff` (trivial) or `suggested-fix.md` (non-trivial).

## Quality bar (self-check)
[ ] RCA bundle was read; no test was re-run; no docker commands were issued.
[ ] `intent_bullets` were derived from the test source, not from the failure message.
[ ] Stage 4 intent gate was applied in order; the first failing check decided non-trivial.
[ ] When trivial, the candidate path was inside `scope_paths` (test file or commit-touched file).
[ ] When trivial, only `candidate.target_path` was modified in the working tree — no incidental edits elsewhere.
[ ] When trivial, `touched_paths` is recorded so the loop can choose `-c` vs `-b` (services/control/** → `-b`).
[ ] When non-trivial, no working-tree changes were made; `suggested-fix.md` quotes the RCA evidence rather than asserting new claims.
[ ] When flaky, no patch and no report; just `triage.json` with `retry-once`.
[ ] `triage.json` schema matches Stage 5/6/6b exactly so the loop can parse it deterministically.
