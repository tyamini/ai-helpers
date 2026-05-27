---
name: iked-failure-rca
description: Perform root-cause analysis on a failed iked / IPsec E2E test while the e2e_* containers are still live. Pulls `show ike *` outputs and trace tails from every router, correlates them with the failing pytest step, and writes a structured RCA report under the run dir. Use when `iked-test-runner` returns a failure with `containers_state: live-failed`, or when a human asks "what went wrong with iked test X" against a still-live setup.
disable-model-invocation: true
---

# iked Failure RCA

## Goal
Given a failed iked / IPsec E2E test with live `e2e_*` containers (or a recently-collected runner log if containers are already gone), produce a deterministic, evidence-backed RCA report at `<run_dir>/rca/summary.md` plus a machine-readable `<run_dir>/rca/evidence.json`. The report must be specific enough that `iked-fix-triage` can classify the failure without re-running the test.

## Inputs
- `run_dir` (required) — absolute path the runner already wrote into, e.g. `~/.iked-runs/<run-id>/items/<seq>-<slug>/`.
- `target` (required) — the test/suite name that was run.
- `verdict_path` (required) — `<run_dir>/verdict.json` from the runner.
- `runner_log` (required) — `<run_dir>/runner.log` from the runner.
- `containers_state` (required) — `live-failed | torn-down-by-script | torn-down-passed`. The runner sets this; this skill must not assume.

## Companion docs (read once)
- `/home/dn/cheetah/AI/rules/routing/iked-e2e-testing.mdc` — source of truth for which trace files matter per suite (`routing` → `iked_traces`, `rib-manager_traces`, `fibmgrd_traces`; `cdnos` / `cli_tests` → those plus `routing_manager`, `cli`).

## Hard invariants
- **Read-only**: never restart containers, never run cleanup, never re-invoke `test_ike.sh`. The setup is forensic state.
- **Never sleep > 30s** when polling for any short follow-up (per the iked-e2e rule). In practice this skill does not poll — it gathers and analyzes.
- **No code edits.** Suggesting a fix is the next skill's job (`iked-fix-triage`).
- **No assertions without evidence.** Every claim in `summary.md` must cite a file/line from the gathered evidence.

## Workflow

### Stage 1: Resolve suite and load runner artifacts
1. Read `verdict_path`. Confirm `status == "failed"`. If not, return immediately with `blocker: not-a-failure` — this skill only runs on failures.
2. Determine the suite from the verdict's `suite_hint`; if absent, derive from `target` by checking which file in the iked-e2e rule it belongs to:
   - `test_ipsec_iked_*` → `routing`
   - `test_ipsec_cdnos_*` → `cdnos`
   - `test_esp_aead` and other CLI-style ipsec tests → `cli_tests`
   If ambiguous, default to `routing` and record `suite_resolution: "defaulted"` in the evidence.
3. Read the tail of `runner.log` (last ~500 lines is usually enough) and locate the pytest failure block. Extract:
   - **Failing test ID** (file::class::method).
   - **Failure type** (`AssertionError`, `Exception`, timeout, `Connection reset`, etc.).
   - **Last 30 lines before the failure** = the "failure context".
   - **Setup stage at the time of failure** if recognizable (e.g. "before tunnel up", "after tunnel up, during config push").

Write the extract verbatim to `<run_dir>/rca/pytest_excerpt.txt`.

**Gate:** `pytest_excerpt.txt` exists and contains the failing block.

### Stage 2: Mechanical evidence collection
Only when `containers_state == "live-failed"`:

1. Invoke the helper script:
   ```bash
   bash $(realpath /home/dn/.drivenets/cheetah/AI/v2/private/skills/iked-failure-rca/scripts/collect-evidence.sh) \
       <run_dir> <suite>
   ```
2. Confirm the script wrote `<run_dir>/rca/shows/` and `<run_dir>/rca/traces/`. If `<run_dir>/rca/no_live_containers.txt` was written, flip `containers_state` to `torn-down-by-script` for the rest of the workflow and continue with what we have (just `runner.log`).

When `containers_state == "torn-down-by-script"` from the start:
- Skip the helper script.
- Note in the evidence that no container-side data was available.

**Gate:** Either `shows/` + `traces/` are populated for every live router, or it is recorded that no live containers were available.

### Stage 3: Correlate
This is the analysis step. Cross-reference the pytest failure point with the gathered evidence:

1. **Identify the failure moment.** From `pytest_excerpt.txt`, extract a rough timestamp (pytest prints test progress with timestamps; otherwise use the runner's `ended_at` from the verdict as an upper bound).
2. **Walk each trace file's tail** under `traces/` and find log lines within a few minutes of the failure. Prioritize:
   - `iked_traces` lines containing `ERROR`, `FATAL`, `WARN`, `state transition`, `negotiation failed`, `IKE_AUTH`, `CHILD_SA`, `delete`, `rekey`.
   - `rib-manager_traces` / `fibmgrd_traces` lines containing the IPsec SA/SP identifiers or `install`, `remove`, `error`.
   - `routing_manager` / `cli` (mgmt env suites only) lines containing the operation the test was performing (`set ike`, `commit`, RPC call names).
3. **Walk each `shows/<container>.show_ike_*` file** and capture:
   - Whether SAs/tunnels exist at all (empty output is itself evidence).
   - SA state (`ESTABLISHED`, `CONNECTING`, `DELETED`, `INSTALLED`).
   - Visible mismatches (peer IP, encryption proposal, lifetime).
4. **Form a hypothesis.** Pick the single most likely root cause and articulate it in 1–3 sentences. Reasonable shapes:
   - "Tunnel never reached IKE_AUTH on R1; `iked_traces` shows repeated `IKE_SA_INIT` retransmits and `show ike sa` is empty. Likely peer IP / port misconfiguration on R2."
   - "Tunnel established but no SA installed in FIB; `iked_traces` shows install request, `fibmgrd_traces` shows error `<reason>`. Likely an issue in the new install-path code under test."
   - "Test asserted `show ike tunnel` count == 2; container shows count == 1. The second tunnel never got created — `iked_traces` has no `CHILD_SA` lines for it."
5. **Identify suspected files** (best-effort list):
   - C/C++: parse trace lines containing source file references (e.g. `iked/.../foo.c:123`) and collect unique file paths.
   - Python (test side): the test file from the pytest excerpt is always a suspect.
   - Map back to repo paths under `/home/dn/cheetah/services/control/quagga/iked/...` or `/home/dn/cheetah/src/tests/...`.

**Gate:** Hypothesis is formed and at least one piece of evidence (trace snippet or show output) supports it.

### Stage 4: Write the structured report
Write `<run_dir>/rca/summary.md` with this exact section structure:

```
# iked Failure RCA — <target>

## 1. Identification
- Target: <target>
- Suite: <suite> (resolution: <given|defaulted>)
- Failure type: <e.g. AssertionError, timeout, exception>
- Containers state: <live-failed|torn-down-by-script>
- Run dir: <abs path>

## 2. Failure point (from pytest)
<verbatim ~10–30 line excerpt from pytest_excerpt.txt that shows the assertion / exception>

## 3. Live container state (show ike *)
For each container where show output exists, include:

### <container>
- `show ike sa`: <one-line summary> — full output: rca/shows/<container>.show_ike_sa
- `show ike tunnel`: <one-line summary>
- `show ike swan-config`: <present|empty|errors>
- `show ike interface`: <present|empty>

(omit this whole section when containers_state != live-failed; note the absence)

## 4. Trace evidence
For each trace file that yielded a relevant snippet, include a small quoted block:

### <container> — <trace_file>
```
<5–20 line snippet around the relevant log lines>
```
Source: rca/traces/<container>.<trace_file>.tail

(include only trace files that produced useful evidence; do not pad)

## 5. Hypothesis
<1–3 sentences. Single most likely root cause. Cites at least one of the
evidence pieces above by section reference.>

## 6. Suspected files
- <repo-relative path 1>
- <repo-relative path 2>
- (test file is always listed)

## 7. Confidence
<low | medium | high> — <one-sentence rationale>

## 8. Trivial-fix candidate hint
<true|false> — <one-sentence rationale. NOT a classification; just a hint for
iked-fix-triage. True iff hypothesis points to the test file, a clearly
mechanical CLI/config string mismatch, or a missed-wait race that does not
relax intent.>
```

Also write `<run_dir>/rca/evidence.json` (machine-readable for the triage skill):

```json
{
  "target": "<target>",
  "suite": "routing|cdnos|cli_tests",
  "failure_type": "<string>",
  "containers_state": "live-failed|torn-down-by-script",
  "failure_excerpt_path": "<abs path>",
  "shows_dir": "<abs path or null>",
  "traces_dir": "<abs path or null>",
  "hypothesis": "<1-3 sentences>",
  "suspected_paths": ["<repo-relative>", "..."],
  "confidence": "low|medium|high",
  "trivial_fix_hint": true|false,
  "trivial_fix_hint_reason": "<string>"
}
```

**Gate:** Both `summary.md` and `evidence.json` exist and validate.

### Stage 5: Return
Return:

```yaml
rca_result:
  run_dir: <abs path>
  summary_path: <run_dir>/rca/summary.md
  evidence_path: <run_dir>/rca/evidence.json
  hypothesis: <one line>
  confidence: low|medium|high
  trivial_fix_hint: true|false
  status: ready | blocker
  blocker: <reason or null>
```

## Halt conditions
- `not-a-failure` — verdict says passed; nothing to RCA.
- `runner-log-missing` — `runner.log` path does not exist or is empty.
- `evidence-collection-failed` — `collect-evidence.sh` did not produce expected output AND `containers_state` was `live-failed` (i.e. we expected live containers and got nothing — the agent should report this rather than silently proceed).

## Output format
The two files above (`summary.md`, `evidence.json`) plus the `rca_result` YAML in Stage 5.

## Quality bar (self-check)
[ ] Verdict was read; this run only proceeds on `status == failed`.
[ ] Helper script was used for evidence collection when containers were live; no manual `docker exec` repetition.
[ ] `summary.md` has all 8 sections filled in.
[ ] Every claim in section 5 (Hypothesis) cites at least one piece of section 3 or section 4 evidence.
[ ] `suspected_paths` are repo-relative and verified to exist (or the test file is included as a fallback).
[ ] `trivial_fix_hint` is set with a one-sentence rationale; this is a hint, not a classification.
[ ] No code was edited; no containers were restarted; no test was re-run.
[ ] Polling intervals (where used) were under 30 seconds.
