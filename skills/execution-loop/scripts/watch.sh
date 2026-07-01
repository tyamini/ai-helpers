#!/usr/bin/env bash
# Robust completion watcher for execution-loop Stage 3.
#
# Re-wakes the executor as soon as the per-plan agent is done by ANY reliable
# signal — never only the process-exit sentinel, because a headless
# `cursor-agent -p` can finish its turn (emit its final message) WITHOUT the
# process ever exiting, which used to strand the executor indefinitely:
#   1. real completion sentinel   (^__EXEC_DONE__ rc=<n>)        normal: process exited
#   2. terminal stream-json result ("type":"result")            turn ended even if the
#                                                               process later hangs on exit
#   3. agent process gone          (pidfile PID not alive)       died without a sentinel
#   4. log idle > IDLE seconds     (no growth)                   hung-without-result backstop
#
# Prints a `__EXEC_DONE__ rc=<n|reason>` line and exits 0; the background shell's
# completion notification re-wakes the executor, which then collects by evidence
# (git + loop_report) — a slightly-early wake is safe. Run with block_until_ms=0
# (background). Run, never read.
#
# Usage: bash watch.sh <abs path to pane.log> [pidfile] [idle_secs]
set -u
LOG="${1:?abs path to pane.log required}"
PIDFILE="${2:-}"
IDLE="${3:-1200}"   # 20 min hung-without-output backstop

done_now() { echo "__EXEC_DONE__ rc=${1}"; exit 0; }

[ -e "$LOG" ] || : > "$LOG" 2>/dev/null || true

while :; do
  # 1. real sentinel — anchored to line start so sentinels embedded inside
  #    stream-json tool output (captures of other panes) never false-match.
  if grep -aqE '^__EXEC_DONE__ rc=' "$LOG" 2>/dev/null; then
    rc=$(grep -aoE '^__EXEC_DONE__ rc=[^[:space:]]+' "$LOG" | tail -1 | sed 's/^__EXEC_DONE__ rc=//')
    done_now "${rc:-0}"
  fi
  # 2. terminal result event — real (unescaped) top-level event only; escaped
  #    copies inside captured tool output (\"type\":\"result\") do not match.
  grep -aq '"type":"result"' "$LOG" 2>/dev/null && done_now "result-event"
  # 3. agent process gone without ever writing a sentinel.
  if [ -n "$PIDFILE" ] && [ -s "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then done_now "proc-exit"; fi
  fi
  # 4. idle backstop — the process is alive but produced no output for IDLE
  #    seconds (the exact hang that stranded the executor for ~13h).
  now=$(date +%s)
  mtime=$(stat -c %Y "$LOG" 2>/dev/null || echo "$now")
  [ $((now - mtime)) -ge "$IDLE" ] && done_now "idle-timeout"
  sleep 5
done
