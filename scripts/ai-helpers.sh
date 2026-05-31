# shellcheck shell=bash
# ---------------------------------------------------------------------------
# AI helpers sync (github.com:tyamini/ai-helpers)
#
# Repo lives at ~/.drivenets/cheetah/AI/v2/private/ and is consumed by
# set-context.sh, which symlinks each private skill into
# ~/cheetah/.claude/skills/ and ~/cheetah/.agents/skills/, and each private
# rule into ~/cheetah/.cursor/rules/private/.
#
#   ai-push [commit message]   stage everything, commit, push to origin
#   ai-pull                    fast-forward pull, then re-apply via set-context
#   ai-show                    list private AI helpers grouped by category
#   goai                       cd into the private AI helpers repo
#
# Sourced from ~/.bashrc. Expects $_AI_PRIVATE_REPO to be exported by the
# caller; falls back to the canonical path if unset.
# ---------------------------------------------------------------------------

: "${_AI_PRIVATE_REPO:=$HOME/.drivenets/cheetah/AI/v2/private}"
: "${_AI_APPLY_SCRIPT:=$HOME/cheetah/.ai/skills/common/set-dev-context/scripts/set-context.sh}"

ai-push() {
    local repo="$_AI_PRIVATE_REPO"
    if [[ ! -d "$repo/.git" ]]; then
        echo "ai-push: $repo is not a git repository" >&2
        return 1
    fi
    local msg="$*"
    [[ -z "$msg" ]] && msg="Update AI helpers ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

    git -C "$repo" add -A || return $?
    if git -C "$repo" diff --cached --quiet; then
        echo "ai-push: nothing to commit in $repo"
    else
        git -C "$repo" commit -m "$msg" || return $?
    fi
    git -C "$repo" push
}

ai-pull() {
    local repo="$_AI_PRIVATE_REPO"
    if [[ ! -d "$repo/.git" ]]; then
        echo "ai-pull: $repo is not a git repository" >&2
        return 1
    fi
    if ! git -C "$repo" diff --quiet || ! git -C "$repo" diff --cached --quiet; then
        echo "ai-pull: $repo has uncommitted changes; commit or stash first" >&2
        git -C "$repo" status --short
        return 1
    fi
    git -C "$repo" pull --ff-only || return $?

    if [[ -x "$_AI_APPLY_SCRIPT" ]]; then
        "$_AI_APPLY_SCRIPT"
    else
        echo "ai-pull: apply script not found or not executable: $_AI_APPLY_SCRIPT" >&2
        echo "ai-pull: skipped re-apply; run set-context.sh manually once available" >&2
        return 1
    fi
}

# ai-show — list everything under the private AI helpers repo, grouped by
# category (skills/rules/commands/agents/profiles/...). Shows the SKILL.md
# `description:` frontmatter for each skill.
ai-show() {
    local repo="$_AI_PRIVATE_REPO"
    if [[ ! -d "$repo" ]]; then
        echo "ai-show: $repo does not exist" >&2
        return 1
    fi

    local branch="-" status="clean" origin="-"
    if [[ -d "$repo/.git" ]]; then
        branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "-")
        origin=$(git -C "$repo" remote get-url origin 2>/dev/null || echo "-")
        if ! git -C "$repo" diff --quiet 2>/dev/null || ! git -C "$repo" diff --cached --quiet 2>/dev/null; then
            status="dirty"
        fi
    fi

    printf '\033[1mai-helpers\033[0m  %s  [%s, %s]\n' "$repo" "$branch" "$status"
    [[ "$origin" != "-" ]] && printf '  origin: %s\n' "$origin"
    if [[ -d "$repo/.git" ]]; then
        local _ai_show_git_status
        _ai_show_git_status=$(git -C "$repo" status --short --branch 2>/dev/null)
        if [[ -n "$_ai_show_git_status" ]]; then
            printf '  git status:\n'
            printf '%s\n' "$_ai_show_git_status" | sed 's/^/    /'
        fi
    fi
    echo

    local section section_path count entry name desc
    for section in skills rules commands agents profiles instructions scripts docs; do
        section_path="$repo/$section"
        if [[ ! -d "$section_path" ]]; then
            continue
        fi

        if [[ "$section" == "skills" ]]; then
            mapfile -d '' -t _ai_show_entries < <(find "$section_path" -mindepth 1 -name SKILL.md -type f -print0 2>/dev/null | sort -z)
            count=${#_ai_show_entries[@]}
            printf '\033[32m%s/\033[0m (%d)\n' "$section" "$count"
            if (( count == 0 )); then
                printf '  (empty)\n\n'
                continue
            fi
            for entry in "${_ai_show_entries[@]}"; do
                name=$(basename "$(dirname "$entry")")
                desc=$(awk '
                    /^---[[:space:]]*$/ { fm = !fm; next }
                    fm && /^description:[[:space:]]*/ {
                        sub(/^description:[[:space:]]*/, "")
                        print
                        exit
                    }
                ' "$entry")
                if [[ -n "$desc" ]]; then
                    printf '  %-28s %s\n' "$name" "${desc:0:120}"
                else
                    printf '  %s\n' "$name"
                fi
            done
            echo
            continue
        fi

        mapfile -d '' -t _ai_show_entries < <(find "$section_path" -mindepth 1 -maxdepth 1 \( -type f -o -type d \) ! -name '.gitkeep' -print0 2>/dev/null | sort -z)
        count=${#_ai_show_entries[@]}
        printf '\033[32m%s/\033[0m (%d)\n' "$section" "$count"
        if (( count == 0 )); then
            printf '  (empty)\n\n'
            continue
        fi
        for entry in "${_ai_show_entries[@]}"; do
            name=$(basename "$entry")
            if [[ -d "$entry" ]]; then
                printf '  %s/\n' "$name"
            else
                printf '  %s\n' "$name"
            fi
        done
        echo
    done
    unset _ai_show_entries
}

# goai — cd into the private AI helpers repo.
goai() {
    local repo="$_AI_PRIVATE_REPO"
    if [[ ! -d "$repo" ]]; then
        echo "goai: $repo does not exist" >&2
        return 1
    fi
    cd "$repo" || return $?
}
