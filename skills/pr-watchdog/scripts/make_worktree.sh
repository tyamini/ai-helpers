#!/usr/bin/env bash
# make_worktree.sh — create or reuse an isolated git worktree for a PR branch.
#
# All pr-watchdog fixes are applied in this worktree, never in the user's main
# checkout. The worktree tracks the PR branch and is synced to origin/<branch>
# before any fix so commits land cleanly on top of the remote tip.
#
# Usage:
#   make_worktree.sh <repo_root> <branch> <worktree_path>
#
# Output (stdout, one KEY=VALUE per line, machine-readable):
#   WORKTREE=<abs path>
#   BRANCH=<branch>
#   HEAD=<sha>
#   CREATED=true|false
#   SYNCED=true|false        # true if reset/pulled to origin/<branch>
#
# Exit codes:
#   0  worktree ready (synced to origin/<branch> when the remote branch exists)
#   3  branch checked out elsewhere / worktree path conflict — caller must halt
#   1  other git error
set -euo pipefail

REPO_ROOT="${1:?repo_root required}"
BRANCH="${2:?branch required}"
WT="${3:?worktree_path required}"

cd "$REPO_ROOT"

git fetch --quiet origin "$BRANCH" 2>/dev/null || true
HAS_REMOTE=false
if git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
  HAS_REMOTE=true
fi

CREATED=false
SYNCED=false

if [ -d "$WT" ] && git -C "$WT" rev-parse --git-dir >/dev/null 2>&1; then
  # Reuse existing worktree. Confirm it is on the expected branch.
  CUR=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
  if [ "$CUR" != "$BRANCH" ]; then
    echo "ERROR: worktree $WT is on '$CUR', expected '$BRANCH'" >&2
    exit 3
  fi
else
  # Fresh worktree. `git worktree add` fails (non-zero) if BRANCH is already
  # checked out in another worktree (e.g. the user's main checkout) — that is the
  # conflict we report as code 3 so the caller can halt and ask the user.
  if ! git worktree add "$WT" "$BRANCH" 2>/tmp/wt_err; then
    grep -q "already checked out\|already used by worktree" /tmp/wt_err && { cat /tmp/wt_err >&2; exit 3; }
    cat /tmp/wt_err >&2
    exit 1
  fi
  CREATED=true
fi

# Sync to the remote tip so fixes are based on what CI actually built. Only do
# this when the worktree is clean — never discard user/handler changes silently.
if $HAS_REMOTE; then
  if [ -z "$(git -C "$WT" status --porcelain)" ]; then
    git -C "$WT" reset --hard --quiet "origin/$BRANCH"
    SYNCED=true
  fi
fi

HEAD=$(git -C "$WT" rev-parse HEAD)
echo "WORKTREE=$WT"
echo "BRANCH=$BRANCH"
echo "HEAD=$HEAD"
echo "CREATED=$CREATED"
echo "SYNCED=$SYNCED"
