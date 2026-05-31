#!/usr/bin/env bash
# Dual-detector watcher for iked-test-loop Stage 2c.
#
# Exits when EITHER the test_ike.sh sentinel is written to <log> OR an
# interactive debugger prompt (ipdb> / (Pdb)) appears as the last non-empty
# line of <pane>. Whichever fires first wins.
#
# IMPORTANT: never quits the debugger — pdb's live state is a high-value
# investigation artifact. The handler owns dismissal.
#
# Usage:
#   bash watcher.sh <session>.<idx> <abs path to runner.log>
#
# Exit codes (consumed by the loop's AwaitShell pattern match):
#   0 — sentinel fired (output line: __IKED_RUN_DONE__ rc=<n>)
#   2 — debugger fired (output line: IPDB_ACTIVE: <last-pane-line>)
#
# Requires bash 4.3+ for `wait -n` (Ubuntu default).

set -u

PANE="${1:?pane required, e.g. mysession.0}"
LOG="${2:?abs path to runner.log required}"

# Watcher A — event-driven sentinel. tail -F follows the file even before
# it exists; grep -m1 exits on first match and SIGPIPEs tail.
( tail -F "$LOG" 2>/dev/null | grep -m1 "__IKED_RUN_DONE__ rc=" ) &
WA=$!

# Watcher B — polled (5s) detection of an interactive debugger prompt as
# the LAST non-empty line in the pane. pdb prompts have no trailing newline
# and may not flush through `tee`, so we read the pane directly. We DO NOT
# send keys to dismiss — the handler owns the live session.
#
# `tmux capture-pane -p` strips trailing whitespace from each line, so the
# captured pdb prompt is literally `ipdb>` / `(Pdb)` with NO trailing space —
# case patterns must match without the space. The trailing `*` allows
# optional residual chars on the line.
(
  while true; do
    sleep 5
    last=$(tmux capture-pane -t "$PANE" -p -S -3 2>/dev/null \
           | grep -v "^$" | tail -1)
    case "$last" in
      "ipdb>"*|"(Pdb)"*)
        echo "IPDB_ACTIVE: $last"
        exit 2
        ;;
    esac
  done
) &
WB=$!

wait -n $WA $WB
RC=$?
kill $WA $WB 2>/dev/null
wait 2>/dev/null
exit $RC
