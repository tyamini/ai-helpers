#!/bin/bash
# sandbox-worktree.sh — Create a git worktree with sandbox isolation for parallel dbuild make quagga.
#
# Usage:
#   bash .ai/skills/common/using-git-worktree/scripts/sandbox-worktree.sh [--allow-existing] <branch> [path]
#
# Arguments:
#   branch  — The branch (or commit) to check out in the worktree.
#   path    — Where to create the worktree (default: ~/worktrees/<branch>).
#             Ignored when --allow-existing adopts a pre-existing worktree.
#
# Flags:
#   --allow-existing  Reuse a linked worktree for <branch> if one already exists
#                     (skip 'git worktree add'). Without this flag, the script
#                     errors out when a worktree for the branch already exists.
#
# What it does:
#   1. Creates a git worktree at the specified path (or reuses an existing one).
#   2. Creates /tmp/dbuild-sandbox/<worktree-name>/debs for this worktree's build output.
#   3. Generates a sandbox-env.sh in the worktree root that:
#      - Overrides ROOT/prod_root so dbuild targets this worktree
#      - Sets DN_QUAGGA_DEBUG_PATH so builds and tests read/write their own debs dir
#
# After setup, cd into the worktree and:
#   source sandbox-env.sh
#   dbuild make quagga   # builds in isolation from the main repo
#
set -euo pipefail

usage() {
    echo "Usage: $0 [--allow-existing] <branch> [path]"
    echo ""
    echo "  branch              Branch or commit to check out"
    echo "  path                Worktree location (default: ~/worktrees/<branch>)"
    echo "                      Ignored when --allow-existing adopts a pre-existing worktree."
    echo ""
    echo "  --allow-existing    If a linked worktree for <branch> already exists,"
    echo "                      reuse it (skip 'git worktree add'). Without this flag,"
    echo "                      the script errors out when a worktree already exists."
    exit 1
}

ALLOW_EXISTING=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --allow-existing) ALLOW_EXISTING=1 ;;
        -h|--help)        usage ;;
        *)                ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]}"

if [[ $# -lt 1 ]]; then
    usage
fi

BRANCH="$1"
BRANCH_SAFE="${BRANCH//\//_}"

git rev-parse --show-toplevel >/dev/null 2>&1 || {
    echo "Error: Not inside a git repository." >&2
    exit 1
}

# Discover any worktree that already has this branch checked out.
EXISTING_WT_PATH="$(
    git worktree list --porcelain | awk -v want="refs/heads/${BRANCH}" '
        /^worktree / { path=$2 }
        /^branch /   { if ($2 == want) { print path; exit } }
    '
)"

# Use pwd -P so the path matches the canonical form 'git worktree list --porcelain' prints
# (resolves symlinks, e.g. ~/cheetah -> ~/workspace/cheetah).
MAIN_WT_PATH="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd -P)"
EXISTING_IS_MAIN=0
if [ -n "$EXISTING_WT_PATH" ] && [ "$EXISTING_WT_PATH" = "$MAIN_WT_PATH" ]; then
    EXISTING_IS_MAIN=1
fi

if [ -n "$EXISTING_WT_PATH" ]; then
    if [ "$EXISTING_IS_MAIN" = 1 ]; then
        echo "Error: branch '${BRANCH}' is checked out in the main worktree (${EXISTING_WT_PATH})." >&2
        echo "Sandboxing requires a separate linked worktree. Check out the branch in a different worktree first." >&2
        exit 1
    fi
    if [ "$ALLOW_EXISTING" = 0 ]; then
        echo "Error: a worktree for branch '${BRANCH}' already exists at ${EXISTING_WT_PATH}." >&2
        echo "  Re-run with --allow-existing to reuse it (the [path] argument will be ignored), or" >&2
        echo "  remove it first: git worktree remove ${EXISTING_WT_PATH}" >&2
        exit 1
    fi
    if [ -n "${2:-}" ] && [ "$2" != "$EXISTING_WT_PATH" ]; then
        echo "Note: --allow-existing — ignoring path '$2', using existing worktree at ${EXISTING_WT_PATH}." >&2
    fi
    WORKTREE_PATH="$EXISTING_WT_PATH"
    SKIP_WORKTREE_ADD=1
else
    WORKTREE_PATH="${2:-${HOME}/worktrees/${BRANCH_SAFE}}"
    SKIP_WORKTREE_ADD=0
fi

SANDBOX_NAME="$(basename "${WORKTREE_PATH}")"
SANDBOX_BASE="/tmp/dbuild-sandbox/${SANDBOX_NAME}"

if [ "$SKIP_WORKTREE_ADD" = 0 ]; then
    echo "==> Creating worktree at ${WORKTREE_PATH} for branch '${BRANCH}'..."
    mkdir -p "$(dirname "${WORKTREE_PATH}")"
    if [[ -d "${WORKTREE_PATH}" ]]; then
        echo "Error: Path ${WORKTREE_PATH} already exists." >&2
        echo "  To remove: git worktree remove ${WORKTREE_PATH}" >&2
        exit 1
    fi
    git worktree add "${WORKTREE_PATH}" "${BRANCH}"
else
    echo "==> Reusing existing worktree at ${WORKTREE_PATH} for branch '${BRANCH}'..."
fi

echo "==> Creating sandbox debs directory at ${SANDBOX_BASE}/debs..."
mkdir -p "${SANDBOX_BASE}/debs"

# Warn if the branch in the worktree predates the sandbox infrastructure.
# Older branches don't have dbuild/tests that honour DN_QUAGGA_DEBUG_PATH,
# so quagga debs land in /tmp/debs and collide with the main worktree.
if [ ! -d "${WORKTREE_PATH}/.ai/skills/common/using-git-worktree" ]; then
    echo "" >&2
    echo "WARNING: branch '${BRANCH}' lacks .ai/skills/common/using-git-worktree." >&2
    echo "  This is an OLD VERSION: dbuild and routing tests on this branch do" >&2
    echo "  not honour DN_QUAGGA_DEBUG_PATH, so quagga debs will read/write" >&2
    echo "  /tmp/debs directly and SHARE it with the main worktree." >&2
    echo "  'dbuild make quagga' on either side can clobber the .deb that a" >&2
    echo "  routing test on the other side is consuming." >&2
    echo "" >&2
fi

SANDBOX_ENV="${WORKTREE_PATH}/sandbox-env.sh"
cat > "${SANDBOX_ENV}" <<ENVEOF
#!/bin/bash
# Auto-generated by sandbox-worktree.sh — source this before running dbuild or tests.
# Sandbox base: ${SANDBOX_BASE}

# Override ROOT so dbuild builds this worktree's code, not the main repo's.
export ROOT="${WORKTREE_PATH}"
export prod_root="\${ROOT}"

# Per-worktree debs dir: builds write here, tests read from here.
# dbuild.yml and the jinja2 docker-compose templates fall back to /tmp/debs
# when this is unset, so the main worktree keeps its shared-path behaviour.
export DN_QUAGGA_DEBUG_PATH="${SANDBOX_BASE}/debs"

echo "Sandbox active: ${SANDBOX_BASE}"
echo "ROOT=${WORKTREE_PATH}"
echo "DN_QUAGGA_DEBUG_PATH=${SANDBOX_BASE}/debs"
ENVEOF
chmod +x "${SANDBOX_ENV}"

echo ""
echo "==> Worktree ready!"
echo ""
echo "  cd ${WORKTREE_PATH}"
echo "  source sandbox-env.sh"
echo "  dbuild make quagga                                 # build"
echo "  ls \"\${DN_QUAGGA_DEBUG_PATH}\"/*.deb                 # where the .deb lands"
echo ""
echo "To remove later:"
echo "  git worktree remove ${WORKTREE_PATH}"
echo "  rm -rf ${SANDBOX_BASE}"
