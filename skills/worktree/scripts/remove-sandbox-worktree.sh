#!/bin/bash
# remove-sandbox-worktree.sh — Remove a sandbox worktree (created by sandbox-worktree.sh) and its debs dir.
#
# Usage:
#   bash .ai/skills/common/using-git-worktree/scripts/remove-sandbox-worktree.sh [--force] <branch>
#
# Arguments:
#   branch  — Branch whose linked worktree should be removed.
#
# Flags:
#   --force  Force removal even if the worktree has modified files or
#            untracked files other than the auto-generated 'sandbox-env.sh'.
#
# What it does:
#   1. Finds the linked worktree for <branch> via 'git worktree list --porcelain'.
#   2. Refuses to remove the main worktree.
#   3. Removes the worktree:
#        - Plain 'git worktree remove' if the tree is clean.
#        - Force-removes if the only untracked file is 'sandbox-env.sh'
#          (auto-generated; safe to delete).
#        - Otherwise requires --force.
#   4. Removes /tmp/dbuild-sandbox/<worktree-name>/ if present.
#
set -euo pipefail

usage() {
    echo "Usage: $0 [--force] <branch>"
    echo ""
    echo "  branch    Branch whose linked worktree should be removed"
    echo ""
    echo "  --force   Force removal even if the worktree has modified or"
    echo "            untracked files other than the auto-generated 'sandbox-env.sh'."
    exit 1
}

FORCE=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --force)   FORCE=1 ;;
        -h|--help) usage ;;
        *)         ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]}"

if [[ $# -lt 1 ]]; then
    usage
fi

BRANCH="$1"

git rev-parse --show-toplevel >/dev/null 2>&1 || {
    echo "Error: Not inside a git repository." >&2
    exit 1
}

# Discover the worktree that has this branch checked out.
WT_PATH="$(
    git worktree list --porcelain | awk -v want="refs/heads/${BRANCH}" '
        /^worktree / { path=$2 }
        /^branch /   { if ($2 == want) { print path; exit } }
    '
)"

if [ -z "$WT_PATH" ]; then
    echo "Error: no worktree found for branch '${BRANCH}'." >&2
    echo "  Run 'git worktree list' to see existing worktrees." >&2
    exit 1
fi

# Use pwd -P so the path matches the canonical form 'git worktree list --porcelain' prints
# (resolves symlinks, e.g. ~/cheetah -> ~/workspace/cheetah).
MAIN_WT_PATH="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd -P)"
if [ "$WT_PATH" = "$MAIN_WT_PATH" ]; then
    echo "Error: branch '${BRANCH}' is checked out in the main worktree (${WT_PATH})." >&2
    echo "Refusing to remove the main worktree." >&2
    exit 1
fi

SANDBOX_NAME="$(basename "${WT_PATH}")"
SANDBOX_BASE="/tmp/dbuild-sandbox/${SANDBOX_NAME}"

# Inspect the worktree's working-tree state. Porcelain status lines are
# "XY <path>"; for our purposes we only care which paths are dirty, not why.
DIRTY_PATHS=()
if [ -d "$WT_PATH" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && DIRTY_PATHS+=("${line:3}")
    done < <(git -C "$WT_PATH" status --porcelain)
fi

USE_FORCE=0
if [ "$FORCE" = 1 ]; then
    USE_FORCE=1
elif [ "${#DIRTY_PATHS[@]}" -eq 0 ]; then
    USE_FORCE=0
elif [ "${#DIRTY_PATHS[@]}" -eq 1 ] && [ "${DIRTY_PATHS[0]}" = "sandbox-env.sh" ]; then
    # Only the auto-generated env file is untracked — safe to force.
    echo "==> Worktree contains only auto-generated sandbox-env.sh; force-removing."
    USE_FORCE=1
else
    echo "Error: worktree at ${WT_PATH} has modified or untracked files:" >&2
    for p in "${DIRTY_PATHS[@]}"; do
        echo "  $p" >&2
    done
    echo "Re-run with --force to remove anyway." >&2
    exit 1
fi

echo "==> Removing worktree at ${WT_PATH}..."
if [ "$USE_FORCE" = 1 ]; then
    git worktree remove --force "${WT_PATH}"
else
    git worktree remove "${WT_PATH}"
fi

if [ -d "${SANDBOX_BASE}" ]; then
    echo "==> Removing sandbox dir ${SANDBOX_BASE}..."
    if ! rm -rf "${SANDBOX_BASE}" 2>/dev/null; then
        echo "Warning: could not remove ${SANDBOX_BASE} (permission denied?)." >&2
        echo "  Try: sudo rm -rf ${SANDBOX_BASE}" >&2
        exit 1
    fi
fi

echo ""
echo "==> Removed worktree for branch '${BRANCH}'."
