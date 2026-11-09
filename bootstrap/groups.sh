#!/usr/bin/env bash
#
# groups.sh -- create and populate role and project groups.
#
# For every distinct value of sn_config.agents.capability the node maintains
# one system group `grp-role-<capability>`, membered by every non-archived
# agent that carries that capability plus the owner seat `admin`. For every
# non-archived row in sn_config.projects the node maintains one system group
# `grp-prj-<slug>` membered by the OS accounts named in sn_config.project_members.
#
# Runs INSIDE the target node as root. Idempotent: an already-correct group and
# membership set is a no-op (no groupadd, no gpasswd). Missing groups are
# created; extra members are removed; renames use groupmod -n.
#
# Prerequisite: the `capability` column exists on sn_config.agents. If the column is absent, role-group MEMBERSHIP is
# skipped with a warning and only the class-home writer/reader groups declared
# in classes.json are ensured (so ACLs still land at bootstrap).
#
# Usage:
#   groups.sh                  apply (default) -- creates and reconciles
#   groups.sh --verify         report only, exit 1 on drift
#   groups.sh --status         read the state file
#   groups.sh --help           this message
#
# Exit: 0 all good; 1 drift (verify); 4 error.
#

set -uo pipefail

STAGE_ID="groups"
STAGE_VERSION="0.1.0"

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

SN_LIB="/usr/local/lib/smart-node"
STATE_DIR="${SMART_NODE_STATE_DIR:-/var/lib/smart-node}/stages"
STATE_FILE="$STATE_DIR/$STAGE_ID.state"
CLASSES="${SMART_NODE_CLASSES:-/etc/smart-node/classes.json}"
# Persistence contract written by B02-persistence (see bootstrap/lib.sh line 99).
# Sourced by pg_run below so this script talks to the SAME database as every
# other stage rather than a name it guessed.
PERSISTENCE_ENV="${SMART_NODE_PERSISTENCE_ENV:-/etc/smart-node/persistence.env}"

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

MODE="apply"
case "${1:-}" in
    --verify|--check) MODE="verify" ;;
    --status)
        [[ -r "$STATE_FILE" ]] && { cat "$STATE_FILE"; exit 0; }
        echo "no state file ($STATE_FILE)"; exit 1 ;;
    "") : ;;
    *) die "unknown argument: $1" ;;
esac

[[ $EUID -eq 0 ]] || die "must run as root"
command -v jq >/dev/null 2>&1 || die "jq is required"
[[ -r "$CLASSES" ]] || die "$CLASSES not found or unreadable"

# Named error: the installed classes.json must carry the
# filesystem_homes table. If it does not, the operator is looking at a file
# from a release without the filesystem_homes table -- silently skipping every row (or
# letting jq throw "Cannot iterate over null") is exactly the class of
# defect that put this ADR back in in_progress. Refuse
# with a name the operator can find in the docs.
if ! jq -e 'has("filesystem_homes") and (.filesystem_homes | type == "array")' \
        "$CLASSES" >/dev/null 2>&1; then
    die "HOME NO FILESYSTEM_HOMES: $CLASSES has no filesystem_homes[] table.
       The installed classes.json is from a release without the filesystem_homes table.
       Re-run install-on.sh (or update-on.sh) so /etc/smart-node/classes.json
       is refreshed to a release that carries the /work path table."
fi

# Owner account. uid 1001 is the owner seat; every
# grp-role-* has this user as a member. Reading by name (`admin`) rather than
# hardcoding uid so a future rename of the owner seat is contained here.
OWNER_USER="${SMART_NODE_OWNER_USER:-admin}"

CREATED=0; RENAMED=0; ADDED=0; REMOVED=0; DRIFT=0

# ---------------------------------------------------------------------------
# db connection contract
#
# The core database name (SMART_NODE_PG_CORE_DB) and its cluster
# (SMART_NODE_PG_CLUSTER) are written by B02-persistence into
# /etc/smart-node/persistence.env. Root connects as the core role
# (SMART_NODE_PG_CORE_ROLE=smartnode) via the smartnode ident map: the same
# contract lib.sh log_event uses (bootstrap/lib.sh 1056-1112). Do NOT hard
# code -U postgres -d smart_node: the DB is `smartnode` and peer as
# `postgres` is refused on this node, which let the database refusal go unnoticed with
# every role-group membership and per-project ACL silently skipped.
_PG_ENV_LOADED=0
load_pg_env() {
    (( _PG_ENV_LOADED )) && return 0
    [[ -r "$PERSISTENCE_ENV" ]] || return 1
    # shellcheck disable=SC1090,SC1091
    source "$PERSISTENCE_ENV" 2>/dev/null || return 1
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" && -n "${SMART_NODE_PG_CORE_DB:-}" ]] \
        || return 1
    _PG_ENV_LOADED=1
    return 0
}

# pg_run [psql-args...] -- run psql against the core DB using the contract.
# stdout is the query result; return code is psql's. Caller checks either.
pg_run() {
    load_pg_env || return 1
    local pg_user="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
    PGCONNECT_TIMEOUT=3 psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -U "$pg_user" -d "$SMART_NODE_PG_CORE_DB" \
        -X -q -v ON_ERROR_STOP=1 "$@"
}

# ---------------------------------------------------------------------------
# helpers

group_exists() { getent group "$1" >/dev/null 2>&1; }
user_exists()  { id -u "$1" >/dev/null 2>&1; }

# Return the group members as a sorted whitespace-separated list.
group_members() {
    getent group "$1" 2>/dev/null | awk -F: '{print $4}' | tr ',' '\n' \
        | sed '/^$/d' | sort -u | paste -sd ' ' -
}

ensure_group() {
    local name="$1"
    if group_exists "$name"; then return 0; fi
    if [[ "$MODE" == verify ]]; then
        log "DRIFT  group $name missing"; DRIFT=$((DRIFT+1)); return 0
    fi
    groupadd -r "$name" && log "created group $name" && CREATED=$((CREATED+1))
}

# reconcile_membership GROUP  desired-user-list...
# The wanted members become the group; extras are removed. Idempotent.
reconcile_membership() {
    local grp="$1"; shift
    local want=("$@")
    group_exists "$grp" || { log "hold   $grp does not exist, cannot reconcile"; return 0; }
    local have
    have=$(group_members "$grp")
    local u present
    # Add every wanted user that is missing.
    for u in "${want[@]}"; do
        [[ -z "$u" ]] && continue
        user_exists "$u" || { log "skip   $u is not an OS account (needed for $grp)"; continue; }
        present=0
        for h in $have; do [[ "$h" == "$u" ]] && present=1; done
        if (( ! present )); then
            if [[ "$MODE" == verify ]]; then
                log "DRIFT  $u is not a member of $grp"; DRIFT=$((DRIFT+1))
            else
                gpasswd -a "$u" "$grp" >/dev/null 2>&1 \
                    && { log "added  $u -> $grp"; ADDED=$((ADDED+1)); }
            fi
        fi
    done
    # Remove any current member not wanted.
    local wanted_str=" ${want[*]} "
    for h in $have; do
        if [[ "$wanted_str" != *" $h "* ]]; then
            if [[ "$MODE" == verify ]]; then
                log "DRIFT  $h is a member of $grp but should not be"; DRIFT=$((DRIFT+1))
            else
                gpasswd -d "$h" "$grp" >/dev/null 2>&1 \
                    && { log "removed $h from $grp"; REMOVED=$((REMOVED+1)); }
            fi
        fi
    done
}

# ---------------------------------------------------------------------------
# 1. class-home writer/reader groups from classes.json
#
# Ensures every named grp-role-* / grp-agents referenced in filesystem_homes
# exists as a system group. Membership is filled later from the capability
# column (section 2); this step only creates the groups so acls.sh has something
# to chgrp to on a fresh install.
declared_groups() {
    jq -r '
        [ .filesystem_homes[] | .writer_group,  (.reader_groups // [])[] ]
        | map(select(. != null and (. | startswith("grp-"))))
        | unique
        | .[]
    ' "$CLASSES"
}

ensure_declared_groups() {
    local g
    while IFS= read -r g; do
        [[ -z "$g" ]] && continue
        ensure_group "$g"
    done < <(declared_groups)
}

# ---------------------------------------------------------------------------
# 2. role groups: one per distinct capability value in sn_config.agents
#
# Reads over the local postgres peer socket. If the DB is not reachable or the
# capability column does not exist yet, this step is skipped WITH A LOG line --
# ACLs still land, the memberships get filled on the next groups.sh run once
# the schema catches up.

pg_can_read() {
    command -v psql >/dev/null 2>&1 || return 1
    pg_run -tAc "SELECT 1" >/dev/null 2>&1 || return 1
    return 0
}

capability_column_exists() {
    pg_run -tAc \
        "SELECT 1 FROM information_schema.columns
          WHERE table_schema='sn_config' AND table_name='agents' AND column_name='capability'" \
        2>/dev/null | grep -q 1
}

# On a real node the persistence contract MUST exist (B02 has run) and the DB
# MUST answer. On a fresh install running from a canon before B02, both are
# absent and we skip -- ACLs still land, memberships fill on the next run.
# Anywhere in between (env exists, DB refuses) is a defect the operator has
# to know about, not a silent skip.
require_db_or_skip() {
    if [[ ! -r "$PERSISTENCE_ENV" ]]; then
        log "skip   $1: $PERSISTENCE_ENV not present (pre-B02 install)"
        return 1
    fi
    if pg_can_read; then
        return 0
    fi
    # Env exists but the DB is unreachable: this is the shape of D1.
    die "DB UNREACHABLE: $1 cannot reach core database via the persistence
       contract at $PERSISTENCE_ENV. Contract:
         cluster=${SMART_NODE_PG_CLUSTER:-<unset>}
         db=${SMART_NODE_PG_CORE_DB:-<unset>}
         role=${SMART_NODE_PG_CORE_ROLE:-smartnode}
       Peer-auth check as root: psql --cluster \"\$SMART_NODE_PG_CLUSTER\"
       -U \"\$SMART_NODE_PG_CORE_ROLE\" -d \"\$SMART_NODE_PG_CORE_DB\"
       must return one row. This script never hardcodes the DB name or the
       auth path; if the contract is wrong, fix persistence.env or the ident
       map (see /etc/postgresql/16/smartnode/pg_ident.conf)."
}

apply_role_groups() {
    require_db_or_skip "role group membership" || return 0
    if ! capability_column_exists; then
        log "skip   role group membership: sn_config.agents.capability column absent"
        return 0
    fi

    # Distinct capabilities in the live table.
    local caps
    caps=$(pg_run -tAc \
        "SELECT DISTINCT capability FROM sn_config.agents
          WHERE archived_at IS NULL
            AND capability IS NOT NULL AND capability <> ''
          ORDER BY capability" 2>/dev/null) || return 0

    if [[ -z "$caps" ]]; then
        log "note   no non-empty capability values found; role groups have no members yet"
        return 0
    fi

    local cap grp os_users_str want_arr
    while IFS= read -r cap; do
        [[ -z "$cap" ]] && continue
        grp="grp-role-$cap"
        ensure_group "$grp"
        # OS accounts of every agent with this capability, plus the owner seat.
        os_users_str=$(pg_run -tAc \
            "SELECT os_user FROM sn_config.agents
              WHERE archived_at IS NULL AND capability = '$cap'
                AND os_user IS NOT NULL AND os_user <> ''
              ORDER BY os_user" 2>/dev/null)
        # shellcheck disable=SC2206
        want_arr=($os_users_str)
        want_arr+=("$OWNER_USER")
        reconcile_membership "$grp" "${want_arr[@]}"
    done <<< "$caps"
}

# grp-agents: the everybody-agent group /work/downloads needs so any agent
# can drop a file. Populated from every non-archived agent's os_user plus
# the owner seat. Named grp-agents (not grp-role-*) because it is a
# collective, not a capability-derived role. classes-check.sh already
# accepts the grp-agents shape alongside grp-role-*/grp-prj-*.
#
# Rename-aware dispatch.
#
# the coordinated group migration will rename `smart-node-agents` to `grp-agents` in one
# coordinated MR that also rewrites every string reference across shell
# scripts, systemd unit files (Group=), sudoers Cmnd_Alias operands,
# docker-compose group_add, chgrp calls in bootstrap, and monitors.
# Until that lands, creating a fresh grp-agents here on a node that still
# carries smart-node-agents makes a NEW GID. The eventual
# `groupmod -n smart-node-agents grp-agents` then FAILS (target name
# already exists with a different GID), forcing manual un-mix on live
# state, exactly the class of change the coordinated group migration is meant to
# avoid. So this function reads the host and picks one of three paths:
#
#   a. smart-node-agents present AND grp-agents absent
#      -> legacy state. DEFER with a named message. Return 0
#         (idempotent no-op, not a failure). The rename is scheduled in
#         the coordinated group migration; groups.sh should touch nothing until then.
#   b. grp-agents present (whether smart-node-agents also exists or not)
#      -> reconcile membership only. Do NOT create the group.
#   c. Neither exists
#      -> clean install. Create grp-agents and reconcile.
apply_agents_group() {
    local has_legacy=0 has_agents=0
    group_exists "smart-node-agents" && has_legacy=1
    group_exists "grp-agents"        && has_agents=1

    if (( has_legacy && ! has_agents )); then
        log "PENDING group rename: smart-node-agents present, apply_agents_group deferred until groupmod -n smart-node-agents grp-agents lands"
        return 0
    fi

    require_db_or_skip "grp-agents membership" || return 0

    # State (c): clean install path. State (b) skips this: the group already
    # exists with the right GID (either created here on a previous run, or
    # renamed from smart-node-agents during the coordinated group migration).
    if (( ! has_agents )); then
        ensure_group "grp-agents"
    fi

    local os_users_str want_arr
    os_users_str=$(pg_run -tAc \
        "SELECT os_user FROM sn_config.agents
          WHERE archived_at IS NULL
            AND os_user IS NOT NULL AND os_user <> ''
          ORDER BY os_user" 2>/dev/null) || return 0
    # shellcheck disable=SC2206
    want_arr=($os_users_str)
    want_arr+=("$OWNER_USER")
    reconcile_membership "grp-agents" "${want_arr[@]}"
}

# ---------------------------------------------------------------------------
# 3. project groups: one per non-archived project

apply_project_groups() {
    require_db_or_skip "project group membership" || return 0
    local projects
    projects=$(pg_run -tAF $'\t' -c \
        "SELECT slug FROM sn_config.projects WHERE archived_at IS NULL ORDER BY slug" \
        2>/dev/null) || return 0
    [[ -z "$projects" ]] && return 0

    local slug grp members_str want_arr
    while IFS= read -r slug; do
        [[ -z "$slug" ]] && continue
        grp="grp-prj-$slug"
        ensure_group "$grp"
        # Members from sn_config.project_members. An agent member's OS account
        # lives on sn_config.agents.os_user; a user member's OS account is the
        # member_id itself.
        members_str=$(pg_run -tAc \
            "SELECT COALESCE(a.os_user, pm.member_id) AS os_name
               FROM sn_config.project_members pm
          LEFT JOIN sn_config.agents a
                 ON pm.member_kind = 'agent' AND a.slug = pm.member_id
                AND a.archived_at IS NULL
              WHERE pm.project_id = (SELECT id FROM sn_config.projects WHERE slug = '$slug')
              ORDER BY os_name" 2>/dev/null)
        # shellcheck disable=SC2206
        want_arr=($members_str)
        reconcile_membership "$grp" "${want_arr[@]}"
    done <<< "$projects"
}

# ---------------------------------------------------------------------------
main() {
    log "$STAGE_ID $STAGE_VERSION -- mode=$MODE"
    ensure_declared_groups
    apply_role_groups
    apply_agents_group
    apply_project_groups

    local summary="created=$CREATED renamed=$RENAMED added=$ADDED removed=$REMOVED drift=$DRIFT"
    log "$summary"

    if [[ "$MODE" == apply ]]; then
        mkdir -p "$STATE_DIR" 2>/dev/null
        {
            echo "stage=$STAGE_ID"
            echo "version=$STAGE_VERSION"
            echo "ts=$(date -Iseconds)"
            echo "status=done"
            echo "$summary"
        } > "$STATE_FILE" 2>/dev/null
    fi

    if [[ "$MODE" == verify ]] && (( DRIFT > 0 )); then exit 1; fi
    exit 0
}

main
