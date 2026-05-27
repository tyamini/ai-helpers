#!/usr/bin/env bash
#
# collect-evidence.sh — mechanical RCA evidence collection for a failed iked E2E test.
#
# Runs against the still-live e2e_* containers left up by `test_ike.sh` after a
# pytest failure (SHOULD_DESTRUCT=0). Pulls `show ike *` outputs from every
# router container and tails the suite-relevant trace files.
#
# Usage:
#   collect-evidence.sh <run_dir> <suite>
#
# Where:
#   <run_dir>   absolute path to ~/.iked-runs/<run-id>/items/<seq>-<slug>/
#   <suite>     one of: routing | cdnos | cli_tests
#
# Produces files under <run_dir>/rca/:
#   shows/<container>.show_ike_<sa|tunnel|swan-config|interface>
#   traces/<container>.<trace_file>.tail
#   containers.txt
#
# Exits 0 always (best-effort gather). The agent layer interprets missing files.

set -u

RUN_DIR="${1:?run_dir required}"
SUITE="${2:?suite required (routing|cdnos|cli_tests)}"

RCA_DIR="${RUN_DIR}/rca"
SHOWS_DIR="${RCA_DIR}/shows"
TRACES_DIR="${RCA_DIR}/traces"
mkdir -p "${SHOWS_DIR}" "${TRACES_DIR}"

# --- Trace file selection per suite (per iked-e2e-testing.mdc) --------------
case "${SUITE}" in
    routing)
        TRACE_FILES=(iked_traces rib-manager_traces fibmgrd_traces)
        ;;
    cdnos|cli_tests)
        TRACE_FILES=(iked_traces rib-manager_traces fibmgrd_traces routing_manager cli)
        ;;
    *)
        echo "ERROR: unknown suite '${SUITE}'" >&2
        exit 0
        ;;
esac

# --- Discover live e2e_* containers -----------------------------------------
mapfile -t CONTAINERS < <(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^e2e_[A-Za-z0-9_]+$' || true)
printf '%s\n' "${CONTAINERS[@]}" > "${RCA_DIR}/containers.txt"

if [[ ${#CONTAINERS[@]} -eq 0 ]]; then
    echo "NOTE: no live e2e_* containers found — likely failed before container bring-up." \
        > "${RCA_DIR}/no_live_containers.txt"
    exit 0
fi

# --- show ike * per container ----------------------------------------------
SHOW_CMDS=(
    "show ike sa:show_ike_sa"
    "show ike tunnel:show_ike_tunnel"
    "show ike swan-config:show_ike_swan-config"
    "show ike interface:show_ike_interface"
)

for C in "${CONTAINERS[@]}"; do
    for entry in "${SHOW_CMDS[@]}"; do
        cli="${entry%%:*}"
        slug="${entry##*:}"
        out="${SHOWS_DIR}/${C}.${slug}"
        {
            echo "# container=${C}  cli=${cli}  ts=$(date -Iseconds)"
            docker exec "${C}" vtysh -c "${cli}" 2>&1
        } > "${out}" || true
    done
done

# --- Trace tails per container (last 200 lines, per iked-e2e rule) ----------
TRACE_LINES="${IKED_RCA_TRACE_LINES:-200}"

for C in "${CONTAINERS[@]}"; do
    for TF in "${TRACE_FILES[@]}"; do
        out="${TRACES_DIR}/${C}.${TF}.tail"
        {
            echo "# container=${C}  trace=${TF}  lines=${TRACE_LINES}  ts=$(date -Iseconds)"
            docker exec "${C}" sh -c "tail -${TRACE_LINES} /core/traces/${C}/${TF} 2>&1" || true
        } > "${out}" || true
    done
done

# --- Done -------------------------------------------------------------------
echo "OK: evidence collected under ${RCA_DIR}"
exit 0
