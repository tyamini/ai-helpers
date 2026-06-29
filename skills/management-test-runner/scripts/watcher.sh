#!/usr/bin/env bash
# Sentinel watcher: returns ~1s after the sentinel line is written to <log>.
# Run via the Shell tool with block_until_ms=0, then AwaitShell on the regex.
#
# Usage:
#   watcher.sh <abs-log-path> [<sentinel-regex>]
# Default regex matches the scripts' sentinels: __MTR_(ENV|APPLY)_DONE__ rc=<n>
#
# tail -F follows the file even before it exists; grep -m1 exits on first match.

set -u
LOG="${1:?abs log path required}"
REGEX="${2:-__MTR_(ENV|APPLY)_DONE__ rc=}"
tail -F "$LOG" 2>/dev/null | grep -m1 -E "$REGEX"
