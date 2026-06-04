#!/bin/bash
# check-test-isolation.sh — Check whether a routing test's compose file is
# worktree-isolation-aware before running it in a sandbox worktree.
#
# Usage:
#   bash .ai/skills/common/using-git-worktree/scripts/check-test-isolation.sh <test_file>
#
# Exit codes:
#   0 — safe to run (yml/jinja2 references DN_QUAGGA_DEBUG_PATH, or no worktree deb exists)
#   1 — not isolation-aware; user must choose how to proceed before running the test
#   2 — usage / environment error
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <test_file>" >&2
    exit 2
fi

TEST_FILE="$1"

if [[ ! -f "$TEST_FILE" ]]; then
    echo "Error: test file not found: ${TEST_FILE}" >&2
    exit 2
fi

# Resolve repo root from the test file location.
REPO_ROOT="$(git -C "$(dirname "$TEST_FILE")" rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: not inside a git repository." >&2
    exit 2
}

# Find a string literal pointing to e2e/config/ in the test file.
YML_REF="$(grep -oE '"[^"]*e2e/config/[^"]+"' "$TEST_FILE" 2>/dev/null | tr -d '"' | head -1 || true)"

if [[ -z "$YML_REF" ]]; then
    exit 0
fi

# Extract the bare name from docker-compose-{name}.yml.
YML_BASENAME="$(basename "$YML_REF")"
NAME="${YML_BASENAME#docker-compose-}"
NAME="${NAME%.yml}"

# Check if a worktree-specific quagga deb exists.
if [[ -z "${DN_QUAGGA_DEBUG_PATH:-}" ]] || ! ls "${DN_QUAGGA_DEBUG_PATH}"/*.deb >/dev/null 2>&1; then
    exit 0
fi

# Check whether the yml or its jinja2 source references DN_QUAGGA_DEBUG_PATH.
YML_FULL="${REPO_ROOT}/src/tests/${YML_REF}"
NAME_UNDERSCORED="${NAME//-/_}"
JINJA2="${REPO_ROOT}/prod/dnos_monolith/docker_templates/e2e_${NAME_UNDERSCORED}/e2e_${NAME_UNDERSCORED}-docker-compose.jinja2"

if grep -q "DN_QUAGGA_DEBUG_PATH" "$YML_FULL" 2>/dev/null; then
    exit 0
fi

if grep -q "DN_QUAGGA_DEBUG_PATH" "$JINJA2" 2>/dev/null; then
    exit 0
fi

# Not isolation-aware — emit structured warning.
if [[ -f "$JINJA2" ]]; then
    FIX_TARGET="${JINJA2#${REPO_ROOT}/}"
else
    FIX_TARGET="${YML_FULL#${REPO_ROOT}/}"
fi

echo "ISOLATION_WARNING"
echo "COMPOSE: ${NAME}"
echo "WORKTREE_DEB: ${DN_QUAGGA_DEBUG_PATH}"
echo "OPTION_1_CMD: cp \"${DN_QUAGGA_DEBUG_PATH}\"/*.deb /tmp/debs/"
echo "FIX_TARGET: ${FIX_TARGET}"

exit 1
