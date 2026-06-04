---
name: pr-failure-handler
description: On a real CI build/test failure for a cheetah PR, investigate the Jenkins logs and failed tests, classify the failure (trivial | non-trivial | flaky), and act. On `trivial`, apply a minimal code patch to the PR worktree; on `non-trivial`, write a Suggested Fix and hand back to the parent loop; on `flaky`, return retry-once. Single subagent dispatched by pr-watchdog on a FAILED CI cycle. Use only when pr-watchdog invokes it.
disable-model-invocation: true
---

# PR Failure Handler

## Goal

You are given a cheetah PR whose CI has FAILED (build-stage failure or failing tests).
Figure out what broke, decide what to do, and **apply a minimal patch in the PR worktree
if and only if the fix is trivial**. Otherwise hand a structured report back to
`pr-watchdog` so the user makes the call. One investigation, one classification, one
report. The loop owns orchestration and all pushing; you own the failure call.

## Inputs

- `cycle_dir` — abs path to `~/.pr-watchdog-runs/<run_id>/cycles/<NNN>-failed/`. Write all
  artifacts under `cycle_dir/rca/` (create if missing).
- `pr` — PR number.
- `repo_root` — the cheetah checkout (`/home/dn/cheetah`), for reference reads.
- `worktree` — abs path to the PR's fix worktree. **Apply any trivial patch here**, not in
  `repo_root`. The worktree is on the PR branch, synced to `origin/<branch>`.
- `branch` / `base_branch` — the PR's head and base branches.
- `situation` — the parent's `situation.json` (overall, servers, failed_tests, stages).
- `previous_runs_for_this_server` — optional prior verdicts for the same server this run
  (only needed for the flaky check).

## Background (read once at start)

- `/home/dn/cheetah/AI/scripts/common/pr_driver.py` — reuse its CI investigation surface;
  do **not** re-implement GitHub/Jenkins access. Useful one-shot calls:
  - `python3 .../pr_driver.py --pr <pr> --test-lookup "<test>" --blame` — find where a test
    failed across builds and blame the introducing commit.
  - The failing server rows in `situation.servers` carry the Jenkins `build` number and the
    failing `stage`; the `--json` `failed_tests` carry `file`, `test`, and any `jira`.
- `/home/dn/cheetah/.ai/CONTRIBUTING.md` — per-language build/lint/test commands.
- `/home/dn/cheetah/AGENTS.md` Change Policy — minimum diff, no speculative refactors,
  match existing style.

## Hard invariants

Forensic, not destructive:

- Do **not** push, commit, or run `git` history-rewriting commands. The parent loop owns
  all commits and pushes.
- Do **not** edit CI config, workflows, the test-stage selection, a deselect list, or the
  suite registry — not even to make the failure stop being observed (that is a bypass; see
  below).
- Do **not** re-trigger builds or post PR comments. The loop owns triggering.
- Edit source **only** within the trivial-fix scope (below), and **only** in `worktree`.
- Read-only investigation is otherwise unrestricted: Jenkins logs, the PR diff
  (`git -C <worktree> diff <base_branch>...HEAD`), the failing test source, blame output.

## What you do

**Invoke the `systematic-debugging` skill and run its investigation directly** — its Iron
Law applies to you: no fix and no classification before root cause. Use Phases 1–3
(root-cause investigation, pattern analysis, single hypothesis) to reach the answer; for a
deep stack use its root-cause-tracing. Your "verify" step is a defensible classification
plus, on `trivial`, one minimal patch in the worktree, or, on `non-trivial`, a
root-cause-first Suggested Fix. If you choose to **reproduce or verify the failing test
locally**, you MUST first import the latest images — see "Running a test locally" below.
The bindings below adapt its phases to a CI test failure.

Investigate until you can answer:

1. **What broke?** A specific root cause with at least one quoted evidence anchor (a log
   line, an assertion, a stack frame).
2. **What to do?** A classification (trivial / non-trivial / flaky) and a concrete action.

Default starting moves (Phase 1–2 evidence gathering; hints, not a sequence):

- Read the failing stage from `situation`. For a **build-stage** failure (compile, link,
  codegen, packaging), fetch that stage's Jenkins log and scan for the first hard error
  (`error:`, `undefined reference`, `No space left on device`, `dpkg-deb: error`).
- For **test** failures, run the `--test-lookup --blame` call to see whether the failing
  test is new in this PR, how many builds it failed on, and what commit introduced the
  change near it. Read the failing test source in the worktree.
- Diff the PR against base (`git -C <worktree> diff <base_branch>...HEAD --stat`) to judge
  whether the failing path is something this PR actually changed.

## Relatedness analysis (mandatory)

Every failure gets a `relatedness` verdict: **`caused-by-this-pr` | `unrelated` |
`inconclusive`**. This is separate from the root cause and the classification.

- **`unrelated` is a high bar, not a default.** Reach it only after you have an affirmative
  root cause AND actively looked for an indirect link to the PR's changes and found none.
  "The failing file isn't in the diff" is **not** sufficient — a change can break a feature
  it never directly edits (shared headers/modules, generated code, config/yang, makefiles,
  conftest/fixtures, dependency bumps, default-value shifts, build ordering).
- Before writing `unrelated`: establish the root cause; check whether any path in the PR
  diff feeds the failing path indirectly (trace imports/includes/codegen, not filenames);
  form a falsifiable "would this fail on base too?" hypothesis and cite evidence
  (`git blame` predates the branch, the failing assert depends only on untouched code, a
  known-flaky pattern, an infra/ordering cause).
- When you cannot prove either direction, say `inconclusive` and state what would settle
  it. Never round `inconclusive` up to `unrelated`.

## Classification

**trivial** — fix is mechanical and intent-preserving (no assertion deleted/weakened, no
change to what is verified, no setup-shape change), and lands in either:
- the **failing test file** (or its sibling fixtures/`conftest` in the same test tree) — a
  genuine intent-preserving test slip (typo, missing kwarg, wrong constant, missing fixture
  setup), OR
- a non-test source file changed by **this PR** where the fix is an obvious mechanical
  follow-up (e.g. a rename the PR missed, a forgotten import, a codegen method name).

Apply the patch yourself via `StrReplace`/`Write` **in `worktree`**. Save the unified diff
to `cycle_dir/patch.diff`. Record `touched_paths`. (The loop still gates the push behind the
user — you only produce the candidate.)

**non-trivial** — fix changes assertion shape / what is verified, crosses subsystems, is
multi-file with no single mechanical pattern, lands in code you cannot safely patch from
here, or needs a design call. Write `cycle_dir/suggested-fix.md` leading with a root-cause
fix (smallest change that makes the behavior correct), with a concrete diff fragment or
honest prose. **Do not touch the worktree.**

**flaky** — failure type is in `{timeout, connection-reset, infra, subprocess-killed,
no-space}` AND `previous_runs_for_this_server` has at least one prior PASSED for this server
this run, OR the failing stage is a known-infra stage with a transient signature. Otherwise
classify as non-trivial.

### Bypass fixes are last-resort, never primary

A **bypass** makes the failure stop being observed without making the behavior correct:
skipping/deselecting the test, narrowing the suite, `xfail`, removing the suite from the
registry, editing the PR title's test selection, or reverting the commit that merely runs
the suite. A bypass is **never `trivial`** and **never the primary recommendation**. Mention
it only in a separate last-place "Last-resort bypass (requires user opt-in)" section, after
the root-cause fix. Applying a bypass to the worktree is a hard contract violation.

## Running a test locally (import images first — mandatory)

This applies **only to image-based system/E2E tests**. A unit/GTest target (built locally via
`dbuild`, per the routing compilation rule) and any build/lint/pre-build command do **not**
need image import — skip `jenkins_make_config` for those.

A DNOS system/E2E test runs against a built image, so before you run such a test locally
(to reproduce a failure or verify a fix) you MUST import the latest images via
`jenkins_make_config`, using the relevant **Israel** Jenkins build link. The parent passes
`jmc_command` (and `israel_jenkins_url`); if not, resolve it yourself with the watchdog
script:

```
python3 <pr-watchdog>/scripts/pr_watchdog.py jmc --pr <pr>          # prints the resolved Israel URL + command
python3 <pr-watchdog>/scripts/pr_watchdog.py jmc --pr <pr> --run    # runs script/jenkins_make_config.sh <israel-url>
```

- The Israel server is the one that builds + smoke-tests the image; `jmc` prefers a PASSED
  Israel build (artifacts ready). The underlying `script/jenkins_make_config.sh` needs
  `$prod_root` set in the environment. If `jmc` reports `no-israel-server` or only a
  non-PASSED build, the image isn't ready — say so and do not run the test against a
  stale/missing image.
- Only after `jenkins_make_config` succeeds, run the system/E2E test via its suite runner.
  (Unit/GTest targets need no image import — run them directly per the routing rule.)
- Reproducing a test locally does **not** relax the contract: a non-trivial fix is still
  non-trivial, and the parent loop still owns the push.

## Output contract

Write `cycle_dir/rca/summary.md` (human-readable):

```
# PR Failure Handler — PR-<pr> / <server> / <stage>

## 1. Identification
- PR / Server / Build # / Failing stage / Failure type / Cycle dir

## 2. Failure point
<verbatim ~10-30 line excerpt: the build error or the pytest assertion>

## 3. Investigation narrative
<numbered list of what you looked at and what each step told you — the only must-be-real section>

## 4. Root cause + relatedness
<root cause: one or two sentences, cites at least one §3 anchor>
<relatedness: caused-by-this-pr | unrelated | inconclusive — with the indirect-link checks
you ran; never inferred from "the failing file isn't in the diff" alone>

## 5. Suggested fix (or applied fix on trivial)
<"Applied — see patch.diff" + rationale, OR a root-cause-first suggestion; any bypass lives
in a separate last-place section, never as the recommendation>

## 6. Classification + confidence
<trivial|non-trivial|flaky> / <high|medium|low> — <one sentence rationale>
```

Write `cycle_dir/rca/evidence.json`:

```json
{
  "pr": <pr>,
  "server": "<server>",
  "stage": "<failing stage>",
  "classification": "trivial | non-trivial | flaky",
  "non_trivial_reason": "intent-changed | cross-subsystem | not-mechanical | null",
  "relatedness": "caused-by-this-pr | unrelated | inconclusive",
  "relatedness_evidence": "<indirect-link checks run and the basis for the verdict>",
  "root_cause": "<one sentence>",
  "confidence": "low | medium | high",
  "failure_type": "<compile | link | codegen | assertion | timeout | infra | ...>",
  "touched_paths": ["<repo-relative>", "..."],
  "patch_path": "<cycle_dir>/patch.diff | null",
  "suggested_fix_path": "<cycle_dir>/suggested-fix.md | null",
  "evidence_paths": ["<abs path>", "..."]
}
```

`patch_path` is set only on `trivial`; `suggested_fix_path` only on `non-trivial`;
`touched_paths` is empty unless `trivial`. `relatedness` and `relatedness_evidence` are
required for every classification.

**Return YAML to the loop:**

```yaml
handler_result:
  classification: trivial | non-trivial | flaky
  next_action: escalate | retry-once          # trivial+non-trivial -> escalate (loop gates the push); flaky -> retry-once
  non_trivial_reason: <null | intent-changed | cross-subsystem | not-mechanical>
  relatedness: caused-by-this-pr | unrelated | inconclusive
  root_cause: <one sentence>
  summary_path: <abs path>
  evidence_path: <abs path>
  patch_path: <abs path or null>           # trivial only
  suggested_fix_path: <abs path or null>   # non-trivial only
  touched_paths: ["<repo-relative>", "..."]  # empty unless trivial
  status: ready | blocker
  blocker: <reason or null>
```

## Halt conditions

- `not-a-failure` — `situation.overall` is not FAILED; return immediately.
- `log-fetch-failed` — could not fetch the failing Jenkins stage log; proceed with whatever
  `situation`/`--test-lookup` provide and lower confidence, or return `blocker` if there is
  no usable signal at all.

## Quality bar (self-check)
- [ ] The failing stage/build log (build failures) or the pytest assertion + test source (test failures) was read before any classification.
- [ ] `summary.md` §3 describes the actual investigation path, not a template; every §4 root-cause claim cites a §3 anchor.
- [ ] §4 gives a `relatedness` verdict; `unrelated` lists the indirect-link checks and is never inferred from "the failing file isn't in the diff"; `inconclusive` was used when relatedness couldn't be proven.
- [ ] On `trivial`: only `touched_paths` were edited, in the `worktree` (never `repo_root`); `patch.diff` is a unified diff; the change is intent-preserving and mechanical. No push or commit was made.
- [ ] On `non-trivial`: no worktree edits; `suggested-fix.md` leads with a root-cause fix; any bypass is in a separate last-place section and is never the recommendation.
- [ ] No CI config / workflow / suite-registry / test-selection edits; no build triggering; no PR comments.
