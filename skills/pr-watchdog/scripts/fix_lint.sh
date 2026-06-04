#!/usr/bin/env bash
# fix_lint.sh — run the repo's standard auto-formatter / lint-fixer for a given
# category inside the worktree, then report whether it changed any files.
#
# This is the deterministic half of a lint fix: the orchestrator (or handler)
# decides the CATEGORY from the failing CI stage; this script runs the matching
# repo-blessed command. Commands come from .ai/CONTRIBUTING.md and the
# pr_driver.py header (canonical lint = `dbuild make mega_lint`).
#
# Usage:
#   fix_lint.sh <worktree> <category>
#     category: rust | yang | python | generic
#
# Output (KEY=VALUE lines):
#   CATEGORY=<category>
#   RAN=<command actually run>
#   CHANGED=true|false        # did the fixer modify tracked files?
#   FILES=<n>                 # number of changed files
#
# Exit codes:
#   0  command ran (CHANGED reflects whether anything was fixed)
#   2  unknown category
#   1  the fixer command itself failed (not a formatting diff)
set -euo pipefail

WT="${1:?worktree required}"
CATEGORY="${2:?category required}"
cd "$WT"

case "$CATEGORY" in
  rust)    CMD="make rust_fmt" ;;
  yang)    CMD="make validate-yangs" ;;
  python)  CMD="dbuild make mega_lint" ;;
  generic) CMD="dbuild make mega_lint" ;;
  *)       echo "ERROR: unknown category '$CATEGORY' (rust|yang|python|generic)" >&2; exit 2 ;;
esac

echo "CATEGORY=$CATEGORY"
echo "RAN=$CMD"

# Run the fixer. A non-zero exit from a pure formatter is a real error; a
# formatter that only rewrites files exits 0 and leaves a dirty tree.
if ! eval "$CMD" >/tmp/lintfix.log 2>&1; then
  echo "CHANGED=false"
  echo "FILES=0"
  echo "--- fixer output (tail) ---" >&2
  tail -n 40 /tmp/lintfix.log >&2
  exit 1
fi

CHANGED_FILES=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$CHANGED_FILES" -gt 0 ]; then
  echo "CHANGED=true"
else
  echo "CHANGED=false"
fi
echo "FILES=$CHANGED_FILES"
