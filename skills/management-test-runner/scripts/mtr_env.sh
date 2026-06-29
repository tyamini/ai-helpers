#!/usr/bin/env bash
# Bring up the emulated-SA management env (emu_sa_* containers) from an image source.
# Pure env bring-up: no cli/orm generation here (mtr_apply.sh owns that).
#
# Usage:
#   mtr_env.sh <images-spec>
#     images-spec: cached | latest | pr | <jenkins-build-url>
#
# Emits a completion sentinel on stdout: __MTR_ENV_DONE__ rc=<n>
# Intended to be run inside a tmux pane, e.g.:
#   ( mtr_env.sh cached ) 2>&1 | tee <log>

set -uo pipefail

IMAGES="${1:?images-spec required: cached|latest|pr|<jenkins-url>}"

emit() { echo "__MTR_ENV_DONE__ rc=$1"; exit "$1"; }

# Retrieve GitHub token from environment variables or ~/.cursor/mcp.json.
get_github_token() {
    local token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
    if [ -n "$token" ]; then
        echo "$token"
        return 0
    fi
    token=$(python3 -c "
import json, os
try:
    with open(os.path.expanduser('~/.cursor/mcp.json')) as f:
        data = json.load(f)
    t = data.get('mcpServers',{}).get('dn-mcp-server',{}).get('headers',{}).get('X-GITHUB-PERSONAL-ACCESS-TOKEN','')
    if t: print(t)
except:
    pass" 2>/dev/null)
    if [ -n "$token" ]; then
        echo "$token"
        return 0
    fi
    # Fall back to the authenticated GitHub CLI (gh auth login / gh auth token).
    if command -v gh >/dev/null 2>&1; then
        token=$(gh auth token 2>/dev/null)
        if [ -n "$token" ]; then
            echo "$token"
            return 0
        fi
    fi
    return 1
}

# Resolve the latest PR Jenkins build URL that has the metadata.images artifact.
resolve_pr_url() {
    local branch token api auth pr_json pr head_sha
    branch=$(git branch --show-current 2>/dev/null)
    [ -z "$branch" ] && { echo "Error: no current branch" >&2; return 1; }
    token=$(get_github_token) || { echo "Error: no GitHub token (set GITHUB_TOKEN or run gh auth login)" >&2; return 1; }
    api="https://api.github.com/repos/drivenets/cheetah"
    auth="Authorization: token $token"
    pr_json=$(curl -s --max-time 15 -H "$auth" "${api}/pulls?head=drivenets:${branch}&state=all&per_page=1&sort=created&direction=desc" 2>/dev/null)
    pr=$(echo "$pr_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['number'] if d else '')" 2>/dev/null)
    [ -z "$pr" ] && { echo "Error: no PR for branch '$branch'" >&2; return 1; }
    head_sha=$(echo "$pr_json" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['head']['sha'])" 2>/dev/null)
    echo "Found PR #${pr} (HEAD ${head_sha:0:12})" >&2
    # Iterate Israel Jenkins builds reported in commit statuses (newest first).
    local builds
    builds=$(curl -s --max-time 15 -H "$auth" "${api}/commits/${head_sha}/statuses?per_page=100" 2>/dev/null | python3 -c "
import json,sys,re
seen=set()
for s in json.load(sys.stdin):
    m=re.match(r'https://(jenkins[0-9]*\.dev\.drivenets\.net)/job/drivenets/job/cheetah/job/PR-\d+/(\d+)/', s.get('target_url',''))
    if m and m.groups() not in seen:
        seen.add(m.groups()); print(m.group(1), m.group(2))
" 2>/dev/null)
    local server num url art code
    while read -r server num; do
        [ -z "$server" ] && continue
        url="https://${server}/job/drivenets/job/cheetah/job/PR-${pr}/${num}/"
        art="${url}artifact/metadata.images"
        code=$(curl -sk --head --max-time 10 -o /dev/null -w "%{http_code}" "$art" 2>/dev/null)
        [ "$code" = "200" ] && { echo "$url"; return 0; }
    done <<< "$builds"
    # Fallback: lastSuccessfulBuild / lastCompletedBuild on each server.
    local s a
    for s in jenkins.dev.drivenets.net jenkins3.dev.drivenets.net; do
        for a in lastSuccessfulBuild lastCompletedBuild; do
            url="https://${s}/job/drivenets/job/cheetah/job/PR-${pr}/${a}/"
            code=$(curl -sk --head --max-time 10 -o /dev/null -w "%{http_code}" "${url}artifact/metadata.images" 2>/dev/null)
            [ "$code" = "200" ] && { echo "$url"; return 0; }
        done
    done
    echo "Error: no PR build with artifacts for PR #${pr}" >&2
    return 1
}

# Use the image's own binaries/python: `--binaries-volume-type never` disables the
# default local dev overlay (host `lib/` -> /tmp/local_libs and host `/dn/python/*`).
# On a dev VM the default `auto` overlays branch host-source on top of the Jenkins
# image, which skews against the image's compiled `.so` (e.g. py_urpc.so missing a
# newly-renamed symbol, libevents bitset size) and crashes the device on boot
# (transaction_agent ImportError, node_manager exit 127). Image-only keeps the
# runtime self-consistent; compile/overlay only when local changes require it.
VOL="${MTR_BINARIES_VOLUME_TYPE:-never}"
case "$IMAGES" in
    cached) dtest start_emu_sa_env --images cached --binaries-volume-type "$VOL" ;;
    latest) dtest start_emu_sa_env --images latest --binaries-volume-type "$VOL" ;;
    pr)
        URL=$(resolve_pr_url) || emit 1
        echo "Using PR Jenkins URL: $URL" >&2
        dtest start_emu_sa_env --images-url "$URL" --binaries-volume-type "$VOL"
        ;;
    http*://*) dtest start_emu_sa_env --images-url "$IMAGES" --binaries-volume-type "$VOL" ;;
    *) echo "Error: bad images-spec '$IMAGES' (cached|latest|pr|<jenkins-url>)" >&2; emit 2 ;;
esac

emit $?
