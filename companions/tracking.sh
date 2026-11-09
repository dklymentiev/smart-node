#!/bin/bash
#
# tracking.sh -- Smart Node installer, Layer C Phase 2 companion: tracking.
#
# Runs INSIDE the target node as root. Idempotent. Hard dependency: B02
# (Postgres). Companion-graph dependency: documents (logical ordering only;
# Planner runs without Doci present).
#
# Installs the tracking companion: the node's work tracker -- a FastAPI
# service backed by PostgreSQL. As of v0.5.0 this is a NATIVE systemd
# service; the Docker container is removed on upgrade.
#
# Why native: a hotfix is git pull + systemctl restart, under 60 seconds.
# On a container it is 3-8 minutes plus operator action. Planner is FastAPI
# on Python 3.12 with PostgreSQL -- both already present on the host. See
#
#
# CUTOVER / ROLLBACK
# Data lives in the B02 cluster `planner` database. Nothing is lost on
# cutover. The existing smart-node-planner Docker container (if any) is
# stopped and removed by migrate_from_container(). The existing tracking.env
# (including the generate-once DB password) is preserved. Rollback: stop
# smart-node-planner.service, re-run the v0.4.0 tracking.sh with a tarball
# (it recreates the container from the same DB).
#
# Creates:
#   - service account smart-node-planner (OS user)
#   - /opt/smart-node/tracking -- install tree (Python source + venv)
#   - /opt/smart-node/tracking/tracking.env (0600 root:root) -- generate-once
#     DB password, runtime config (DATABASE_URL etc.), captured root API key
#   - planner database + role in the B02 cluster (idempotent)
#   - smart-node-planner.service -- FastAPI/uvicorn on 127.0.0.1:8080
#   - /etc/smart-node/planner.env -- service contract (PLANNER_API_URL)
#     read by core-api.sh resolve_planner_url() to locate the service
#   - /etc/smart-node/planner-key-core-api.env (0600, delegating key)
#   - /etc/smart-node/planner-key-runner.env (0600)
#   - /etc/smart-node/planner-key-voice.env (0600)
#   - /etc/cron.d/smart-node-planner-sweep -- hourly staleness sweep (CLI, no HTTP needed)
#   - /opt/smart-node/tracking/mcp_planner.py -- MCP server (stdio, via core-api socket)
#   - companions.d/tracking.json
#   - "Companion tracking" section in the install report
#   - /var/lib/smart-node/stages/tracking.state
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

STAGE_ID="tracking"
STAGE_NAME="tracking"
STAGE_VERSION="0.5.12"

# Where this script lives -- the vendored planner snapshot ships beside it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/planner"

# Service account and layout (follow the core-api / dispatcher pattern).
SERVICE_SHORT="planner"
SERVICE_USER="smart-node-planner"
INSTALL_DIR="/opt/smart-node/tracking"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="/var/log/smart-node/tracking"

# The env file carries the generate-once DB password and derived runtime config.
TRACKING_ENV="$INSTALL_DIR/tracking.env"

# Systemd unit.
SYSTEMD_UNIT="smart-node-planner.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"

# Network: the API binds loopback only.
PLANNER_HTTP_PORT=8080

# The dedicated database + its owner/login role inside the B02 cluster.
PLANNER_DB="planner"
PLANNER_ROLE="planner"

# Per-client credentials.
KEY_DIR="/etc/smart-node"
KEY_ENV_CORE_API="$KEY_DIR/planner-key-core-api.env"
KEY_ENV_RUNNER="$KEY_DIR/planner-key-runner.env"
KEY_ENV_VOICE="$KEY_DIR/planner-key-voice.env"

# Docker container name from v0.4.x (only referenced for upgrade removal).
LEGACY_CT="smart-node-planner"

# MCP server script (stdio; proxies through core-api socket for identity).
MCP_SRC="$SCRIPT_DIR/tracking/mcp_planner.py"
MCP_DST="$INSTALL_DIR/mcp_planner.py"
MCP_PYTHON="/opt/smart-node/dispatcher/venv/bin/python"

# Python interpreter. ensure_python guarantees it exists.
PYTHON_BIN="python3"

# lib.sh is installed by B01-core; the companion has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# Optional integration helper (B2-Integrations phase 4).
INTG_HELPER="$SCRIPT_DIR/_shared/integration.sh"
if [[ -r "$INTG_HELPER" ]]; then
    # shellcheck source=/dev/null
    source "$INTG_HELPER"
fi

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")          MODE="run" ;;
    --verify)    MODE="verify" ;;
    --status)    MODE="status" ;;
    --uninstall) MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)           die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------

# load_contracts -- source the B02 persistence contract.
load_contracts() {
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] && source "$SMART_NODE_PERSISTENCE_ENV"
    return 0
}

# psql_pg -- run psql against the B02 cluster as the postgres OS user.
psql_pg() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d postgres -v ON_ERROR_STOP=1 -X "$@"
}

# ensure_python -- the node needs python3 + the venv module.
ensure_python() {
    command -v "$PYTHON_BIN" >/dev/null 2>&1 \
        || die "python3 not found on the node"
    if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
        log "python venv module missing -- installing python3-venv"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null 2>&1 \
            || die "could not install python3-venv"
    fi
}

# ---------- modes ----------

probe_before() {
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1 \
       && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "smart-node-planner service already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service unit does not -- treating as a resumed run"
    fi
}

ensure_dirs() {
    # 0755: agents traverse this directory to reach the stdio MCP server.
    # The secret in tracking.env is protected by its own 0600 root:root mode.
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$LOG_DIR"
    manifest_record dir "$INSTALL_DIR"
    manifest_record dir "$LOG_DIR"
}

# load_or_generate_secrets -- the generate-once DB password (and API key if
# already captured). A re-run re-reads rather than regenerating so the
# existing planner role password stays valid.
load_or_generate_secrets() {
    if [[ -f "$TRACKING_ENV" ]]; then
        log "tracking.env present -- loading (generate-once)"
        # shellcheck source=/dev/null
        source "$TRACKING_ENV"
        [[ -n "${SMART_NODE_PLANNER_DB_PW:-}" ]] \
            || die "tracking.env present but SMART_NODE_PLANNER_DB_PW missing -- inspect $TRACKING_ENV"
        return 0
    fi
    command -v openssl >/dev/null 2>&1 \
        || die "openssl not found"
    log "generating Planner DB password"
    SMART_NODE_PLANNER_DB_PW=$(openssl rand -hex 24)
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node tracking companion -- generated by tracking.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root. Generate-once password."
        echo "SMART_NODE_PLANNER_DB_PW=$SMART_NODE_PLANNER_DB_PW"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$TRACKING_ENV"
    rm -f "$tmp"
    manifest_record file "$TRACKING_ENV"
}

# create_planner_db -- the dedicated database + its owner/login role.
# Planner self-migrates (python -m planner.install) so no schema is
# applied here. Idempotent: passwords are always reset to the current
# values from tracking.env so a re-run heals a broken password.
create_planner_db() {
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PLANNER_ROLE'")" == 1 ]]; then
        psql_pg -c "ALTER ROLE \"$PLANNER_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PLANNER_DB_PW';"
    else
        log "creating role $PLANNER_ROLE"
        psql_pg -c "CREATE ROLE \"$PLANNER_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PLANNER_DB_PW';"
    fi
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_database WHERE datname='$PLANNER_DB'")" == 1 ]]; then
        log "database $PLANNER_DB already exists"
    else
        log "creating database $PLANNER_DB (owner $PLANNER_ROLE)"
        psql_pg -c "CREATE DATABASE \"$PLANNER_DB\" OWNER \"$PLANNER_ROLE\";"
    fi
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d "$PLANNER_DB" -v ON_ERROR_STOP=1 -X \
        -c "GRANT ALL ON SCHEMA public TO \"$PLANNER_ROLE\";"
}

# install_snapshot -- sync the vendored planner Python source to the install
# tree. Venv and tracking.env are excluded from the sync.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored planner snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/pyproject.toml" && -d "$SRC_SNAPSHOT/src" ]] \
        || die "vendored planner snapshot incomplete ($SRC_SNAPSHOT) -- missing pyproject.toml or src/"
    log "syncing planner snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' \
            --exclude 'tracking.env' \
            --exclude 'mcp_planner.py' \
            --exclude '__pycache__/' \
            --exclude '*.pyc' \
            --exclude 'tests/' \
            --exclude 'docs/' \
            --exclude 'scripts/' \
            --exclude 'Dockerfile*' \
            --exclude 'docker-compose*' \
            --exclude '.github/' \
            --exclude '.git/' \
            --exclude '*.egg-info/' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv ! -name tracking.env ! -name mcp_planner.py \
            -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$INSTALL_DIR"/
    fi
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
    # rsync -a carries the SOURCE modes onto the node, and git does not track
    # directory modes: a checkout made under umask 002 yields 775 directories,
    # which the permissions gate then refuses as writable beyond their owner and
    # undeclared. voice.sh and workflows.sh have normalised this now;
    # tracking and memory inherited the rsync when they stopped being containers
    # in 1.12.25 and did not inherit the line beside it. A customer node stopped
    # at the gate on nineteen directories under src/planner.
    chmod -R go-w "$INSTALL_DIR"
}

# build_venv -- create or reuse the venv and install the planner package.
# pip install . builds from pyproject.toml, identical to the Dockerfile.
build_venv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "creating venv -> $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" \
            || die "venv creation failed ($VENV_DIR)"
    else
        log "venv already present -- reusing"
    fi
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$VENV_DIR"
    log "upgrading pip"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    log "installing planner package"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet "$INSTALL_DIR" \
        || die "pip install of planner failed"
}

# apply_migrations -- run python -m planner.install against the B02 cluster.
# Idempotent: already-applied migrations are skipped.
apply_migrations() {
    local pg_port="${SMART_NODE_PG_PORT:-5432}"
    local db_url="postgresql://${PLANNER_ROLE}:${SMART_NODE_PLANNER_DB_PW}@127.0.0.1:${pg_port}/${PLANNER_DB}"
    log "applying planner migrations"
    PLANNER_MIGRATIONS_DIR="$INSTALL_DIR/migrations" \
    DATABASE_URL="$db_url" \
    PLANNER_AUTH_MODE=apikey \
        "$VENV_DIR/bin/python" -m planner.install \
        || die "planner migration failed -- check DB connectivity and migrations/"
}

# resolve_core_api_url -- the URL native processes use to reach core-api.
# core-api binds 0.0.0.0; native callers always use 127.0.0.1.
# Port is overridable via SMART_NODE_CORE_API_PORT for non-standard layouts.
# Echoes "http://127.0.0.1:<port>" on stdout. Called at install time.
resolve_core_api_url() {
    local port
    port="${SMART_NODE_CORE_API_PORT:-8030}"
    echo "http://127.0.0.1:${port}"
}

# write_tracking_env -- write (or refresh) tracking.env with all runtime config.
# The generate-once DB password is preserved; only derived config is refreshed.
write_tracking_env() {
    local pg_port="${SMART_NODE_PG_PORT:-5432}"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node tracking companion -- generated by tracking.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root."
        echo "# Password: generate-once. Config: refreshed on each run."
        echo ""
        echo "# Generate-once DB password."
        echo "SMART_NODE_PLANNER_DB_PW=$SMART_NODE_PLANNER_DB_PW"
        echo ""
        echo "# Runtime config (read by systemd EnvironmentFile and the CLI)."
        echo "DATABASE_URL=postgresql://${PLANNER_ROLE}:${SMART_NODE_PLANNER_DB_PW}@127.0.0.1:${pg_port}/${PLANNER_DB}"
        echo "PLANNER_MIGRATIONS_DIR=$INSTALL_DIR/migrations"
        echo "PLANNER_AUTH_MODE=apikey"
        echo "PLANNER_LOG_LEVEL=info"
        # The wire speaks the three statuses (queued / in_progress / closed with
        # wait_reason and close_outcome). The legacy one-word names stay
        # accepted on input and readable as `stage` on every task.
        echo "PLANNER_STATUS_COMPAT=new"
        # core-api notify endpoint: the planner wakes agents by POSTing here
        # after every status transition and assignee change. Without this the
        # planner is silently deaf to task events.
        echo "PLANNER_NOTIFY_URL=$(resolve_core_api_url)/api/notify"
        # Preserve the root API key if it was already captured.
        if [[ -n "${SMART_NODE_PLANNER_API_KEY:-}" ]]; then
            echo ""
            echo "# Root API key: captured on first boot, preserved on re-run."
            echo "SMART_NODE_PLANNER_API_KEY=$SMART_NODE_PLANNER_API_KEY"
        fi
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$TRACKING_ENV"
    rm -f "$tmp"
    manifest_record file "$TRACKING_ENV"
}

# write_planner_contract -- write /etc/smart-node/planner.env so core-api
# can resolve the native planner URL without querying Docker.
# core-api.sh reads PLANNER_API_URL from this file via resolve_planner_url().
write_planner_contract() {
    local contract_dir="/etc/smart-node"
    local contract="$contract_dir/planner.env"
    install -d -m 0755 -o root -g root "$contract_dir"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node tracking (planner) service contract -- generated by tracking.sh@$STAGE_VERSION."
        echo "# Read by core-api.sh resolve_planner_url() to locate the native service."
        echo "PLANNER_API_URL=http://127.0.0.1:${PLANNER_HTTP_PORT}"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$contract"
    rm -f "$tmp"
    manifest_record file "$contract"
    log "wrote planner service contract -> $contract"
}

# migrate_from_container -- stop and remove the legacy Docker container from
# v0.4.x. Non-fatal: a fresh node has no container; missing docker is fine.
# The database is in B02 and is preserved.
migrate_from_container() {
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    if docker ps -aq -f "name=^${LEGACY_CT}\$" 2>/dev/null | grep -q .; then
        log "removing legacy Docker container '$LEGACY_CT' (upgrade from v0.4.x)"
        docker rm -f "$LEGACY_CT" >/dev/null 2>&1 || true
    fi
}

# render_unit -- write the planner systemd unit to a file.
render_unit() {
    local pg_cluster pg_unit
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    [[ -n "$pg_cluster" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    pg_unit="postgresql@${pg_cluster/\//-}.service"

    cat > "$1" <<EOF
[Unit]
Description=Smart Node tracking companion (task queue, FastAPI on 127.0.0.1:${PLANNER_HTTP_PORT})
After=network-online.target $pg_unit
Wants=network-online.target
Requires=$pg_unit

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
EnvironmentFile=$TRACKING_ENV
# The notify to core-api is raised for the node owner and proved with the
# service credential; both are written by the core-api companion. The credential
# file is root-only and systemd reads it before dropping to the service user.
# '-' so a node without them still serves tasks; the planner then logs each
# notify it could not send, naming the missing setting.
EnvironmentFile=-/etc/smart-node/operator.env
EnvironmentFile=-/etc/smart-node/core-api-service-auth.env
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/uvicorn planner.app:app --host 127.0.0.1 --port ${PLANNER_HTTP_PORT}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
# Also wanted by the database: Requires= stops this unit when the cluster is
# stopped, and nothing would start it again when the cluster comes back.
WantedBy=multi-user.target $pg_unit
EOF
}

install_service() {
    install -d -m 0755 -o root -g root /etc/systemd/system
    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record file "$SYSTEMD_PATH"

    systemctl daemon-reload

    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || log "WARN: could not enable $SYSTEMD_UNIT"

    if systemctl is-active "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "restarting $SYSTEMD_UNIT"
        systemctl restart "$SYSTEMD_UNIT" >/dev/null 2>&1 \
            || die "failed to restart $SYSTEMD_UNIT"
    else
        log "starting $SYSTEMD_UNIT"
        systemctl start "$SYSTEMD_UNIT" >/dev/null 2>&1 \
            || die "failed to start $SYSTEMD_UNIT"
    fi

    manifest_record systemd-unit "$SYSTEMD_UNIT"
}

# wait_for_planner -- poll /health until the API answers or the 120s deadline.
# First boot runs the root API key bootstrap; allow time for that.
wait_for_planner() {
    local deadline=$(( $(date +%s) + 120 ))
    log "waiting for Planner to report healthy (first boot runs API key bootstrap)"
    while (( $(date +%s) < deadline )); do
        if curl -sf "http://127.0.0.1:${PLANNER_HTTP_PORT}/health" \
                >/dev/null 2>&1; then
            log "Planner is healthy"
            return 0
        fi
        sleep 5
    done
    die "Planner did not become healthy within 120s -- check $LOG_DIR/service.log"
}

# capture_api_key -- the app prints the root API key once on first boot
# to stdout (which goes to $LOG_DIR/service.log via the unit's StandardOutput).
# Grep it out and append to tracking.env. Idempotent: skipped if already captured.
capture_api_key() {
    if grep -q '^SMART_NODE_PLANNER_API_KEY=' "$TRACKING_ENV" 2>/dev/null; then
        log "Planner root API key already captured"
        # Load it so write_tracking_env can preserve it.
        # shellcheck source=/dev/null
        source "$TRACKING_ENV"
        return 0
    fi
    local key deadline
    deadline=$(( $(date +%s) + 15 ))
    key=""
    while (( $(date +%s) < deadline )); do
        key=$(grep -oE 'pln_[A-Za-z0-9_-]+' "$LOG_DIR/service.log" 2>/dev/null \
              | head -n1 || true)
        [[ -n "$key" ]] && break
        sleep 1
    done
    if [[ -n "$key" ]]; then
        SMART_NODE_PLANNER_API_KEY="$key"
        # Rewrite tracking.env to include the key (the env var is now set).
        write_tracking_env
        log "captured Planner root API key -> $TRACKING_ENV"
    else
        log "WARN: Planner root API key not found in $LOG_DIR/service.log -- read it from 'journalctl -u $SYSTEMD_UNIT' by hand"
    fi
}

# ensure_planner_actors -- the actors that per-client keys bind to.
# The planner bootstrap seeds `system` only; `runner` and `voice` must be
# inserted before their keys can be minted. Idempotent.
ensure_planner_actors() {
    local a
    for a in runner voice; do
        runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
            -d "$PLANNER_DB" -v ON_ERROR_STOP=1 -X -c \
            "INSERT INTO actors (id, display_name, type) VALUES ('$a', '$a', 'system')
             ON CONFLICT (id) DO NOTHING;" >/dev/null \
            || die "could not ensure the '$a' actor in $PLANNER_DB"
    done
}

# mint_key <label> <actor> <delegate:0|1> -- create one key, echo the plaintext.
# Calls the CLI directly in the venv; DATABASE_URL is sourced from tracking.env.
mint_key() {
    local label="$1" actor="$2" delegate="$3"
    local args=("$VENV_DIR/bin/planner" key create --label "$label" --actor "$actor")
    (( delegate )) && args+=(--delegate)
    # Source tracking.env in a subshell so DATABASE_URL is available to the CLI.
    # `|| true`: if the CLI produces no output (e.g. missing command), the grep
    # returns empty and the caller's die() fires the message rather than set -e
    # killing the script here with no context.
    (
        # shellcheck source=/dev/null
        # set -a: the env file holds bare KEY=value, and `source` alone makes them
        # SHELL variables. The CLI is a child process and sees only exported ones,
        # so without this it ran with no DATABASE_URL and died. In a container the
        # variable was in the process environment and this could not happen.
        set -a
        source "$TRACKING_ENV"
        set +a
        export PLANNER_MIGRATIONS_DIR="$INSTALL_DIR/migrations"
        # stderr is NOT suppressed: swallowing it turned "DATABASE_URL is not set"
        # into "produced no key -- inspect it yourself".
        "${args[@]}"
    ) | grep -oE 'pln_[A-Za-z0-9_-]+' | head -n1 || true
}

# provision_client_key <file> <label> <actor> <delegate> -- generate-once.
MINTED_ANY=0

provision_client_key() {
    local file="$1" label="$2" actor="$3" delegate="$4"
    if [[ -f "$file" ]]; then
        log "planner key for $label already provisioned ($file)"
        return 0
    fi
    local key=""; key=$(mint_key "$label" "$actor" "$delegate") || true
    [[ -n "$key" ]] \
        || die "minting the planner key for $label produced no key -- inspect: $VENV_DIR/bin/planner key list"
    local tmp; tmp=$(mktemp)
    {
        echo "# Planner API key for $label -- written by tracking.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root. Generate-once. To rotate:"
        echo "#   $VENV_DIR/bin/planner key list"
        echo "#   $VENV_DIR/bin/planner key revoke <id>"
        if (( delegate )); then
            echo "# DELEGATING KEY. Holder may author planner work under ANY name."
            echo "# Belongs only to core-api, which authenticates its own callers."
        fi
        echo "PLANNER_API_KEY=$key"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$file"
    rm -f "$tmp"
    manifest_record file "$file"
    MINTED_ANY=1
    log "provisioned planner key for $label -> $file"
}

# refresh_core_api -- restart core-api so it picks up its new planner key,
# and also update PLANNER_API_URL in the unit to point to the native service.
# Non-fatal: tracking's own job is done either way.
refresh_core_api() {
    # Update PLANNER_API_URL in the running core-api unit to point to the native
    # service. This is needed when upgrading from container to native: the existing
    # unit still carries the Docker gateway address. core-api.sh will render the
    # correct URL when re-run (via resolve_planner_url()), but tracking.sh should
    # fix it immediately so the proxy works after this run.
    local unit="$SYSTEMD_PATH" ca_unit="/etc/systemd/system/smart-node-core-api.service"
    local url_changed=0
    if [[ -f "$ca_unit" ]] \
       && grep -q "PLANNER_API_URL=" "$ca_unit" 2>/dev/null; then
        local before after
        before=$(grep 'PLANNER_API_URL=' "$ca_unit" 2>/dev/null || true)
        sed -i "s|Environment=PLANNER_API_URL=http://[^[:space:]]*|Environment=PLANNER_API_URL=http://127.0.0.1:${PLANNER_HTTP_PORT}|g" \
            "$ca_unit" \
            && log "updated PLANNER_API_URL in core-api unit -> http://127.0.0.1:${PLANNER_HTTP_PORT}" \
            || log "WARN: could not update PLANNER_API_URL in core-api unit"
        after=$(grep 'PLANNER_API_URL=' "$ca_unit" 2>/dev/null || true)
        [[ "$before" != "$after" ]] && url_changed=1
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi

    # Restart core-api when the URL changed (upgrade from container to native
    # planner) OR when new per-client keys were minted. Without this restart,
    # core-api runs with the stale address from its last start and the /api/planner
    # proxy is dead until an operator restarts it by hand.
    (( MINTED_ANY || url_changed )) || return 0
    systemctl is-active --quiet smart-node-core-api.service 2>/dev/null || return 0
    log "restarting core-api so it picks up the new PLANNER_API_URL and planner key"
    systemctl restart smart-node-core-api.service 2>/dev/null \
        || { log "WARN: core-api restart failed -- run 'systemctl restart smart-node-core-api' by hand"; return 0; }
    local host deadline
    host="127.0.0.1"; deadline=$(( $(date +%s) + 30 ))
    while (( $(date +%s) < deadline )); do
        if curl -s -o /dev/null --max-time 2 "http://$host:8030/api/health" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    log "WARN: core-api did not answer within 30s -- registrations below may be missing"
}

# reseed_planner_rows -- make core-api write the planner api rows on a fresh node.
#   seed-tools (core-api ExecStartPre) skips them while the planner integration is
#   not registered, and on a clean install core-api starts before this stage
#   registers it; refresh_core_api's restart also runs before the registration.
#   Nobody would restart core-api afterwards, so an HTTP agent on a new node had
#   no planner rows. Restart once, only when the rows are missing: an updated node
#   already has them and keeps its core-api running.
reseed_planner_rows() {
    systemctl is-active --quiet smart-node-core-api.service 2>/dev/null || return 0
    local have
    have=$(runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" -d smartnode -tAX         -c "SELECT count(*) FROM sn_core.tools WHERE name = 'planner_advance'" 2>/dev/null | tr -d '[:space:]')
    [[ "$have" == "0" ]] || return 0
    log "planner api rows missing (planner integration just registered): restarting core-api so its seed writes them"
    systemctl restart smart-node-core-api.service 2>/dev/null         || { log "WARN: core-api restart failed -- run 'systemctl restart smart-node-core-api' by hand"; return 0; }
    local deadline=$(( $(date +%s) + 30 ))
    while (( $(date +%s) < deadline )); do
        curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8030/api/health" 2>/dev/null && return 0
        sleep 1
    done
    log "WARN: core-api did not answer /api/health within 30s after the planner reseed"
}

provision_client_keys() {
    [[ -d "$KEY_DIR" ]] || install -d -m 0755 -o root -g root "$KEY_DIR"
    ensure_planner_actors
    provision_client_key "$KEY_ENV_CORE_API" core-api system 1
    provision_client_key "$KEY_ENV_RUNNER"   runner   runner 0
    provision_client_key "$KEY_ENV_VOICE"    voice    voice  0
    refresh_core_api
}

# install_staleness_cron -- the planner's maintenance sweeps: hourly for stale
# tasks, every five minutes for recurring tasks whose next cycle came due.
# The CLI talks directly to the DB (no API key, no HTTP stack), so this cron
# works even if the planner service is unhealthy. Idempotent.
#
# The recurrence sweep loads the service's own environment (the same files as
# the unit's EnvironmentFile lines) the way systemd reads them, one KEY=value
# line at a time, exported: it wakes assignees, which needs the operator and
# service-auth settings. Not `source`: tracking.env has no `export`, and
# core-api-service-auth.env carries values with spaces that a shell runs as
# commands.
# The hourly line is left as it was. Its plain `source` does not export, so on
# a node it fails with ConfigError (DATABASE_URL is not set) and has never
# escalated anything; making it run changes what happens to every task
# waiting for approval, which is a decision of its own, not part of this one.
install_staleness_cron() {
    local cron_file="/etc/cron.d/smart-node-planner-sweep"
    local log_file="$LOG_DIR/sweep.log"
    local recur_log="$LOG_DIR/recur.log"
    local env_load="for f in $TRACKING_ENV /etc/smart-node/operator.env /etc/smart-node/core-api-service-auth.env; do [ -r \$f ] || continue; while IFS= read -r l; do case \$l in [A-Za-z_]*=*) export \"\$l\";; esac; done < \$f; done"
    touch "$log_file" "$recur_log" 2>/dev/null && chmod 0644 "$log_file" "$recur_log" || true
    cat > "$cron_file" <<EOF
# Planner maintenance sweeps, run as the CLI directly against the DB.
#   sweep: auto-escalate / unblock / remind (hourly)
#   recur: open the next cycle of recurring tasks that came due (every 5 min)
# tracking.sh@$STAGE_VERSION
0 * * * *  root  bash -c 'source $TRACKING_ENV && PLANNER_MIGRATIONS_DIR=$INSTALL_DIR/migrations $VENV_DIR/bin/planner maintenance sweep' >> $log_file 2>&1
*/5 * * * *  root  bash -c '$env_load; PLANNER_MIGRATIONS_DIR=$INSTALL_DIR/migrations $VENV_DIR/bin/planner maintenance recur' >> $recur_log 2>&1
EOF
    chmod 0644 "$cron_file"
    log "planner sweep crons installed -> $cron_file"
}

# install_planner_mcp -- copy the stdio MCP shim from the staging tree.
# mcp_planner.py proxies through core-api's Unix socket (SO_PEERCRED identity;
# see the file-header comment for why stdio, not a shared sidecar).
install_planner_mcp() {
    [[ -f "$MCP_SRC" ]] || die "planner MCP source missing: $MCP_SRC"
    install -m 0755 -o root -g "$SMART_NODE_GROUP" "$MCP_SRC" "$MCP_DST"
    manifest_record file "$MCP_DST"
    log "planner MCP installed -> $MCP_DST"
}

register_mcp_server() {
    declare -F sn_mcp_server_register >/dev/null || return 0
    local args_json
    args_json=$(python3 -c "import json,sys; print(json.dumps([sys.argv[1]]))" "$MCP_DST")
    sn_mcp_server_register \
        --slug      "planner" \
        --name      "Planner" \
        --transport stdio \
        --command   "$MCP_PYTHON" \
        --args      "$args_json" || true
}

write_report_section() {
    local body
    body=$(cat <<EOF
**Version:** tracking@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  Planner (native systemd)
service:    $SYSTEMD_UNIT  (uvicorn on 127.0.0.1:${PLANNER_HTTP_PORT})
source:     $INSTALL_DIR  (from companions/planner/)
venv:       $VENV_DIR
database:   $PLANNER_DB DB + role $PLANNER_ROLE in the B02 cluster
db url:     postgresql://...@127.0.0.1:${SMART_NODE_PG_PORT:-5432}/$PLANNER_DB (scram-sha-256)
env:        $TRACKING_ENV  (0600 root:root, generate-once password)
keys:       $KEY_ENV_CORE_API  (DELEGATING)
            $KEY_ENV_RUNNER
            $KEY_ENV_VOICE
contract:   /etc/smart-node/planner.env
\`\`\`

The node's work tracker -- six-stage task pipeline, objective hierarchy,
projects, worklog and plan references. Bound to 127.0.0.1, no Traefik
route; core-api proxies it at /api/planner.

Upgrade: any smart-node-planner Docker container from v0.4.x is removed
automatically. The database and tracking.env are preserved.
EOF
)
    report_section "Companion tracking" "$body"
}

run_intent() {
    require_root
    log "companion tracking -- start (v$STAGE_VERSION native)"
    require_stage B02
    require_stage B03
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    ensure_python
    probe_before
    ensure_dirs
    create_service_user "$SERVICE_SHORT" >/dev/null
    load_or_generate_secrets
    create_planner_db
    install_snapshot
    build_venv
    apply_migrations
    write_tracking_env
    migrate_from_container
    write_planner_contract
    install_service
    wait_for_planner
    capture_api_key
    provision_client_keys
    install_staleness_cron
    install_planner_mcp
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,documents"
    write_report_section
    register_mcp_server
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=tracking@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=tracking@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"planner\",\"native\":true,\"checks\":$VERIFY_TOTAL}"
    fi
    # B2-Integrations: declare planner, node-admin, and contacts integrations.
    # (Identical to v0.4.0 -- the integration contracts are transport-independent.)
    # Derive the core-api URL once for all three registrations so the endpoint and
    # the usage text shown to agents both reflect the actual gateway.
    local _core_api_url; _core_api_url=$(resolve_core_api_url)
    if declare -F sn_integration_register >/dev/null; then
        local tools_tmp usage_tmp
        tools_tmp=$(mktemp); usage_tmp=$(mktemp)
        cat > "$usage_tmp" <<'USAGE'
## planner -- your node task queue (NOT the built-in task tool)

Ignore any built-in task tool (TaskList / TaskCreate / TaskUpdate) -- it belongs
to the runtime, not to your work, and is always empty. Your tasks are here.

Reach it over the node's own socket, and only over it. Your work includes tasks
where your slug is the assignee OR the reviewer. This raw read lists assigned
tasks only; use planner_my_tasks() for the combined queue including reviews:

    curl -s --unix-socket /run/smart-node/api/core-api.sock "http://localhost/api/planner/api/v1/tasks?assignee=<slug>&status_filter=queued"

Send NO Remote-User header. The socket is what proves who you are: the kernel
names the caller, so core-api accepts an agent identity that arrives that way.
Over TCP the same name is refused outright, because a header can be typed by
anyone, and the call fails with "agent identity asserted, not proved".

    GET   /tasks?assignee=<slug>&status_filter=queued
    GET   /tasks?assignee=<slug>&status_filter=queued&wait_reason=review
    GET   /tasks?assignee=<slug>&status_filter=in_progress
    GET   /tasks/<id>
    GET   /tasks/<id>/comments

WRITE WITH THE PLANNER TOOLS, NOT WITH CURL. planner_comment, planner_advance and
planner_file make every change a task needs, with your identity attached. A curl
that sends a body is a shell command that changes something, and a run with no
operator attached refuses it ("needs the operator's approval and this run has no
operator attached"): the comment or move you meant to make is lost, not queued.
curl is for the reads above. If your .mcp.json does not list `planner`, say so in
your reply and stop; do not retry with curl. The raw forms below are for a session
an operator is watching:

    POST  /tasks/<id>/comments   {"content":"..."}      # note: content, not body
    PATCH /tasks/<id>            {"status":"in_progress"}
    POST  /tasks                 {"title":"...","description":"...","assignee":"..."}

A task has one of three statuses: queued (not being worked on), in_progress, or
closed. A queued task carries wait_reason, why it waits: approval (an approver
decides), review (the reviewer checks it), owner, person, dependency, schedule or
error; none means nobody has taken it yet. A closed task carries close_outcome:
completed, duplicate, or a person's call (cancelled, rejected, not_possible).

On a wake: call planner_my_tasks() without a status. It lists in_progress first,
then tasks queued for review, then queued tasks nobody has taken, for your
assignee OR reviewer role. Resume started work first, read reviews waiting on
you, then take new work. A task queued for any other reason waits on someone
else. An explicit status (and wait_reason) narrows the read. Listing never
reassigns or moves a task.

**Read the task before you continue it.** Not the title and the status -- the
comments, newest first, and anything linked to it. A task you already know is the
one most likely to have changed: the answer you were waiting for, a review that
returned it, a decision that makes half your plan wrong. GET /tasks/<id>/comments,
or planner_comments. This has cost real time: on 2026-09-07 an agent blocked a task
asking the owner to run queries whose results were already sitting in its comments,
written ninety seconds earlier.

When you file a task, put the whole request in `description`. A title alone loses
what was actually asked.

If your .mcp.json lists `planner` you have the same contract as tools, with no
shell and no URL to remember: planner_my_tasks, planner_task, planner_comments,
planner_comment, planner_advance, planner_file, planner_search. Also: planner_next_task
(what to work on when you were woken with no task named), planner_list (every task in
one status, whoever holds it), planner_tag / planner_tags / planner_untag, and
planner_objective_create, planner_objective_attach, planner_objective_detach,
planner_objective_get and planner_objective_close for release objectives. A task that
comes back on a schedule (a reminder, a daily check) is filed with planner_file's
recur_interval and next_run and changed or ended with planner_recur.
Identity is not a
parameter there either -- you are always yourself.

LOG WHAT YOU DO ON THE TASK, AS YOU GO. This is the rule on this node, not a
courtesy. Each real decision, each finding, and each thing you rejected and why,
while you work, not one summary at the end. Work that is not written on the task
did not happen as far as the next reader can tell, and the next session will
either redo it or act on a stale premise still sitting in the description. If you
learn the task itself is wrong, correct it: a false premise left in a title
outlives everyone who knew better.

A change starts with a task, not ends with one. Search before you file; a
duplicate splits the record in two and both halves rot.

A task run by a workflow is the one exception to what follows. You know it by a
comment on the task whose first line is "[work order] <flow>/<block>". On such
a task you decide and the workflow acts: do the work the order asks for, post
ONE comment that ends with the VERDICT line the order names, and stop. Do not
change that task's status or assignee and do not advance it, even though
everywhere else you would: the workflow moves the task from your verdict, and a
status you move yourself takes the task off the path it was on.

Moving a task, with planner_advance (PATCH /tasks/<id> is its raw form):

    start work          {"status":"in_progress"}
    hand to review      {"status":"queued","wait_reason":"review"}
    stop for a person   {"status":"queued","wait_reason":"person","comment":"who and why"}
    finish              {"status":"closed","close_outcome":"completed","comment":"the report"}

Finishing your own fix does not close it. Hand it to review and let someone else
confirm it -- the confirmer is never the finder. There is no transition table:
any status may follow any other, and what the planner refuses it names. A close
needs a comment (for completed, the report quoting the evidence for each
acceptance item); only the reviewer closes a reviewed task as completed; a stop
for owner, person, dependency, schedule or error says why; cancelled, rejected
and not_possible are a person's call, so an agent stops the task for a person
instead.
USAGE
        # Substitute the __CORE_API_URL__ token with the actual gateway.
        # Fail the install if the token survives -- a surviving token ships a
        # literal placeholder into every agent's CLAUDE.md, which is the defect.
        sed -i "s|__CORE_API_URL__|${_core_api_url}|g" "$usage_tmp"
        grep -qF '__CORE_API_URL__' "$usage_tmp" \
            && die "install_failed: __CORE_API_URL__ token survived in planner usage_md -- resolve_core_api_url returned empty"
        if command -v jq >/dev/null; then
            jq -n --rawfile usage "$usage_tmp" \
                '[{name:"planner",kind:"api",title:"Planner",
                   description:"File a task in the node planner. Use when the owner asks for work that is not yours to do right now, or that a colleague must pick up. Search first (planner_search): a duplicate splits the record in two.",
                   usage_md:$usage,
                   http_method:"POST",http_path:"/api/v1/tasks",
                   input_schema:{type:"object",
                     properties:{
                       title:{type:"string",description:"One line naming the work, in the language the owner used."},
                       description:{type:"string",description:"What was actually asked, in full, including context the doer will not otherwise have."},
                       assignee:{type:"string",description:"Agent slug to do it. Omit to leave unassigned."},
                       priority:{type:"string",enum:["low","medium","high","critical"],description:"Default medium."}},
                     required:["title","description"]}},
                  {name:"planner_tasks",kind:"api",title:"List tasks",
                   description:"Read the task list of this node. Use whenever someone asks what tasks exist, what is open, what is in progress, or what a colleague is working on -- and answer from what it returns. planner_task reads one task in full, planner_comments its log.",
                   http_method:"GET",http_path:"/api/v1/tasks",
                   input_schema:{type:"object",
                     properties:{
                       status_filter:{type:"string",description:"queued | in_progress | closed. Omit for all of them."},
                       wait_reason:{type:"string",description:"With queued: only tasks waiting for this reason (approval, review, owner, person, dependency, schedule, error)."},
                       assignee:{type:"string",description:"Agent slug; filters to tasks assigned to that agent."},
                       limit:{type:"integer",description:"How many to return. Default 20."}}}}]' \
                > "$tools_tmp"
        else
            printf '[{"name":"planner","kind":"api","title":"Planner","description":"List and advance your node planner tasks (through core)."}]' > "$tools_tmp"
        fi
        sn_integration_register \
            --slug             planner \
            --label            "Planner" \
            --purpose          "Tasks" \
            --kind             app \
            --status           available \
            --install-uri      "http://planner.${SMART_NODE_DOMAIN}" \
            --install-path     "$INSTALL_DIR" \
            --endpoint         "${_core_api_url}/api/planner" \
            --description      "Task and objective management." \
            --companion-slug   "$STAGE_ID" \
            --capabilities     '["tasks"]' \
            --tools-json       "$tools_tmp" || true
        rm -f "$tools_tmp" "$usage_tmp"
        reseed_planner_rows
    fi
    # node-admin tool
    if declare -F sn_integration_register >/dev/null; then
        local na_tools na_usage
        na_tools=$(mktemp); na_usage=$(mktemp)
        cat > "$na_usage" <<'USAGE'
## node-admin -- create and wire a business sub-agent (through core)

Every call carries `Remote-User: <your-slug>`. Never hand-write grants.json,
agent.yaml, .mcp.json or /etc/cron.d -- those are projections; editing them
changes nothing. Only these calls change real state.

    POST __CORE_API_URL__/api/agents
      {"tenant_id":1,"slug":"...","title":"<its NAME>","role":"<its JOB>","model":"claude-cli/sonnet",
       "enabled":true,"mesh_workspace":"...","specialist_body":"<its one job>",
       "fs_read":["/path/it/must/read"],"fs_write":[]}

A NAME AND A JOB ARE DIFFERENT THINGS, and both are required. `title` is what a
person calls it: Alex, John, a name a human would use out loud. `role` is what it
does: Secretary, Foreman, Node Architect. Naming an agent after its job gives the
owner a roster of functions rather than of colleagues, and he then has to
remember which function he was speaking to. Ask the owner for the name; do not
invent one and do not reuse the job.

    PUT  __CORE_API_URL__/api/agents/<slug>/grants
      {"grants":{"fs":"r--","doci":"rwd","mesh":"rwd","planner":"rw-","messaging":"rw-"}}

A GRANT IS NOT A PATH. `fs` says the agent may read files at all; `fs_read` says
WHICH. The default is an empty list, so `fs` granted with no `fs_read` is
permission over nothing: the agent asks for a log and gets refused, and nothing
says why. Decide what it must see and name it at creation. Keep the list as short
as the job allows -- an agent that reads the whole disk is not narrow.

A SECRET needs two grants, both in the same request: the read-only tool and the
exact ref (or the narrowest dotted namespace) it may resolve. Ask the owner
before handing out a credential, preserve the agent's existing grants, then:

    curl --unix-socket /run/smart-node/api/core-api.sock -X PUT \
      http://localhost/api/agents/<slug>/grants \
      -H 'Content-Type: application/json' \
      -d '{"grants":{"...":"...","secrets":"r--"},"secret_refs":["service.account_token","agent_slug.connector.*"]}'

Never use `*`; the API rejects it. A `secrets` tool grant without `secret_refs`
opens nothing. Only you, foreman, root, or an authenticated owner session may
change a secrets grant; every refused attempt is audited. The recipient may
resolve only those refs and cannot list, store, overwrite, or delete secrets.

THE AGENT RUNS WHEN WOKEN. Send it a message to start its first task: the Runner
launches it on its next tick. A task filed by an agent lands unassigned, waiting
for the owner's approval, and wakes nobody. Do not move it to in_progress or
promise it has started. Approval alone does not wake an unassigned task; the
owner must assign it or write to the agent. An owner-assigned, approved task
raises a wake flag through core-api. An idle agent costs nothing.

Building an agent is those two calls. Nothing else is required to make it run,
and there is no schedule to set: an agent that must look at something on a
timer is not something you build here. Say so to the owner instead.

## Wiring the rest, once the agent exists

A CHANNEL, so a person can reach it directly rather than through you:

    PUT  __CORE_API_URL__/api/agents/<slug>/channels/<channel_slug>
      {"instance_ref":"<bot handle or mailbox>","enabled":true}

A binding is only half. The agent still may not write to a person it has no
contact grant for -- see the `contacts` contract. A channel bound without that
grant is a channel that records every message and refuses to answer, and the
refusal is written to the event log where nobody is looking.

A RULE, when the owner says "always do X" or "never do Y". Rules are POLICIES, not
persona text: a policy overrides the prompt, and the persona does not.

    POST __CORE_API_URL__/api/policies
      {"slug":"answer-briefly-on-telegram","category":"workflow",
       "title":"...","body_md":"<the rule, in one or two sentences>",
       "severity":"warn","enabled":true}

`category` is `safety` or `workflow`, and an agent receives a policy because it
carries that category in `policy_categories`. Writing a rule into a specialist
body instead is the common mistake: it reads the same and does not override
anything. After writing a policy, confirm it landed by reading it back -- a rule
you reported and did not write is worse than no rule.

## Models and providers

`model` is `<provider>/<model>`, stored on each agent: `claude-cli/sonnet` runs on
the Claude CLI, `gemini/gemini-flash-latest` on Google directly (http-agent lives there),
`openrouter/openai/<model>` on OpenRouter. The provider is the runtime that
executes the turn, not who makes the model: GPT, Grok, Llama and the rest are
`openrouter/<vendor>/<model>`. Do not keep a list of providers in your head; ask.

    GET  __CORE_API_URL__/api/models/providers

answers, for every provider this node's gateway can run: whether it can run now
(`available` / `not_configured`), the key it lacks (`key_ref`, `missing`), the
catalog models on it and the agents using it. An agent may only be put on a model
that is in the catalog. To add one:

    POST __CORE_API_URL__/api/models
      {"provider":"openrouter","model":"openai/<name>","vendor":"openai","label":"<what a person reads>"}

A provider that is `not_configured` needs its API key. That key is the owner's to
give and to store (the AI Gateway settings, or POST /api/gateway/secrets at the
`key_ref` the providers call named); ask him for it once, plainly, and never paste
a key into a prompt, a file or a message. To move an existing agent to another
model: `PUT __CORE_API_URL__/api/agents/<slug>` with `{"model":"<provider>/<model>"}`;
never edit agent.yaml or config.json.

When the owner says "put an agent on <model>", the whole job is: look (providers
call), ask for the key only if it is missing, add the model if the catalog lacks
it, create or move the agent, send it one message and show him its answer. That is
one short turn, not a search.

## The rules of this job

Always grant `messaging` -- it is how the agent reaches a colleague and works its
own inbox. Grant `contacts` to any agent that must reach a PERSON. Never grant
`bash` or `node-admin`: those powers stay yours.

Build the SMALLEST thing that moves the owner's biggest current constraint. One
narrow agent, never an org chart up front.

An agent that cannot do its job yet is not finished, however cleanly the calls
returned 201. Before you report it working, check the thing it exists to do: can
it read what it must read, reach who it must reach, and is there anything that
will wake it. Say plainly what is still missing rather than reporting success.
USAGE
        # Substitute the __CORE_API_URL__ token with the actual gateway.
        # Fail the install if the token survives -- a surviving token ships a
        # literal placeholder into every agent's CLAUDE.md, which is the defect.
        sed -i "s|__CORE_API_URL__|${_core_api_url}|g" "$na_usage"
        grep -qF '__CORE_API_URL__' "$na_usage" \
            && die "install_failed: __CORE_API_URL__ token survived in node-admin usage_md -- resolve_core_api_url returned empty"
        if command -v jq >/dev/null; then
            jq -n --rawfile usage "$na_usage" \
                '[{name:"node-admin",kind:"api",title:"Node configurator",description:"Create and wire narrow business sub-agents (create->grant->wake, through core).",usage_md:$usage}]' \
                > "$na_tools"
        else
            printf '[{"name":"node-admin","kind":"api","title":"Node admin","description":"Create and wire narrow business sub-agents (through core)."}]' > "$na_tools"
        fi
        sn_integration_register \
            --slug             node-admin \
            --label            "Node configurator" \
            --purpose          "Configure the node" \
            --kind             app \
            --status           available \
            --install-path     "$INSTALL_DIR" \
            --description      "Create and wire narrow business sub-agents." \
            --companion-slug   "$STAGE_ID" \
            --tools-json       "$na_tools" || true
        rm -f "$na_tools" "$na_usage"

        # contacts tool
        local ct_tools ct_usage
        ct_tools=$(mktemp); ct_usage=$(mktemp)
        cat > "$ct_usage" <<'USAGE'
## contacts -- the address book, and who may write to whom

This node will not let an agent write to a stranger, and with the inbound gate on
it is not even woken by mail from one. An unlisted address is not "not tried
yet" -- it cannot be reached at all. `Remote-User: <your-slug>` on every call.

A contact is a PARTY; one party may hold several addresses on several channels.
Creating it grants it to nobody -- the catalogue and the permission are two
steps, and the second is the one that changes what an agent may do.

    GET    /api/contacts
    POST   /api/contacts
      {"display_name":"...","kind":"customer","org_name":"...",
       "channels":[{"channel":"email","handle":"anna@example.com"}]}
    POST   /api/contacts/<id>/channels    {"channel":"email","handle":"..."}
    DELETE /api/contacts/<id>                        # retires it from every agent

    GET    /api/agents/<slug>/contacts
    PUT    /api/agents/<slug>/contacts/<contact-id>  {"channels":["email"]}
    DELETE /api/agents/<slug>/contacts/<contact-id>

`kind`: agent, person, customer, supplier, vendor, lead, partner, other.

ASK THE OWNER BEFORE GRANTING. A grant is permission to write to a real person on
their behalf, and it decides whose mail may wake an agent and spend their budget.
Never grant a contact to yourself or a colleague to get around a refusal.

Matching: email is case-insensitive and a display-name form matches the bare
address. Plus-addressing is NOT stripped -- `anna+shop@` needs its own entry. A
whole company is a handle of `@example.com` (at-sign, nothing before it); it does
not cover subdomains.
USAGE
        if command -v jq >/dev/null; then
            jq -n --rawfile usage "$ct_usage" \
                '[{name:"contacts",kind:"api",title:"Contacts",description:"The address book, and which agent may write to which party on which channel (through core).",usage_md:$usage}]' \
                > "$ct_tools"
        else
            printf '[{"name":"contacts","kind":"api","title":"Contacts","description":"The address book and per-agent write permissions (through core)."}]' > "$ct_tools"
        fi
        sn_integration_register \
            --slug             contacts \
            --label            "Contacts" \
            --purpose          "Address book" \
            --kind             app \
            --status           available \
            --install-path     "$INSTALL_DIR" \
            --endpoint         "${_core_api_url}" \
            --description      "The address book, and who may write to whom." \
            --companion-slug   "$STAGE_ID" \
            --tools-json       "$ct_tools" || true
        rm -f "$ct_tools" "$ct_usage"
    fi

    log "companion tracking -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

planner_accepts_core_api_key() {
    # A restarted planner can answer /health before its DB-backed key check is
    # ready. Give that dependency up to one minute, but a definite auth refusal
    # is a bad key and should fail immediately. Keep the last HTTP code for the
    # verify report; verify_check suppresses command output by design.
    PLANNER_KEY_LAST_HTTP_CODE=000
    [[ -f "$KEY_ENV_CORE_API" ]] || return 1
    local PLANNER_API_KEY=""
    # shellcheck source=/dev/null
    source "$KEY_ENV_CORE_API"
    [[ -n "$PLANNER_API_KEY" ]] || return 1
    local deadline=$((SECONDS + 60)) remaining code
    while :; do
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || return 1
        code=$(curl -s -o /dev/null -w '%{http_code}' \
            --max-time "$((remaining < 4 ? remaining : 4))" \
            -H "X-API-Key: $PLANNER_API_KEY" \
            "http://127.0.0.1:${PLANNER_HTTP_PORT}/api/v1/tasks" 2>/dev/null) || true
        PLANNER_KEY_LAST_HTTP_CODE=${code:-000}
        [[ "$PLANNER_KEY_LAST_HTTP_CODE" == 200 ]] && return 0
        [[ "$PLANNER_KEY_LAST_HTTP_CODE" == 401 || "$PLANNER_KEY_LAST_HTTP_CODE" == 403 ]] && return 1
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || return 1
        sleep "$((remaining < 2 ? remaining : 2))"
    done
}

run_verify_checks() {
    load_contracts
    [[ -f "$TRACKING_ENV" ]] && source "$TRACKING_ENV"
    local pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. B03 precondition.
    verify_check "stage B03 done" require_stage B03
    # 3. service user exists.
    verify_check "service user $SERVICE_USER exists" bash -c "
        id '$SERVICE_USER' >/dev/null 2>&1"
    # 4. install dir present.
    verify_check "install dir $INSTALL_DIR present" test -d "$INSTALL_DIR"
    # 5. venv has uvicorn.
    verify_check "venv has uvicorn" test -x "$VENV_DIR/bin/uvicorn"
    # 6. venv has planner CLI.
    verify_check "venv has planner CLI" test -x "$VENV_DIR/bin/planner"
    # 7. tracking.env present and 0600.
    verify_check "tracking.env present 0600" bash -c "
        [[ -f '$TRACKING_ENV' && \$(stat -c '%a' '$TRACKING_ENV' 2>/dev/null) == 600 ]]"
    # 8. planner database exists.
    verify_check "database $PLANNER_DB exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d postgres -tAc \
            \"SELECT 1 FROM pg_database WHERE datname='$PLANNER_DB'\" 2>/dev/null) == 1 ]]"
    # 9. planner role exists.
    verify_check "role $PLANNER_ROLE exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d postgres -tAc \
            \"SELECT 1 FROM pg_roles WHERE rolname='$PLANNER_ROLE'\" 2>/dev/null) == 1 ]]"
    # 10. migrations applied (_migrations table exists in the planner DB).
    verify_check "Planner migrations applied" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d '$PLANNER_DB' -tAc \
            \"SELECT 1 FROM information_schema.tables WHERE table_name='_migrations'\" 2>/dev/null) == 1 ]]"
    # 11. systemd unit installed.
    verify_check "$SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # 12. service active.
    verify_check "$SYSTEMD_UNIT active" bash -c "
        systemctl is-active '$SYSTEMD_UNIT' >/dev/null 2>&1"
    # The checks below need the API up. On an update the unit was restarted a moment ago
    # and uvicorn takes a few seconds to listen; checked at once, both failed although the
    # Planner came up seconds later (an update of a live node stopped here). Wait for it.
    local _pw
    for _pw in $(seq 1 30); do
        curl -sf -m 2 "http://127.0.0.1:${PLANNER_HTTP_PORT}/health" >/dev/null 2>&1 && break
        sleep 2
    done
    # 13. binds on 127.0.0.1 only. Compare the LOCAL address column ONLY:
    # a listening socket always shows 0.0.0.0:* as its PEER address, so grepping
    # the whole ss line matched every time and this check could never pass.
    verify_check "$SYSTEMD_UNIT binds loopback only" bash -c "
        ss -tlnH 2>/dev/null | awk '{print \$4}' | grep -q '^127[.]0[.]0[.]1:${PLANNER_HTTP_PORT}$' \
        && ! ss -tlnH 2>/dev/null | awk '{print \$4}' | grep -qE '^(0[.]0[.]0[.]0|[*]):${PLANNER_HTTP_PORT}$'"
    # 14. /health answers.
    verify_check "Planner /health answers" bash -c "
        curl -sf 'http://127.0.0.1:${PLANNER_HTTP_PORT}/health' >/dev/null 2>&1"
    # 15. planner runs in apikey mode.
    verify_check "planner runs in apikey mode" bash -c "
        curl -sf 'http://127.0.0.1:${PLANNER_HTTP_PORT}/health' 2>/dev/null \
        | grep -q '\"auth_mode\": *\"apikey\"' || \
        curl -so /dev/null -w '%{http_code}' \
            'http://127.0.0.1:${PLANNER_HTTP_PORT}/api/v1/tasks' 2>/dev/null \
        | grep -q 401"
    # 16-18. per-client credential files present and 0600 root:root.
    local f
    for f in "$KEY_ENV_CORE_API" "$KEY_ENV_RUNNER" "$KEY_ENV_VOICE"; do
        verify_check "$(basename "$f") present 0600 root:root" bash -c "
            [[ -f '$f' ]] || exit 1
            [[ \$(stat -c '%a %U %G' '$f' 2>/dev/null) == '600 root root' ]]"
    done
    # 19. the service refuses unauthenticated calls.
    verify_check "planner refuses unauthenticated calls" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            'http://127.0.0.1:${PLANNER_HTTP_PORT}/api/v1/tasks' 2>/dev/null)
        [[ \"\$code\" == 401 ]]"
    # 20. the service accepts the core-api credential.
    local key_check_errors=$VERIFY_ERRORS
    verify_check "planner accepts the core-api key" planner_accepts_core_api_key
    if (( VERIFY_ERRORS > key_check_errors )); then
        echo "[VERIFY-FAIL] planner core-api key probe: HTTP ${PLANNER_KEY_LAST_HTTP_CODE:-000}" >&2
    fi
    # 21. staleness sweep cron present.
    verify_check "staleness sweep cron present" bash -c "
        [[ -f /etc/cron.d/smart-node-planner-sweep ]]"
    # 22. planner contract file present.
    verify_check "/etc/smart-node/planner.env present" bash -c "
        [[ -f /etc/smart-node/planner.env ]] && \
        grep -q 'PLANNER_API_URL=http://127.0.0.1:${PLANNER_HTTP_PORT}' /etc/smart-node/planner.env"
    # 22b. tracking.env carries the notify URL.
    verify_check "tracking.env carries PLANNER_NOTIFY_URL" bash -c "
        [[ -f '$TRACKING_ENV' ]] && \
        grep -qE '^PLANNER_NOTIFY_URL=http://.+/api/notify' '$TRACKING_ENV'"
    # 23. companion manifest written.
    verify_check "companions.d/tracking.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/tracking.json"
    # 24. install-report has the tracking section.
    verify_check "install-report has the tracking section" \
        bash -c "grep -qFx '## Companion tracking' '$SMART_NODE_REPORT'"
    # 25. manifest records the systemd unit.
    verify_check "manifest records $SYSTEMD_UNIT" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$SYSTEMD_UNIT\",' \"\$m\""
}

run_verify_mode() {
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

run_uninstall_mode() {
    # Step 1: TTY check -- must be the very first thing in the destructive path.
    [ -t 0 ] || { echo "ERROR: --uninstall requires an interactive terminal." >&2; exit 1; }

    require_root
    log_init "$STAGE_ID"
    load_contracts
    log "companion tracking -- uninstall"

    # Step 2: Summary -- print what will be removed before asking.
    # read node identity from routing.env; no hostname(1) fallback.
    local node_fqdn db_size
    node_fqdn="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$node_fqdn" && -f /etc/smart-node/routing.env ]]; then
        # shellcheck source=/dev/null
        source /etc/smart-node/routing.env
        node_fqdn="${SMART_NODE_DOMAIN:-unknown}"
    fi
    if [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]]; then
        db_size=$(runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
            -d postgres -tAc \
            "SELECT pg_size_pretty(pg_database_size('$PLANNER_DB'))" 2>/dev/null \
            | tr -d '[:space:]' || echo "unknown")
    else
        db_size="unknown"
    fi
    echo ""
    echo "About to PERMANENTLY uninstall the tracking companion (planner) from this node."
    echo "  Node FQDN: $node_fqdn"
    echo "  Database:  $PLANNER_DB (${db_size})"
    echo "  Service:   $SYSTEMD_UNIT"
    echo ""

    # Step 3: Confirmation -- operator must type the exact node FQDN.
    local CONFIRM NODE_FQDN
    NODE_FQDN="${node_fqdn}"
    read -r -p "Type the node FQDN to confirm deletion: " CONFIRM
    if [ "$CONFIRM" != "$NODE_FQDN" ]; then
        echo "Aborted: input did not match node FQDN ($NODE_FQDN)." >&2
        exit 1
    fi

    # Step 4: Dump -- must succeed before any DROP.
    local backup_dir="/opt/smart-node/backups"
    local dump_stamp dump_file
    dump_stamp=$(date +%Y%m%d-%H%M%S)
    dump_file="$backup_dir/tracking-${PLANNER_DB}-${dump_stamp}.dump"
    if [[ ! -d "$backup_dir" ]]; then
        echo "ERROR: $backup_dir does not exist -- create it before uninstalling." >&2
        exit 1
    fi
    if [[ ! -w "$backup_dir" ]]; then
        echo "ERROR: $backup_dir is not writable." >&2
        exit 1
    fi
    if [[ -z "${SMART_NODE_PG_CLUSTER:-}" ]]; then
        echo "ERROR: no SMART_NODE_PG_CLUSTER in the persistence contract -- cannot dump." >&2
        exit 1
    fi
    log "dumping $PLANNER_DB -> $dump_file"
    if ! runuser -u postgres -- pg_dump \
            --cluster "$SMART_NODE_PG_CLUSTER" -Fc "$PLANNER_DB" \
            > "$dump_file" 2>/dev/null; then
        echo "ERROR: pg_dump failed -- will not proceed to drop." >&2
        rm -f "$dump_file"
        exit 1
    fi
    if [[ ! -s "$dump_file" ]]; then
        echo "ERROR: dump file is empty -- will not proceed to drop." >&2
        rm -f "$dump_file"
        exit 1
    fi
    log "dump written: $dump_file"

    unregister_companion "$STAGE_ID"
    if declare -F sn_integration_unregister >/dev/null; then
        sn_integration_unregister --slug planner || true
        sn_integration_unregister --slug node-admin || true
        sn_integration_unregister --slug contacts || true
    fi
    # Stop and disable the service.
    systemctl stop "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    systemctl disable "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    # Also remove any legacy Docker container if still present.
    if command -v docker >/dev/null 2>&1; then
        docker rm -f "$LEGACY_CT" >/dev/null 2>&1 || true
    fi
    # Step 5: Drop -- only after the dump is confirmed written.
    psql_pg -c "DROP DATABASE IF EXISTS \"$PLANNER_DB\";" 2>/dev/null || true
    psql_pg -c "DROP ROLE IF EXISTS \"$PLANNER_ROLE\";" 2>/dev/null || true
    # Remove the contract file, key files, cron, and install tree.
    rm -f /etc/smart-node/planner.env
    rm -f /etc/cron.d/smart-node-planner-sweep
    rm -rf "$INSTALL_DIR"
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion tracking -- uninstalled"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   create_service_user (OS account, one-time), ensure_dirs (directory
#   create, one-time), create_planner_db (DB + role creation, one-time),
#   install_snapshot (extract application files, version-gated), build_venv
#   (pip install, version-gated), apply_migrations (schema migrations, only
#   on version change), migrate_from_container (one-time data migration),
#   wait_for_planner (health probe, no state written), capture_api_key
#   (reads the root API key from app startup log, one-time; on contracts-only
#   the API key is already in tracking.env and is sourced by
#   load_or_generate_secrets).
#
# CONTRACT (run): load_contracts (sources persistence contract, needed for
#   env vars), load_or_generate_secrets (idempotent: sources the existing
#   tracking.env when present, loading DB password AND API key; generates
#   only when the file is absent, which does not happen in contracts-only
#   mode), write_tracking_env (rewrites tracking.env with current runtime
#   config -- PLANNER_NOTIFY_URL uses resolve_core_api_url() which can
#   change across deploys; DB password and API key are preserved from the
#   values loaded by load_or_generate_secrets), write_planner_contract
#   (planner.env read by core-api to locate the native planner -- must be
#   current), install_service (systemd unit file -- must be current),
#   provision_client_keys (API key grants for agent slugs -- must be
#   current), install_staleness_cron (staleness sweep cron entry -- must be
#   current), install_planner_mcp (MCP shim file -- must be current),
#   register_companion (companions.d record for version tracking),
#   register_mcp_server (sn_config mcp row -- must be current),
#   sn_integration_register (sn_config.integrations rows -- must be
#   current), stage_mark done.
run_contracts_only() {
    require_root
    log "companion tracking -- contracts only (v$STAGE_VERSION)"
    require_stage B02
    require_stage B03
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    load_or_generate_secrets
    write_tracking_env
    write_planner_contract
    install_service
    provision_client_keys
    install_staleness_cron
    install_planner_mcp
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,documents"
    register_mcp_server
    if declare -F sn_integration_register >/dev/null; then
        local tools_tmp usage_tmp
        tools_tmp=$(mktemp); usage_tmp=$(mktemp)
        cat > "$usage_tmp" <<'USAGE'
## planner -- your node task queue (NOT the built-in task tool)

Ignore any built-in task tool (TaskList / TaskCreate / TaskUpdate) -- it belongs
to the runtime, not to your work, and is always empty. Your tasks are here.

Reach it over the node's own socket, and only over it. Your work includes tasks
where your slug is the assignee OR the reviewer. This raw read lists assigned
tasks only; use planner_my_tasks() for the combined queue including reviews:

    curl -s --unix-socket /run/smart-node/api/core-api.sock "http://localhost/api/planner/api/v1/tasks?assignee=<slug>&status_filter=queued"

Send NO Remote-User header. The socket is what proves who you are: the kernel
names the caller, so core-api accepts an agent identity that arrives that way.
Over TCP the same name is refused outright, because a header can be typed by
anyone, and the call fails with "agent identity asserted, not proved".

    GET   /tasks?assignee=<slug>&status_filter=queued
    GET   /tasks?assignee=<slug>&status_filter=queued&wait_reason=review
    GET   /tasks?assignee=<slug>&status_filter=in_progress
    GET   /tasks/<id>
    GET   /tasks/<id>/comments

WRITE WITH THE PLANNER TOOLS, NOT WITH CURL. planner_comment, planner_advance and
planner_file make every change a task needs, with your identity attached. A curl
that sends a body is a shell command that changes something, and a run with no
operator attached refuses it ("needs the operator's approval and this run has no
operator attached"): the comment or move you meant to make is lost, not queued.
curl is for the reads above. If your .mcp.json does not list `planner`, say so in
your reply and stop; do not retry with curl. The raw forms below are for a session
an operator is watching:

    POST  /tasks/<id>/comments   {"content":"..."}      # note: content, not body
    PATCH /tasks/<id>            {"status":"in_progress"}
    POST  /tasks                 {"title":"...","description":"...","assignee":"..."}

A task has one of three statuses: queued (not being worked on), in_progress, or
closed. A queued task carries wait_reason, why it waits: approval (an approver
decides), review (the reviewer checks it), owner, person, dependency, schedule or
error; none means nobody has taken it yet. A closed task carries close_outcome:
completed, duplicate, or a person's call (cancelled, rejected, not_possible).

On a wake: call planner_my_tasks() without a status. It lists in_progress first,
then tasks queued for review, then queued tasks nobody has taken, for your
assignee OR reviewer role. Resume started work first, read reviews waiting on
you, then take new work. A task queued for any other reason waits on someone
else. An explicit status (and wait_reason) narrows the read. Listing never
reassigns or moves a task.

**Read the task before you continue it.** Not the title and the status -- the
comments, newest first, and anything linked to it. A task you already know is the
one most likely to have changed: the answer you were waiting for, a review that
returned it, a decision that makes half your plan wrong. GET /tasks/<id>/comments,
or planner_comments. This has cost real time: on 2026-09-07 an agent blocked a task
asking the owner to run queries whose results were already sitting in its comments,
written ninety seconds earlier.

When you file a task, put the whole request in `description`. A title alone loses
what was actually asked.

If your .mcp.json lists `planner` you have the same contract as tools, with no
shell and no URL to remember: planner_my_tasks, planner_task, planner_comments,
planner_comment, planner_advance, planner_file, planner_search. Also: planner_next_task
(what to work on when you were woken with no task named), planner_list (every task in
one status, whoever holds it), planner_tag / planner_tags / planner_untag, and
planner_objective_create, planner_objective_attach, planner_objective_detach,
planner_objective_get and planner_objective_close for release objectives. A task that
comes back on a schedule (a reminder, a daily check) is filed with planner_file's
recur_interval and next_run and changed or ended with planner_recur.
Identity is not a
parameter there either -- you are always yourself.

LOG WHAT YOU DO ON THE TASK, AS YOU GO. This is the rule on this node, not a
courtesy. Each real decision, each finding, and each thing you rejected and why,
while you work, not one summary at the end. Work that is not written on the task
did not happen as far as the next reader can tell, and the next session will
either redo it or act on a stale premise still sitting in the description. If you
learn the task itself is wrong, correct it: a false premise left in a title
outlives everyone who knew better.

A change starts with a task, not ends with one. Search before you file; a
duplicate splits the record in two and both halves rot.

A task run by a workflow is the one exception to what follows. You know it by a
comment on the task whose first line is "[work order] <flow>/<block>". On such
a task you decide and the workflow acts: do the work the order asks for, post
ONE comment that ends with the VERDICT line the order names, and stop. Do not
change that task's status or assignee and do not advance it, even though
everywhere else you would: the workflow moves the task from your verdict, and a
status you move yourself takes the task off the path it was on.

Moving a task, with planner_advance (PATCH /tasks/<id> is its raw form):

    start work          {"status":"in_progress"}
    hand to review      {"status":"queued","wait_reason":"review"}
    stop for a person   {"status":"queued","wait_reason":"person","comment":"who and why"}
    finish              {"status":"closed","close_outcome":"completed","comment":"the report"}

Finishing your own fix does not close it. Hand it to review and let someone else
confirm it -- the confirmer is never the finder. There is no transition table:
any status may follow any other, and what the planner refuses it names. A close
needs a comment (for completed, the report quoting the evidence for each
acceptance item); only the reviewer closes a reviewed task as completed; a stop
for owner, person, dependency, schedule or error says why; cancelled, rejected
and not_possible are a person's call, so an agent stops the task for a person
instead.
USAGE
        if command -v jq >/dev/null; then
            jq -n --rawfile usage "$usage_tmp" \
                '[{name:"planner",kind:"api",title:"Planner",
                   description:"File a task in the node planner. Use when the owner asks for work that is not yours to do right now, or that a colleague must pick up. Search first (planner_search): a duplicate splits the record in two.",
                   usage_md:$usage,
                   http_method:"POST",http_path:"/api/v1/tasks",
                   input_schema:{type:"object",
                     properties:{
                       title:{type:"string",description:"One line naming the work, in the language the owner used."},
                       description:{type:"string",description:"What was actually asked, in full, including context the doer will not otherwise have."},
                       assignee:{type:"string",description:"Agent slug to do it. Omit to leave unassigned."},
                       priority:{type:"string",enum:["low","medium","high","critical"],description:"Default medium."}},
                     required:["title","description"]}},
                  {name:"planner_tasks",kind:"api",title:"List tasks",
                   description:"Read the task list of this node. Use whenever someone asks what tasks exist, what is open, what is in progress, or what a colleague is working on -- and answer from what it returns. planner_task reads one task in full, planner_comments its log.",
                   http_method:"GET",http_path:"/api/v1/tasks",
                   input_schema:{type:"object",
                     properties:{
                       status_filter:{type:"string",description:"queued | in_progress | closed. Omit for all of them."},
                       wait_reason:{type:"string",description:"With queued: only tasks waiting for this reason (approval, review, owner, person, dependency, schedule, error)."},
                       assignee:{type:"string",description:"Agent slug; filters to tasks assigned to that agent."},
                       limit:{type:"integer",description:"How many to return. Default 20."}}}}]' \
                > "$tools_tmp"
        else
            printf '[{"name":"planner","kind":"api","title":"Planner","description":"List and advance your node planner tasks (through core)."}]' > "$tools_tmp"
        fi
        sn_integration_register \
            --slug             planner \
            --label            "Planner" \
            --purpose          "Tasks" \
            --kind             app \
            --status           available \
            --install-uri      "http://planner.${SMART_NODE_DOMAIN}" \
            --install-path     "$INSTALL_DIR" \
            --endpoint         "http://127.0.0.1:8030/api/planner" \
            --description      "Task and objective management." \
            --companion-slug   "$STAGE_ID" \
            --capabilities     '["tasks"]' \
            --tools-json       "$tools_tmp" || true
        rm -f "$tools_tmp" "$usage_tmp"
    fi
    stage_mark "$STAGE_ID" done \
        "version=tracking@$STAGE_VERSION" \
        "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
