# Smart Node installer -- shared helpers.
#
# This file is installed inside the target host at
#   /usr/local/lib/smart-node/lib.sh
# by stage B01-core, then sourced by every stage script:
#
#   source /usr/local/lib/smart-node/lib.sh
#
# Not executable. Pure functions. No top-level side effects beyond
# defining vars + functions, so sourcing is idempotent.
#
# Contract surface (stable):
#   log MSG...
#   die MSG...
#   require_root
#   verify_check NAME CMD       # runs CMD; on fail increments VERIFY_ERRORS, prints [VERIFY-FAIL]
#   sudoers_alias_commands FILE ALIAS...  # expanded commands of named Cmnd_Alias entries
#   sudo_ll_within ALLOWED_FILE  # stdin = sudo -ll output; 0 only if every grant is root + in ALLOWED_FILE
#   stage_mark   ID STATUS [KEY=VAL ...]
#   stage_status ID             # prints state file; exits 0=done 1=failed 2=missing
#   require_stage ID            # die unless stage ID has status=done
#   report_section TITLE BODY   # idempotent append to /etc/smart-node/install-report.md
#   create_service_user SHORT   # service account smart-node-SHORT + dirs + pg ident map
#   grant_pg_docker_access DB ROLE  # open the smart-node docker net to a B02 DB/role
#   log_init STAGE_ID           # point per-stage log at /var/log/smart-node/STAGE_ID.log
#   manifest_record TYPE VALUE  # record a created artifact in manifest.jsonl
#   manifest_retire_stale STAGE # withdraw STAGE's rows it no longer creates
#   manifest_retire_artifact TYPE VALUE [REASON]  # withdraw one row by artifact
#   artifact_verifiable TYPE    # true if TYPE's liveness can be re-checked
#   artifact_alive TYPE VALUE   # true if the recorded artifact is still live
#   register_mcp NAME FILE      # install an MCP shim into /etc/smart-node/mcp.d/
#   unregister_mcp NAME         # remove an MCP shim
#   register_companion NAME VERSION DEPS [STATE]  # write companions.d/NAME.json
#   unregister_companion NAME   # remove a companion manifest
#   register_cron NAME SCHEDULE COMMAND [USER]    # write /etc/cron.d/smart-node-NAME
#   unregister_cron NAME        # remove a cron.d job file
#   register_route NAME FILE    # install a Traefik dynamic-route fragment
#   unregister_route NAME       # remove a Traefik dynamic-route fragment
#   register_hook EVENT NAME FILE  # install + enable a lifecycle hook
#   unregister_hook NAME        # remove a hook and its enabled links
#   run_hooks EVENT [ARG...]    # run enabled hooks for a lifecycle EVENT
#   log_event ACTOR ETYPE [ENTYPE] [ENID] [PAYLOAD]
#                               # record an event: 3-tier pg -> buffer -> stderr
#   flush_event_buffer          # drain the pre-DB event buffer into events
#
# Globals it may set:
#   VERIFY_ERRORS  (int; init to 0 by callers before running checks)
#   VERIFY_TOTAL   (int; init to 0 by callers; verify_check counts every check)
#   SMART_NODE_LOG (per-stage log path; set by log_init; empty = stdout only)

SMART_NODE_STATE_DIR="/var/lib/smart-node/stages"
SMART_NODE_REPORT="/etc/smart-node/install-report.md"
# Record of every host artifact the installer creates, one JSON object
# per line. uninstall-from.sh replays it in reverse. Rows are only ever
# appended or RETIRED in place (a "retired" timestamp field is added);
# a row is never deleted, so the file stays a full history.
SMART_NODE_MANIFEST="/var/lib/smart-node/manifest.jsonl"
# Per-stage log file; set by log_init. Empty -> log() writes stdout only.
SMART_NODE_LOG="${SMART_NODE_LOG:-}"
# SERVICE group: every Smart Node service account belongs to it. Created by
# B01-core; create_service_user makes it the primary group of each companion
# account. It holds SERVICE accounts only -- an agent user may never be a
# member, because most group-write in the core tree is granted to it.
SMART_NODE_GROUP="smart-node"
# AGENT group: agent-<slug> accounts run here (see admin-runner
# ensure_agent_users), and it owns what agents genuinely need -- their model
# credential, the shared cron log tree, the scratch area. Service accounts join
# it as a supplementary group where they share that state with agents; that
# direction is safe, the reverse is the route-to-wrong-database hole.
SMART_NODE_AGENT_GROUP="smart-node-agents"
# Registry dirs created by B01-core; companion installers populate them.
# mcp.d/  -- one shim per MCP server, globbed by agent runtimes.
# companions.d/ -- one manifest per companion, read by the B06 web UI.
SMART_NODE_MCP_DIR="/etc/smart-node/mcp.d"
SMART_NODE_COMPANIONS_DIR="/etc/smart-node/companions.d"
# Cron: file-based registry, cron.d/-style. B01-core installs the wrapper
# and the log dir; companion installers call register_cron to drop a job
# file. Each job file is /etc/cron.d/smart-node-NAME; its command runs
# through the wrapper (per-job lock + run log).
SMART_NODE_CRON_PREFIX="/etc/cron.d/smart-node-"
SMART_NODE_CRON_RUNNER="/usr/local/lib/smart-node/cron-runner.sh"
SMART_NODE_CRON_LOG_DIR="/var/log/smart-node/cron"
# Traefik dynamic-route registry. B03-routing creates this directory and
# the Traefik file-provider watches it; companion installers drop a route
# fragment here via register_route. Same cron.d/-style pattern as mcp.d/.
SMART_NODE_TRAEFIK_DYNAMIC_DIR="/etc/smart-node/traefik/dynamic"
# Routing contract written by B03-routing -- the docker network name, the
# dynamic-route dir, the node domain and TLS resolver. grant_pg_docker_access
# reads the network name from it to find the bridge gateway/subnet.
SMART_NODE_ROUTING_ENV="/etc/smart-node/routing.env"
# Stage lifecycle hooks. hooks/available/ holds hook scripts; a hook is
# enabled for a lifecycle EVENT by a symlink hooks/enabled/EVENT.NAME.
# run_hooks dispatches them. These are install/uninstall lifecycle hooks
# -- NOT Claude Code-style agent hooks (those are an agent-runtimes
# companion concern).
SMART_NODE_HOOKS_DIR="/etc/smart-node/hooks"
SMART_NODE_HOOK_EVENTS="pre-install post-install pre-uninstall"
# Event log (log_event / flush_event_buffer). The persistence contract is
# written by B02-persistence once Postgres is up; the buffer holds events
# emitted before that (or while the DB is briefly unreachable).
SMART_NODE_PERSISTENCE_ENV="/etc/smart-node/persistence.env"
SMART_NODE_EVENT_BUFFER="/var/lib/smart-node/stages/events.buffer"
SMART_NODE_LIB_VERSION="0.11.0"

# Artifacts recorded by THIS process, keyed "STAGE\tTYPE\tVALUE". It is
# what lets stage_mark tell "the stage still creates this" from "the
# stage stopped creating this" -- see manifest_retire_stale.
# Declared at top level on purpose: an associative array declared inside
# a function is local to it.
declare -A _MANIFEST_RUN=()

# _log_tee LINE -- append LINE to the per-stage log if one is configured
# and its directory is writable. Never fails the caller (set -e safe).
_log_tee() {
    [[ -n "${SMART_NODE_LOG:-}" ]] || return 0
    local d; d=$(dirname "$SMART_NODE_LOG")
    if [[ -d "$d" && -w "$d" ]]; then
        printf '%s\n' "$1" >> "$SMART_NODE_LOG" 2>/dev/null || true
    fi
    return 0
}

log() { local line="[$(date '+%H:%M:%S')] $*"; echo "$line";    _log_tee "$line"; }
die() { local line="[ERROR] $*";              echo "$line" >&2; _log_tee "$line"; exit 1; }

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root"
}

# verify_check NAME CMD...
# Runs CMD in a subshell. Counts every call in VERIFY_TOTAL. On non-zero
# exit, prints [VERIFY-FAIL] and increments VERIFY_ERRORS (caller must
# init both to 0). On success, optional verbose mode prints [ok] when
# SMART_NODE_VERIFY_VERBOSE=1.
#
# Always returns 0 -- never trips set -e in the caller. Caller reads
# VERIFY_ERRORS / VERIFY_TOTAL to learn how many checks ran and failed.
verify_check() {
    local name="$1"; shift
    VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
    if "$@" >/dev/null 2>&1; then
        [[ "${SMART_NODE_VERIFY_VERBOSE:-0}" -eq 1 ]] && echo "[ok] $name"
        return 0
    fi
    echo "[VERIFY-FAIL] $name" >&2
    VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
    return 0
}

# sudoers_alias_commands FILE ALIAS...
# Prints every command of the named Cmnd_Alias entries in FILE, one per line,
# in the form `sudo -ll` prints them: continuation lines joined, the body split
# on commas, whitespace collapsed. Returns 1 if any named alias is missing or
# empty, so a renamed alias cannot quietly shrink the allowed set to nothing.
# Does not handle escaped commas or several aliases on one line; the shipped
# agent-max.sudoers uses neither.
sudoers_alias_commands() {
    local file="$1"; shift
    awk -v want="$*" '
        BEGIN { n = split(want, w, " "); for (i = 1; i <= n; i++) keep[w[i]] = 1 }
        cont == "" && /^[[:space:]]*#/ { next }
        {
            line = cont $0
            if (line ~ /\\[[:space:]]*$/) { sub(/\\[[:space:]]*$/, " ", line); cont = line; next }
            cont = ""
            if (line !~ /^[[:space:]]*Cmnd_Alias[[:space:]]/) next
            sub(/^[[:space:]]*Cmnd_Alias[[:space:]]+/, "", line)
            eq = index(line, "=")
            name = substr(line, 1, eq - 1); gsub(/[[:space:]]/, "", name)
            if (!(name in keep)) next
            m = split(substr(line, eq + 1), parts, ",")
            for (i = 1; i <= m; i++) {
                c = parts[i]
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", c); gsub(/[[:space:]]+/, " ", c)
                if (c != "") { print c; found[name]++ }
            }
        }
        END { for (k in keep) if (!found[k]) { print "alias " k " missing or empty" > "/dev/stderr"; exit 1 } }
    ' "$file"
}

# sudo_ll_within ALLOWED_FILE
# Reads `sudo -ll -U <user>` output on stdin and returns 0 only when the user
# is granted at least one command, every entry runs as root and nothing else,
# and every granted command is a line of ALLOWED_FILE. Anything it does not
# recognise inside the rules section (an LDAP role, a new sudo output shape)
# is a failure, not a skip: this is a scope check, and an unread rule is an
# unchecked rule. The reason for a failure goes to stderr.
#
# Why -ll and not -l: the short form prints each rule on one line and
# the check that grepped it for alias names could never pass, because sudo
# expands aliases in both forms. The long form puts one command per line, so
# the comparison is exact rather than a pattern over a joined line.
sudo_ll_within() {
    local allowed="$1"
    awk -v allowed="$allowed" '
        BEGIN {
            while ((getline l < allowed) > 0) { gsub(/[[:space:]]+/, " ", l); ok[l] = 1 }
            close(allowed)
        }
        / may run the following commands / { rules = 1; next }
        !rules { next }
        /^Sudoers entry:/ { entry = 1; cmds_open = 0; next }
        /^[^[:space:]]/ { bad = "unrecognised rule source: " $0; exit 1 }
        !entry { if ($0 ~ /[^[:space:]]/) { bad = "rule outside a sudoers entry: " $0; exit 1 } next }
        /^[[:space:]]+RunAsUsers:/ {
            v = $0; sub(/^[[:space:]]+RunAsUsers:[[:space:]]*/, "", v)
            if (v != "root") { bad = "runs as " v ", not root only"; exit 1 }
            next
        }
        /^[[:space:]]+RunAsGroups:/ { bad = "grants a run-as group: " $0; exit 1 }
        /^[[:space:]]+Commands:/ { cmds_open = 1; next }
        cmds_open && /^\t/ {
            c = $0; gsub(/^[[:space:]]+|[[:space:]]+$/, "", c); gsub(/[[:space:]]+/, " ", c)
            granted++
            if (!(c in ok)) { bad = "command outside the narrow scope: " c; exit 1 }
            next
        }
        { cmds_open = 0 }
        END {
            if (bad != "") { print bad > "/dev/stderr"; exit 1 }
            if (granted == 0) { print "no command granted at all" > "/dev/stderr"; exit 1 }
        }
    '
}

# stage_mark ID STATUS [extra KEY=VAL ...]
# Writes /var/lib/smart-node/stages/ID.state atomically.
# Layer A creates the state dir as root:root; state files are root-only-write.
#
# On STATUS=done it also retires the manifest rows this stage has stopped
# creating -- the completion of a stage is the one moment where
# "what this stage builds" is fully known.
stage_mark() {
    local id="$1" status="$2"; shift 2
    [[ -d "$SMART_NODE_STATE_DIR" ]] \
        || die "state dir missing ($SMART_NODE_STATE_DIR); Layer A not done?"
    local tmp
    tmp=$(mktemp "${SMART_NODE_STATE_DIR}/.${id}.state.XXXXXX")
    {
        echo "status=$status"
        echo "timestamp=$(date -Iseconds)"
        echo "lib_version=$SMART_NODE_LIB_VERSION"
        for kv in "$@"; do
            echo "$kv"
        done
    } > "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$SMART_NODE_STATE_DIR/$id.state"
    [[ "$status" == done ]] && manifest_retire_stale "$id"
    return 0
}

# stage_status ID
# Prints state file body if present; exit 0=done, 1=failed, 2=missing.
stage_status() {
    local id="$1" f="$SMART_NODE_STATE_DIR/$1.state"
    if [[ ! -f "$f" ]]; then
        echo "stage $id: missing"
        return 2
    fi
    cat "$f"
    local st
    st=$(awk -F= '$1=="status"{print $2; exit}' "$f")
    case "$st" in
        done)   return 0 ;;
        failed) return 1 ;;
        *)      return 1 ;;
    esac
}

# require_stage ID -- die unless ID.state has status=done.
require_stage() {
    local id="$1" f="$SMART_NODE_STATE_DIR/$1.state"
    [[ -f "$f" ]] || die "require_stage $id: state file missing ($f)"
    awk -F= '$1=="status"{print $2; exit}' "$f" | grep -qx done \
        || die "require_stage $id: not done"
}

# report_section TITLE BODY
# Append "## TITLE" + BODY to install-report.md only if that heading is
# not already present. Layer A creates the report file with the master
# header; here we just append. Stage scripts must run AFTER Layer A.
report_section() {
    local title="$1" body="$2"
    [[ -f "$SMART_NODE_REPORT" ]] \
        || die "report file missing ($SMART_NODE_REPORT); Layer A not done?"
    if grep -qFx "## $title" "$SMART_NODE_REPORT"; then
        return 0
    fi
    {
        echo ""
        echo "## $title"
        echo ""
        echo "$body"
    } >> "$SMART_NODE_REPORT"
}

# log_init STAGE_ID
# Point the per-stage log at /var/log/smart-node/STAGE_ID.log and emit a
# run header. Safe to call before /var/log/smart-node exists -- teeing
# starts once the dir is created (B01-core ensure_dirs); earlier lines
# are stdout-only. Sets global SMART_NODE_LOG.
log_init() {
    local id="${1:-stage}"
    SMART_NODE_LOG="/var/log/smart-node/${id}.log"
    log "=== ${id} run @ $(date -Iseconds) (lib ${SMART_NODE_LIB_VERSION}) ==="
}

# manifest_record TYPE VALUE
# Append one JSON line describing a host artifact created by the current
# stage to $SMART_NODE_MANIFEST. uninstall-from.sh replays the manifest
# in reverse to undo an install. Idempotent: a (TYPE,VALUE) pair already
# present is not appended again.
#
# TYPE is one of: group user dir file package systemd-unit pg-ident
# pg-cluster docker-network docker-container ufw-rule.
# The stage name is taken from $STAGE_ID if the caller set it.
manifest_record() {
    local mtype="${1:-}" mvalue="${2:-}"
    [[ -n "$mtype" && -n "$mvalue" ]] \
        || die "manifest_record: TYPE and VALUE required"
    local mdir; mdir=$(dirname "$SMART_NODE_MANIFEST")
    [[ -d "$mdir" ]] \
        || die "manifest_record: dir missing ($mdir); Layer A not done?"
    # Note the claim BEFORE the idempotency check below: a row already on
    # disk is still a row this run declares, and manifest_retire_stale
    # must not read the skipped append as "the stage dropped it".
    _MANIFEST_RUN["${STAGE_ID:-unknown}"$'\t'"$mtype"$'\t'"$mvalue"]=1
    if [[ ! -f "$SMART_NODE_MANIFEST" ]]; then
        : > "$SMART_NODE_MANIFEST"
        chmod 0644 "$SMART_NODE_MANIFEST"
    fi
    # Idempotency: the leading {"type":...,"value":...,} fragment uniquely
    # keys an artifact; skip if it is already recorded.
    local key="{\"type\":\"$mtype\",\"value\":\"$mvalue\","
    if grep -qF "$key" "$SMART_NODE_MANIFEST" 2>/dev/null; then
        return 0
    fi
    printf '{"type":"%s","value":"%s","stage":"%s","ts":"%s"}\n' \
        "$mtype" "$mvalue" "${STAGE_ID:-unknown}" "$(date -Iseconds)" \
        >> "$SMART_NODE_MANIFEST"
}

# artifact_verifiable TYPE
# True when a manifest row of TYPE can be re-checked after the fact.
# pg-ident map lines and individual ufw rules cannot: they are folded
# into a file whose other lines have other owners, so their absence
# proves nothing. Those rows are informational -- never failed, and
# never retired.
artifact_verifiable() {
    case "${1:-}" in
        docker-container|docker-compose-project|docker-network|docker-volume) return 0 ;;
        systemd-unit|package|dir|file|user|group|pg-cluster)                  return 0 ;;
        *)                                                                    return 1 ;;
    esac
}

# artifact_alive TYPE VALUE
# True when the artifact a manifest row claims is still there. The one
# definition of liveness in the installer: validate.sh checks rows with
# it, and manifest_retire_stale decides with it. Two answers to the same
# question would let a node pass validation and still be retiring live
# rows behind it.
artifact_alive() {
    local mtype="${1:-}" mvalue="${2:-}"
    case "$mtype" in
        docker-container)
            [[ $(docker inspect -f '{{.State.Running}}' "$mvalue" 2>/dev/null) == true ]] ;;
        docker-compose-project)
            docker ps -q -f "label=com.docker.compose.project=$mvalue" 2>/dev/null | grep -q . ;;
        docker-network)
            docker network inspect "$mvalue" >/dev/null 2>&1 ;;
        docker-volume)
            docker volume inspect "$mvalue" >/dev/null 2>&1 ;;
        systemd-unit)
            # Long-running services must be active; oneshot units (seed / reconcile
            # jobs) run once and exit, so is-active reads 'inactive' even on success.
            # Accept those iff they finished with Result=success; a genuinely failed
            # oneshot (Result!=success) still counts as dead.
            systemctl is-active --quiet "$mvalue" 2>/dev/null \
                || [[ $(systemctl show -p Type --value "$mvalue" 2>/dev/null) == oneshot \
                   && $(systemctl show -p Result --value "$mvalue" 2>/dev/null) == success ]] ;;
        package)     dpkg -s "$mvalue" >/dev/null 2>&1 ;;
        dir)         [[ -d "$mvalue" ]] ;;
        file)        [[ -e "$mvalue" ]] ;;
        user)        id -u "$mvalue" >/dev/null 2>&1 ;;
        group)       getent group "$mvalue" >/dev/null 2>&1 ;;
        pg-cluster)
            pg_lsclusters -h 2>/dev/null \
                | awk '{print $1"/"$2" "$4}' | grep -qx "$mvalue online" ;;
        *)           return 1 ;;
    esac
}

# manifest_retire_stale STAGE
# Withdraw the rows STAGE has stopped creating. Called by stage_mark on
# STATUS=done.
#
# WHY: the manifest only ever grew. A component that moved off
# Docker onto systemd -- mesh, mesh-mcp and planner did, in 1.12.25 --
# left its container rows behind, and validate.sh replayed them forever
# on every node that UPDATED rather than installed fresh: 3 VERIFY-FAIL,
# exit 6, finalize never ran, and no re-run could clear it because a run
# only appended. Three special cases would have fixed those three names
# and waited for the fourth. The rule instead: on the run that completes
# a stage, a row of that stage which the run did NOT re-record and whose
# artifact is NOT there any more is retired.
#
# Both halves of that condition matter:
#   - "did not re-record" alone would retire a row for a container that
#     the stage still creates but that is down right now -- masking the
#     exact failure validate.sh exists to catch.
#   - "not there any more" alone would retire a row on any run that
#     happens not to touch the stage.
# A run that recorded nothing for STAGE (--verify, --status, an install
# that took an early-exit path) retires nothing: it never looked, so it
# has no standing to withdraw a claim.
#
# Retiring is annotation, not deletion -- the row keeps its history and
# gains "retired":TS. Readers skip retired rows.
manifest_retire_stale() {
    local mstage="${1:-}"
    [[ -n "$mstage" ]] || die "manifest_retire_stale: STAGE required"
    [[ -f "$SMART_NODE_MANIFEST" ]] || return 0

    # Standing: did this run record anything at all for this stage?
    local k declared=0
    for k in "${!_MANIFEST_RUN[@]}"; do
        [[ "${k%%$'\t'*}" == "$mstage" ]] || continue
        declared=1
        break
    done
    (( declared )) || return 0

    local mtype mvalue
    local -a retire=()
    while IFS=$'\t' read -r mtype mvalue; do
        [[ -n "$mtype" && -n "$mvalue" ]] || continue
        [[ -n "${_MANIFEST_RUN["$mstage"$'\t'"$mtype"$'\t'"$mvalue"]:-}" ]] && continue
        artifact_verifiable "$mtype" || continue
        artifact_alive "$mtype" "$mvalue" && continue
        retire+=("$mtype"$'\t'"$mvalue")
    done < <(awk -F'"' -v st="$mstage" '
        $2 == "type" && $6 == "value" && $10 == "stage" && $12 == st \
        && index($0, "\"retired\":") == 0 { print $4 "\t" $8 }' \
        "$SMART_NODE_MANIFEST")

    (( ${#retire[@]} )) || return 0

    local ts; ts=$(date -Iseconds)
    local list; list=$(printf '%s\n' "${retire[@]}")
    local tmp; tmp=$(mktemp "${SMART_NODE_MANIFEST}.XXXXXX")
    awk -F'"' -v st="$mstage" -v ts="$ts" -v list="$list" '
        BEGIN { n = split(list, rows, "\n"); for (i = 1; i <= n; i++) want[rows[i]] = 1 }
        $2 == "type" && $6 == "value" && $10 == "stage" && $12 == st \
        && index($0, "\"retired\":") == 0 && (($4 "\t" $8) in want) {
            sub(/\}$/, ",\"retired\":\"" ts "\"}")
        }
        { print }' "$SMART_NODE_MANIFEST" > "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$SMART_NODE_MANIFEST"

    for k in "${retire[@]}"; do
        log "manifest: retired ${k%%$'\t'*} ${k#*$'\t'} -- stage $mstage no longer creates it"
    done
    return 0
}

# manifest_retire_artifact TYPE VALUE [REASON]
# Withdraw one row by artifact, whatever stage owns it.
#
# WHY: manifest_retire_stale is stage-scoped and runs when a stage
# finishes, and it only retires a row whose artifact is already gone. That is
# right for a stage that stopped creating something of its own. It cannot cover
# the case where stage X deletes an artifact stage Y recorded: on the run that
# does it, Y has already finished with the file still in place, so Y keeps the
# claim and X never had it. The row then survives until the NEXT update, and in
# between validate.sh fails on a file the release deleted on purpose -- exit 6,
# finalize never runs, and the node keeps the old release stamp while running
# the new code. That is what 1.12.31 did to this node.
#
# So: the code that deletes an artifact withdraws the claim, in the same breath.
# Retiring is annotation, not deletion, exactly as in manifest_retire_stale.
manifest_retire_artifact() {
    local mtype="${1:-}" mvalue="${2:-}" reason="${3:-artifact removed}"
    [[ -n "$mtype" && -n "$mvalue" ]] \
        || die "manifest_retire_artifact: TYPE and VALUE required"
    [[ -f "$SMART_NODE_MANIFEST" ]] || return 0

    grep -Fq "\"value\":\"$mvalue\"" "$SMART_NODE_MANIFEST" 2>/dev/null || return 0

    local ts; ts=$(date -Iseconds)
    local tmp; tmp=$(mktemp "${SMART_NODE_MANIFEST}.XXXXXX")
    awk -F'"' -v ty="$mtype" -v va="$mvalue" -v ts="$ts" '
        $2 == "type" && $4 == ty && $6 == "value" && $8 == va \
        && index($0, "\"retired\":") == 0 {
            sub(/\}$/, ",\"retired\":\"" ts "\"}")
        }
        { print }' "$SMART_NODE_MANIFEST" > "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$SMART_NODE_MANIFEST"
    log "manifest: retired $mtype $mvalue -- $reason"
    return 0
}

# create_service_user SHORTNAME
# Creates a Smart Node service account and its owned dirs, following one
# convention shared by every companion installer (B02+) and any custom
# service. Idempotent -- safe to call repeatedly.
#
# Given SHORTNAME (e.g. "mesh") it ensures:
#   - system user  smart-node-SHORTNAME  (UID<1000, nologin,
#     home /var/lib/smart-node/SHORTNAME), primary group = umbrella
#     group $SMART_NODE_GROUP
#   - owned dirs   /var/lib/smart-node/SHORTNAME  (0750)
#                  /var/log/smart-node/SHORTNAME  (0750)
#   - a Postgres peer-auth ident mapping "smartnode <user> <user>".
#     If SMART_NODE_PG_IDENT points at an existing pg_ident.conf the line
#     is appended there; otherwise -- the bootstrap case, no Postgres yet
#     -- the mapping is queued in $SMART_NODE_STATE_DIR/pg-ident.pending
#     for a later persistence stage to apply.
#
# Echoes the full system username (smart-node-SHORTNAME) on stdout.
# Pre-condition: umbrella group $SMART_NODE_GROUP exists (B01-core).
create_service_user() {
    local short="${1:-}"
    [[ -n "$short" ]] || die "create_service_user: SHORTNAME required"
    [[ "$short" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "create_service_user: invalid name '$short' (lowercase letter, then alnum/dash)"

    local user="smart-node-$short"
    local home="/var/lib/smart-node/$short"
    local logdir="/var/log/smart-node/$short"

    getent group "$SMART_NODE_GROUP" >/dev/null 2>&1 \
        || die "create_service_user: umbrella group '$SMART_NODE_GROUP' missing (run B01-core first)"

    if ! id -u "$user" >/dev/null 2>&1; then
        useradd -r -g "$SMART_NODE_GROUP" -d "$home" \
            -s /usr/sbin/nologin "$user"
    elif ! id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$SMART_NODE_GROUP"; then
        # Account predates the service group -- pull it in.
        usermod -g "$SMART_NODE_GROUP" "$user"
    fi

    # Supplementary membership in the AGENT group. Needed because service crons
    # append to the same log tree agent wake-crons do, and because core-api
    # provisions the agents' model credential. Safe by the asymmetry: a service
    # in the agent group grants agents nothing (see permissions.list).
    if getent group "$SMART_NODE_AGENT_GROUP" >/dev/null 2>&1 \
       && ! id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$SMART_NODE_AGENT_GROUP"; then
        usermod -a -G "$SMART_NODE_AGENT_GROUP" "$user"
    fi

    install -d -m 0750 -o "$user" -g "$SMART_NODE_GROUP" "$home"
    install -d -m 0750 -o "$user" -g "$SMART_NODE_GROUP" "$logdir"

    # Postgres peer-auth ident map line. "smartnode" is the shared map
    # name every component DB role authenticates through.
    local ident_line="smartnode  $user  $user"
    if [[ -n "${SMART_NODE_PG_IDENT:-}" && -f "${SMART_NODE_PG_IDENT:-}" ]]; then
        grep -qF "$ident_line" "$SMART_NODE_PG_IDENT" \
            || echo "$ident_line" >> "$SMART_NODE_PG_IDENT"
    else
        [[ -d "$SMART_NODE_STATE_DIR" ]] \
            || die "create_service_user: state dir missing ($SMART_NODE_STATE_DIR)"
        local pending="$SMART_NODE_STATE_DIR/pg-ident.pending"
        touch "$pending"
        grep -qF "$ident_line" "$pending" \
            || echo "$ident_line" >> "$pending"
    fi

    manifest_record user     "$user"
    manifest_record dir      "$home"
    manifest_record dir      "$logdir"
    manifest_record pg-ident "$user"

    echo "$user"
}

# _envfile_get FILE KEY -- echo the value of a KEY=VALUE line in a Smart
# Node contract file (persistence.env / routing.env). Last assignment
# wins; empty output if the file or the key is absent. Internal helper.
# host_zone -- the IANA zone the host is set to, UTC when it names none.
# The node keeps no zone of its own: "local time" anywhere in the node is the
# host zone, which the operator sets (configure-host.sh --timezone, or
# timedatectl set-timezone).
host_zone() {
    local z=""
    [[ -r /etc/timezone ]] && z=$(tr -d '[:space:]' < /etc/timezone)
    if [[ -z "$z" && -L /etc/localtime ]]; then
        z=$(readlink -f /etc/localtime); z=${z#*/zoneinfo/}
    fi
    [[ -n "$z" && -f "/usr/share/zoneinfo/$z" ]] || z="UTC"
    printf '%s' "$z"
}

_envfile_get() {
    local file="$1" key="$2"
    [[ -r "$file" ]] || return 0
    sed -n -E "s/^${key}=//p" "$file" | tail -n1
}

# grant_pg_docker_access DB ROLE
# Open a scram-authenticated network path from the smart-node Docker
# network to the host-native B02 PostgreSQL cluster, for one (DB, ROLE)
# pair -- and nothing else.
#
# B04 onward runs Docker services (Infisical now; the Phase 2
# observability companion later) whose data lives in the B02 cluster.
# That cluster is host-native with peer auth on a local socket -- a
# container reaches neither the socket nor a peer identity. This helper
# makes exactly one DB/role reachable over TCP from the bridge: native
# peer auth for the bootstrap stages is left fully intact.
#
# Three additive, idempotent changes:
#   postgresql.conf -- set listen_addresses = '*' (no bridge literal
#                      stored in config; containers reach the cluster via
#                      host.docker.internal; pg_hba and ufw scope access).
#   pg_hba.conf     -- append `host DB ROLE <bridge-subnet> scram-sha-256`,
#                      scoping the grant to the smart-node subnet.
#   ufw             -- allow the smart-node subnet to the cluster port.
#                      Layer A's firewall defaults to deny-inbound, so
#                      listen + hba alone leave the packets dropped.
# Then it activates the cluster config: a restart if listen_addresses
# changed (that GUC is not reloadable), otherwise a plain reload for the
# new hba line. A re-run that changes nothing does not touch the cluster.
#
# The DB and ROLE themselves are the caller's job (the caller owns the
# password); this helper only grants the network path. Idempotent and
# multi-caller safe -- listen_addresses is set to '*' once; a host line for
# a DB/role already present is not duplicated; ufw skips a duplicate rule.
#
# Pre-conditions: B02 done (persistence.env) and B03 done (routing.env +
# the smart-node docker network). Records only the ufw rule in the
# manifest (an `ufw-rule` entry -- reversed by the existing uninstall
# handler); the postgresql.conf / pg_hba.conf edits live inside the B02
# cluster config and are undone when uninstall drops the cluster.
grant_pg_docker_access() {
    local db="${1:-}" role="${2:-}"
    [[ -n "$db" && -n "$role" ]] \
        || die "grant_pg_docker_access: DB and ROLE required"

    # B02 contract -- the cluster id, the pg_hba path (-> the config
    # dir) and the cluster port (for the post-restart readiness wait).
    local pg_cluster pg_hba pg_port
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    pg_hba=$(_envfile_get     "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_HBA)
    pg_port=$(_envfile_get    "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_PORT)
    [[ -n "$pg_cluster" && -n "$pg_hba" ]] \
        || die "grant_pg_docker_access: incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    [[ -f "$pg_hba" ]] \
        || die "grant_pg_docker_access: pg_hba.conf missing ($pg_hba)"
    local pg_conf; pg_conf="$(dirname "$pg_hba")/postgresql.conf"
    [[ -f "$pg_conf" ]] \
        || die "grant_pg_docker_access: postgresql.conf missing ($pg_conf)"

    # B03 contract -- the smart-node docker network name.
    local net
    net=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_DOCKER_NETWORK)
    [[ -n "$net" ]] \
        || die "grant_pg_docker_access: routing contract missing ($SMART_NODE_ROUTING_ENV) -- run B03 first"
    command -v docker >/dev/null 2>&1 \
        || die "grant_pg_docker_access: docker not installed -- run B03 first"

    # The docker bridge subnet (scopes the pg_hba line and the ufw rule).
    # Take the first IPv4 config. `|| true`: a no-match `grep` exits 1,
    # which `pipefail` in the caller would otherwise turn into a silent
    # `set -e` abort.
    # listen_addresses = '*' (not the gateway IP). The subnet is
    # still needed to scope pg_hba and the ufw rule.
    local subnet
    subnet=$(docker network inspect "$net" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    [[ -n "$subnet" ]] \
        || die "grant_pg_docker_access: cannot read subnet of docker network '$net' -- is B03 done?"

    # ---- postgresql.conf: listen_addresses ----
    # set listen_addresses = '*' so postgres binds 0.0.0.0 and does
    # not depend on the docker bridge being up at start time. Access is
    # restricted by pg_hba (host line scoped to bridge subnet) and ufw (ALLOW
    # bridge subnet only, never Anywhere). The last active assignment wins in
    # postgresql.conf; no active line -> upstream default 'localhost'.
    local la_changed=0 cur_la
    cur_la=$(grep -E '^[[:space:]]*listen_addresses[[:space:]]*=' "$pg_conf" 2>/dev/null \
        | tail -n1 | sed -E "s/^[^=]*=[[:space:]]*'?//; s/'?[[:space:]]*(#.*)?\$//" || true)
    [[ -n "$cur_la" ]] || cur_la="localhost"
    if [[ "$cur_la" == "*" ]]; then
        log "grant_pg_docker_access: listen_addresses already '*'"
    else
        log "grant_pg_docker_access: setting listen_addresses to '*'"
        # Comment any active listen_addresses line, then append ours.
        sed -i -E 's/^([[:space:]]*listen_addresses[[:space:]]*=)/#\1/' "$pg_conf"
        printf '\n# Smart Node -- listen on all interfaces.\nlisten_addresses = '\''*'\''\n' \
            >> "$pg_conf"
        la_changed=1
    fi

    # ---- pg_hba.conf: the host line ----
    local hba_changed=0
    if grep -qE "^host[[:space:]]+${db}[[:space:]]+${role}[[:space:]]" "$pg_hba"; then
        log "grant_pg_docker_access: pg_hba already grants ${role}@${db}"
    else
        log "grant_pg_docker_access: adding pg_hba host line for ${role}@${db} ($subnet)"
        printf '# Smart Node -- %s docker network access for %s (grant_pg_docker_access).\nhost    %s    %s    %s    scram-sha-256\n' \
            "$net" "$role" "$db" "$role" "$subnet" >> "$pg_hba"
        hba_changed=1
    fi

    # ---- ufw: let the docker subnet reach the cluster port ----
    # listen_addresses + pg_hba make Postgres *willing*; Layer A's ufw
    # defaults to deny-inbound, so the firewall must also *let the
    # packets in*. Open exactly the cluster port to the smart-node
    # subnet -- pg_hba still scopes auth to the one DB/ROLE. Idempotent:
    # ufw skips a duplicate rule, manifest_record skips a duplicate
    # entry. The `ufw-rule` entry is reversed by the existing uninstall
    # handler (`ufw delete allow <rule>`), so uninstall needs no change.
    if command -v ufw >/dev/null 2>&1; then
        local ufw_rule="from $subnet to any port ${pg_port:-5432} proto tcp"
        log "grant_pg_docker_access: ufw allow $ufw_rule"
        # shellcheck disable=SC2086  # rule words must split into args
        ufw allow $ufw_rule >/dev/null 2>&1 \
            || log "grant_pg_docker_access: ufw allow returned non-zero (continuing)"
        manifest_record ufw-rule "$ufw_rule"
    else
        log "grant_pg_docker_access: ufw not installed -- skipping firewall rule"
    fi

    # ---- activate ----
    local pg_major="${pg_cluster%/*}" pg_name="${pg_cluster#*/}"
    local unit="postgresql@${pg_major}-${pg_name}"

    # ---- boot ordering: remove docker dependency ----
    # listen_addresses = '*' binds 0.0.0.0. Postgres picks up any
    # interface that comes up after it starts, including the docker bridge, so
    # the After=docker.service dropin is no longer needed. Remove it on update.
    local dropin_dir="/etc/systemd/system/${unit}.service.d"
    local dropin="$dropin_dir/10-smart-node-docker-bridge.conf"
    if [[ -f "$dropin" ]]; then
        log "grant_pg_docker_access: removing docker boot-order dropin ('*' bind, no docker dependency)"
        rm -f "$dropin"
        command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload >/dev/null 2>&1 || true
        # The row belongs to whichever stage created it -- `memory` on a node
        # installed before 1.12.25 -- and that stage finishes before this
        # function runs, so its own retire pass still sees the file. Withdraw
        # the claim here, where the file actually goes.
        manifest_retire_artifact file "$dropin" \
            "postgres binds '*', the docker boot-order dropin is gone"
    fi

    local action=""
    if   (( la_changed ));  then action="restart"   # listen_addresses is not reloadable
    elif (( hba_changed )); then action="reload"
    fi
    if [[ -z "$action" ]]; then
        log "grant_pg_docker_access: no change -- cluster left untouched"
        return 0
    fi
    log "grant_pg_docker_access: $action cluster $pg_cluster"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl "$action" "$unit" >/dev/null 2>&1 \
            || die "grant_pg_docker_access: systemctl $action $unit failed"
    else
        # No systemd (build sandbox) -- drive the cluster directly.
        pg_ctlcluster "$pg_major" "$pg_name" "$action" >/dev/null 2>&1 \
            || die "grant_pg_docker_access: pg_ctlcluster $action $pg_name failed"
    fi
    # A restart drops connections -- wait for the cluster to come back.
    if [[ "$action" == "restart" ]]; then
        local i
        for i in $(seq 1 30); do
            pg_isready -p "${pg_port:-5432}" -q 2>/dev/null && return 0
            sleep 1
        done
        die "grant_pg_docker_access: cluster $pg_cluster not ready 30s after restart"
    fi
    return 0
}

# register_mcp NAME CONFIG_FILE
# Install an MCP server shim: copy CONFIG_FILE into
# /etc/smart-node/mcp.d/NAME.json. Agent runtimes glob that directory at
# startup and merge the shims into their MCP config. Idempotent
# (overwrites). Records the file in the manifest.
register_mcp() {
    local name="${1:-}" src="${2:-}"
    [[ -n "$name" ]] || die "register_mcp: NAME required"
    [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "register_mcp: invalid name '$name' (lowercase letter, then alnum/dash)"
    [[ -n "$src" && -r "$src" ]] \
        || die "register_mcp: config file unreadable ($src)"
    [[ -d "$SMART_NODE_MCP_DIR" ]] \
        || die "register_mcp: $SMART_NODE_MCP_DIR missing (run B01-core first)"
    if command -v jq >/dev/null 2>&1; then
        jq -e . "$src" >/dev/null 2>&1 \
            || die "register_mcp: $src is not valid JSON"
    fi
    local dst="$SMART_NODE_MCP_DIR/$name.json"
    install -m 0640 -g "$SMART_NODE_GROUP" "$src" "$dst"
    manifest_record file "$dst"
}

# unregister_mcp NAME
# Remove an MCP shim. Idempotent. The manifest entry is left in place
# (the manifest is append-only); uninstall tolerates a missing file.
unregister_mcp() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "unregister_mcp: NAME required"
    rm -f "$SMART_NODE_MCP_DIR/$name.json"
}

# register_companion NAME VERSION DEPS [STATE]
# Write /etc/smart-node/companions.d/NAME.json -- the manifest the B06
# web interface reads to build companion cards. DEPS is a comma-separated
# list of stage/companion ids (e.g. "B02,documents"); STATE defaults to
# "installed". Re-call to update the record (e.g. on a state change).
# Idempotent overwrite; records the file in the manifest.
register_companion() {
    local name="${1:-}" version="${2:-}" deps="${3:-}" state="${4:-installed}"
    [[ -n "$name" ]] || die "register_companion: NAME required"
    [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "register_companion: invalid name '$name' (lowercase letter, then alnum/dash)"
    [[ -n "$version" ]] || die "register_companion: VERSION required"
    [[ -d "$SMART_NODE_COMPANIONS_DIR" ]] \
        || die "register_companion: $SMART_NODE_COMPANIONS_DIR missing (run B01-core first)"
    # Comma-separated DEPS -> JSON array of strings.
    local deps_json="" d
    local -a deps_arr=()
    [[ -n "$deps" ]] && IFS=',' read -ra deps_arr <<< "$deps"
    for d in "${deps_arr[@]}"; do
        [[ -n "$d" ]] || continue
        deps_json+="${deps_json:+,}\"$d\""
    done
    # WHICH VERSION IS "THE" VERSION. The caller passes its own
    # STAGE_VERSION -- the version of the INSTALLER SCRIPT. That is a real
    # number and it is not what anyone reading a component's "version" means:
    # the panel renders this field as the component's version, so a node running
    # core-api 0.18.0 displayed 0.3.0, and the two are not even in the same
    # series. Measured 2026-08-14 on a clean install AND on an updated node, so
    # it was never an artifact of upgrading by hand.
    #
    # `version` therefore becomes the PAYLOAD's own version, read off the disk
    # at install time -- the thing that actually runs -- while the installer's
    # number keeps its own name beside it. `version_source` says which of the
    # two `version` ended up carrying, so the ambiguity cannot quietly return: a
    # component with no VERSION file falls back to the stage number and SAYS so
    # instead of looking authoritative.
    local payload="" vsource="stage"
    local payload_file="/opt/smart-node/$name/VERSION"
    if [[ -r "$payload_file" ]]; then
        payload=$(head -1 "$payload_file" | tr -d '[:space:]')
        [[ -n "$payload" ]] && vsource="payload"
    fi
    [[ -n "$payload" ]] || payload="$version"

    local dst="$SMART_NODE_COMPANIONS_DIR/$name.json"
    printf '{"name":"%s","version":"%s","version_source":"%s","stage_version":"%s","state":"%s","deps":[%s],"ts":"%s"}\n' \
        "$name" "$payload" "$vsource" "$version" "$state" "$deps_json" "$(date -Iseconds)" > "$dst"
    chmod 0640 "$dst"
    chgrp "$SMART_NODE_GROUP" "$dst"
    manifest_record file "$dst"
}

# unregister_companion NAME
# Remove a companion manifest. Idempotent. The manifest entry is left in
# place (append-only); uninstall tolerates a missing file.
unregister_companion() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "unregister_companion: NAME required"
    rm -f "$SMART_NODE_COMPANIONS_DIR/$name.json"
}

# register_cron NAME SCHEDULE COMMAND [USER]
# Write a cron job file at /etc/cron.d/smart-node-NAME. The job runs
# COMMAND through the Smart Node cron wrapper (per-job lock + run log).
# SCHEDULE is a 5-field cron spec ("*/5 * * * *"); USER defaults to root.
#
# This is the file-based cron registry -- no daemon, no DB. cron rescans
# /etc/cron.d every minute and notices the new/removed file on its own,
# so no service reload is needed. Idempotent overwrite; records the file
# in the manifest so uninstall removes it.
#
# Pre-condition: cron-runner.sh installed (B01-core). The job file is
# 0644 root:root -- cron ignores cron.d files that are group/world
# writable, so the perms are not negotiable.
register_cron() {
    local name="${1:-}" schedule="${2:-}" command="${3:-}" user="${4:-root}"
    [[ -n "$name" ]] || die "register_cron: NAME required"
    [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "register_cron: invalid name '$name' (lowercase letter, then alnum/dash)"
    [[ -n "$schedule" ]] || die "register_cron: SCHEDULE required (5-field cron spec)"
    [[ -n "$command" ]]  || die "register_cron: COMMAND required"
    [[ -x "$SMART_NODE_CRON_RUNNER" ]] \
        || die "register_cron: cron wrapper missing ($SMART_NODE_CRON_RUNNER); run B01-core first"
    # '%' is special in crontab commands (cron turns it into a newline);
    # a job command containing a literal '%' must escape it as '\%'.
    local dst="${SMART_NODE_CRON_PREFIX}${name}"
    local tmp; tmp=$(mktemp "${dst}.XXXXXX")
    {
        echo "# Smart Node cron job: $name"
        echo "# Managed by lib.sh register_cron -- do not edit by hand."
        echo "# Remove with: unregister_cron $name  (or rm this file)"
        echo "SHELL=/bin/bash"
        echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        echo "MAILTO=\"\""
        echo "$schedule $user $SMART_NODE_CRON_RUNNER $name $command"
    } > "$tmp"
    chmod 0644 "$tmp"
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dst"
    manifest_record file "$dst"
}

# unregister_cron NAME
# Remove a cron.d job file. Idempotent. The manifest entry is left in
# place (append-only); uninstall tolerates a missing file.
unregister_cron() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "unregister_cron: NAME required"
    rm -f "${SMART_NODE_CRON_PREFIX}${name}"
}

# register_route NAME ROUTE_FILE
# Install a Traefik dynamic-route fragment: copy ROUTE_FILE into
# /etc/smart-node/traefik/dynamic/NAME.yml. The Traefik file-provider
# (configured by B03-routing) watches that directory and picks the
# fragment up live -- no proxy reload needed. This is the routing
# counterpart of register_mcp: a companion with a web UI calls it after
# its service is up. Idempotent overwrite; records the file in the
# manifest. Pre-condition: B03-routing has created the dynamic dir.
register_route() {
    local name="${1:-}" src="${2:-}"
    [[ -n "$name" ]] || die "register_route: NAME required"
    [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "register_route: invalid name '$name' (lowercase letter, then alnum/dash)"
    [[ -n "$src" && -r "$src" ]] \
        || die "register_route: route file unreadable ($src)"
    [[ -d "$SMART_NODE_TRAEFIK_DYNAMIC_DIR" ]] \
        || die "register_route: $SMART_NODE_TRAEFIK_DYNAMIC_DIR missing (run B03-routing first)"
    local dst="$SMART_NODE_TRAEFIK_DYNAMIC_DIR/$name.yml"
    install -m 0640 -g "$SMART_NODE_GROUP" "$src" "$dst"
    manifest_record file "$dst"
}

# unregister_route NAME
# Remove a Traefik dynamic-route fragment. Idempotent. The manifest entry
# is left in place (append-only); uninstall tolerates a missing file.
unregister_route() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "unregister_route: NAME required"
    rm -f "$SMART_NODE_TRAEFIK_DYNAMIC_DIR/$name.yml"
}

# ---- public route eligibility --------------------------------
#
# The installer INVENTS public hostnames. Declare a domain and it derives
# panel.<domain>, studio.<domain>, voice.<domain>, doci.<domain>,
# dashboard.<domain>, auth.<domain> and writes a router for each. Declaring a
# domain is not the same as having published a record for every name the
# installer can think of, and nothing ever checked the difference.
#
# What that costs, measured on this node 2026-09-09: dashboard.<domain> and
# doci.<domain> had no DNS record at all. Traefik served routers nobody could
# reach, and for the one carrying `certResolver: letsencrypt` it asked for a
# certificate it could never validate -- five failed authorizations and the
# ACME account was rate limited for that name. The install was green
# throughout, because a rejected router is not a failed install and validate
# does not read Traefik.
#
# It matters more for a node that is not ours. This is an open-source
# installer: whoever runs it declares THEIR domain, and the same derivation
# then claims six names in it on their behalf.
#
# SMART_NODE_ROUTE_REQUIRE_DNS=0 turns the gate off. That is a declaration by
# the operator that DNS lives somewhere this node cannot see -- split horizon,
# a record about to be created -- and not a default.
SMART_NODE_ROUTE_REQUIRE_DNS="${SMART_NODE_ROUTE_REQUIRE_DNS:-1}"

# public_route_eligible HOST
#
# Whether a public route claiming HOST should exist on this node. Writers AND
# their verify checks must ask THIS function, never their own version of the
# question: a check that asks something slightly different from the writer is
# how the wrong database connection happened and why 981e75a had to fix core-api a second time.
#
# 0 (write it) unless:
#   * HOST is empty -- no public domain, so there is no name to claim;
#   * HOST has no address record WHILE ITS PARENT DOMAIN DOES. That pair is a
#     definitive negative: the resolver is answering, and its answer is that
#     this name does not exist.
#
# If NEITHER resolves the verdict is "write it", with a warning. The resolver
# may be down or the domain may be minutes old, and a resolver that cannot
# answer must not be able to retire a node front door.
#
# On failure PUBLIC_ROUTE_SKIP_REASON carries the sentence to log. The
# function itself returns only a status -- it prints nothing, so no caller can
# capture a log line as a value.
PUBLIC_ROUTE_SKIP_REASON=""
public_route_eligible() {
    local host="${1:-}"
    PUBLIC_ROUTE_SKIP_REASON=""
    if [[ -z "$host" ]]; then
        PUBLIC_ROUTE_SKIP_REASON="no public domain"
        return 1
    fi
    [[ "${SMART_NODE_ROUTE_REQUIRE_DNS:-1}" == "1" ]] || return 0
    getent ahosts "$host" >/dev/null 2>&1 && return 0
    local parent="${host#*.}"
    # A bare label, or a name whose parent is the apex we just failed on:
    # nothing left to cross-check against, so do not claim an answer.
    if [[ -z "$parent" || "$parent" == "$host" ]]; then
        PUBLIC_ROUTE_SKIP_REASON=""
        return 0
    fi
    if getent ahosts "$parent" >/dev/null 2>&1; then
        PUBLIC_ROUTE_SKIP_REASON="$host has no DNS record while $parent resolves, so this is an answer and not a resolver failure"
        return 1
    fi
    # >&2 deliberately: this function is used for its STATUS, and a function
    # that also writes to stdout is the shape that put a log line inside a
    # Traefik Host rule. Nothing it says may become a value.
    log "WARN: neither $host nor $parent resolves -- writing the route anyway, because a resolver that cannot answer must not retire a door. If the resolver is healthy, this name is unreachable." >&2
    return 0
}

# web_local [DOMAIN] -- a node named localhost or *.localhost is served by a
# machine to itself: those names resolve to loopback, and a browser treats
# http on them as a secure context (cookies, passkeys, service workers). Such a
# node speaks plain http on the web entrypoint, so a person installing it sees no
# certificate warning; a self-signed one could only be made trusted per
# system and per browser. Every other node is unchanged: https on websecure.
web_local() {
    local d="${1-${SMART_NODE_DOMAIN:-}}"
    [[ "$d" == localhost || "$d" == *.localhost ]]
}
web_scheme() { web_local "$@" && echo http || echo https; }
web_entry()  { web_local "$@" && echo web  || echo websecure; }
# The tls line of a router in a dynamic file: `tls: {}`, or a comment where a
# local node has none. Only the value changes, so the YAML indent stays put.
web_tls_yaml() { web_local "$@" && echo "# plain http: a local node" || echo "tls: {}"; }

# retire_route COMPONENT FILE REASON
#
# Remove a route file this run decided not to write, and withdraw the manifest
# claim in the same breath, so validate does not later fail on a file that was
# deleted on purpose (manifest_retire_artifact, 1.12.32). Skipping a route
# means RETIRING it: returning without touching the file leaves whatever an
# earlier release wrote sitting on disk and Traefik keeps serving it.
retire_route() {
    local component="${1:-}" file="${2:-}" reason="${3:-}"
    [[ -n "$component" && -n "$file" ]] || die "retire_route: COMPONENT and FILE required"
    if [[ -f "$file" ]]; then
        rm -f "$file"
        manifest_retire_artifact file "$file" "$reason"
        log "[$component] $reason -- Traefik route retired ($file)"
    else
        log "[$component] $reason -- no Traefik route to write"
    fi
}

# _validate_hook_event EVENT -- die unless EVENT is a known lifecycle
# event. Internal helper for register_hook / run_hooks.
_validate_hook_event() {
    local e="${1:-}" valid
    for valid in $SMART_NODE_HOOK_EVENTS; do
        [[ "$e" == "$valid" ]] && return 0
    done
    die "invalid hook event '$e' (valid: $SMART_NODE_HOOK_EVENTS)"
}

# register_hook EVENT NAME FILE
# Install hook script FILE as hooks/available/NAME (0750) and enable it
# for lifecycle EVENT via the symlink hooks/enabled/EVENT.NAME. Safe to
# re-call -- e.g. to enable the same hook for a second event. Records
# both the script and the link in the manifest.
register_hook() {
    local event="${1:-}" name="${2:-}" src="${3:-}"
    [[ -n "$event" ]] || die "register_hook: EVENT required"
    _validate_hook_event "$event"
    [[ -n "$name" ]] || die "register_hook: NAME required"
    [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]] \
        || die "register_hook: invalid name '$name' (lowercase letter, then alnum/dash)"
    [[ -n "$src" && -r "$src" ]] \
        || die "register_hook: hook script unreadable ($src)"
    [[ -d "$SMART_NODE_HOOKS_DIR/available" && -d "$SMART_NODE_HOOKS_DIR/enabled" ]] \
        || die "register_hook: hooks dirs missing (run B01-core first)"
    local avail="$SMART_NODE_HOOKS_DIR/available/$name"
    install -m 0750 -g "$SMART_NODE_GROUP" "$src" "$avail"
    # Relative target so the tree stays relocatable.
    ln -sfn "../available/$name" "$SMART_NODE_HOOKS_DIR/enabled/$event.$name"
    manifest_record file "$avail"
    manifest_record file "$SMART_NODE_HOOKS_DIR/enabled/$event.$name"
}

# unregister_hook NAME
# Remove a hook: the available/NAME script and every enabled/*.NAME
# symlink pointing at it. Idempotent. Manifest entries are left in place
# (append-only); uninstall tolerates missing files.
unregister_hook() {
    local name="${1:-}"
    [[ -n "$name" ]] || die "unregister_hook: NAME required"
    rm -f "$SMART_NODE_HOOKS_DIR/available/$name"
    local link
    for link in "$SMART_NODE_HOOKS_DIR/enabled/"*".$name"; do
        [[ -e "$link" || -L "$link" ]] && rm -f "$link"
    done
    return 0
}

# run_hooks EVENT [ARG...]
# Dispatcher: run every enabled hook for lifecycle EVENT, in sorted
# order. Each hook runs with SMART_NODE_HOOK_EVENT exported and ARG...
# passed through. Dangling links / non-executable files are skipped with
# a log line. Returns the number of hooks that failed (0 = all ok, or
# none present) -- so a caller MAY abort on a non-zero pre-install run.
run_hooks() {
    local event="${1:-}"; shift || true
    [[ -n "$event" ]] || die "run_hooks: EVENT required"
    _validate_hook_event "$event"
    local dir="$SMART_NODE_HOOKS_DIR/enabled"
    [[ -d "$dir" ]] || return 0
    local failed=0 hook rc
    for hook in "$dir/$event."*; do
        [[ -e "$hook" ]] || continue          # no match -> glob stays literal
        if [[ ! -x "$hook" ]]; then
            log "hook skipped (not executable): $(basename "$hook")"
            continue
        fi
        log "hook: running $(basename "$hook") [$event]"
        if SMART_NODE_HOOK_EVENT="$event" "$hook" "$@"; then
            :
        else
            rc=$?
            log "hook FAILED (exit $rc): $(basename "$hook")"
            failed=$((failed + 1))
        fi
    done
    return "$failed"
}

# ---- event log -------------------------------------------------------
# The Smart Node action journal. Every meaningful step (stage run,
# companion install, agent activity) is recorded as a row in
# sn_core.events, the journal the whole node reads. log_event is resilient by
# design -- it must never abort its caller, so it degrades through three
# tiers:
#
#   1. PostgreSQL  -- insert into events (the persistence contract at
#      $SMART_NODE_PERSISTENCE_ENV exists and the DB accepts the row)
#   2. buffer file -- append a JSON line to $SMART_NODE_EVENT_BUFFER
#      (Postgres not up yet, or briefly unreachable)
#   3. stderr      -- last resort, if even buffering fails
#
# flush_event_buffer drains tier 2 into the table once Postgres is up
# (called by B02-persistence after the core schema is applied).

# _event_pg_insert ACTOR ETYPE ENTYPE ENID PAYLOAD TS
# Insert one event row. TS empty -> the events.ts default (now()) is
# used; non-empty -> the given timestamp is preserved (buffer flush).
# Returns non-zero if the persistence contract is absent or the insert
# fails -- the caller then falls back to the buffer. Never prints.
_event_pg_insert() {
    local actor="$1" et="$2" ent="$3" eid="$4" payload="$5" ts="$6"
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] || return 1
    (
        # shellcheck source=/dev/null
        . "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null || exit 1
        [[ -n "${SMART_NODE_PG_CLUSTER:-}" && -n "${SMART_NODE_PG_CORE_DB:-}" ]] \
            || exit 1
        # root authenticates as the core role through the `smartnode`
        # ident map; any other caller connects as itself (peer auth 1:1).
        local pg_user
        if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
            pg_user="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
        else
            pg_user="$(id -un)"
        fi
        local cols="actor,event_type,entity_type,entity_id,payload"
        local vals=":'a',:'et',NULLIF(:'ent',''),NULLIF(:'eid',''),:'pl'::jsonb"
        if [[ -n "$ts" ]]; then
            cols="$cols,ts"
            vals="$vals,:'ts'::timestamptz"
        fi
        # sn_core.events, QUALIFIED, and it stays qualified. An unqualified
        # name is resolved by search_path ("$user", public), which is a
        # property of the session and not of the intent -- that is how this
        # journal ended up split in two for three releases, with the whole
        # install history landing somewhere no reader on the node opens.
        # sn_core.events.actor is a FK to sn_core.actors, so the actor row is
        # ensured first, by the same prefix -> layer rule that
        # events_emit._ensure_actor applies.
        #
        # The statements are fed on stdin (-f -), NOT via -c: psql performs
        # :'var' interpolation only when scanning a script/stdin, never on
        # a -c string (a -c string is sent verbatim -> syntax error).
        {
            printf '%s\n' \
                "INSERT INTO sn_core.actors (id, display_name, type, layer)" \
                "SELECT :'a', :'a', split_part(:'a', ':', 1)," \
                "       CASE split_part(:'a', ':', 1)" \
                "           WHEN 'user'         THEN 'human'" \
                "           WHEN 'human'        THEN 'human'" \
                "           WHEN 'agent'        THEN 'specialist'" \
                "           WHEN 'specialist'   THEN 'specialist'" \
                "           WHEN 'orchestrator' THEN 'orchestrator'" \
                "           WHEN 'guard'        THEN 'guard'" \
                "           ELSE 'system' END" \
                "ON CONFLICT (id) DO NOTHING;"
            printf 'INSERT INTO sn_core.events (%s) VALUES (%s);\n' "$cols" "$vals"
        } | \
        PGCONNECT_TIMEOUT=3 psql --cluster "$SMART_NODE_PG_CLUSTER" \
            -U "$pg_user" -d "$SMART_NODE_PG_CORE_DB" \
            -X -q -v ON_ERROR_STOP=1 \
            -v a="$actor" -v et="$et" -v ent="$ent" -v eid="$eid" \
            -v pl="$payload" -v ts="$ts" \
            -f - >/dev/null 2>&1
    )
}

# log_event ACTOR EVENT_TYPE [ENTITY_TYPE] [ENTITY_ID] [PAYLOAD_JSON]
# Record one event. ACTOR must be an id present in the actors table
# (e.g. system:installer). PAYLOAD is a JSON object string; absent or
# invalid payload becomes '{}'. Always returns 0 -- safe under set -e.
log_event() {
    local actor="${1:-}" et="${2:-}" ent="${3:-}" eid="${4:-}" payload="${5:-}"
    if [[ -z "$actor" || -z "$et" ]]; then
        echo "[event] log_event: ACTOR and EVENT_TYPE required" >&2
        return 0
    fi
    [[ -n "$payload" ]] || payload='{}'
    # Payload must be valid JSON -- otherwise drop to an empty object.
    if ! printf '%s' "$payload" | jq -e . >/dev/null 2>&1; then
        payload='{}'
    fi
    # Tier 1: PostgreSQL.
    if _event_pg_insert "$actor" "$et" "$ent" "$eid" "$payload" ""; then
        return 0
    fi
    # Tier 2: buffer file -- one JSON line, original timestamp preserved.
    local ts line
    ts="$(date -Iseconds)"
    if line=$(jq -cn \
            --arg actor "$actor" --arg et "$et" --arg ent "$ent" \
            --arg eid "$eid" --argjson pl "$payload" --arg ts "$ts" \
            '{actor:$actor,event_type:$et,entity_type:$ent,entity_id:$eid,payload:$pl,ts:$ts}' \
            2>/dev/null) \
        && printf '%s\n' "$line" >> "$SMART_NODE_EVENT_BUFFER" 2>/dev/null; then
        return 0
    fi
    # Tier 3: stderr.
    echo "[event] $ts actor=$actor type=$et entity=$ent/$eid payload=$payload" >&2
    return 0
}

# flush_event_buffer
# Drain $SMART_NODE_EVENT_BUFFER into the events table -- called once the
# DB is up (B02-persistence, after the core schema is applied). Each
# buffered line keeps its original timestamp. Lines that still fail are
# kept for a later retry; once fully drained the buffer file is removed.
# Returns 0 if the buffer is empty or fully drained, non-zero if any line
# is still pending.
flush_event_buffer() {
    [[ -s "$SMART_NODE_EVENT_BUFFER" ]] || return 0
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] || return 1   # DB not up yet
    local remain ok=0 fail=0 line
    remain=$(mktemp)
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -n "$line" ]] || continue
        local actor et ent eid pl ts
        actor=$(jq -r '.actor       // ""' <<<"$line" 2>/dev/null)
        et=$(jq    -r '.event_type  // ""' <<<"$line" 2>/dev/null)
        ent=$(jq   -r '.entity_type // ""' <<<"$line" 2>/dev/null)
        eid=$(jq   -r '.entity_id   // ""' <<<"$line" 2>/dev/null)
        pl=$(jq    -c '.payload     // {}' <<<"$line" 2>/dev/null)
        ts=$(jq    -r '.ts          // ""' <<<"$line" 2>/dev/null)
        [[ -n "$pl" ]] || pl='{}'
        if [[ -n "$actor" && -n "$et" ]] \
                && _event_pg_insert "$actor" "$et" "$ent" "$eid" "$pl" "$ts"; then
            ok=$((ok + 1))
        else
            printf '%s\n' "$line" >> "$remain"
            fail=$((fail + 1))
        fi
    done < "$SMART_NODE_EVENT_BUFFER"
    if (( fail == 0 )); then
        rm -f "$SMART_NODE_EVENT_BUFFER" "$remain"
    else
        cat "$remain" > "$SMART_NODE_EVENT_BUFFER"
        rm -f "$remain"
    fi
    log "event buffer flush: $ok inserted, $fail still buffered"
    (( fail == 0 ))
}
