#!/usr/bin/env bash
# Sentinel watcher for execution-loop Stage 3 (mirrors iked-test-loop/watcher.sh).
#
# Exits 0 as soon as the per-plan agent's completion sentinel
# (`__EXEC_DONE__ rc=<n>`) is written to <log>. Run with block_until_ms=0 and
# await via AwaitShell on the regex `__EXEC_DONE__ rc=`. Run, never read.
#
# Usage: bash watch.sh <abs path to pane.log>
set -u
LOG="${1:?abs path to pane.log required}"
# tail -F follows even before the file grows; grep -m1 exits on first match.
tail -F "$LOG" 2>/dev/null | grep -m1 "__EXEC_DONE__ rc="
