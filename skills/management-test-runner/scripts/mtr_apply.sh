#!/usr/bin/env bash
# Apply the minimal action(s) for a set of triage tokens, then emit a sentinel.
# Touches <marker> FIRST so the next triage delta is measured from this apply's start.
#
# Usage:
#   mtr_apply.sh <marker-file> <images-spec> <token...>
#     images-spec: cached | latest | pr | <jenkins-url>   (used only by REDEPLOY)
#     token: BUILD_QUAGGA GENERATE_CLI BUILD_ORM RESTART_ROUTING_MANAGER RELOAD_IPYTHON REDEPLOY
#
# RELOAD_IPYTHON is a signal to the caller (prints __MTR_RELOAD_IPYTHON__); the
# skill owns exiting/relaunching the ipython container in its tmux pane.
# Emits a completion sentinel on stdout: __MTR_APPLY_DONE__ rc=<n>

set -uo pipefail

MARKER="${1:?marker-file required}"; shift
IMAGES="${1:?images-spec required}"; shift
TOKENS="$*"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Record apply start: edits during/after this run are caught on the next cycle.
touch "$MARKER"

emit() { echo "__MTR_APPLY_DONE__ rc=$1"; exit "$1"; }
has() { case " $TOKENS " in *" $1 "*) return 0;; *) return 1;; esac; }

# dbuild generate/build steps can wedge (rust step waiting on sccache / the
# remote build-cache) with the builder container idle and producing no output.
# A plain `timeout` on the dbuild client does NOT kill the underlying
# `docker run` builder, so instead we detect a stall by OUTPUT INACTIVITY,
# docker-stop the builder container(s) this step spawned, drop sccache, and
# retry the command once.
STALL_SECS="${MTR_STALL_SECS:-600}"   # no new output for this long => stalled

# Stop builder containers that appeared since <before-id-list>. Never touches
# the env's emu_sa_*/mgmt_* containers.
_stop_new_builders() {
    local before="$1" after new c name
    after=$(docker ps -q | sort)
    new=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))
    for c in $new; do
        name=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null | sed 's#^/##')
        case "$name" in emu_sa_*|mgmt_*) continue;; esac
        echo ">>> stopping wedged builder container ${name:-$c}"
        docker stop "$c" >/dev/null 2>&1 || true
    done
}

run_guarded() {
    local desc="$1"; shift
    echo ">>> $desc"
    local before log pid tpid now mt
    before=$(docker ps -q | sort)
    log=$(mktemp)
    # stdin from /dev/null so dbuild runs the builder non-interactively in bg.
    "$@" </dev/null >"$log" 2>&1 &
    pid=$!
    tail -n +1 -f "$log" --pid="$pid" 2>/dev/null &   # live-stream to the pane
    tpid=$!
    while kill -0 "$pid" 2>/dev/null; do
        sleep 15
        now=$(date +%s); mt=$(stat -c %Y "$log" 2>/dev/null || echo "$now")
        if [ $((now - mt)) -ge "$STALL_SECS" ]; then
            echo ">>> $desc stalled (no output for ${STALL_SECS}s); stopping builder + sccache, retrying once"
            _stop_new_builders "$before"
            sccache --stop-server 2>/dev/null || true
            kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
            kill "$tpid" 2>/dev/null; wait "$tpid" 2>/dev/null
            rm -f "$log"
            echo ">>> retry: $desc"
            "$@" </dev/null   # single synchronous retry, unguarded
            return $?
        fi
    done
    wait "$pid"; local rc=$?
    kill "$tpid" 2>/dev/null; wait "$tpid" 2>/dev/null
    rm -f "$log"
    return $rc
}

restart_routing_manager() {
    local re prog
    re=$(docker ps --format '{{.Names}}' | grep -E '_routing-engine_1$' | head -1)
    [ -z "$re" ] && { echo "Error: no *_routing-engine_1 container running" >&2; return 1; }
    echo ">>> Restarting routing_manager in $re"
    for ctl in supervisorctl ubervisorctl; do
        prog=$(docker exec "$re" "$ctl" status 2>/dev/null | awk '/routing_manager/{print $1; exit}')
        if [ -n "$prog" ]; then
            docker exec "$re" "$ctl" restart "$prog"
            return $?
        fi
    done
    echo "Error: routing_manager process not found via supervisorctl/ubervisorctl" >&2
    return 1
}

rc=0
has BUILD_QUAGGA            && { run_guarded "dbuild make quagga"            dbuild make quagga            || rc=$?; }
has GENERATE_CLI            && { run_guarded "dbuild make generate-dn-cli-api" dbuild make generate-dn-cli-api || rc=$?; }
has BUILD_ORM              && { run_guarded "dbuild make orm"              dbuild make orm               || rc=$?; }
has RESTART_ROUTING_MANAGER && { restart_routing_manager                    || rc=$?; }
has RELOAD_IPYTHON          && echo "__MTR_RELOAD_IPYTHON__"
# Pick the binaries-volume mode for the redeploy:
#   - compiled something (GENERATE_CLI/BUILD_ORM/BUILD_QUAGGA) => overlay host
#     artifacts so the local changes actually reach the device (`always`).
#   - pure image bring-up (no compile) => use the image's own binaries (`never`).
# An explicit MTR_BINARIES_VOLUME_TYPE always wins.
if has REDEPLOY; then
    if has GENERATE_CLI || has BUILD_ORM || has BUILD_QUAGGA; then vol=always; else vol=never; fi
    MTR_BINARIES_VOLUME_TYPE="${MTR_BINARIES_VOLUME_TYPE:-$vol}" bash "$HERE/mtr_env.sh" "$IMAGES" || rc=$?
fi

emit "$rc"
