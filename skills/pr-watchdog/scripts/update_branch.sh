#!/usr/bin/env bash
# update_branch.sh — bring a PR branch up to date with its base branch.
#
# Handles the "branch behind base" / "missing ref" / "Update branch required"
# class of failure deterministically: merge origin/<base> into the worktree's
# PR branch and (in apply mode) push. A clean merge is a SAFE fix and is
# auto-pushable; a conflicting merge is aborted and reported so the orchestrator
# halts and asks the user.
#
# Usage:
#   update_branch.sh --check <worktree> <base_branch>
#   update_branch.sh --apply <worktree> <base_branch> [--push]
#
# Output (KEY=VALUE lines):
#   BEHIND=true|false        # is the branch behind origin/<base>?
#   ACTION=none|merged|conflict
#   PUSHED=true|false
#   HEAD=<sha>
#
# Exit codes:
#   0   up to date, or merged cleanly (and pushed if --push)
#   10  behind base (only in --check mode; no changes made)
#   3   merge conflict — aborted; caller must halt and ask the user
#   1   other git error
set -euo pipefail

MODE="${1:?--check or --apply required}"
WT="${2:?worktree required}"
BASE="${3:?base_branch required}"
PUSH=false
[ "${4:-}" = "--push" ] && PUSH=true

cd "$WT"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch --quiet origin "$BASE" 2>/dev/null || true

if ! git rev-parse --verify --quiet "origin/$BASE" >/dev/null; then
  echo "ERROR: origin/$BASE not found" >&2
  exit 1
fi

BEHIND_COUNT=$(git rev-list --count "HEAD..origin/$BASE" 2>/dev/null || echo 0)
if [ "$BEHIND_COUNT" -eq 0 ]; then
  echo "BEHIND=false"
  echo "ACTION=none"
  echo "PUSHED=false"
  echo "HEAD=$(git rev-parse HEAD)"
  exit 0
fi

if [ "$MODE" = "--check" ]; then
  echo "BEHIND=true"
  echo "ACTION=none"
  echo "PUSHED=false"
  echo "HEAD=$(git rev-parse HEAD)"
  exit 10
fi

# --apply: attempt a non-interactive merge of the base branch.
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: worktree is dirty; refusing to merge base over uncommitted changes" >&2
  exit 1
fi

if git merge --no-edit "origin/$BASE" 2>/tmp/merge_err; then
  PUSHED=false
  if $PUSH; then
    git push origin "HEAD:$BRANCH"
    PUSHED=true
  fi
  echo "BEHIND=true"
  echo "ACTION=merged"
  echo "PUSHED=$PUSHED"
  echo "HEAD=$(git rev-parse HEAD)"
  exit 0
else
  git merge --abort 2>/dev/null || true
  echo "BEHIND=true"
  echo "ACTION=conflict"
  echo "PUSHED=false"
  echo "HEAD=$(git rev-parse HEAD)"
  cat /tmp/merge_err >&2 || true
  exit 3
fi
