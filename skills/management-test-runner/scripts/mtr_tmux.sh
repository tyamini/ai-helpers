#!/usr/bin/env bash
# Resolve the tmux pane to drive the management test loop, and print its target
# ("<session>:<window>.<pane>") on stdout.
#
# REUSE an existing session whenever ANY session exists. Create a new session
# ONLY when there are zero sessions. This is deterministic on purpose so the
# loop never spawns a stray session next to an existing one.
#
# Selection order:
#   1. idle shell pane whose recent output shows a prior test/env run
#   2. any idle shell pane in an existing session
#   3. a brand-new window in the first existing session (sessions exist, all busy)
#   4. a brand-new session (only when no sessions exist at all)

set -uo pipefail

SHELLS='bash|zsh|fish|sh|dash'
NEW_SESSION_NAME="${MTR_TMUX_SESSION:-mtr}"
ACTIVITY='dtest |make .*test|run_mgmt_ipython|mtr_(env|apply|triage)|start_emu_sa_env'

mapfile -t SESSIONS < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)

# 4 (no sessions): create one.
if [ "${#SESSIONS[@]}" -eq 0 ]; then
    tmux new-session -d -s "$NEW_SESSION_NAME" >/dev/null 2>&1
    echo "${NEW_SESSION_NAME}:0.0"
    exit 0
fi

# Collect idle shell panes across all existing sessions.
idle=()
while read -r target cmd; do
    [ -z "$target" ] && continue
    [[ "$cmd" =~ ^($SHELLS)$ ]] && idle+=("$target")
done < <(tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}' 2>/dev/null)

# 1: idle pane that recently ran tests/env.
for t in "${idle[@]:-}"; do
    [ -z "$t" ] && continue
    if tmux capture-pane -p -S -200 -t "$t" 2>/dev/null | grep -qE "$ACTIVITY"; then
        echo "$t"; exit 0
    fi
done

# 2: any idle shell pane.
if [ "${#idle[@]}" -gt 0 ] && [ -n "${idle[0]:-}" ]; then
    echo "${idle[0]}"; exit 0
fi

# 3: sessions exist but no idle pane -> new window in the first existing session.
win=$(tmux new-window -t "${SESSIONS[0]}" -P -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null)
[ -n "$win" ] && { echo "$win"; exit 0; }

# Last-resort fallback.
tmux new-session -d -s "$NEW_SESSION_NAME" >/dev/null 2>&1
echo "${NEW_SESSION_NAME}:0.0"
