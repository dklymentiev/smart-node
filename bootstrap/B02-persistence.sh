#!/bin/bash
#
# B02-persistence.sh -- Smart Node installer, Layer C bootstrap stage B02.
#
# Runs INSIDE the target host as root. Idempotent. Depends on B01-core.
#
# Installs and fully owns the Smart Node data engine: a managed PostgreSQL
# cluster with pgvector. Smart Node is an appliance -- it assumes a clean,
# dedicated host and owns the machine, so B02 installs its own Postgres
# (managed mode). There is no shared / external-DB mode: a pre-existing
# PostgreSQL on the host is an abort condition, not a co-tenant.
#
# Creates:
#   - the PGDG apt repository (apt.postgresql.org) + its signing key --
#     pgvector is not packaged in Debian; PGDG is the PostgreSQL project's
#     official apt channel and ships postgresql-NN-pgvector for every major
#   - apt packages: postgresql-common, postgresql-NN, postgresql-NN-pgvector
#   - a dedicated cluster `NN/smartnode` with data_directory under the
#     Smart Node tree: /opt/smart-node/postgres (postgres:postgres 0700)
#   - systemd unit postgresql@NN-smartnode (enabled + started)
#   - peer-auth: pg_hba.conf `local all all` -> `peer map=smartnode`;
#     pg_ident.conf gets the `smartnode` map (root -> smartnode role) plus
#     any lines queued by create_service_user in pg-ident.pending
#   - core database `smartnode` owned by role `smartnode`
#   - schema `smartnode_ext` with the `vector` extension (pgvector)
#   - core schema (from the B02-schema.sql sidecar): the `events`
#     (immutable, append-only log) + `actors` (registry) tables, seeded
#     with the `system:installer` actor
#   - sn_core + sn_config schema layer (from the B02-sn-core-schema.sql +
#     B02-sn-core-seed.sql sidecars): the session-first AI
#     gateway tables (sn_core.ai_sessions, ai_session_messages) + the
#     sn_config registry, plus the minimal seed (default tenant id=1,
#     model_pricing, cli_providers claude-cli, default budget, retention)
#     and the additive public.messages.ai_session_id FK column. Baked end
#     state of core-api migrations 001-011 -- no migration runner.
#   - /etc/smart-node/persistence.env -- the Postgres connection contract
#     consumed by later stages and lib.sh log_event()
#   - "Stage B02 -- persistence" section in the install report
#   - /var/lib/smart-node/stages/B02.state
#
# Ownership: every object carries the `smartnode` prefix -- database
# `smartnode`, role `smartnode`, cluster `smartnode`, schema
# `smartnode_ext`, ident map `smartnode`.
#
# B02 also drains the pre-DB event buffer into the events table and emits
# its own `stage.done` event, via lib.sh log_event() / flush_event_buffer
# (provided by lib.sh).
#
# PostgreSQL major: pinned via SMART_NODE_PG_MAJOR (default 16 -- current
# stable, matches the pgvector image). Postgres and pgvector both
# come from PGDG, which carries every supported major, so the pin is a
# free choice rather than whatever the base distro happens to ship.
#
# Usage:
#   B02-persistence.sh            full run: probe -> intent -> probe -> mark
#   B02-persistence.sh --verify   idempotent post-check; exit 0 on full
#                                 pass, N (fail count) with [VERIFY-FAIL]
#   B02-persistence.sh --status   read /var/lib/smart-node/stages/B02.state;
#                                 exit 0=done 1=failed 2=missing
#   B02-persistence.sh --help     this message
#
#
# Exits 0 on success.

set -euo pipefail

# --help works without lib.sh -- handle it before anything else.
case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="B02"
STAGE_NAME="persistence"
STAGE_VERSION="0.6.7"

# Resolve script dir so the SQL sidecars can be found.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SCHEMA_FILE="$SCRIPT_DIR/B02-schema.sql"
# sn_core + sn_config schema layer + its minimal seed. The
# baked end-state of core-api migrations 001-011, applied after the kernel
# schema so the session-first AI gateway has sn_core.ai_sessions etc.
SN_CORE_SCHEMA_FILE="$SCRIPT_DIR/B02-sn-core-schema.sql"
SN_CORE_SEED_FILE="$SCRIPT_DIR/B02-sn-core-seed.sql"

# PostgreSQL major. Default 16 (current stable; matches the
# pgvector image). Postgres + pgvector are installed from PGDG, which
# carries every supported major -- override freely.
PG_MAJOR="${SMART_NODE_PG_MAJOR:-16}"
PG_CLUSTER_NAME="smartnode"
PG_CLUSTER="${PG_MAJOR}/${PG_CLUSTER_NAME}"
PG_UNIT="postgresql@${PG_MAJOR}-${PG_CLUSTER_NAME}"

# Cluster data dir under the Smart Node tree -- keeps the install a single
# coherent subtree (/opt/smart-node = native component installs).
PG_DATADIR="/opt/smart-node/postgres"
PG_CONFDIR="/etc/postgresql/${PG_MAJOR}/${PG_CLUSTER_NAME}"

# Core database + its owning role. events/actors land here.
PG_CORE_DB="smartnode"
PG_CORE_ROLE="smartnode"
# Schema that holds extensions, kept out of public for ownership clarity.
PG_EXT_SCHEMA="smartnode_ext"

# Connection contract written for later stages + lib.sh log_event().
PERSISTENCE_ENV="/etc/smart-node/persistence.env"

# ---------- the schema stage runs against a node that is working ----------
#
# On 2026-09-04 B02 wanted an AccessExclusiveLock on sn_config.users while a
# live agent session held that table and was itself waiting to read
# sn_config.contact_channels. Postgres broke the cycle by killing the
# migration and the update died on a bare "deadlock detected". The stand had
# taken the same release minutes earlier and passed, because the stand is
# idle and this node had 973 sessions and a running roster.
#
# WHY BOTH HALVES, and why neither alone. lock_timeout makes the schema stage
# YIELD instead of deadlocking, so it is never chosen as the victim of
# somebody else's cycle and the wait is bounded; but a busy node can keep it
# waiting until the attempts run out. Pausing the wakers removes the traffic
# that creates the contention; but a session already running when they stop
# still holds its locks. The run says which of the two it did.
SCHEMA_LOCK_TIMEOUT="${SMART_NODE_SCHEMA_LOCK_TIMEOUT:-5s}"
SCHEMA_STATEMENT_TIMEOUT="${SMART_NODE_SCHEMA_STATEMENT_TIMEOUT:-300s}"
SCHEMA_APPLY_ATTEMPTS="${SMART_NODE_SCHEMA_APPLY_ATTEMPTS:-5}"
SCHEMA_APPLY_BACKOFF="${SMART_NODE_SCHEMA_APPLY_BACKOFF:-6}"

# The units that wake agents and deliver messages -- the traffic the schema
# stage competes with. They are the product's own unit names, so they are
# named here rather than discovered.
SCHEMA_QUIESCE_UNITS=(smart-node-runner.service smart-node-communication-dispatch.service)

# Filled in by quiesce_wakers/resume_wakers. An update that silently stops
# the agents is its own class of surprise, so the pause is reported: which
# services, and for how long.
QUIESCED_UNITS=()
QUIESCE_STARTED_AT=0
QUIESCE_REPORT="none (no wakers were running)"

# lib.sh is installed by B01-core; B02 has no sidecar fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")        MODE="run" ;;
    --verify)  MODE="verify" ;;
    --status)  MODE="status" ;;
    *)         die "unknown arg: $1 (use --help)" ;;
esac

# ---------- postgres helpers ----------

# pg_has_lsclusters -- true if the postgresql-common tooling is present.
pg_has_lsclusters() { command -v pg_lsclusters >/dev/null 2>&1; }

# pg_cluster_exists -- true if our NN/smartnode cluster is registered.
pg_cluster_exists() {
    pg_has_lsclusters || return 1
    pg_lsclusters --no-header "$PG_MAJOR" "$PG_CLUSTER_NAME" >/dev/null 2>&1
}

# pg_cluster_online -- true if the cluster is registered AND online.
pg_cluster_online() {
    pg_cluster_exists || return 1
    pg_lsclusters --no-header "$PG_MAJOR" "$PG_CLUSTER_NAME" 2>/dev/null \
        | awk '{print $4}' | grep -qx online
}

# pg_cluster_port -- echo the port the cluster listens on.
pg_cluster_port() {
    pg_lsclusters --no-header "$PG_MAJOR" "$PG_CLUSTER_NAME" 2>/dev/null \
        | awk '{print $3}'
}

# psql_postgres / psql_core -- run psql against the cluster as the
# postgres OS user (cluster superuser via peer auth). ON_ERROR_STOP so a
# failed statement is a non-zero exit, not a silent partial.
psql_postgres() {
    runuser -u postgres -- psql --cluster "$PG_CLUSTER" \
        -d postgres -v ON_ERROR_STOP=1 -X "$@"
}
psql_core() {
    runuser -u postgres -- psql --cluster "$PG_CLUSTER" \
        -d "$PG_CORE_DB" -v ON_ERROR_STOP=1 -X "$@"
}

# name_relation OID -- resolve a relation oid to schema.table. The deadlock
# message names oids ("relation 17246 of database 16385") and an operator
# should not have to run that lookup by hand at two in the morning.
name_relation() {
    psql_core -tAc "SELECT c.oid::regclass::text FROM pg_class c WHERE c.oid = $1" 2>/dev/null
}

# report_lock_blockers ERROR_TEXT -- say which relation and which process
# stopped the schema stage. A bare deadlock line tells an operator nothing
# about who to talk to.
report_lock_blockers() {
    local err="${1:-}" oid name line
    for oid in $(grep -oE 'relation [0-9]+' <<< "$err" | awk '{print $2}' | sort -u); do
        name=$(name_relation "$oid")
        [[ -n "$name" ]] && log "    relation $oid is $name"
    done
    # Who holds relation locks in this database right now. The session that
    # blocked us is often still there; when it is not, an empty list is
    # itself the answer.
    while IFS= read -r line; do
        [[ -n "$line" ]] && log "    lock holder: $line"
    done < <(psql_core -tA -F' | ' -c "
        SELECT a.pid,
               a.state,
               coalesce(c.oid::regclass::text, '-'),
               left(regexp_replace(a.query, '\s+', ' ', 'g'), 100)
          FROM pg_locks l
          JOIN pg_stat_activity a ON a.pid = l.pid
          LEFT JOIN pg_class c ON c.oid = l.relation
         WHERE l.granted
           AND l.locktype = 'relation'
           AND a.datname = current_database()
           AND a.pid <> pg_backend_pid()
         ORDER BY a.query_start
         LIMIT 20" 2>/dev/null)
}

# Read a named numeric setting from the service process that is running before
# the update. A unit may use an EnvironmentFile, so `systemctl show Environment`
# alone misses the effective value. On a clean install there is no process.
legacy_unit_number() {
    local unit="$1" key="$2" fallback="$3" pid value
    pid=$(systemctl show "$unit" -p MainPID --value 2>/dev/null || true)
    if [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/environ" ]]; then
        value=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=\([0-9][0-9]*\)$/\1/p" | head -1)
        if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then printf '%s' "$value"; return; fi
    fi
    value="${!key:-}"
    if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then printf '%s' "$value"; else printf '%s' "$fallback"; fi
}

legacy_turn_seconds() {
    local value
    value=$(legacy_unit_number smart-node-gateway.service SN_TURN_MAX_SECONDS 0)
    if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then printf '%s' "$value"; return; fi
    legacy_unit_number smart-node-gateway.service SN_CLI_TURN_TIMEOUT 600
}

# apply_sql_file FILE LABEL -- apply one SQL sidecar inside a single
# transaction, yielding rather than deadlocking when the node is busy
# Bounded retries, every wait logged, and a refusal that names the
# relation and the process instead of dying on a bare deadlock.
apply_sql_file() {
    local file="$1" label="$2"
    local attempt=1 rc out
    while :; do
        rc=0
        # SET rather than PGOPTIONS: runuser does not carry the environment
        # through to psql, and -1 wraps the file in one transaction, so both
        # settings apply to exactly the work being done here.
        out=$( { printf "SET lock_timeout = '%s';\nSET statement_timeout = '%s';\n" \
                     "$SCHEMA_LOCK_TIMEOUT" "$SCHEMA_STATEMENT_TIMEOUT"
                 if [[ "$file" == "$SN_CORE_SEED_FILE" ]]; then
                     # Read the values the OLD running units actually use before
                     # companion updates replace them. Only positive integers
                     # enter SQL; no environment file or secret is printed.
                     printf "SELECT set_config('smart_node.legacy_turn_seconds', '%s', true);\n" \
                         "$(legacy_turn_seconds)"
                     printf "SELECT set_config('smart_node.legacy_wake_hour', '%s', true);\n" \
                         "$(legacy_unit_number smart-node-core-api.service SN_WAKE_CAP_HOUR 30)"
                     printf "SELECT set_config('smart_node.legacy_wake_day', '%s', true);\n" \
                         "$(legacy_unit_number smart-node-core-api.service SN_WAKE_CAP_DAY 150)"
                 fi
                 cat "$file"
               } | psql_core -1 -f - 2>&1 ) || rc=$?
        if (( rc == 0 )); then
            (( attempt > 1 )) && log "$label applied on attempt $attempt"
            return 0
        fi
        if ! grep -qiE 'deadlock detected|due to lock timeout' <<< "$out"; then
            printf '%s\n' "$out" >&2
            die "$label failed, and not on a lock -- the error is above"
        fi
        log "$label could not take its lock (attempt $attempt/$SCHEMA_APPLY_ATTEMPTS)"
        # The NOTICEs are the idempotent "already exists, skipping" wall and say
        # nothing about the lock. Print only what an operator has to read.
        printf '%s\n' "$out" | grep -vE 'NOTICE:' | sed 's/^/    /' >&2
        report_lock_blockers "$out"
        if (( attempt >= SCHEMA_APPLY_ATTEMPTS )); then
            die "$label still could not take its lock after $SCHEMA_APPLY_ATTEMPTS attempts.
       The relations and the processes holding them are named above. Stop the
       work that holds them, or run this stage when the node is quieter.
       Nothing was applied: the transaction rolled back on every attempt."
        fi
        log "waiting ${SCHEMA_APPLY_BACKOFF}s, then attempt $((attempt + 1))"
        sleep "$SCHEMA_APPLY_BACKOFF"
        attempt=$((attempt + 1))
    done
}

# quiesce_wakers / resume_wakers -- pause the agent traffic the schema stage
# competes with, and always put it back. Only units that are actually
# running are touched, so a clean install pauses nothing and says so.
quiesce_wakers() {
    QUIESCED_UNITS=()
    QUIESCE_STARTED_AT=$(date +%s)
    command -v systemctl >/dev/null 2>&1 || return 0
    local unit
    for unit in "${SCHEMA_QUIESCE_UNITS[@]}"; do
        systemctl is-active "$unit" >/dev/null 2>&1 || continue
        if systemctl stop "$unit" >/dev/null 2>&1; then
            QUIESCED_UNITS+=("$unit")
            log "paused $unit for the schema stage"
        else
            log "[WARN] could not stop $unit -- the schema stage runs against live traffic"
        fi
    done
}

resume_wakers() {
    local secs=$(( $(date +%s) - QUIESCE_STARTED_AT ))
    if (( ${#QUIESCED_UNITS[@]} == 0 )); then
        QUIESCE_REPORT="none (no wakers were running)"
        return 0
    fi
    local unit failed=""
    for unit in "${QUIESCED_UNITS[@]}"; do
        if systemctl start "$unit" >/dev/null 2>&1; then
            log "resumed $unit"
        else
            failed="$failed $unit"
            log "[WARN] could not restart $unit -- start it by hand"
        fi
    done
    QUIESCE_REPORT="${QUIESCED_UNITS[*]} paused for ${secs}s"
    [[ -n "$failed" ]] && QUIESCE_REPORT="$QUIESCE_REPORT (NOT restarted:$failed)"
    log "agent traffic: $QUIESCE_REPORT"
    QUIESCED_UNITS=()
    return 0
}

# pg_role_exists / pg_db_exists NAME -- catalog lookups.
pg_role_exists() {
    [[ "$(psql_postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='$1'" 2>/dev/null)" == 1 ]]
}
pg_db_exists() {
    [[ "$(psql_postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='$1'" 2>/dev/null)" == 1 ]]
}

# ---------- modes ----------

# probe_before -- clean-server guard. A re-run (our own cluster present)
# proceeds idempotently; otherwise ANY pre-existing PostgreSQL aborts the
# stage -- Smart Node owns its engine, it never co-tenants.
probe_before() {
    if pg_cluster_exists; then
        log "smart-node cluster $PG_CLUSTER already present -- idempotent re-run"
        return 0
    fi
    local found=""
    # A registered cluster that is not ours.
    if pg_has_lsclusters && [[ -n "$(pg_lsclusters --no-header 2>/dev/null)" ]]; then
        found="an existing cluster (pg_lsclusters)"
    fi
    # A postgresql server package installed.
    if [[ -z "$found" ]]; then
        if dpkg-query -W -f='${Package} ${Status}\n' 'postgresql-[0-9]*' 2>/dev/null \
                | grep -q 'install ok installed'; then
            found="an installed postgresql-NN package"
        fi
    fi
    # Something already listening on the default port.
    if [[ -z "$found" ]] && command -v ss >/dev/null 2>&1; then
        if ss -ltnH 'sport = :5432' 2>/dev/null | grep -q .; then
            found="a process on port 5432"
        fi
    fi
    if [[ -n "$found" ]]; then
        # Loosened 2026-05-27: allow installing on a host that already has the
        # default '16/main' cluster (created automatically by `apt install
        # postgresql-16`, e.g. when system packages are pre-baked into an LXC
        # snapshot for fast test loops). Smart Node drops 16/main if it is
        # empty so its own cluster '$PG_CLUSTER_NAME' can take port 5432.
        # A 16/main with user databases is still rejected -- we don't silently
        # nuke real data.
        log "WARN: pre-existing PostgreSQL detected ($found) -- attempting coexistence"
        if pg_has_lsclusters && pg_lsclusters --no-header 2>/dev/null | grep -q '^16 main '; then
            local user_dbs
            user_dbs=$(runuser -u postgres -- psql --cluster 16/main -tAc \
                "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')" \
                2>/dev/null | tr -d '[:space:]')
            if [[ -z "$user_dbs" ]]; then
                log "default cluster 16/main is empty -- dropping to free port 5432"
                pg_dropcluster --stop 16 main || die "could not drop default cluster 16/main"
            else
                die "cluster 16/main holds user databases ($user_dbs) -- refusing to drop; remove manually if you want Smart Node to take over"
            fi
        fi
    fi
}

PG_PKGS_INSTALLED=()   # packages this run actually installed (for the report)

# PGDG apt repository -- the PostgreSQL project's official channel.
PGDG_KEY="/etc/apt/keyrings/pgdg.asc"
PGDG_LIST="/etc/apt/sources.list.d/pgdg.list"
PGDG_KEY_URL="https://www.postgresql.org/media/keys/ACCC4CF8.asc"

# _ensure_pgdg_repo -- add apt.postgresql.org as an apt source. pgvector
# is not packaged in Debian (base or backports); PGDG is the PostgreSQL
# project's official apt channel and ships postgresql-NN-pgvector for
# every supported major. Idempotent. The key + sources file are recorded
# in the manifest so uninstall removes them. Modern apt reads an
# ASCII-armored key directly via signed-by, so no gnupg/dearmor is needed.
_ensure_pgdg_repo() {
    if [[ -f "$PGDG_KEY" && -f "$PGDG_LIST" ]]; then
        log "PGDG apt repository already configured"
    else
        log "configuring PGDG apt repository ($PGDG_LIST)"
        command -v curl >/dev/null 2>&1 || _install_pkg curl
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$PGDG_KEY" "$PGDG_KEY_URL" \
            || die "failed to fetch the PGDG signing key ($PGDG_KEY_URL) -- network down or apt.postgresql.org unreachable?"
        chmod 0644 "$PGDG_KEY"
        local codename
        codename=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-bookworm}")
        echo "deb [signed-by=$PGDG_KEY] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
            > "$PGDG_LIST"
        manifest_record file "$PGDG_KEY"
        manifest_record file "$PGDG_LIST"
    fi
    apt-get update -qq
}

install_engine() {
    export DEBIAN_FRONTEND=noninteractive
    # apt-get update only if the cache is stale (same policy as B01).
    local cache_age=999999
    if [[ -f /var/cache/apt/pkgcache.bin ]]; then
        cache_age=$(( $(date +%s) - $(stat -c %Y /var/cache/apt/pkgcache.bin) ))
    fi
    if (( cache_age > 3600 )); then
        log "apt-get update (cache age ${cache_age}s)"
        apt-get update -qq
    fi

    # pgvector lives only in PGDG -- add that repo before any PG package.
    _ensure_pgdg_repo

    # postgresql-common first: it provides pg_createcluster and the
    # createcluster.conf we flip before the server package is unpacked.
    _install_pkg postgresql-common

    # Stop the server package's postinst from auto-creating a `main`
    # cluster -- B02 creates its own `smartnode` cluster with a relocated
    # data_directory instead.
    local cc=/etc/postgresql-common/createcluster.conf
    if [[ -f "$cc" ]]; then
        if grep -qE '^[[:space:]]*create_main_cluster' "$cc"; then
            sed -i -E 's/^[[:space:]]*create_main_cluster.*/create_main_cluster = false/' "$cc"
        else
            echo "create_main_cluster = false" >> "$cc"
        fi
    fi

    # Pinned major must have both the server and a packaged pgvector.
    local server_pkg="postgresql-${PG_MAJOR}"
    local vector_pkg="postgresql-${PG_MAJOR}-pgvector"
    _assert_pkg_available "$server_pkg"
    _assert_pkg_available "$vector_pkg"
    _install_pkg "$server_pkg"
    _install_pkg "$vector_pkg"
}

# _assert_pkg_available PKG -- die unless apt has an installable candidate.
_assert_pkg_available() {
    local pkg="$1" cand
    cand=$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/{print $2}')
    if [[ -z "$cand" || "$cand" == "(none)" ]]; then
        die "package '$pkg' has no installable candidate -- PGDG repo unreachable, or PG major $PG_MAJOR not carried by PGDG. Check $PGDG_LIST and set SMART_NODE_PG_MAJOR to a supported major."
    fi
}

# _install_pkg PKG -- install PKG if missing; record it in the manifest.
_install_pkg() {
    local pkg="$1"
    if dpkg-query -W -f='${Status}\n' "$pkg" 2>/dev/null \
            | grep -q 'install ok installed'; then
        return 0
    fi
    log "installing: $pkg"
    apt-get install -y -qq "$pkg"
    manifest_record package "$pkg"
    PG_PKGS_INSTALLED+=("$pkg")
}

create_cluster() {
    if pg_cluster_exists; then
        log "cluster $PG_CLUSTER already exists"
    else
        # /opt/smart-node exists (B01). pg_createcluster creates the
        # data dir and runs initdb. data-checksums: a sane appliance
        # default, only settable at init time.
        log "creating cluster $PG_CLUSTER at $PG_DATADIR"
        pg_createcluster --datadir "$PG_DATADIR" \
            "$PG_MAJOR" "$PG_CLUSTER_NAME" \
            -- --data-checksums --encoding=UTF8 >/dev/null
        manifest_record pg-cluster "$PG_CLUSTER"
    fi
    # The setgid `smart-node` group on /opt/smart-node would otherwise be
    # inherited by the data dir. Postgres requires the data dir owned by
    # its own user, mode 0700 -- force it regardless of how it was made.
    chown -R postgres:postgres "$PG_DATADIR"
    chmod 0700 "$PG_DATADIR"
}

# ensure_line FILE LINE -- append LINE to FILE if not already present.
ensure_line() {
    local file="$1" line="$2"
    grep -qxF "$line" "$file" 2>/dev/null || echo "$line" >> "$file"
}

configure_auth() {
    local hba="$PG_CONFDIR/pg_hba.conf"
    local ident="$PG_CONFDIR/pg_ident.conf"
    [[ -f "$hba"   ]] || die "pg_hba.conf missing ($hba) -- cluster not created?"
    [[ -f "$ident" ]] || die "pg_ident.conf missing ($ident) -- cluster not created?"

    # Local socket auth: every connection is peer-authenticated and the OS
    # user is resolved to a DB role through the shared `smartnode` ident
    # map. The `local all postgres peer` line (1:1, no map) is left as-is
    # so the cluster superuser keeps working.
    if ! grep -qE '^local[[:space:]]+all[[:space:]]+all[[:space:]]+peer[[:space:]]+map=smartnode\b' "$hba"; then
        sed -i -E 's/^(local[[:space:]]+all[[:space:]]+all[[:space:]]+)peer[[:space:]]*$/\1peer map=smartnode/' "$hba"
    fi

    # `smartnode` ident map. root runs the bootstrap stages, so it maps to
    # the core role; companion service users were queued by
    # create_service_user into pg-ident.pending -- apply them now.
    ensure_line "$ident" "# Smart Node component peer-auth map (managed by B02-persistence)."
    ensure_line "$ident" "smartnode  root  $PG_CORE_ROLE"
    local pending="$SMART_NODE_STATE_DIR/pg-ident.pending"
    if [[ -f "$pending" ]]; then
        local l
        while IFS= read -r l; do
            [[ -n "$l" ]] || continue
            ensure_line "$ident" "$l"
        done < "$pending"
        log "applied queued ident lines from $pending"
    else
        log "no pg-ident.pending -- no companion accounts queued yet"
    fi
}

start_cluster() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable "$PG_UNIT" >/dev/null 2>&1 \
            || log "could not enable $PG_UNIT (continuing)"
        if systemctl is-active "$PG_UNIT" >/dev/null 2>&1; then
            # Already running -- pick up any pg_hba / pg_ident edits.
            systemctl reload "$PG_UNIT" >/dev/null 2>&1 \
                || systemctl restart "$PG_UNIT" >/dev/null 2>&1 \
                || die "failed to reload $PG_UNIT"
        else
            systemctl start "$PG_UNIT" >/dev/null 2>&1 \
                || die "failed to start $PG_UNIT"
        fi
        manifest_record systemd-unit "$PG_UNIT"
    else
        # No systemd (build sandbox) -- drive the cluster directly.
        if pg_cluster_online; then
            pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER_NAME" reload >/dev/null 2>&1 || true
        else
            pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER_NAME" start >/dev/null 2>&1 \
                || die "failed to start cluster $PG_CLUSTER"
        fi
    fi
    # Wait for the cluster to accept connections.
    local port; port=$(pg_cluster_port)
    local i
    for i in $(seq 1 30); do
        if pg_isready -p "${port:-5432}" -q 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    die "cluster $PG_CLUSTER did not become ready within 30s"
}

create_core_db() {
    # Core role: LOGIN, not superuser. Owns the core DB; the migration creates the
    # events/actors tables under it.
    if pg_role_exists "$PG_CORE_ROLE"; then
        log "role $PG_CORE_ROLE already exists"
    else
        log "creating role $PG_CORE_ROLE"
        psql_postgres -c "CREATE ROLE \"$PG_CORE_ROLE\" WITH LOGIN;"
    fi
    # Core database. CREATE DATABASE cannot run inside a txn block, so it
    # is gated from the shell rather than a DO block.
    if pg_db_exists "$PG_CORE_DB"; then
        log "database $PG_CORE_DB already exists"
    else
        log "creating database $PG_CORE_DB (owner $PG_CORE_ROLE)"
        psql_postgres -c "CREATE DATABASE \"$PG_CORE_DB\" OWNER \"$PG_CORE_ROLE\";"
    fi
    # pgvector lives in its own schema -- ownership clarity, and this also
    # validates that the packaged pgvector actually loads in this cluster.
    log "ensuring schema $PG_EXT_SCHEMA + vector extension in $PG_CORE_DB"
    psql_core -c "CREATE SCHEMA IF NOT EXISTS \"$PG_EXT_SCHEMA\";"
    psql_core -c "CREATE EXTENSION IF NOT EXISTS vector SCHEMA \"$PG_EXT_SCHEMA\";"
    # pg_trgm: the desk lookups match a conversation key by suffix (LIKE '%:desk:<id>'), which only a
    # trigram index can serve. Created here because the schema file runs as a role without the right.
    psql_core -c "CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA \"$PG_EXT_SCHEMA\";"
    psql_core -c "GRANT USAGE ON SCHEMA \"$PG_EXT_SCHEMA\" TO \"$PG_CORE_ROLE\";"
}

check_table_ownership() {
    # Pre-flight: find tables in sn_core and sn_config that are NOT owned by
    # the smartnode role. B02-sn-core-schema.sql runs under SET ROLE smartnode;
    # ALTER TABLE on a postgres-owned table fails there with a permission error
    # that halts the update. This check runs as the postgres superuser (peer
    # auth) so it can see all owners before any migration starts.
    #
    # If any misowned tables are found, the exact ALTER TABLE SQL is printed
    # and the update aborts with a non-zero exit. The operator runs those
    # commands as the postgres OS user and re-runs the update. This check covers
    # tables such as sn_core.message_attachments that an older migration may
    # have left owned by postgres.
    # psql_core connects to $PG_CORE_DB (the "smartnode" database).
    # pg_tables is per-database; sn_core and sn_config only exist in
    # the smartnode database, so the query returns nothing when run
    # against the "postgres" database (which psql_postgres() uses).
    local out
    out=$(psql_core -tAc "
        SELECT schemaname || '.' || tablename
          FROM pg_tables
         WHERE schemaname IN ('sn_core', 'sn_config')
           AND tableowner <> '$PG_CORE_ROLE'
         ORDER BY schemaname, tablename
    " 2>/dev/null) || {
        log "WARN: could not run table-ownership pre-flight (postgres peer auth may not be ready)"
        return 0
    }
    [[ -z "$out" ]] && {
        log "table ownership pre-flight: all sn_core/sn_config tables owned by $PG_CORE_ROLE -- ok"
        return 0
    }
    {
        printf '\n'
        printf '[B02] TABLE OWNERSHIP PRE-FLIGHT FAILED\n'
        printf '[B02] The following tables are not owned by %s:\n' "$PG_CORE_ROLE"
        while IFS= read -r tbl; do printf '[B02]   %s\n' "$tbl"; done <<<"$out"
        printf '[B02]\n'
        printf '[B02] B02-sn-core-schema.sql runs as SET ROLE %s and cannot ALTER them.\n' "$PG_CORE_ROLE"
        printf '[B02] Run these commands as the postgres OS user, then re-run the update:\n'
        printf '[B02]\n'
        printf '[B02]   sudo -u postgres psql -d %s <<'"'"'SQL'"'"'\n' "$PG_CORE_DB"
        while IFS= read -r tbl; do
            printf '[B02]   ALTER TABLE %s OWNER TO %s;\n' "$tbl" "$PG_CORE_ROLE"
        done <<<"$out"
        printf '[B02]   SQL\n'
        printf '\n'
    } >&2
    die "table ownership pre-flight failed -- see above for the SQL to run as postgres"
}

apply_core_schema() {
    # The immutable event log + actor registry -- core kernel schema, so
    # every bootstrap action is attributable. Applied from the
    # B02-schema.sql sidecar in a single transaction; the file is
    # idempotent (CREATE ... IF NOT EXISTS, ON CONFLICT DO NOTHING).
    [[ -r "$SCHEMA_FILE" ]] \
        || die "core schema sidecar not found ($SCHEMA_FILE) -- push it next to B02-persistence.sh"
    log "applying core schema (events + actors) to database $PG_CORE_DB"
    apply_sql_file "$SCHEMA_FILE" "core schema"
}

apply_sn_core_schema() {
    # The sn_core + sn_config schema layer needed by the session-first AI
    # gateway (ai_sessions, ai_session_messages, the sn_config registry) plus
    # its minimal seed (default tenant, model_pricing, cli_providers, budget,
    # retention). Baked end state of core-api migrations 001-011 -- NOT a
    # migration runner. Applied AFTER apply_core_schema so:
    #   - the schema file never touches public.messages/message_state
    #     (the kernel tables B02-schema.sql owns -- the 007/008a conflict);
    #   - the seed's public.messages.ai_session_id FK can target
    #     sn_core.ai_sessions, which the schema file just created.
    # Both files are idempotent (CREATE ... IF NOT EXISTS, guarded
    # constraints, ON CONFLICT DO NOTHING), each applied in one transaction.
    [[ -r "$SN_CORE_SCHEMA_FILE" ]] \
        || die "sn_core schema sidecar not found ($SN_CORE_SCHEMA_FILE) -- push it next to B02-persistence.sh"
    [[ -r "$SN_CORE_SEED_FILE" ]] \
        || die "sn_core seed sidecar not found ($SN_CORE_SEED_FILE) -- push it next to B02-persistence.sh"
    log "applying sn_core + sn_config schema layer to database $PG_CORE_DB"
    apply_sql_file "$SN_CORE_SCHEMA_FILE" "sn_core schema"
    log "applying sn_core + sn_config minimal seed to database $PG_CORE_DB"
    apply_sql_file "$SN_CORE_SEED_FILE" "sn_core seed"
}

write_persistence_env() {
    # The connection contract every later stage + lib.sh log_event() reads
    # to reach the core DB. Not secret (peer auth, no password) -- 0644.
    local port; port=$(pg_cluster_port)
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node persistence contract -- generated by B02-persistence@$STAGE_VERSION."
        echo "# Do not edit by hand. Sourced by later stages and lib.sh log_event()."
        echo "SMART_NODE_PG_MAJOR=$PG_MAJOR"
        echo "SMART_NODE_PG_CLUSTER=$PG_CLUSTER"
        echo "SMART_NODE_PG_PORT=${port:-5432}"
        echo "SMART_NODE_PG_CORE_DB=$PG_CORE_DB"
        echo "SMART_NODE_PG_CORE_ROLE=$PG_CORE_ROLE"
        echo "SMART_NODE_PG_EXT_SCHEMA=$PG_EXT_SCHEMA"
        echo "SMART_NODE_PG_DATADIR=$PG_DATADIR"
        echo "SMART_NODE_PG_HBA=$PG_CONFDIR/pg_hba.conf"
        echo "SMART_NODE_PG_IDENT=$PG_CONFDIR/pg_ident.conf"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$PERSISTENCE_ENV"
    rm -f "$tmp"
    manifest_record file "$PERSISTENCE_ENV"
}

write_report_section() {
    local port; port=$(pg_cluster_port)
    local pkgs="${PG_PKGS_INSTALLED[*]:-(already present)}"
    local body
    body=$(cat <<EOF
**Version:** B02-persistence@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
engine:   PostgreSQL $PG_MAJOR + pgvector (managed, smart-node-owned)
source:   PGDG apt repo (apt.postgresql.org)
cluster:  $PG_CLUSTER  (port ${port:-5432})
datadir:  $PG_DATADIR  (postgres:postgres 0700)
service:  $PG_UNIT
auth:     local peer, ident map 'smartnode'
core db:  $PG_CORE_DB  (owner role $PG_CORE_ROLE)
ext:      vector in schema $PG_EXT_SCHEMA
schema:   events (immutable log) + actors (registry), seed system:installer
sn_core:  ai_sessions + ai_session_messages (session-first gateway)
sn_config: tenants/model_pricing/cli_providers/budgets/... registry
seed:     default tenant(1) + model_pricing + claude-cli + budget
traffic:  $QUIESCE_REPORT
contract: $PERSISTENCE_ENV
packages: $pkgs
\`\`\`

The \`log_event()\` helper stages call to emit events is provided by lib.sh.
EOF
)
    report_section "Stage B02 -- persistence" "$body"
}

run_intent() {
    require_root
    log "stage B02 (persistence) -- start"
    require_stage B01
    log_init "$STAGE_ID"
    probe_before
    install_engine
    create_cluster
    configure_auth
    start_cluster
    create_core_db
    # Pre-flight: any sn_core/sn_config table not owned by smartnode will
    # cause a permission error in apply_sn_core_schema (which runs as SET
    # ROLE smartnode). Check first so the operator gets the exact SQL to fix
    # the ownership rather than a cryptic mid-migration failure.
    check_table_ownership
    # The schema stage is the one part of B02 that takes exclusive locks on
    # tables a working node is using. Pause the wakers around it and
    # put them back even if the apply dies, which is what the trap is for.
    quiesce_wakers
    trap resume_wakers EXIT
    apply_core_schema
    apply_sn_core_schema
    resume_wakers
    trap - EXIT
    write_persistence_env
    # The DB + connection contract now exist -- drain any events buffered
    # by earlier stages into the events table.
    if declare -F flush_event_buffer >/dev/null; then
        flush_event_buffer || log "event buffer not fully drained (will retry later)"
    fi
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=B02-persistence@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=B02-persistence@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    # Record stage completion in the event log now that it exists.
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "stage.done" "stage" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"cluster\":\"$PG_CLUSTER\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "stage B02 (persistence) -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # 1. B01 precondition.
    verify_check "stage B01 done" require_stage B01
    # 2. server package installed.
    verify_check "postgresql-$PG_MAJOR installed" bash -c "
        dpkg-query -W -f='\${Status}\n' 'postgresql-$PG_MAJOR' 2>/dev/null \
            | grep -q 'install ok installed'"
    # 3. pgvector package installed.
    verify_check "postgresql-$PG_MAJOR-pgvector installed" bash -c "
        dpkg-query -W -f='\${Status}\n' 'postgresql-$PG_MAJOR-pgvector' 2>/dev/null \
            | grep -q 'install ok installed'"
    # 4. our cluster registered.
    verify_check "cluster $PG_CLUSTER registered" pg_cluster_exists
    # 5. cluster online.
    verify_check "cluster $PG_CLUSTER online" pg_cluster_online
    # 6. data dir is the relocated path.
    verify_check "data dir = $PG_DATADIR" bash -c "
        pg_lsclusters --no-header '$PG_MAJOR' '$PG_CLUSTER_NAME' 2>/dev/null \
            | awk '{print \$6}' | grep -qx '$PG_DATADIR'"
    # 7. data dir ownership + perms.
    verify_check "data dir postgres:postgres 0700" bash -c "
        [[ \$(stat -c '%U:%G %a' '$PG_DATADIR' 2>/dev/null) == 'postgres:postgres 700' ]]"
    # 8. systemd unit active (lenient: a non-systemd host passes).
    verify_check "$PG_UNIT active" bash -c "
        command -v systemctl >/dev/null 2>&1 || exit 0
        systemctl is-active '$PG_UNIT' >/dev/null 2>&1"
    # 9. pgvector available to the cluster.
    verify_check "vector extension available" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d postgres -tAc \
            \"SELECT 1 FROM pg_available_extensions WHERE name='vector'\" 2>/dev/null \
            | grep -qx 1"
    # 10. core role exists.
    verify_check "role $PG_CORE_ROLE exists" pg_role_exists "$PG_CORE_ROLE"
    # 11. core database exists.
    verify_check "database $PG_CORE_DB exists" pg_db_exists "$PG_CORE_DB"
    # 12. core DB owned by the core role.
    verify_check "database $PG_CORE_DB owned by $PG_CORE_ROLE" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d postgres -tAc \
            \"SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname='$PG_CORE_DB'\" 2>/dev/null) \
            == '$PG_CORE_ROLE' ]]"
    # 13. vector extension installed in the core DB.
    verify_check "vector extension in $PG_CORE_DB" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM pg_extension WHERE extname='vector'\" 2>/dev/null \
            | grep -qx 1"
    # 14. core schema: the events journal present (sn_core, not public).
    verify_check "events table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_core.events') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 15. core schema: the actor registry present (sn_core, not public).
    verify_check "actors table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_core.actors') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 16. actors.layer CHECK constraint present.
    verify_check "actors layer CHECK constraint present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM pg_constraint WHERE conname='actors_layer_chk'\" 2>/dev/null \
            | grep -qx 1"
    # 17. events immutability trigger present.
    verify_check "events immutability trigger present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM pg_trigger WHERE tgname='events_no_update_delete' AND tgrelid='sn_core.events'::regclass\" 2>/dev/null \
            | grep -qx 1"
    # core schema: messages registry table present.
    verify_check "messages table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('public.messages') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # core schema: message_state work-queue table present.
    verify_check "message_state table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('public.message_state') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # core schema: the three messages-registry triggers attached.
    # Scoped to public.* -- core-api 0.16.0 also carries sn_core.messages/
    # message_state with same-named triggers, so count only the public relations.
    verify_check "messages-registry triggers present (3)" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT count(*) FROM pg_trigger WHERE tgname IN ('messages_no_update_delete','messages_seed_state_trg','message_state_guard_trg') AND tgrelid IN ('public.messages'::regclass, 'public.message_state'::regclass)\" 2>/dev/null \
            | grep -qx 3"
    # 18. seed actor system:installer present.
    verify_check "seed actor system:installer present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_core.actors WHERE id='system:installer'\" 2>/dev/null \
            | grep -qx 1"
    # sn_core/sn_config schema layer:
    # 18a. sn_core.ai_sessions present (session-first gateway core table).
    verify_check "sn_core.ai_sessions table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_core.ai_sessions') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 18b. sn_core.ai_session_messages present.
    verify_check "sn_core.ai_session_messages table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_core.ai_session_messages') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 18c. sn_config registry present (tenants as a proxy for the schema).
    verify_check "sn_config.tenants table exists" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_config.tenants') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 18d. sn_core.events IS REQUIRED by core-api 0.16.0 (events_emit + projector +
    # messages routes read/write sn_core.events/messages/actors; 37 refs in code).
    # This supersedes the pre-0.16 007/008a "no sn_core.events" invariant -- the
    # app moved to the sn_core copies; the public.* events table remains too.
    verify_check "sn_core.events present (0.16.0)" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT to_regclass('sn_core.events') IS NOT NULL\" 2>/dev/null \
            | grep -qx t"
    # 18e. seed: default tenant id=1 present.
    verify_check "seed default tenant (id=1) present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_config.tenants WHERE id=1 AND slug='default'\" 2>/dev/null \
            | grep -qx 1"
    # 18f. seed: model_pricing rows present (>=12 generic catalogue rows).
    verify_check "seed model_pricing rows present" bash -c "
        n=\$(runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT count(*) FROM sn_config.model_pricing\" 2>/dev/null | tr -d '[:space:]')
        [[ \${n:-0} -ge 12 ]]"
    # 18g. seed: claude-cli provider present.
    verify_check "seed cli_providers claude-cli present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_config.cli_providers WHERE slug='claude-cli'\" 2>/dev/null \
            | grep -qx 1"
    verify_check "claude-cli auto-compact safety window present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_config.cli_providers WHERE slug='claude-cli' AND env_vars ? 'CLAUDE_CODE_AUTO_COMPACT_WINDOW'\" 2>/dev/null \
            | grep -qx 1"
    # 18h. context handoff policy: the global row must exist on both fresh
    # installs and update runs.  Without it the gateway's code fallback works,
    # but the panel has no durable setting to edit or audit.  Its VALUE
    # is the owner's: the seed keeps an edited one and the panel edits it, so
    # the check asks for presence within the table's own bounds, never for the
    # 120k seed.  Pinning 120000 stopped example-node's 1.12.45 update at B02.
    verify_check "session context global limit present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_config.session_context_limits WHERE scope_type='global' AND scope_id IS NULL AND threshold_tokens BETWEEN 10000 AND 1000000\" 2>/dev/null \
            | grep -qx 1"
    # 18i. seed: default tenant budget present.
    verify_check "seed default tenant budget present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM sn_config.budgets WHERE scope_type='tenant' AND scope_id='1' AND period='monthly'\" 2>/dev/null \
            | grep -qx 1"
    # 18j. migration-009 public side: public.messages.ai_session_id column.
    verify_check "public.messages.ai_session_id column present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='messages' AND column_name='ai_session_id'\" 2>/dev/null \
            | grep -qx 1"
    # 18k. and its FK to sn_core.ai_sessions.
    verify_check "public.messages ai_session_id FK present" bash -c "
        runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM pg_constraint WHERE conname='messages_ai_session_id_fkey'\" 2>/dev/null \
            | grep -qx 1"
    # 18l. table ownership: all sn_core/sn_config tables owned by smartnode.
    # Surfaces tables that will fail apply_sn_core_schema on the next update
    # so the operator can correct ownership before the schema stage hits them.
    verify_check "sn_core/sn_config table ownership" bash -c "
        n=\$(runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT count(*) FROM pg_tables WHERE schemaname IN ('sn_core','sn_config') AND tableowner <> '$PG_CORE_ROLE'\" 2>/dev/null | tr -d '[:space:]')
        [[ \${n:-1} -eq 0 ]]"
    # 19. pg_hba uses the smartnode ident map.
    verify_check "pg_hba local-all uses map=smartnode" bash -c "
        grep -qE '^local[[:space:]]+all[[:space:]]+all[[:space:]]+peer[[:space:]]+map=smartnode\b' \
            '$PG_CONFDIR/pg_hba.conf'"
    # 20. pg_ident has the root -> core-role mapping.
    verify_check "pg_ident has smartnode map (root)" bash -c "
        grep -qE '^smartnode[[:space:]]+root[[:space:]]+$PG_CORE_ROLE\b' \
            '$PG_CONFDIR/pg_ident.conf'"
    # 21. connection contract written.
    verify_check "persistence.env written" test -r "$PERSISTENCE_ENV"
    # 22. install-report has the B02 section.
    verify_check "install-report has Stage B02 section" \
        bash -c "grep -qFx '## Stage B02 -- persistence' '$SMART_NODE_REPORT'"
    # 23. manifest records the B02 cluster + contract.
    verify_check "manifest records B02 artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"pg-cluster\",\"value\":\"$PG_CLUSTER\",' \"\$m\" &&
        grep -qF '{\"type\":\"file\",\"value\":\"$PERSISTENCE_ENV\",' \"\$m\""
    # 24. PGDG apt repository configured.
    verify_check "PGDG apt repository configured" \
        bash -c "[[ -f '$PGDG_KEY' && -f '$PGDG_LIST' ]]"
}

run_verify_mode() {
    log_init "$STAGE_ID"
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    if (( VERIFY_ERRORS > 0 )); then
        echo "[FAIL] $VERIFY_ERRORS check(s) failed" >&2
        exit "$VERIFY_ERRORS"
    fi
    echo "[OK] $VERIFY_TOTAL/$VERIFY_TOTAL checks passed"
}

run_status_mode() {
    stage_status "$STAGE_ID"
    exit $?
}

case "$MODE" in
    run)    run_intent ;;
    verify) run_verify_mode ;;
    status) run_status_mode ;;
esac
