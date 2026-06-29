#!/usr/bin/env bash
# Triage source edits SINCE the last apply into action tokens.
# Looks ONLY at files newer than <marker> (delta), never the cumulative git diff.
#
# Usage:
#   mtr_triage.sh <marker-file> [<repo-root>]
#
# Prints action tokens (one per line, canonical order). No side effects.
# Mapping (source of truth: references/triage-map.md):
#   prod/dnos_monolith/yangs/**        -> GENERATE_CLI BUILD_ORM REDEPLOY
#   prod/dnos_monolith/autogen_cli/**  -> GENERATE_CLI RELOAD_IPYTHON
#   src/py_packages/routing_manager/** -> RESTART_ROUTING_MANAGER
#   services/control/**                -> BUILD_QUAGGA REDEPLOY
# REDEPLOY supersedes RELOAD_IPYTHON and RESTART_ROUTING_MANAGER.

set -uo pipefail

MARKER="${1:?marker-file required}"
ROOT="${2:-$(git rev-parse --show-toplevel 2>/dev/null || echo /home/dn/cheetah)}"

# A root is "touched" if it has any file newer than the marker.
touched() {
    local d="$ROOT/$1"
    [ -d "$d" ] || return 1
    [ -n "$(find "$d" -type f -newer "$MARKER" -print -quit 2>/dev/null)" ]
}

declare -A T=()
touched prod/dnos_monolith/yangs        && { T[GENERATE_CLI]=1; T[BUILD_ORM]=1; T[REDEPLOY]=1; }
touched prod/dnos_monolith/autogen_cli  && { T[GENERATE_CLI]=1; T[RELOAD_IPYTHON]=1; }
touched src/py_packages/routing_manager && { T[RESTART_ROUTING_MANAGER]=1; }
touched services/control                && { T[BUILD_QUAGGA]=1; T[REDEPLOY]=1; }

# REDEPLOY covers in-place reload/restart.
if [ -n "${T[REDEPLOY]:-}" ]; then
    unset 'T[RELOAD_IPYTHON]' 'T[RESTART_ROUTING_MANAGER]'
fi

for tok in BUILD_QUAGGA GENERATE_CLI BUILD_ORM RESTART_ROUTING_MANAGER RELOAD_IPYTHON REDEPLOY; do
    [ -n "${T[$tok]:-}" ] && echo "$tok"
done
exit 0
