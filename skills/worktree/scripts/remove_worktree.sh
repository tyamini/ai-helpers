#!/usr/bin/env bash
# remove_worktree.sh — remove a fix worktree created by Workflow B (pr-watchdog cleanup).
#
# Usage:
#   remove_worktree.sh [--force] <repo_root> <worktree_path> [dedicated_branch]
#
# Removes the linked worktree at <worktree_path>. REFUSES to remove the main
# worktree. If <dedicated_branch> is given, deletes that local branch after the
# worktree is gone (its commits are already pushed to the PR branch). A clean
# worktree is removed plainly; if it has modified/untracked files the script
# requires --force (the caller should surface those files to the user first and
# never discard pending work silently).
#
# Output (stdout, KEY=VALUE):
#   REMOVED=true|false
#   REASON=removed|already-gone|is-main|dirty
#
# Exit codes:
#   0  removed, or the worktree was already gone
#   2  refused: target is the main worktree
#   1  worktree has changes and --force not given, or other git error
set -euo pipefail

FORCE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --force)   FORCE=1 ;;
    -h|--help) echo "Usage: $0 [--force] <repo_root> <worktree_path> [dedicated_branch]"; exit 0 ;;
    *)         ARGS+=("$arg") ;;
  esac
done
set -- "${ARGS[@]}"

REPO_ROOT="${1:?repo_root required}"
WT="${2:?worktree_path required}"
DEDICATED_BRANCH="${3:-}"

cd "$REPO_ROOT"

# Canonical main worktree path (resolves symlinks), to refuse removing it.
MAIN_WT_PATH="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd -P)"
WT_CANON="$WT"
[ -d "$WT" ] && WT_CANON="$(cd "$WT" && pwd -P)"

if [ "$WT_CANON" = "$MAIN_WT_PATH" ]; then
  echo "ERROR: refusing to remove the main worktree ($WT_CANON)." >&2
  echo "REMOVED=false"; echo "REASON=is-main"
  exit 2
fi

# Not a registered worktree (already removed)? Prune stale entries and succeed.
if ! git worktree list --porcelain | grep -qx "worktree $WT_CANON"; then
  git worktree prune
  echo "REMOVED=false"; echo "REASON=already-gone"
  exit 0
fi

# Dirty (modified or untracked) and not forced → stop; caller must decide.
if [ "$FORCE" = 0 ] && [ -n "$(git -C "$WT_CANON" status --porcelain)" ]; then
  echo "ERROR: worktree at $WT_CANON has uncommitted/untracked changes:" >&2
  git -C "$WT_CANON" status --porcelain >&2
  echo "Re-run with --force to remove anyway (only after confirming nothing is needed)." >&2
  echo "REMOVED=false"; echo "REASON=dirty"
  exit 1
fi

if [ "$FORCE" = 1 ]; then
  git worktree remove --force "$WT_CANON"
else
  git worktree remove "$WT_CANON"
fi
git worktree prune

if [ -n "$DEDICATED_BRANCH" ]; then
  git branch -D "$DEDICATED_BRANCH" 2>/dev/null || true
fi

echo "REMOVED=true"; echo "REASON=removed"
