#!/usr/bin/env bash
# update_branch.sh — bring a PR branch up to date with its base branch.
#
# Handles the "branch behind base" / "missing ref" / "Update branch required"
# class of failure deterministically: merge origin/<base> into the worktree's
# PR branch and (in apply mode) push. A clean merge is a SAFE fix and is
# auto-pushable; a conflicting merge is, by default, aborted and reported.
# With --keep-conflict the conflicted merge is LEFT IN PLACE (not aborted) and
# the conflicting files are printed so the caller's merge-conflict subagent can
# resolve them (pr-watchdog Stage 3m) instead of halting.
#
# Usage:
#   update_branch.sh --check <worktree> <base_branch>
#   update_branch.sh --apply <worktree> <base_branch> [--push] [--keep-conflict]
#
# Output (KEY=VALUE lines):
#   BEHIND=true|false        # is the branch behind origin/<base>?
#   ACTION=none|merged|conflict
#   PUSHED=true|false
#   HEAD=<sha>
#   CONFLICT_FILES=<sp-separated paths>   # only on conflict (unmerged paths)
#
# Exit codes:
#   0   up to date, or merged cleanly (and pushed if --push)
#   10  behind base (only in --check mode; no changes made)
#   3   merge conflict — aborted (default) or LEFT IN PLACE (--keep-conflict);
#       caller resolves (Stage 3m) or halts
#   1   other git error
set -euo pipefail

MODE="${1:?--check or --apply required}"
WT="${2:?worktree required}"
BASE="${3:?base_branch required}"
PUSH=false
KEEP_CONFLICT=false
for arg in "${@:4}"; do
  case "$arg" in
    --push) PUSH=true ;;
    --keep-conflict) KEEP_CONFLICT=true ;;
  esac
done

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
  CONFLICT_FILES=$(git diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
  if ! $KEEP_CONFLICT; then
    git merge --abort 2>/dev/null || true
  fi
  echo "BEHIND=true"
  echo "ACTION=conflict"
  echo "PUSHED=false"
  echo "HEAD=$(git rev-parse HEAD)"
  echo "CONFLICT_FILES=$CONFLICT_FILES"
  cat /tmp/merge_err >&2 || true
  exit 3
fi
