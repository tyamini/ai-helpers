#!/usr/bin/env bash
# Poll cursor-agent tmux panes across one or more hosts and report, for each,
# its identity, working directory/branch, a first-pass state guess, and the
# captured pane tail (so the leader can summarize context + read a live
# question that was never flushed to disk).
#
# Host-agnostic: a host whose name matches the local `hostname` is polled via
# a local `tmux`; every other host is polled over `ssh`. The leader may run on
# ANY of the hosts (do not assume which one is local).
#
# Usage:
#   poll-agents.sh [host ...]          # defaults to: tyamini-dev tyamini-dev2
#   POLL_HOSTS="hostA hostB" poll-agents.sh
#
# Output: one block per cursor-agent pane, delimited by `===AGENT===`:
#   host:    <host>
#   target:  <session>:<window>.<pane>      # pass to tmux -t for capture/send
#   title:   <pane_title>
#   cwd:     <pane_current_path>
#   pid:     <pane_pid>
#   state:   waiting-question | working | waiting-conversation | unknown
#   ---capture---
#   <last ~40 non-empty lines of the pane>
#
# `state` is a heuristic hint only. The leader MUST confirm by reading capture.

set -u

HOSTS=("$@")
if [ "${#HOSTS[@]}" -eq 0 ]; then
  # shellcheck disable=SC2206
  HOSTS=(${POLL_HOSTS:-tyamini-dev tyamini-dev2})
fi

CAPTURE_LINES="${POLL_CAPTURE_LINES:-40}"

# Remote/local snippet. Enumerates panes whose foreground command is
# cursor-agent, then prints a delimited block per pane with a state guess and
# the captured tail. Runs identically under local bash and `ssh host bash -s`.
read -r -d '' REMOTE_SNIPPET <<'SNIP'
set -u
CAP_LINES="${1:-40}"
LEADER_PANE="${2:-}"     # exclude the leader's own pane when polling locally

command -v tmux >/dev/null 2>&1 || { echo "__NO_TMUX__"; exit 0; }
tmux list-sessions >/dev/null 2>&1 || { echo "__NO_TMUX_SERVER__"; exit 0; }

# Fields: target \t pid \t current_command \t current_path \t title
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}	#{pane_pid}	#{pane_current_command}	#{pane_current_path}	#{pane_title}' \
| while IFS=$'\t' read -r target pid cmd cwd title; do
    case "$cmd" in *cursor-agent*) : ;; *) continue ;; esac
    [ -n "$LEADER_PANE" ] && [ "$target" = "$LEADER_PANE" ] && continue

    # Context capture: includes scrollback, shown to the leader for summarizing.
    cap=$(tmux capture-pane -p -t "$target" -S -"$CAP_LINES" 2>/dev/null | grep -v '^[[:space:]]*$')
    # State capture: ONLY the currently visible screen. Classifying on scrollback
    # mis-fires (a stale, already-answered question box stays in history).
    vis=$(tmux capture-pane -p -t "$target" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n 15)

    state=unknown
    if printf '%s' "$vis" | grep -qE 'Esc to skip|↑/↓ option|Enter (next/)?submit|type to answer'; then
      state=waiting-question
    elif printf '%s' "$vis" | grep -qE 'esc to interrupt|ctrl\+c to stop|[[:space:]]Working([[:space:]]|$)|Waiting [0-9]'; then
      state=working
    else
      state=waiting-conversation
    fi

    echo "===AGENT==="
    echo "target:  $target"
    echo "title:   $title"
    echo "cwd:     $cwd"
    echo "pid:     $pid"
    echo "state:   $state"
    echo "---capture---"
    printf '%s\n' "$cap" | tail -n "$CAP_LINES"
done
SNIP

local_name="$(hostname -s 2>/dev/null || hostname)"

for host in "${HOSTS[@]}"; do
  is_local=0
  if [ "$host" = "$local_name" ] || [ "$host" = "$(hostname 2>/dev/null)" ] \
     || [ "$host" = "localhost" ] || [ "$host" = "127.0.0.1" ]; then
    is_local=1
  fi

  if [ "$is_local" -eq 1 ]; then
    out=$(bash -c "$REMOTE_SNIPPET" _ "$CAPTURE_LINES" "${TMUX_PANE:-}" 2>&1)
  else
    out=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" \
            "bash -s -- '$CAPTURE_LINES' ''" <<<"$REMOTE_SNIPPET" 2>&1)
    rc=$?
    if [ $rc -ne 0 ] && [ -z "$out" ]; then
      echo "===HOST_ERROR==="
      echo "host:    $host"
      echo "error:   ssh failed (rc=$rc)"
      continue
    fi
  fi

  # Tag every block from this host with the host name.
  printf '%s\n' "$out" | sed "s/^===AGENT===$/===AGENT===\nhost:    $host/"
done
