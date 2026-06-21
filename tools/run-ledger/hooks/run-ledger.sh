#!/usr/bin/env bash
# run-ledger Cursor hook entrypoint (dumb on purpose).
#
# Cursor passes the hook payload as JSON on stdin. We forward it to the Python
# client's `hook` subcommand, which extracts fields and decides keep-vs-drop
# (all policy lives in one place). $1 is the Cursor event name, supplied by the
# hooks.json registration (so we don't depend on an undocumented stdin field).
#
# Fail-open: any error here must never block the agent, so we always exit 0.

CURSOR_EVENT="${1:-}"
CLIENT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../client" && pwd)/run_ledger.py"

python3 "$CLIENT" hook --cursor-event "$CURSOR_EVENT" 2>/dev/null || echo '{}'
exit 0
