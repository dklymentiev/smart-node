#!/bin/bash
#
# permissions.sh -- Smart Node: apply and assert the declared permission surface.
#
# Runs INSIDE the target node as root. Reads permissions.list -- the single
# place where "who may write here" is decided -- and makes the node match it.
#
# It is the CLOSING step of every install path: the full installer and a
# single-component install/update alike. A companion that creates a directory
# does not choose its permissions; this stage corrects whatever it left.
# That is why a component re-run cannot silently reopen a hole.
#
# Three outcomes per declared entry:
#
#   matches            -- silent
#   declared, wrong    -- corrected (apply mode) / reported (verify mode)
#   present, UNDECLARED and writable beyond its owner -- REFUSED
#
# The refusal is the point. This stage never guesses permissions for a path
# nobody declared: it stops, and the declaration has to be added to the list.
# Refusal is fatal during an install; the scheduled check only reports.
#
# A node may declare its OWN state paths in /etc/smart-node/permissions.local.list
# (same format). That file is for what the node itself adds -- a flow's working
# directory, say -- and it is held to three rules:
#
#   - it must be root-owned and writable by root only, or it is refused whole;
#   - an entry must lie under /var/lib/smart-node or /srv: code (/opt), config
#     (/etc) and the stage itself are the release's, never the node's;
#   - it can only ADD: an entry for a path the release already declares is
#     ignored, so a node-local line can never loosen the release's surface.
#
# It also owns the group split it depends on:
#
#   smart-node         -- service accounts
#   smart-node-agents  -- agent-<slug> accounts
#
# A service user may join the agent group (it shares state with agents on
# purpose). An agent user may never join the service group. Moving an agent
# across is done here, and includes REMOVING the old membership -- the agent
# reconciler only ever adds groups, so nothing else would.
#
# Usage:
#   permissions.sh              apply the list, then assert it (default)
#   permissions.sh --verify     report only, change nothing; exit 1 on drift
#   permissions.sh --check      alias for --verify
#   permissions.sh --status     read the state file
#   permissions.sh --help       this message
#
# Exit: 0 all good; 1 drift (verify mode); 2 undeclared writable path; 4 error.
#
# Permission classes: see permissions.list.

set -uo pipefail

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="permissions"
STAGE_VERSION="0.1.0"

SN_LIB="/usr/local/lib/smart-node"
STATE_DIR="${SMART_NODE_STATE_DIR:-/var/lib/smart-node}/stages"
STATE_FILE="$STATE_DIR/$STAGE_ID.state"

# lib.sh when installed (log/die/manifest_record); standalone otherwise, so the
# scheduled check does not depend on an install being complete.
if [[ -r "$SN_LIB/lib.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SN_LIB/lib.sh" 2>/dev/null || true
fi
if ! declare -F log >/dev/null; then
    log() { echo "[$(date '+%H:%M:%S')] [$STAGE_ID] $*"; }
fi
if ! declare -F die >/dev/null; then
    die() { echo "[ERROR] $*" >&2; exit "${2:-4}"; }
fi

SERVICE_GROUP="${SMART_NODE_GROUP:-smart-node}"
AGENT_GROUP="${SMART_NODE_AGENT_GROUP:-smart-node-agents}"

# The list: next to this script (installer run) or installed under lib (node).
LIST=""
for cand in "$(dirname "$0")/permissions.list" "$SN_LIB/permissions.list"; do
    [[ -r "$cand" ]] && { LIST="$cand"; break; }
done

# The node's own declarations, and where they may point. Both are overridable
# only so the test can run the stage against a scratch tree.
LOCAL_LIST="${SN_PERMISSIONS_LOCAL_LIST:-/etc/smart-node/permissions.local.list}"
read -r -a LOCAL_ROOTS <<< "${SN_PERMISSIONS_LOCAL_ROOTS:-/var/lib/smart-node /srv}"

MODE="apply"
case "${1:-}" in
    --verify|--check) MODE="verify" ;;
    --status)
        [[ -r "$STATE_FILE" ]] && { cat "$STATE_FILE"; exit 0; }
        echo "no state file ($STATE_FILE)"; exit 1 ;;
    "") : ;;
    *) die "unknown argument: $1" ;;
esac

[[ -n "$LIST" ]] || die "permissions.list not found (looked next to $0 and in $SN_LIB)"
[[ $EUID -eq 0 ]] || die "must run as root"

OK=0; FIXED=0; SKIPPED=0; DRIFT=0; UNDECLARED=0; LOCAL=0
DECLARED_PATHS=()
COVER_PREFIXES=()
REPORT=()

note()  { REPORT+=("$*"); }
say()   { echo "$*"; note "$*"; }

# normalise a mode to 4 octal digits ("755" -> "0755")
norm_mode() { printf '%04d' "$((10#${1:-0}))" 2>/dev/null || echo "0000"; }

# ---------------------------------------------------------------------------
# 1. groups
# ---------------------------------------------------------------------------
ensure_groups() {
    if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        [[ "$MODE" == verify ]] && { say "DRIFT  group $SERVICE_GROUP missing"; DRIFT=$((DRIFT+1)); return; }
        groupadd -r "$SERVICE_GROUP" && say "created group $SERVICE_GROUP"
    fi
    if ! getent group "$AGENT_GROUP" >/dev/null 2>&1; then
        if [[ "$MODE" == verify ]]; then
            say "DRIFT  group $AGENT_GROUP missing"; DRIFT=$((DRIFT+1)); return
        fi
        groupadd -r "$AGENT_GROUP" && say "created group $AGENT_GROUP"
    fi
}

# ---------------------------------------------------------------------------
# 2. membership -- the boundary itself
#
# Agents: primary group = AGENT_GROUP, and NOT a member of SERVICE_GROUP.
# The agent reconciler (admin-runner ensure_agent_users) only ever runs
# `usermod -a -G`, so a stale SERVICE_GROUP membership would survive forever
# and the hole would stay open while looking closed. Remove it here.
#
# Services: joined to AGENT_GROUP as a supplementary group. This is safe by the
# asymmetry -- a service in the agent group grants agents nothing -- and it is
# necessary: core-api provisions the agents' model credential, and service crons
# append to the same cron log tree agents do.
# ---------------------------------------------------------------------------
fix_membership() {
    local u primary
    while IFS=: read -r u _ _ gid _; do
        case "$u" in
            agent-*)
                primary=$(getent group "$gid" 2>/dev/null | cut -d: -f1)
                if [[ "$primary" != "$AGENT_GROUP" ]]; then
                    if [[ "$MODE" == verify ]]; then
                        say "DRIFT  $u primary group is '$primary', want '$AGENT_GROUP'"
                        DRIFT=$((DRIFT+1))
                    else
                        usermod -g "$AGENT_GROUP" "$u" \
                            && say "moved $u primary group -> $AGENT_GROUP"
                    fi
                fi
                if id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$SERVICE_GROUP"; then
                    if [[ "$MODE" == verify ]]; then
                        say "DRIFT  $u is still a member of the service group $SERVICE_GROUP"
                        DRIFT=$((DRIFT+1))
                    else
                        gpasswd -d "$u" "$SERVICE_GROUP" >/dev/null 2>&1 \
                            && say "removed $u from $SERVICE_GROUP"
                    fi
                fi
                ;;
            smart-node-*|sn-admin)
                if ! id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$AGENT_GROUP"; then
                    if [[ "$MODE" == verify ]]; then
                        say "DRIFT  service user $u not in $AGENT_GROUP (needs it to write agent-shared state)"
                        DRIFT=$((DRIFT+1))
                    else
                        usermod -a -G "$AGENT_GROUP" "$u" \
                            && say "added $u -> $AGENT_GROUP (supplementary)"
                    fi
                fi
                ;;
        esac
    done < <(getent passwd)
}

# ---------------------------------------------------------------------------
# 3. the declared entries
# ---------------------------------------------------------------------------
apply_entry() {
    local path="$1" owner="$2" group="$3" mode="$4"
    local cur_o cur_g cur_m want_m
    want_m=$(norm_mode "$mode")

    cur_o=$(stat -c %U "$path" 2>/dev/null); cur_g=$(stat -c %G "$path" 2>/dev/null)
    cur_m=$(norm_mode "$(stat -c %a "$path" 2>/dev/null)")
    # numeric owner/group in the list: compare numerically
    [[ "$owner" =~ ^[0-9]+$ ]] && cur_o=$(stat -c %u "$path" 2>/dev/null)
    [[ "$group" =~ ^[0-9]+$ ]] && cur_g=$(stat -c %g "$path" 2>/dev/null)

    local bad=0
    [[ "$owner" != "-" && "$cur_o" != "$owner" ]] && bad=1
    [[ "$group" != "-" && "$cur_g" != "$group" ]] && bad=1
    [[ "$cur_m" != "$want_m" ]] && bad=1

    if (( bad == 0 )); then OK=$((OK+1)); return 0; fi

    if [[ "$MODE" == verify ]]; then
        say "DRIFT  $path is $cur_o:$cur_g $cur_m, declared $owner:$group $want_m"
        DRIFT=$((DRIFT+1)); return 0
    fi

    [[ "$owner" != "-" ]] && chown "$owner" "$path" 2>/dev/null
    [[ "$group" != "-" ]] && chgrp "$group" "$path" 2>/dev/null
    chmod "$want_m" "$path" 2>/dev/null
    say "fixed  $path -> $owner:$group $want_m (was $cur_o:$cur_g $cur_m)"
    FIXED=$((FIXED+1))
}

# apply_tree -- "nothing under here may be writable beyond its owner".
#
# Deliberately does NOT chown recursively. A component tree legitimately mixes
# ownership (a venv, a vendored snapshot), and rewriting that could break the
# component in ways that have nothing to do with this boundary. Stripping the
# write bit is the whole job; ownership is left exactly as the component set it.
apply_tree() {
    local root="$1"
    local offenders
    # Symlinks are excluded: a symlink's own mode is always 0777 and means
    # nothing -- what matters is the target, which is walked on its own.
    # Without this a venv (lib64, python3, python) reads as four world-writable
    # paths that can never be fixed, and the stage never converges.
    offenders=$(find "$root" -xdev ! -type l -perm /022 2>/dev/null | wc -l)
    (( offenders == 0 )) && return 0
    if [[ "$MODE" == verify ]]; then
        say "DRIFT  $root: $offenders path(s) under it are writable beyond their owner"
        DRIFT=$((DRIFT+1)); return 0
    fi
    find "$root" -xdev ! -type l -perm /022 -exec chmod g-w,o-w {} + 2>/dev/null
    say "fixed  $root: stripped group/other write from $offenders path(s)"
    FIXED=$((FIXED+1))
}

# local_entry_ok PATH -- may the node-local list declare PATH?
local_entry_ok() {
    local p="$1" r d ok=0
    [[ "$p" == /* && "$p" != *"/../"* && "$p" != */.. ]] || { say "REFUSE local list: '$p' is not a plain absolute path"; return 2; }
    for r in "${LOCAL_ROOTS[@]}"; do [[ "$p" == "$r"/* ]] && ok=1; done
    (( ok )) || { say "REFUSE local list: '$p' is outside ${LOCAL_ROOTS[*]}"; return 2; }
    for d in "${DECLARED_PATHS[@]}" "${RELEASE_PATTERNS[@]}"; do
        [[ "$p" == "$d" ]] && { say "ignore local list: '$p' is declared by the release"; return 1; }
    done
    for d in "${COVER_PREFIXES[@]}"; do
        [[ "$p" == "$d"/* ]] && { say "ignore local list: '$p' is inside the release's $d"; return 1; }
    done
    return 0
}

RELEASE_PATTERNS=()

read_list() {
    local list="$1" origin="${2:-release}"
    local path owner group mode flags
    while read -r path owner group mode flags _; do
        [[ -z "${path:-}" || "${path:0:1}" == "#" ]] && continue
        [[ -n "${mode:-}" ]] || { say "REFUSE malformed list line for '$path'"; UNDECLARED=$((UNDECLARED+1)); continue; }
        if [[ "$origin" == local ]]; then
            local_entry_ok "$path"
            case $? in
                0) LOCAL=$((LOCAL+1)) ;;
                1) continue ;;
                *) UNDECLARED=$((UNDECLARED+1)); continue ;;
            esac
        else
            RELEASE_PATTERNS+=("$path")
        fi
        # During an explicit legacy HITL adoption the old Docker server still
        # owns this database until the HITL companion stops it. Earlier
        # companion permission passes must not take its write access away.
        if [[ "$path" == /var/lib/smart-node/hitl \
              && -d /opt/smart-node/hitl/app \
              && ! -f /etc/smart-node/hitl-adoption.json ]]; then
            owner=www-data; group=www-data; mode=0700
        fi
        # `tree` also covers the subtree for the scan -- it is actively enforced
        # below, so the scan must not report the same paths a second time.
        [[ "${flags:-}" == "cover" || "${flags:-}" == "tree" ]] && COVER_PREFIXES+=("$path")

        # glob entries expand to zero or more real paths
        local -a targets=()
        if [[ "$path" == *"*"* ]]; then
            shopt -s nullglob
            targets=($path)
            shopt -u nullglob
        else
            targets=("$path")
        fi

        if (( ${#targets[@]} == 0 )); then SKIPPED=$((SKIPPED+1)); continue; fi
        local t
        for t in "${targets[@]}"; do
            DECLARED_PATHS+=("$t")
            if [[ ! -e "$t" ]]; then SKIPPED=$((SKIPPED+1)); continue; fi
            apply_entry "$t" "$owner" "$group" "$mode"
            [[ "${flags:-}" == "tree" ]] && apply_tree "$t"
        done
    done < <(sed 's/#.*$//' "$list")
}

# read_local_list -- the node's own declarations, if it has any. A list anyone
# but root could edit would let that someone declare their way past the scan,
# so such a file is refused whole rather than read.
read_local_list() {
    [[ -e "$LOCAL_LIST" ]] || return 0
    local o m
    o=$(stat -c %u "$LOCAL_LIST" 2>/dev/null); m=$(stat -c %a "$LOCAL_LIST" 2>/dev/null)
    if [[ "$o" != 0 || ! -f "$LOCAL_LIST" ]] || (( 8#${m:-777} & 8#022 )); then
        say "REFUSE $LOCAL_LIST must be a root-owned file writable by root only (is $(stat -c '%U %a' "$LOCAL_LIST" 2>/dev/null)); not read"
        UNDECLARED=$((UNDECLARED+1)); return 0
    fi
    read_list "$LOCAL_LIST" local
}

# ---------------------------------------------------------------------------
# 4. the undeclared-writable scan
#
# Independent of the install manifest ON PURPOSE. The manifest is incomplete --
# /srv/agents, /srv/tools and /srv/generated are created by core-api's installer
# and never recorded, so a manifest-driven check would miss exactly the paths
# where the original exposure was demonstrated. This walks the filesystem.
# ---------------------------------------------------------------------------
read -r -a SCAN_ROOTS <<< "${SN_PERMISSIONS_SCAN_ROOTS:-/opt/smart-node /srv /etc/smart-node /var/lib/smart-node /var/log/smart-node /usr/local/lib/smart-node}"

is_declared() {
    local p="$1" d
    for d in "${DECLARED_PATHS[@]}"; do [[ "$p" == "$d" ]] && return 0; done
    for d in "${COVER_PREFIXES[@]}"; do [[ "$p" == "$d"/* ]] && return 0; done
    return 1
}

scan_undeclared() {
    local p root
    for root in "${SCAN_ROOTS[@]}"; do
        [[ -d "$root" ]] || continue
        while IFS= read -r p; do
            is_declared "$p" && continue
            say "REFUSE $p is writable beyond its owner ($(stat -c '%U:%G %a' "$p" 2>/dev/null)) and is not in the list"
            UNDECLARED=$((UNDECLARED+1))
        done < <(find "$root" -xdev ! -type l -perm /022 \( -type d -o -type f \) 2>/dev/null)
    done
}

# ---------------------------------------------------------------------------
main() {
    log "$STAGE_ID $STAGE_VERSION -- mode=$MODE list=$LIST"
    ensure_groups
    fix_membership
    read_list "$LIST" release
    read_local_list
    scan_undeclared

    local summary="ok=$OK fixed=$FIXED skipped=$SKIPPED drift=$DRIFT undeclared=$UNDECLARED local=$LOCAL"
    log "$summary"

    if [[ "$MODE" == apply ]]; then
        mkdir -p "$STATE_DIR" 2>/dev/null
        {
            echo "stage=$STAGE_ID"
            echo "version=$STAGE_VERSION"
            echo "ts=$(date -Iseconds)"
            if (( UNDECLARED > 0 )); then echo "status=failed"; else echo "status=done"; fi
            echo "$summary"
        } > "$STATE_FILE" 2>/dev/null
    fi

    if (( UNDECLARED > 0 )); then
        echo "[REFUSED] $UNDECLARED writable path(s) are not declared in $LIST." >&2
        echo "          Declare them (with the reason) or take the write away." >&2
        echo "          A path the node itself added goes in $LOCAL_LIST." >&2
        exit 2
    fi
    if [[ "$MODE" == verify ]] && (( DRIFT > 0 )); then exit 1; fi
    exit 0
}

main
