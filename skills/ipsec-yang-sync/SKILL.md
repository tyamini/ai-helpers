# IPsec YANG Branch Sync

## Goal
Maintain `ipsecYang/<base>` (e.g. `ipsecYang/dev_v26_2`) as a branch that mirrors the IPsec-relevant changes from a feature branch (e.g. `feature/v262_routing_ike`). Each run adds **one commit** on top of the current target, replaying only the IPsec-scoped files that actually changed in the input branch vs its parent base.

## Inputs
- **Input branch** (required): the feature branch to sync from (e.g. `feature/v262_routing_ike`, `infra/feature/v262_routing_ike`).
- **Base branch** (optional): the dev branch the input is built on (e.g. `dev_v26_2`). Auto-detected from the input name if omitted.

## File scope
Files changed in the input branch (vs. base) matching:

| Category | Path pattern | Selection |
|----------|--------------|-----------|
| YANG | `prod/dnos_monolith/yangs/*.yang` | only files changed in input vs base |
| CLI YAML | `prod/dnos_monolith/autogen_cli/**/*.yaml` | only files changed in input vs base |
| CLI RST | `prod/dnos_monolith/dnos_cli/**/*.rst` | only files changed in input vs base |
| IPsec spec | `.ai/spec/planned/Services/transport/ipsec/**` | full snapshot from input (replaces target dir contents) |

Deletions and renames in the input branch are mirrored.

## Worktree
The script runs entirely in a sibling worktree at:

```
<repo>/../cheetah-ipsec-yang-<base>
```

so the user's main checkout is never touched. The worktree is reused across runs.

## Target branch policy
- The target branch is `ipsecYang/<base>` (e.g. `ipsecYang/dev_v26_2`).
- If it does not exist on origin → it is created from `origin/<base>` in the worktree.
- If it exists → the worktree is reset to `origin/<target>` before applying changes.
- Each run adds at most **one** commit on top of the current target. No force-push: the branch accumulates history.

## Workflow

The skill invokes:

```
bash $(realpath .claude/skills/ipsec-yang-sync)/scripts/sync-ipsec-yang.sh \
    <input-branch> [--base <base-branch>] [--no-push]
```

### Stage 1: Fetch and resolve refs
1. `git fetch --prune origin`.
2. Resolve `<input>` as `origin/<input>` (preferred) or `refs/heads/<input>`.
3. Resolve base: use `--base` if given; otherwise scan the input name for `v<digits>[._]?<digits>` tokens, normalize to `dev_v<x>_<y>`, and require exactly one of them to exist on `origin/`. Fail with a clear message asking for `--base` if 0 or >1 candidates match.

### Stage 2: Prepare the worktree
1. `git worktree prune`.
2. If the worktree dir already exists as a worktree → reuse it.
3. Otherwise:
   - Remove any stray non-worktree dir at the same path.
   - Delete any stale local `ipsecYang/<base>` branch outside the worktree (legacy cleanup from older versions of this script).
   - `git worktree add -B ipsecYang/<base> <wt-dir> <start-point>` where `<start-point>` is `origin/ipsecYang/<base>` if it exists, else `origin/<base>`.
4. If the target exists on origin, `git -C <wt> reset --hard origin/<target>`.

### Stage 3: Compute filtered change set
1. `git diff --name-status -z origin/<base>...origin/<input>`.
2. Filter to the three glob patterns (`prod/dnos_monolith/yangs/*.yang`, `autogen_cli/**/*.yaml`, `dnos_cli/**/*.rst`).
3. Bucket into `ADDS_MODS`, `DELS`, `RENAMES`.
4. Check whether `.ai/spec/planned/Services/transport/ipsec` exists on the input branch.
5. Print counts.

### Stage 4: Apply changes in the worktree
1. `ADDS_MODS`: `git -C <wt> checkout origin/<input> -- <path>`.
2. `DELS`: `git -C <wt> rm -f --ignore-unmatch -- <path>`.
3. `RENAMES`: rm-old + checkout-new.
4. Spec dir: `git rm -rf` + `rm -rf` + `git checkout origin/<input> -- <spec-dir>` (full snapshot).
5. All operations self-stage in the index; the script does **not** run `git add -A`.

### Stage 5: Commit and push
1. If `git diff --cached --quiet` → exit cleanly with `Target already in sync`.
2. Otherwise commit with a templated message listing source SHA, base SHA, and file counts.
3. `git push origin <target>` (plain push, no force). Skipped if `--no-push`.

## Output

```
================ ipsec-yang-sync summary ================
Input branch : feature/v262_routing_ike
Base branch  : dev_v26_2
Target branch: ipsecYang/dev_v26_2
Commit SHA   : <sha>
Worktree     : /home/dn/cheetah-ipsec-yang-dev_v26_2
Files: <N> yang, <N> autogen_cli yaml, <N> dnos_cli rst, spec snapshot: yes/no
Pushed to    : origin/ipsecYang/dev_v26_2
=========================================================
```

If the target is already in sync, the script prints `(no change)` and exits 0 without an empty commit.

## Quality bar (self-check)
[ ] Runs entirely in a sibling worktree; the user's main working tree is never modified.
[ ] No force-push — the target branch accumulates history.
[ ] Input and base both resolve before any worktree or branch is touched.
[ ] Base auto-detection succeeds only when exactly one `dev_v<x>_<y>` candidate exists on origin; otherwise asks for `--base`.
[ ] Only paths matching the four configured patterns are staged on the target.
[ ] Deletions and renames from the input branch are mirrored.
[ ] The IPsec spec dir is replaced as a full snapshot, not a per-file diff.
[ ] CLI RST paths with spaces are quoted correctly in all git invocations.
[ ] Stale local `ipsecYang/<base>` branches outside the worktree are cleaned up automatically.
[ ] No-op runs (target already in sync) exit 0 without an empty commit.
[ ] Script exits non-zero on any failure.
