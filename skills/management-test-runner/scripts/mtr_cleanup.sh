#!/usr/bin/env bash
# Cleanup leftovers from PRIOR runs of this skill before a fresh bring-up.
# Removes only runner/build leftovers; NEVER touches the emu_sa_* env containers.
#
# Targets:
#   - hung skill processes: mtr_apply.sh / mtr_env.sh / watcher.sh
#   - a stuck start_emu_sa_env pytest
#   - orphaned dbuild builder containers running generate-dn-cli-api / yang_orm
#     (the host-side `make` wrapper plus the builder container), which otherwise
#     hold the tmux pane and block the next run.
#   - stale generated YANG artifacts (gitignored) left over from a prior branch/
#     build. A leftover yang-library-data-gen.json that references a module the
#     current branch no longer ships makes generate-dn-cli-api crash with a
#     yangson ModuleNotFound, and there is no fallback because generate-dn-cli-api
#     consumes the gen file without regenerating it. Removing the gen set forces a
#     clean regen from the current branch's tracked yang sources on bring-up.
#
# Usage:
#   mtr_cleanup.sh
# Emits a completion sentinel on stdout: __MTR_CLEANUP_DONE__ rc=<n>

set -uo pipefail

emit() { echo "__MTR_CLEANUP_DONE__ rc=$1"; exit "$1"; }

echo ">>> mtr cleanup: killing stale skill processes (if any)"
for pat in 'scripts/mtr_apply\.sh' 'scripts/mtr_env\.sh' 'scripts/watcher\.sh' \
           'make .*generate-dn-cli-api' 'make .*yang_orm' 'start_emu_sa_env'; do
    if pkill -f "$pat" 2>/dev/null; then echo "  killed: $pat"; fi
done

echo ">>> mtr cleanup: stopping orphaned dbuild builder containers (not emu_sa_*)"
docker ps --no-trunc --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Command}}' 2>/dev/null \
  | grep -E '\|.*registry\.[^|]*/builder' \
  | grep -Ev '\|emu_sa_' \
  | grep -E 'generate-dn-cli-api|yang_orm' \
  | cut -d'|' -f1 \
  | while read -r id; do
        [ -n "$id" ] || continue
        if docker kill "$id" >/dev/null 2>&1; then echo "  killed container $id"; fi
    done

echo ">>> mtr cleanup: removing stale generated YANG artifacts (regenerated on bring-up)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo /home/dn/cheetah)"
YANGS_DIR="$ROOT/prod/dnos_monolith/yangs"
# All of these are generated build outputs (yang-library-data-gen.json + the
# generated cg/ tree + its lock). Safety gate: only delete a path that git tracks
# NOTHING at (`git ls-files` empty), so a tracked file is never removed -- this is
# uniform for files and dirs and is robust to the cg/ ignore pattern only matching
# `cg/*` (the dir itself is not reported by check-ignore). dm.py regenerates the
# gen file from the current branch's tracked yang sources when it is missing.
for rel in yang-library-data-gen.json cg cg_yangs_gen.lock; do
    p="$YANGS_DIR/$rel"
    [ -e "$p" ] || continue
    if [ -z "$(git -C "$ROOT" ls-files -- "$p" 2>/dev/null)" ]; then
        rm -rf "$p" && echo "  removed stale: prod/dnos_monolith/yangs/$rel"
    else
        echo "  skip (git-tracked): prod/dnos_monolith/yangs/$rel" >&2
    fi
done

n=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^emu_sa_' || true)
echo ">>> mtr cleanup: emu_sa_* env left intact (${n:-0} containers)"

emit 0
