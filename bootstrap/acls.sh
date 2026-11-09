#!/usr/bin/env bash
#
# acls.sh -- apply /work class-home ownership and default POSIX ACLs.
#
# Reads /etc/smart-node/classes.json (installed by the orchestrator during the
# release publish step) and, for every row under `filesystem_homes`, makes the
# on-disk directory match:
#
#   root:<writer_group>  <mode>  default ACL: group:<writer>:rwx, group:<reader>:r-x, other::---
#
# Runs INSIDE the target node as root. Idempotent: a byte-identical tree is
# left untouched (no mtime changes), a divergent tree is corrected and named
# in the report. A row with a writer_group that does not yet exist as a system
# group is SKIPPED with a warning -- the group creation step (bootstrap/groups.sh)
# is responsible for making the group exist first.
#
# Per-project ACLs on /work/projects/<slug> are computed at run time from the
# project registry (sn_config.projects): every project gets a
# `group:grp-prj-<slug>:rwx` default entry, and a project with `restricted=true`
# drops the `group:grp-role-developer:rwx` entry so the tree is invisible to
# developers not explicitly on the team.
#
# Usage:
#   acls.sh                    apply (default) -- corrects, writes report
#   acls.sh --verify           report only, exit 1 on drift
#   acls.sh --status           read the state file
#   acls.sh --help             this message
#
# Exit: 0 all good; 1 drift (verify); 2 refused (missing writer group with no
#       root:root override); 4 error.
#

set -uo pipefail

STAGE_ID="acls"
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
REPORT_DIR="/work/_release/install-reports"
CLASSES="${SMART_NODE_CLASSES:-/etc/smart-node/classes.json}"
# Persistence contract written by B02-persistence (bootstrap/lib.sh line 99).
# apply_projects sources this to reach the core DB with the right name and
# the right ident map; the previous release hard-coded -U postgres -d
# smart_node and silently skipped every project on peer refusal when database peer authentication refused the connection.
PERSISTENCE_ENV="${SMART_NODE_PERSISTENCE_ENV:-/etc/smart-node/persistence.env}"

# lib.sh when installed (log/die); standalone otherwise so a scheduled --verify
# does not depend on the install having completed.
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
command -v jq       >/dev/null 2>&1 || die "jq is required"
command -v setfacl  >/dev/null 2>&1 || die "setfacl is required (install package: acl)"
command -v getfacl  >/dev/null 2>&1 || die "getfacl is required (install package: acl)"
[[ -r "$CLASSES" ]] || die "$CLASSES not found or unreadable"
jq empty "$CLASSES" >/dev/null 2>&1 || die "$CLASSES is not valid JSON"
# Named error, symmetrical with groups.sh: refuse a classes.json
# from a legacy release rather than letting jq throw "Cannot iterate
# over null" mid-loop.
if ! jq -e 'has("filesystem_homes") and (.filesystem_homes | type == "array")' \
        "$CLASSES" >/dev/null 2>&1; then
    die "HOME NO FILESYSTEM_HOMES: $CLASSES has no filesystem_homes[] table.
       The installed classes.json is from a release without the filesystem_homes table.
       Re-run install-on.sh (or update-on.sh) so /etc/smart-node/classes.json
       is refreshed to a release that carries the /work path table."
fi

APPLIED=0; SKIPPED=0; DRIFT=0; REFUSED=0
REPORT_LINES=()

# ---------------------------------------------------------------------------
# db connection contract (see bootstrap/groups.sh for the long comment).
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

pg_run() {
    load_pg_env || return 1
    local pg_user="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
    PGCONNECT_TIMEOUT=3 psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -U "$pg_user" -d "$SMART_NODE_PG_CORE_DB" \
        -X -q -v ON_ERROR_STOP=1 "$@"
}

report() { REPORT_LINES+=("$*"); }
note()   { report "$*"; log "$*"; }

# ---------------------------------------------------------------------------
# Idempotent-write helpers. Each returns 0 on no-op, 1 on change, 2 on drift
# (verify mode).
# ---------------------------------------------------------------------------

want_owner_group() {
    local path="$1" want_o="$2" want_g="$3"
    local cur_o cur_g
    cur_o=$(stat -c '%U' "$path" 2>/dev/null)
    cur_g=$(stat -c '%G' "$path" 2>/dev/null)
    if [[ "$cur_o" == "$want_o" && "$cur_g" == "$want_g" ]]; then return 0; fi
    if [[ "$MODE" == verify ]]; then
        note "DRIFT  $path owner:group is $cur_o:$cur_g, want $want_o:$want_g"
        DRIFT=$((DRIFT+1)); return 2
    fi
    chown "$want_o:$want_g" "$path" 2>/dev/null \
        || { note "REFUSE chown $want_o:$want_g $path failed"; REFUSED=$((REFUSED+1)); return 2; }
    note "fixed  $path -> $want_o:$want_g (was $cur_o:$cur_g)"
    return 1
}

want_mode() {
    local path="$1" want="$2"
    local cur
    cur=$(stat -c '%a' "$path" 2>/dev/null)
    # Normalise both to leading-zero form for comparison.
    cur=$(printf '%04d' "$((10#$cur))" 2>/dev/null || echo "0000")
    want=$(printf '%04d' "$((10#$want))" 2>/dev/null || echo "0000")
    if [[ "$cur" == "$want" ]]; then return 0; fi
    if [[ "$MODE" == verify ]]; then
        note "DRIFT  $path mode is $cur, want $want"
        DRIFT=$((DRIFT+1)); return 2
    fi
    chmod "$want" "$path" 2>/dev/null \
        || { note "REFUSE chmod $want $path failed"; REFUSED=$((REFUSED+1)); return 2; }
    note "fixed  $path mode -> $want (was $cur)"
    return 1
}

# want_default_acl PATH ACL-SPEC
# ACL-SPEC is a newline-free string of entries the default ACL must contain,
# separated by commas. Reads the existing default ACL, compares as a set, and
# either replaces (apply) or reports (verify).
want_default_acl() {
    local path="$1" spec="$2"
    local cur
    # Extract default entries (`default:*`), sort, join comma-separated. Blank
    # if the directory has no default ACL yet.
    cur=$(getfacl -p --absolute-names "$path" 2>/dev/null \
        | awk '/^default:/{sub(/^default:/,""); print}' \
        | sort -u | paste -sd, -)
    local want
    want=$(printf '%s\n' "$spec" | tr ',' '\n' | sort -u | paste -sd, -)
    if [[ "$cur" == "$want" ]]; then return 0; fi
    if [[ "$MODE" == verify ]]; then
        note "DRIFT  $path default ACL"
        note "       have: $cur"
        note "       want: $want"
        DRIFT=$((DRIFT+1)); return 2
    fi
    # Rebuild: clear default ACL, then apply the wanted entries as one setfacl call.
    setfacl -k "$path" 2>/dev/null || true
    local acl_args=()
    local ent
    while IFS= read -r ent; do
        [[ -z "$ent" ]] && continue
        acl_args+=(-d -m "$ent")
    done < <(printf '%s\n' "$spec" | tr ',' '\n')
    if ! setfacl "${acl_args[@]}" "$path" 2>/dev/null; then
        note "REFUSE setfacl on $path failed"
        REFUSED=$((REFUSED+1)); return 2
    fi
    note "fixed  $path default ACL -> $want"
    return 1
}

# group_exists NAME  -- 0 if the system group is present, 1 otherwise
group_exists() { getent group "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. filesystem_homes: the /work class-home rows
# ---------------------------------------------------------------------------
apply_home() {
    local path="$1" writer="$2" mode="$3" owner="$4" group="$5" readers_csv="$6"
    if [[ ! -d "$path" ]]; then
        note "skip   $path missing on disk (created by owner or a companion)"
        SKIPPED=$((SKIPPED+1)); return 0
    fi
    if [[ "$writer" == "null" ]]; then
        # root-owned home (e.g. _archive, _release, _tmp): fixed ownership+mode,
        # no default ACL touched (owners of these expect Linux defaults).
        want_owner_group "$path" "$owner" "$group" || true
        want_mode        "$path" "$mode" || true
        APPLIED=$((APPLIED+1))
        return 0
    fi
    if ! group_exists "$writer"; then
        note "hold   $path writer_group '$writer' does not exist yet (run groups.sh first)"
        SKIPPED=$((SKIPPED+1)); return 0
    fi
    want_owner_group "$path" root "$writer" || true
    want_mode        "$path" "$mode" || true
    # Build the default ACL spec: writer rwx, each reader r-x, other:::---
    local spec="user::rwx,group::rwx,group:${writer}:rwx,mask::rwx,other::---"
    local r
    for r in $readers_csv; do
        [[ -z "$r" || "$r" == "-" ]] && continue
        if ! group_exists "$r"; then
            note "hold   $path reader_group '$r' does not exist yet, entry deferred"
            continue
        fi
        spec="${spec},group:${r}:r-x"
    done
    want_default_acl "$path" "$spec" || true
    APPLIED=$((APPLIED+1))
}

apply_filesystem_homes() {
    local n
    n=$(jq '.filesystem_homes | length // 0' "$CLASSES")
    if [[ "$n" -eq 0 ]]; then
        die "$CLASSES declares no filesystem_homes; the installer requires the /work path table"
    fi
    log "applying $n filesystem_homes row(s) from $CLASSES"
    while IFS=$'\t' read -r path writer mode owner group readers; do
        [[ -z "$path" ]] && continue
        apply_home "$path" "$writer" "$mode" "$owner" "$group" "$readers"
    done < <(jq -r '
        .filesystem_homes[] |
        [
            .path,
            (.writer_group // "null"),
            (.mode // "2775"),
            (.owner // "root"),
            (.group // "root"),
            ((.reader_groups // []) | join(" "))
        ] | @tsv' "$CLASSES")
}

# ---------------------------------------------------------------------------
# 2. per-project ACLs on /work/projects/<slug>
#
# Read sn_config.projects through the postgres peer socket if the DB is up and
# psql is available; otherwise skip with a warning (fresh install has no rows
# to apply anyway, so this is not fatal).
# ---------------------------------------------------------------------------
apply_projects() {
    local projects_root="/work/projects"
    [[ -d "$projects_root" ]] || return 0
    if ! command -v psql >/dev/null 2>&1; then
        note "skip   per-project ACLs: psql not available"
        return 0
    fi
    if [[ ! -r "$PERSISTENCE_ENV" ]]; then
        note "skip   per-project ACLs: $PERSISTENCE_ENV not present (pre-B02 install)"
        return 0
    fi
    if ! pg_run -tAc "SELECT 1" >/dev/null 2>&1; then
        # Env exists but the DB is unreachable: fail loud, do not skip. This
        # is the shape of D1 -- silent skip left every per-project ACL off.
        die "DB UNREACHABLE: per-project ACLs cannot reach core database via
       $PERSISTENCE_ENV (cluster=${SMART_NODE_PG_CLUSTER:-<unset>}
       db=${SMART_NODE_PG_CORE_DB:-<unset>}
       role=${SMART_NODE_PG_CORE_ROLE:-smartnode}). Fix persistence.env or
       the smartnode ident map (/etc/postgresql/16/smartnode/pg_ident.conf)."
    fi
    local rows
    rows=$(pg_run -tAF $'\t' -c \
        "SELECT slug, restricted FROM sn_config.projects WHERE archived_at IS NULL ORDER BY slug" \
        2>/dev/null) || rows=""
    [[ -z "$rows" ]] && { log "no active projects to ACL"; return 0; }
    local slug restricted p spec
    while IFS=$'\t' read -r slug restricted; do
        [[ -z "$slug" ]] && continue
        p="$projects_root/$slug"
        [[ -d "$p" ]] || { note "skip   $p missing (project row present, directory not provisioned)"; SKIPPED=$((SKIPPED+1)); continue; }
        local proj_group="grp-prj-$slug"
        if ! group_exists "$proj_group"; then
            note "hold   $p project group '$proj_group' does not exist yet"
            SKIPPED=$((SKIPPED+1)); continue
        fi
        # /work/projects/<slug>: root:grp-role-developer 2775, default ACL:
        #   group:grp-role-developer:rwx (unless restricted)
        #   group:grp-prj-<slug>:rwx  (always)
        #   group:grp-role-architect:r-x
        #   group:grp-role-sysadmin:r-x
        want_owner_group "$p" root "grp-role-developer" || true
        want_mode        "$p" 2775 || true
        spec="user::rwx,group::rwx,group:${proj_group}:rwx,mask::rwx,other::---"
        if [[ "$restricted" != "t" ]]; then
            group_exists grp-role-developer && spec="${spec},group:grp-role-developer:rwx"
        fi
        group_exists grp-role-architect && spec="${spec},group:grp-role-architect:r-x"
        group_exists grp-role-sysadmin  && spec="${spec},group:grp-role-sysadmin:r-x"
        want_default_acl "$p" "$spec" || true
        APPLIED=$((APPLIED+1))
    done <<< "$rows"
}

# ---------------------------------------------------------------------------
# 3. write the human-readable install report
# ---------------------------------------------------------------------------
write_report() {
    local ts f
    ts=$(date -Iseconds)
    install -d -m 2775 "$REPORT_DIR" 2>/dev/null || true
    f="$REPORT_DIR/$(date -u +%Y%m%dT%H%M%SZ)-acls.md"
    {
        echo "# ACL install report"
        echo
        echo "- Ran at:  $ts"
        echo "- Mode:    $MODE"
        echo "- Classes: $CLASSES"
        echo "- Summary: applied=$APPLIED skipped=$SKIPPED drift=$DRIFT refused=$REFUSED"
        echo
        echo "## Actions"
        echo
        if (( ${#REPORT_LINES[@]} == 0 )); then
            echo "_(no changes)_"
        else
            printf -- '- %s\n' "${REPORT_LINES[@]}"
        fi
    } > "$f" 2>/dev/null && log "wrote $f" || log "could not write report (permissions?)"
}

# ---------------------------------------------------------------------------
main() {
    log "$STAGE_ID $STAGE_VERSION -- mode=$MODE classes=$CLASSES"
    apply_filesystem_homes
    apply_projects
    write_report

    local summary="applied=$APPLIED skipped=$SKIPPED drift=$DRIFT refused=$REFUSED"
    log "$summary"

    if [[ "$MODE" == apply ]]; then
        mkdir -p "$STATE_DIR" 2>/dev/null
        {
            echo "stage=$STAGE_ID"
            echo "version=$STAGE_VERSION"
            echo "ts=$(date -Iseconds)"
            if (( REFUSED > 0 )); then echo "status=failed"; else echo "status=done"; fi
            echo "$summary"
        } > "$STATE_FILE" 2>/dev/null
    fi

    if (( REFUSED > 0 )); then exit 2; fi
    if [[ "$MODE" == verify ]] && (( DRIFT > 0 )); then exit 1; fi
    exit 0
}

main
