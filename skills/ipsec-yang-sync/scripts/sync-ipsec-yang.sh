#!/usr/bin/env bash
# Sync ipsecYang/<base> from a feature branch by replaying the IPsec-relevant
# files that changed in the input branch vs its parent base.
#
# Runs in a sibling worktree at <repo>/../cheetah-ipsec-yang-<base>, so the
# user's main checkout is never touched. The target branch accumulates one
# commit per run (no force-push).
#
# Usage:
#   sync-ipsec-yang.sh <input-branch> [--base <base-branch>] [--no-push]
#
# Examples:
#   sync-ipsec-yang.sh feature/v262_routing_ike
#   sync-ipsec-yang.sh proposal/infra/feature/v262_routing_ike --base dev_v26_2
#   sync-ipsec-yang.sh feature/v262_routing_ike --no-push

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { printf "${BLUE}[i]${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
err()   { printf "${RED}[x]${NC} %s\n" "$*" >&2; }
die()   { err "$*"; exit 1; }

INPUT=""; BASE=""; DO_PUSH=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --no-push) DO_PUSH=0; shift ;;
        -h|--help)
            sed -n '/^# Sync/,/^# *$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        -*) die "Unknown flag: $1" ;;
        *)
            [[ -z "$INPUT" ]] || die "Unexpected positional arg: $1"
            INPUT="$1"; shift ;;
    esac
done
[[ -n "$INPUT" ]] || die "Missing required <input-branch>. See --help."

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "Not inside a git repo."
info "Repo root: $REPO_ROOT"

info "Fetching origin (prune)..."
git -C "$REPO_ROOT" fetch --prune origin || die "git fetch failed."

# ---- resolve input ----
if git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/remotes/origin/$INPUT" >/dev/null; then
    INPUT_REF="origin/$INPUT"
elif git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/heads/$INPUT" >/dev/null; then
    INPUT_REF="$INPUT"
else
    die "Input branch not found: $INPUT (neither origin nor local)."
fi
INPUT_SHA="$(git -C "$REPO_ROOT" rev-parse "$INPUT_REF")"
ok "Input branch: $INPUT_REF ($INPUT_SHA)"

# ---- resolve / detect base ----
detect_base() {
    local name="$INPUT" cand
    local -a out=() seen=()
    while [[ "$name" =~ v([0-9]+)[._]?([0-9]+) ]]; do
        cand="dev_v${BASH_REMATCH[1]}_${BASH_REMATCH[2]}"
        local dup=0
        for s in "${seen[@]+"${seen[@]}"}"; do
            [[ "$s" == "$cand" ]] && { dup=1; break; }
        done
        if [[ $dup -eq 0 ]]; then
            seen+=("$cand")
            if git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/remotes/origin/$cand" >/dev/null; then
                out+=("$cand")
            fi
        fi
        name="${name#*"${BASH_REMATCH[0]}"}"
    done
    printf '%s\n' "${out[@]+"${out[@]}"}"
}

if [[ -z "$BASE" ]]; then
    info "Auto-detecting base branch from input name..."
    mapfile -t CANDS < <(detect_base)
    if [[ ${#CANDS[@]} -eq 0 ]]; then
        die "Could not infer base from '$INPUT'. Pass --base <dev_vXX_Y>."
    elif [[ ${#CANDS[@]} -gt 1 ]]; then
        die "Multiple base candidates (${CANDS[*]}). Pass --base to disambiguate."
    fi
    BASE="${CANDS[0]}"
fi
git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/remotes/origin/$BASE" >/dev/null \
    || die "Base branch not found on origin: $BASE"
BASE_REF="origin/$BASE"
BASE_SHA="$(git -C "$REPO_ROOT" rev-parse "$BASE_REF")"
ok "Base branch: $BASE_REF ($BASE_SHA)"

TARGET="ipsecYang/$BASE"
WT_PARENT="$(cd "$REPO_ROOT/.." && pwd)"
WT_DIR="$WT_PARENT/cheetah-ipsec-yang-$BASE"
ok "Target branch: $TARGET"
info "Worktree:      $WT_DIR"

TARGET_EXISTS=0
git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/remotes/origin/$TARGET" >/dev/null \
    && TARGET_EXISTS=1

# ---- prepare worktree ----
git -C "$REPO_ROOT" worktree prune

if [[ -e "$WT_DIR/.git" ]]; then
    info "Reusing existing worktree."
else
    if [[ -e "$WT_DIR" ]]; then
        warn "Removing stale dir at $WT_DIR (not a worktree)."
        rm -rf "$WT_DIR"
    fi
    # Drop any stale local target branch outside the worktree (e.g. leftover
    # from a previous version of this script).
    if git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/heads/$TARGET" >/dev/null; then
        warn "Deleting stale local branch $TARGET."
        git -C "$REPO_ROOT" branch -D "$TARGET" >/dev/null || die "branch -D failed."
    fi
    local_start="$BASE_REF"
    [[ $TARGET_EXISTS -eq 1 ]] && local_start="origin/$TARGET"
    info "Creating worktree from $local_start..."
    git -C "$REPO_ROOT" worktree add -B "$TARGET" "$WT_DIR" "$local_start" >/dev/null \
        || die "git worktree add failed."
fi

# Pull target to latest (no-op when it didn't exist on origin).
if [[ $TARGET_EXISTS -eq 1 ]]; then
    info "Resetting worktree to origin/$TARGET."
    git -C "$WT_DIR" reset --hard "origin/$TARGET" >/dev/null \
        || die "reset --hard failed."
fi

# ---- compute filtered diff ----
info "Computing changes ${BASE_REF}...${INPUT_REF}..."
path_matches() {
    case "$1" in
        prod/dnos_monolith/yangs/*.yang) return 0 ;;
        prod/dnos_monolith/autogen_cli/*) [[ "$1" == *.yaml ]] && return 0 ;;
        prod/dnos_monolith/dnos_cli/*)    [[ "$1" == *.rst  ]] && return 0 ;;
    esac
    return 1
}

YANG_COUNT=0; YAML_COUNT=0; RST_COUNT=0
declare -a ADDS_MODS=() DELS=() RENAMES=()

bump_count() {
    case "$1" in
        *.yang) YANG_COUNT=$((YANG_COUNT+1)) ;;
        *.yaml) YAML_COUNT=$((YAML_COUNT+1)) ;;
        *.rst)  RST_COUNT=$((RST_COUNT+1))   ;;
    esac
}

while IFS= read -r -d '' STATUS && IFS= read -r -d '' P1; do
    case "$STATUS" in
        R*|C*)
            IFS= read -r -d '' P2
            if path_matches "$P2"; then
                if path_matches "$P1"; then
                    RENAMES+=("$P1"$'\t'"$P2")
                else
                    ADDS_MODS+=("$P2")
                fi
                bump_count "$P2"
            elif path_matches "$P1"; then
                DELS+=("$P1")
            fi
            ;;
        A|M|T)
            if path_matches "$P1"; then
                ADDS_MODS+=("$P1")
                bump_count "$P1"
            fi
            ;;
        D)
            path_matches "$P1" && DELS+=("$P1")
            ;;
        *) warn "Unhandled diff status '$STATUS' for $P1 — skipping." ;;
    esac
done < <(git -C "$REPO_ROOT" diff --name-status -z "${BASE_REF}...${INPUT_REF}")

SPEC_DIR=".ai/spec/planned/Services/transport/ipsec"
SPEC_EXISTS=0
git -C "$REPO_ROOT" cat-file -e "$INPUT_REF:$SPEC_DIR" 2>/dev/null && SPEC_EXISTS=1

info "Filtered change set:"
echo "    YANG    (changed): $YANG_COUNT"
echo "    autogen_cli YAML : $YAML_COUNT"
echo "    dnos_cli RST     : $RST_COUNT"
echo "    deletions        : ${#DELS[@]}"
echo "    renames          : ${#RENAMES[@]}"
echo "    spec dir snapshot: $([[ $SPEC_EXISTS -eq 1 ]] && echo yes || echo "no (not in input branch)")"

# ---- apply changes ----
for p in ${ADDS_MODS[@]+"${ADDS_MODS[@]}"}; do
    git -C "$WT_DIR" checkout "$INPUT_REF" -- "$p" 2>/dev/null \
        || warn "checkout failed: $p"
done
for p in ${DELS[@]+"${DELS[@]}"}; do
    git -C "$WT_DIR" rm -f --ignore-unmatch -- "$p" >/dev/null 2>&1 || true
done
for entry in ${RENAMES[@]+"${RENAMES[@]}"}; do
    old="${entry%%$'\t'*}"; new="${entry##*$'\t'}"
    git -C "$WT_DIR" rm -f --ignore-unmatch -- "$old" >/dev/null 2>&1 || true
    git -C "$WT_DIR" checkout "$INPUT_REF" -- "$new" 2>/dev/null \
        || warn "rename checkout failed: $new"
done

if [[ $SPEC_EXISTS -eq 1 ]]; then
    info "Replacing spec dir snapshot from $INPUT_REF..."
    git -C "$WT_DIR" rm -rf --ignore-unmatch -- "$SPEC_DIR" >/dev/null 2>&1 || true
    rm -rf "$WT_DIR/$SPEC_DIR"
    git -C "$WT_DIR" checkout "$INPUT_REF" -- "$SPEC_DIR" \
        || warn "spec dir checkout failed."
fi

# ---- commit + push ----
if git -C "$WT_DIR" diff --cached --quiet; then
    ok "Target already in sync — nothing to commit."
    echo ""
    echo "================ ipsec-yang-sync summary ================"
    printf "Input branch : %s\n" "$INPUT"
    printf "Base branch  : %s\n" "$BASE"
    printf "Target branch: %s (no change)\n" "$TARGET"
    printf "Worktree     : %s\n" "$WT_DIR"
    echo "========================================================="
    exit 0
fi

COMMIT_MSG_FILE="$(mktemp)"
{
    printf "Sync ipsec YANG/CLI/spec from %s\n\n" "$INPUT"
    printf "Source: %s @ %s\n" "$INPUT_REF" "$INPUT_SHA"
    printf "Base  : %s @ %s\n" "$BASE_REF" "$BASE_SHA"
    printf "\nFile counts:\n"
    printf "  - YANG (prod/dnos_monolith/yangs)        : %d\n" "$YANG_COUNT"
    printf "  - autogen_cli YAML                       : %d\n" "$YAML_COUNT"
    printf "  - dnos_cli RST                           : %d\n" "$RST_COUNT"
    printf "  - deletions                              : %d\n" "${#DELS[@]}"
    printf "  - renames                                : %d\n" "${#RENAMES[@]}"
    printf "  - spec snapshot                          : %s\n" \
        "$([[ $SPEC_EXISTS -eq 1 ]] && echo yes || echo no)"
} > "$COMMIT_MSG_FILE"
git -C "$WT_DIR" commit --quiet -F "$COMMIT_MSG_FILE" || die "git commit failed."
rm -f "$COMMIT_MSG_FILE"
COMMIT_SHA="$(git -C "$WT_DIR" rev-parse HEAD)"
ok "Committed $COMMIT_SHA on $TARGET."

if [[ $DO_PUSH -eq 1 ]]; then
    info "Pushing $TARGET to origin..."
    git -C "$WT_DIR" push origin "$TARGET" || die "git push failed."
    ok "Pushed origin/$TARGET."
else
    warn "Skipping push (--no-push)."
fi

PUSH_LINE="origin/$TARGET"
[[ $DO_PUSH -eq 0 ]] && PUSH_LINE="<skipped --no-push>"

echo ""
echo "================ ipsec-yang-sync summary ================"
printf "Input branch : %s\n" "$INPUT"
printf "Base branch  : %s\n" "$BASE"
printf "Target branch: %s\n" "$TARGET"
printf "Commit SHA   : %s\n" "$COMMIT_SHA"
printf "Worktree     : %s\n" "$WT_DIR"
printf "Files: %d yang, %d autogen_cli yaml, %d dnos_cli rst, spec snapshot: %s\n" \
    "$YANG_COUNT" "$YAML_COUNT" "$RST_COUNT" \
    "$([[ $SPEC_EXISTS -eq 1 ]] && echo yes || echo no)"
printf "Pushed to    : %s\n" "$PUSH_LINE"
echo "========================================================="
exit 0
