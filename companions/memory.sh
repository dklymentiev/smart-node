#!/bin/bash
#
# memory.sh -- Smart Node installer, Layer C Phase 2 companion: memory.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B02.
#
# Installs the memory companion: the node's semantic memory store. As of
# v0.3.0 this is a NATIVE systemd service, not a Docker container.
# The companion ships its Python source (companions/mesh/) alongside this
# script and installs it under a dedicated OS user with a Python venv.
#
# Why native: a hotfix is git pull + systemctl restart, under 60 seconds.
# On a container it is 3-8 minutes plus operator action. mesh is FastAPI
# on Python 3.12 with PostgreSQL -- both already present on the host. Its
# database already lives in the shared B02 cluster; the container bought
# reproducibility, not compatibility.
#
# EMBEDDING MODEL
# intfloat/multilingual-e5-base (~560 MB) is downloaded from HuggingFace
# at install time and cached at /opt/smart-node/memory/model (HF_HOME).
# If HuggingFace is unreachable the install fails loudly: there is no
# silent fallback. The model is persistent across updates -- it is not
# part of the release tarball and is never wiped by a re-run.
#
# Creates:
#   - service account smart-node-mesh (OS user, via lib.sh create_service_user)
#   - /opt/smart-node/memory -- install tree (Python source + venv)
#   - /opt/smart-node/memory/model -- HuggingFace model cache (HF_HOME)
#   - /opt/smart-node/memory/memory.env (0600 root:root) -- generate-once
#     DB passwords (mesh owner role + mesh_app RLS role) + full runtime config
#   - mesh database + roles in the B02 cluster, idempotent; keeps existing
#     data on re-run or upgrade from the Docker container version
#   - smart-node-mesh.service -- main API (gunicorn/uvicorn, 127.0.0.1:8000)
#   - smart-node-mesh-mcp.service -- MCP sidecar (streamable-http, :8100)
#   - companions.d/memory.json -- the manifest the panel reads
#   - "Companion memory" section in the install report
#   - /var/lib/smart-node/stages/memory.state
#
# UPGRADE FROM CONTAINER (v0.2.x)
# The smart-node-mesh and smart-node-mesh-mcp Docker containers are stopped
# and removed before the native service starts. The mesh database and
# memory.env (which carries the generate-once passwords) are preserved.
# Docker images are NOT removed automatically.
#
# Usage:
#   memory.sh             full run: probe -> intent -> verify -> mark
#   memory.sh --verify    idempotent post-check; exit 0 / N failures
#   memory.sh --status    read /var/lib/smart-node/stages/memory.state
#   memory.sh --uninstall reverse the install
#   memory.sh --help      this message
#
# Environment:
#   SMART_NODE_MESH_EMBEDDING_MODEL  override model name
#                                    (default intfloat/multilingual-e5-base)
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

STAGE_ID="memory"
STAGE_NAME="memory"
STAGE_VERSION="0.3.4"

# Where this script lives -- the vendored mesh snapshot ships beside it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/mesh"

# Service account and layout (follow the core-api / gateway pattern).
SERVICE_USER="smart-node-mesh"
SERVICE_SHORT="mesh"   # create_service_user prefixes smart-node- itself
INSTALL_DIR="/opt/smart-node/memory"
VENV_DIR="$INSTALL_DIR/venv"
MODEL_DIR="$INSTALL_DIR/model"
LOG_DIR="/var/log/smart-node/memory"

# The env file carries both the generate-once DB passwords and the derived
# runtime config that references them (DATABASE_URL etc.).
MEMORY_ENV="$INSTALL_DIR/memory.env"

# Systemd units.
SYSTEMD_UNIT="smart-node-mesh.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"
SYSTEMD_MCP_UNIT="smart-node-mesh-mcp.service"
SYSTEMD_MCP_PATH="/etc/systemd/system/$SYSTEMD_MCP_UNIT"

# Network: the API binds loopback-only.
MESH_API_HOST="127.0.0.1"
MESH_HTTP_PORT=8010   # NOT 8000: the gateway owns 127.0.0.1:8000 on the host. In a container mesh had its own namespace and the clash could not happen.
MESH_MCP_PORT=8100

# The dedicated mesh database + roles inside the B02 cluster.
MESH_DB="mesh"
MESH_ROLE="mesh"
MESH_APP_ROLE="mesh_app"

# Embedding model. Override via env to test an alternative.
EMBEDDING_MODEL="${SMART_NODE_MESH_EMBEDDING_MODEL:-intfloat/multilingual-e5-base}"

# Docker container names from v0.2.x (only referenced for upgrade removal).
LEGACY_CT="smart-node-mesh"
LEGACY_MCP_CT="smart-node-mesh-mcp"

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

# Optional integration helper.
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

# psql_pg -- run psql against the B02 cluster as postgres.
psql_pg() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d postgres -v ON_ERROR_STOP=1 -X "$@"
}

# psql_mesh -- run psql against the mesh database as postgres.
psql_mesh() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d "$MESH_DB" -v ON_ERROR_STOP=1 -X "$@"
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
        log "smart-node-mesh service already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service unit does not -- treating as a resumed run"
    fi
}

ensure_dirs() {
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$LOG_DIR"
    manifest_record dir "$INSTALL_DIR"
    manifest_record dir "$LOG_DIR"
}

# load_or_generate_secrets -- generate DB passwords once, then re-read.
# Preserves existing passwords on re-run or upgrade from v0.2.x.
load_or_generate_secrets() {
    if [[ -f "$MEMORY_ENV" ]]; then
        log "memory.env present -- loading passwords (generate-once)"
        # shellcheck source=/dev/null
        source "$MEMORY_ENV"
        [[ -n "${SMART_NODE_PG_MESH_PW:-}" && -n "${SMART_NODE_PG_MESH_APP_PW:-}" ]] \
            || die "memory.env present but a password is missing -- inspect $MEMORY_ENV"
        return 0
    fi
    command -v openssl >/dev/null 2>&1 \
        || die "openssl not found"
    log "generating mesh DB passwords"
    SMART_NODE_PG_MESH_PW=$(openssl rand -hex 24)
    SMART_NODE_PG_MESH_APP_PW=$(openssl rand -hex 24)
    # Write a minimal file first so crashes do not lose the secrets.
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node memory companion -- generated by memory.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root. Generate-once passwords."
        echo "SMART_NODE_PG_MESH_PW=$SMART_NODE_PG_MESH_PW"
        echo "SMART_NODE_PG_MESH_APP_PW=$SMART_NODE_PG_MESH_APP_PW"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$MEMORY_ENV"
    rm -f "$tmp"
    manifest_record file "$MEMORY_ENV"
}

# create_mesh_db -- create the dedicated database, roles, and vector
# extension in the B02 cluster. Idempotent; passwords are always reset to
# the current values from memory.env so a re-run heals a broken password.
create_mesh_db() {
    # owner role
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_roles WHERE rolname='$MESH_ROLE'")" == 1 ]]; then
        psql_pg -c "ALTER ROLE \"$MESH_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PG_MESH_PW';"
    else
        log "creating role $MESH_ROLE"
        psql_pg -c "CREATE ROLE \"$MESH_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PG_MESH_PW';"
    fi
    # RLS app role
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_roles WHERE rolname='$MESH_APP_ROLE'")" == 1 ]]; then
        psql_pg -c "ALTER ROLE \"$MESH_APP_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PG_MESH_APP_PW';"
    else
        log "creating role $MESH_APP_ROLE"
        psql_pg -c "CREATE ROLE \"$MESH_APP_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_PG_MESH_APP_PW';"
    fi
    # database
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_database WHERE datname='$MESH_DB'")" == 1 ]]; then
        log "database $MESH_DB already exists"
    else
        log "creating database $MESH_DB (owner $MESH_ROLE)"
        psql_pg -c "CREATE DATABASE \"$MESH_DB\" OWNER \"$MESH_ROLE\";"
    fi
    # Vector extension (pgvector is installed cluster-wide by B02).
    log "ensuring vector extension and grants in $MESH_DB"
    psql_mesh -c "CREATE EXTENSION IF NOT EXISTS vector;"
    psql_mesh -c "GRANT ALL ON SCHEMA public TO \"$MESH_ROLE\";"
    psql_mesh -c "GRANT USAGE ON SCHEMA public TO \"$MESH_APP_ROLE\";"
    psql_mesh -c "ALTER DEFAULT PRIVILEGES FOR ROLE \"$MESH_ROLE\" IN SCHEMA public GRANT ALL ON TABLES TO \"$MESH_APP_ROLE\";"
    psql_mesh -c "ALTER DEFAULT PRIVILEGES FOR ROLE \"$MESH_ROLE\" IN SCHEMA public GRANT USAGE ON SEQUENCES TO \"$MESH_APP_ROLE\";"
}

# install_snapshot -- sync the vendored mesh Python source to the install
# tree. Venv, model cache, and memory.env are excluded from the sync.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored mesh snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/requirements.txt" && -d "$SRC_SNAPSHOT/mesh" ]] \
        || die "vendored mesh snapshot incomplete ($SRC_SNAPSHOT) -- missing requirements.txt or mesh/"
    log "syncing mesh snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' \
            --exclude 'model/' \
            --exclude 'memory.env' \
            --exclude '__pycache__/' \
            --exclude '*.pyc' \
            --exclude 'tests/' \
            --exclude 'examples/' \
            --exclude 'docs/' \
            --exclude 'scripts/' \
            --exclude 'Dockerfile*' \
            --exclude 'docker-compose*' \
            --exclude '.github/' \
            --exclude '.git/' \
            --exclude '*.md' \
            --exclude '*.egg-info/' \
            --exclude '.env' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        # rsync-free fallback: wipe source files, recopy, preserve venv/model.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv ! -name model ! -name memory.env \
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
    # Ensure model dir exists and is owned by the service user.
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$MODEL_DIR"
}

# build_venv -- create or reuse the venv and install pinned requirements.
# CPU-only torch is installed first to prevent pip from fetching the CUDA
# build (~2-3 GB) that sentence-transformers would otherwise pull in.
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
    # CPU-only torch MUST precede the main requirements so pip never fetches
    # the CUDA variant. This mirrors the mesh Dockerfile approach.
    log "installing CPU-only torch (prevents ~2 GB CUDA download)"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet \
        torch --index-url https://download.pytorch.org/whl/cpu \
        || die "CPU torch install failed -- check https://download.pytorch.org/whl/cpu is reachable"
    log "installing requirements"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip install of requirements failed"
}

# download_model -- pre-cache the embedding model at install time.
# Fails loudly if HuggingFace is unreachable. Skipped if already cached.
download_model() {
    # HuggingFace caches under hub/models--OWNER--NAME.
    local hf_owner="${EMBEDDING_MODEL%%/*}"
    local hf_name="${EMBEDDING_MODEL#*/}"
    local model_cache="$MODEL_DIR/hub/models--${hf_owner}--${hf_name}"

    if [[ -d "$model_cache" ]]; then
        log "embedding model already cached at $model_cache -- skipping download"
        return 0
    fi

    log "downloading embedding model $EMBEDDING_MODEL (first time, ~560 MB) ..."
    log "model will be cached at $MODEL_DIR (persistent across updates)"
    if ! HF_HOME="$MODEL_DIR" runuser -u "$SERVICE_USER" -- \
            "$VENV_DIR/bin/python" -c "
import os
os.environ['HF_HOME'] = '$MODEL_DIR'
from sentence_transformers import SentenceTransformer
print('[mesh] downloading $EMBEDDING_MODEL ...')
SentenceTransformer('$EMBEDDING_MODEL')
print('[mesh] model downloaded successfully')
" 2>&1; then
        cat >&2 <<ERR

[ERROR] embedding model download failed.

The memory companion requires $EMBEDDING_MODEL (~560 MB) from HuggingFace.
The download failed -- likely because this node has no outbound internet
access or huggingface.co is blocked.

To pre-seed the model manually:
  1. On a machine with internet access:
     pip install sentence-transformers
     python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$EMBEDDING_MODEL')"
  2. Locate the cache (usually ~/.cache/huggingface/hub/models--${hf_owner}--${hf_name})
  3. Rsync it to $MODEL_DIR/hub/ on this node
  4. Ensure ownership: chown -R smart-node-mesh:smart-node $MODEL_DIR
  5. Re-run this installer

ERR
        die "embedding model download failed -- see instructions above"
    fi
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$MODEL_DIR"
    log "embedding model cached at $MODEL_DIR"
}

# write_mesh_contract -- write /etc/smart-node/mesh.env so core-api and
# dispatcher can resolve the native mesh URL without querying Docker.
# core-api.sh reads MESH_API_URL from this file as its fallback.
write_mesh_contract() {
    local contract_dir="/etc/smart-node"
    local contract="$contract_dir/mesh.env"
    install -d -m 0755 -o root -g root "$contract_dir"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node mesh (memory) service contract -- generated by memory.sh@$STAGE_VERSION."
        echo "# Read by core-api.sh resolve_mesh_url() to locate the native service."
        echo "MESH_API_URL=http://${MESH_API_HOST}:${MESH_HTTP_PORT}"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$contract"
    rm -f "$tmp"
    manifest_record file "$contract"
    log "wrote mesh service contract -> $contract"
}

# write_env -- write (or refresh) memory.env with passwords + derived config.
# Called AFTER load_or_generate_secrets so the password vars are set.
# Passwords are never changed here; only derived config is refreshed.
write_env() {
    local pg_port="${SMART_NODE_PG_PORT:-5432}"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node memory companion -- generated by memory.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root."
        echo "# Passwords: generate-once. Config: refreshed on each run."
        echo ""
        echo "# Generate-once DB passwords."
        echo "SMART_NODE_PG_MESH_PW=$SMART_NODE_PG_MESH_PW"
        echo "SMART_NODE_PG_MESH_APP_PW=$SMART_NODE_PG_MESH_APP_PW"
        echo ""
        echo "# Runtime config (read by systemd EnvironmentFile and the Python app)."
        echo "DATABASE_URL=postgresql://${MESH_ROLE}:${SMART_NODE_PG_MESH_PW}@127.0.0.1:${pg_port}/${MESH_DB}"
        echo "DATABASE_URL_APP=postgresql://${MESH_APP_ROLE}:${SMART_NODE_PG_MESH_APP_PW}@127.0.0.1:${pg_port}/${MESH_DB}"
        echo "MESH_APP_PASSWORD=$SMART_NODE_PG_MESH_APP_PW"
        echo "EMBEDDING_MODEL=$EMBEDDING_MODEL"
        echo "AUTH_REQUIRED=false"
        echo "ENVIRONMENT=production"
        echo "LOG_LEVEL=INFO"
        echo "API_HOST=$MESH_API_HOST"
        echo "API_PORT=$MESH_HTTP_PORT"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$MEMORY_ENV"
    rm -f "$tmp"
    manifest_record file "$MEMORY_ENV"
}

# migrate_from_container -- remove legacy Docker containers from v0.2.x.
# Non-fatal: a fresh node has no containers; missing docker is also fine.
migrate_from_container() {
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    for ct in "$LEGACY_CT" "$LEGACY_MCP_CT"; do
        if docker ps -aq -f "name=^${ct}\$" 2>/dev/null | grep -q .; then
            log "removing legacy Docker container '$ct' (upgrade from v0.2.x)"
            docker rm -f "$ct" >/dev/null 2>&1 || true
        fi
    done
}

# render_unit -- write the main mesh API systemd unit.
render_unit() {
    local pg_cluster pg_unit
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    [[ -n "$pg_cluster" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    pg_unit="postgresql@${pg_cluster/\//-}.service"

    cat > "$1" <<EOF
[Unit]
Description=Smart Node memory companion (semantic memory API; gunicorn on 127.0.0.1:$MESH_HTTP_PORT)
After=network-online.target $pg_unit
Wants=network-online.target
Requires=$pg_unit

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
Environment=HF_HOME=$MODEL_DIR
EnvironmentFile=$MEMORY_ENV
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/gunicorn mesh.main:app -k uvicorn.workers.UvicornWorker --bind ${MESH_API_HOST}:${MESH_HTTP_PORT} --workers 1 --graceful-timeout 30 --timeout 120
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR $MODEL_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
# Also wanted by the database: Requires= stops this unit when the cluster is
# stopped, and nothing would start it again when the cluster comes back.
WantedBy=multi-user.target $pg_unit
EOF
}

# render_mcp_unit -- write the MCP sidecar systemd unit.
render_mcp_unit() {
    cat > "$1" <<EOF
[Unit]
Description=Smart Node memory MCP sidecar (streamable-http on :$MESH_MCP_PORT)
After=$SYSTEMD_UNIT
Requires=$SYSTEMD_UNIT

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
Environment=MESH_API_URL=http://${MESH_API_HOST}:${MESH_HTTP_PORT}
Environment=MESH_MCP_PORT=$MESH_MCP_PORT
Environment=MESH_MCP_HOST=0.0.0.0  # bind all interfaces; ufw restricts access to bridge subnets
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python mcp_server.py --transport http
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR
StandardOutput=append:$LOG_DIR/mcp.log
StandardError=append:$LOG_DIR/mcp.log

[Install]
# Also wanted by the API unit, for the same reason: started again with it.
WantedBy=multi-user.target $SYSTEMD_UNIT
EOF
}

# open_firewall -- allow bridge subnet access to the MCP sidecar port.
# the sidecar binds 0.0.0.0 so containers can reach it via
# host.docker.internal. ufw default is deny-incoming; without this rule the
# sidecar is unreachable from containers. The ALLOW is scoped to the docker
# bridge subnet only -- never to Anywhere.
open_firewall() {
    local sn_cidr
    sn_cidr=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    if command -v ufw >/dev/null 2>&1 && [[ -n "$sn_cidr" ]]; then
        ufw allow from "$sn_cidr" to any port "$MESH_MCP_PORT" proto tcp >/dev/null 2>&1 || true
        log "ufw: allowed $sn_cidr -> :$MESH_MCP_PORT (MCP sidecar)"
    fi
}

# remove_mesh_relay -- remove the work-mcp-mesh-relay forwarding unit if present.
# now that the MCP sidecar binds 0.0.0.0, the relay that forwarded
# 172.19.0.1:8100 -> 127.0.0.1:8100 is no longer needed. Its whole job was to
# paper over the loopback-only bind; removing it is the correct current state.
remove_mesh_relay() {
    local relay_unit="work-mcp-mesh-relay.service"
    local relay_path="/etc/systemd/system/${relay_unit}"
    if [[ -f "$relay_path" ]] || systemctl is-active --quiet "$relay_unit" 2>/dev/null; then
        log "removing legacy $relay_unit"
        systemctl stop    "$relay_unit" 2>/dev/null || true
        systemctl disable "$relay_unit" 2>/dev/null || true
        rm -f "$relay_path"
        systemctl daemon-reload 2>/dev/null || true
        log "$relay_unit removed"
    else
        log "$relay_unit not present -- nothing to remove"
    fi
}

install_service() {
    install -d -m 0755 -o root -g root /etc/systemd/system
    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record file "$SYSTEMD_PATH"

    tmp=$(mktemp)
    render_mcp_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_MCP_PATH"
    rm -f "$tmp"
    manifest_record file "$SYSTEMD_MCP_PATH"

    systemctl daemon-reload

    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || log "WARN: could not enable $SYSTEMD_UNIT"
    systemctl enable "$SYSTEMD_MCP_UNIT" >/dev/null 2>&1 \
        || log "WARN: could not enable $SYSTEMD_MCP_UNIT"

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
    manifest_record systemd-unit "$SYSTEMD_MCP_UNIT"
}

# wait_for_mesh -- poll /health until the API answers or the 5-minute deadline.
# First boot runs DB table creation and lazily loads the embedding model.
wait_for_mesh() {
    local deadline=$(( $(date +%s) + 300 ))
    log "waiting for mesh API /health (first boot creates DB tables; up to 5 min)"
    while (( $(date +%s) < deadline )); do
        if curl -sf "http://${MESH_API_HOST}:${MESH_HTTP_PORT}/health" \
                >/dev/null 2>&1; then
            log "mesh API is healthy"
            log "starting $SYSTEMD_MCP_UNIT"
            systemctl start "$SYSTEMD_MCP_UNIT" >/dev/null 2>&1 \
                || log "WARN: MCP sidecar did not start cleanly -- will retry on next boot"
            return 0
        fi
        sleep 5
    done
    die "mesh API did not become healthy within 5 min -- check $LOG_DIR/service.log"
}

write_report_section() {
    local body
    body=$(cat <<EOF
**Version:** memory@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  Mesh (native systemd)
api:        $SYSTEMD_UNIT  (gunicorn/uvicorn on ${MESH_API_HOST}:${MESH_HTTP_PORT})
mcp:        $SYSTEMD_MCP_UNIT  (streamable-http on :${MESH_MCP_PORT})
source:     $INSTALL_DIR  (from companions/mesh/)
venv:       $VENV_DIR
model:      $MODEL_DIR  ($EMBEDDING_MODEL)
database:   $MESH_DB DB + roles $MESH_ROLE / $MESH_APP_ROLE in the B02 cluster
db url:     postgresql://...@127.0.0.1:${SMART_NODE_PG_PORT:-5432}/$MESH_DB (scram-sha-256)
env:        $MEMORY_ENV  (0600 root:root, generate-once passwords)
\`\`\`

The node's semantic memory store. Internal service: bound to 127.0.0.1,
no Traefik route. The MCP sidecar (streamable-http) is the agent-facing
door on port $MESH_MCP_PORT.

Upgrade note: any smart-node-mesh / smart-node-mesh-mcp Docker containers
from v0.2.x are removed automatically. Images are left for manual cleanup.
EOF
)
    report_section "Companion memory" "$body"
}

run_intent() {
    require_root
    log "companion memory -- start (v$STAGE_VERSION native)"
    require_stage B02
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    ensure_python
    probe_before
    ensure_dirs
    create_service_user "$SERVICE_SHORT"
    load_or_generate_secrets
    create_mesh_db
    install_snapshot
    build_venv
    download_model
    write_env
    migrate_from_container
    write_mesh_contract
    install_service
    open_firewall
    remove_mesh_relay
    wait_for_mesh
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    write_report_section
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=memory@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=memory@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"mesh\",\"native\":true,\"checks\":$VERIFY_TOTAL}"
    fi
    if declare -F sn_integration_register >/dev/null; then
        local tools_tmp
        tools_tmp=$(mktemp)
        cat > "$tools_tmp" << 'TOOLS'
[
  {"name":"mesh","kind":"mcp","title":"Memory","description":"Semantic memory: search what this node already knows, and record what it learns.",
   "usage_md":"## mesh -- this node's memory\n\nmesh is where the node remembers. A turn is ephemeral; anything worth keeping goes here or it is lost.\n\n**Search before you act.** If the work could have prior context -- a customer, a decision, a recurring problem -- search first. Repeating work the node has already done is the most common waste.\n\n**Write outcomes, not chatter.** Decisions, results, things learned the hard way. One fact per document, so it can be found on its own. A transcript of a conversation is not a memory.\n\n**Tag every write with a type and a date.** Untagged documents are found by accident or not at all.\n\n**Never put a secret in memory.** Passwords, tokens and keys belong in the node's secret store. Memory is searchable by every agent that shares the workspace.\n\nTools: mesh_search and mesh_bytag to find, mesh_get to read one, mesh_add to write, mesh_update to correct, mesh_recent for what happened lately, mesh_stats and mesh_tags to see the shape of what is stored, mesh_schema for the tag taxonomy, mesh_versions to see how one document changed, mesh_projects to list projects, mesh_focus to set the workspace your writes go to, and mesh_delete to remove a document that should not exist."},
  {"name":"mesh_search",   "kind":"mcp","title":"Search memory",     "description":"Find documents by meaning."},
  {"name":"mesh_bytag",    "kind":"mcp","title":"Find by tag",       "description":"Find documents by exact tag."},
  {"name":"mesh_get",      "kind":"mcp","title":"Read a document",   "description":"Read one document by id."},
  {"name":"mesh_add",      "kind":"mcp","title":"Remember",          "description":"Write a document to memory."},
  {"name":"mesh_update",   "kind":"mcp","title":"Correct a memory",  "description":"Update an existing document."},
  {"name":"mesh_delete",   "kind":"mcp","title":"Forget",            "description":"Delete a document."},
  {"name":"mesh_recent",   "kind":"mcp","title":"Recent",            "description":"Most recent documents, optionally by type."},
  {"name":"mesh_versions", "kind":"mcp","title":"History",           "description":"Version history of one document."},
  {"name":"mesh_focus",    "kind":"mcp","title":"Set workspace",     "description":"Set the active memory workspace."},
  {"name":"mesh_projects", "kind":"mcp","title":"Projects",          "description":"List known projects."},
  {"name":"mesh_stats",    "kind":"mcp","title":"Stats",             "description":"Counts and coverage of what is stored."},
  {"name":"mesh_tags",     "kind":"mcp","title":"Tags",              "description":"Tags in use."},
  {"name":"mesh_schema",   "kind":"mcp","title":"Tag schema",        "description":"The tag taxonomy."}
]
TOOLS
        sn_integration_register \
            --slug             mesh \
            --label            "Memory" \
            --purpose          "Notes agents write down and search later" \
            --kind             app \
            --status           available \
            --install-uri      "http://memory.${SMART_NODE_DOMAIN}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Semantic memory + workspaces." \
            --capabilities     '["memory"]' \
            --tools-json       "$tools_tmp" \
            --companion-slug   "$STAGE_ID" || true
        rm -f "$tools_tmp"
    fi
    if declare -F sn_mcp_server_register >/dev/null; then
        sn_mcp_server_register \
            --slug      "mesh" \
            --name      "Mesh" \
            --transport http \
            --url       "http://localhost:${MESH_MCP_PORT}/mcp" || true
    fi
    log "companion memory -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    load_contracts
    [[ -f "$MEMORY_ENV" ]] && source "$MEMORY_ENV"
    local pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. service user exists.
    verify_check "service user $SERVICE_USER exists" bash -c "
        id '$SERVICE_USER' >/dev/null 2>&1"
    # 3. install dir present.
    verify_check "install dir $INSTALL_DIR present" test -d "$INSTALL_DIR"
    # 4. venv has gunicorn.
    verify_check "venv has gunicorn" test -x "$VENV_DIR/bin/gunicorn"
    # 5. venv has uvicorn (worker class for gunicorn).
    verify_check "venv has uvicorn" test -x "$VENV_DIR/bin/uvicorn"
    # 6. memory.env present and 0600.
    verify_check "memory.env present 0600" bash -c "
        [[ -f '$MEMORY_ENV' && \$(stat -c '%a' '$MEMORY_ENV' 2>/dev/null) == 600 ]]"
    # 7. model dir has model files (presence of a config.json inside hub/).
    verify_check "embedding model cached in $MODEL_DIR" bash -c "
        [[ -d '$MODEL_DIR/hub' ]] && find '$MODEL_DIR/hub' -name 'config.json' | grep -q ."
    # 8. mesh database exists.
    verify_check "database $MESH_DB exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d postgres -tAc \
            \"SELECT 1 FROM pg_database WHERE datname='$MESH_DB'\" 2>/dev/null) == 1 ]]"
    # 9. mesh owner role exists.
    verify_check "role $MESH_ROLE exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d postgres -tAc \
            \"SELECT 1 FROM pg_roles WHERE rolname='$MESH_ROLE'\" 2>/dev/null) == 1 ]]"
    # 10. mesh_app RLS role exists.
    verify_check "role $MESH_APP_ROLE exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d postgres -tAc \
            \"SELECT 1 FROM pg_roles WHERE rolname='$MESH_APP_ROLE'\" 2>/dev/null) == 1 ]]"
    # 11. vector extension in the mesh DB.
    verify_check "vector extension in $MESH_DB" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '${pg_cluster}' -d '$MESH_DB' -tAc \
            \"SELECT 1 FROM pg_extension WHERE extname='vector'\" 2>/dev/null) == 1 ]]"
    # 12. systemd unit installed.
    verify_check "$SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # 13. main service active.
    verify_check "$SYSTEMD_UNIT active" bash -c "
        systemctl is-active '$SYSTEMD_UNIT' >/dev/null 2>&1"
    # 14. MCP unit installed.
    verify_check "$SYSTEMD_MCP_UNIT installed" test -f "$SYSTEMD_MCP_PATH"
    # 15. MCP service active.
    verify_check "$SYSTEMD_MCP_UNIT active" bash -c "
        systemctl is-active '$SYSTEMD_MCP_UNIT' >/dev/null 2>&1"
    # 16. /health answers.
    verify_check "mesh API answers /health" bash -c "
        curl -sf 'http://${MESH_API_HOST}:${MESH_HTTP_PORT}/health' >/dev/null 2>&1"
    # 17. companion manifest written.
    verify_check "companions.d/memory.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/memory.json"
    # 18. install-report has the memory section.
    verify_check "install-report has the memory section" \
        bash -c "grep -qFx '## Companion memory' '$SMART_NODE_REPORT'"
    # 19. manifest records the main unit.
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
    load_contracts
    log "companion memory -- uninstall"

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
            "SELECT pg_size_pretty(pg_database_size('$MESH_DB'))" 2>/dev/null \
            | tr -d '[:space:]' || echo "unknown")
    else
        db_size="unknown"
    fi
    echo ""
    echo "About to PERMANENTLY uninstall the memory companion (mesh) from this node."
    echo "  Node FQDN: $node_fqdn"
    echo "  Database:  $MESH_DB (${db_size})"
    echo "  Services:  $SYSTEMD_UNIT, $SYSTEMD_MCP_UNIT"
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
    dump_file="$backup_dir/memory-${MESH_DB}-${dump_stamp}.dump"
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
    log "dumping $MESH_DB -> $dump_file"
    if ! runuser -u postgres -- pg_dump \
            --cluster "$SMART_NODE_PG_CLUSTER" -Fc "$MESH_DB" \
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
        sn_integration_unregister --slug mesh || true
    fi
    # Stop and disable services.
    for unit in "$SYSTEMD_MCP_UNIT" "$SYSTEMD_UNIT"; do
        systemctl stop "$unit" >/dev/null 2>&1 || true
        systemctl disable "$unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload >/dev/null 2>&1 || true
    # Also remove any legacy Docker containers if still present.
    if command -v docker >/dev/null 2>&1; then
        docker rm -f "$LEGACY_CT" >/dev/null 2>&1 || true
        docker rm -f "$LEGACY_MCP_CT" >/dev/null 2>&1 || true
    fi
    # Step 5: Drop -- only after the dump is confirmed written.
    psql_pg -c "DROP DATABASE IF EXISTS \"$MESH_DB\";" 2>/dev/null || true
    psql_pg -c "DROP ROLE IF EXISTS \"$MESH_APP_ROLE\";" 2>/dev/null || true
    psql_pg -c "DROP ROLE IF EXISTS \"$MESH_ROLE\";" 2>/dev/null || true
    # Remove the mesh service contract and the install tree.
    rm -f /etc/smart-node/mesh.env
    rm -rf "$INSTALL_DIR"
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion memory -- uninstalled"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   create_service_user (OS account, one-time), ensure_dirs (directory
#   create, one-time), create_mesh_db (DB creation, one-time),
#   install_snapshot (extract application files, version-gated), build_venv
#   (pip install, version-gated), download_model (embedding model download,
#   version-gated), migrate_from_container (one-time data migration).
#
# CONTRACT (run): load_contracts (needed to resolve env vars for write_env
#   and write_mesh_contract), load_or_generate_secrets (NOTE: listed here,
#   not in INSTALL -- the function is idempotent; when the secrets file
#   already exists it sources it without regenerating, which is what
#   contracts-only needs; it is only expensive the first time; write_env
#   and write_mesh_contract both consume the credentials it populates),
#   write_env (env file read by the mesh service on every restart -- must be
#   current), write_mesh_contract (mesh contract file read by agents to
#   locate the service -- must be current), install_service (systemd unit
#   file -- must be current), register_companion (companions.d record for
#   version tracking), sn_integration_register (sn_config row -- must be
#   current), sn_mcp_server_register (sn_config mcp row -- must be current),
#   stage_mark done.
run_contracts_only() {
    require_root
    log "companion memory -- contracts only (v$STAGE_VERSION)"
    require_stage B02
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    load_or_generate_secrets
    write_env
    write_mesh_contract
    install_service
    open_firewall
    remove_mesh_relay
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    if declare -F sn_integration_register >/dev/null; then
        local tools_tmp
        tools_tmp=$(mktemp)
        cat > "$tools_tmp" << 'TOOLS'
[
  {"name":"mesh","kind":"mcp","title":"Memory","description":"Semantic memory: search what this node already knows, and record what it learns.",
   "usage_md":"## mesh -- this node's memory\n\nmesh is where the node remembers. A turn is ephemeral; anything worth keeping goes here or it is lost.\n\n**Search before you act.** If the work could have prior context -- a customer, a decision, a recurring problem -- search first. Repeating work the node has already done is the most common waste.\n\n**Write outcomes, not chatter.** Decisions, results, things learned the hard way. One fact per document, so it can be found on its own. A transcript of a conversation is not a memory.\n\n**Tag every write with a type and a date.** Untagged documents are found by accident or not at all.\n\n**Never put a secret in memory.** Passwords, tokens and keys belong in the node's secret store. Memory is searchable by every agent that shares the workspace.\n\nTools: mesh_search and mesh_bytag to find, mesh_get to read one, mesh_add to write, mesh_update to correct, mesh_recent for what happened lately, mesh_stats and mesh_tags to see the shape of what is stored, mesh_schema for the tag taxonomy, mesh_versions to see how one document changed, mesh_projects to list projects, mesh_focus to set the workspace your writes go to, and mesh_delete to remove a document that should not exist."},
  {"name":"mesh_search",   "kind":"mcp","title":"Search memory",     "description":"Find documents by meaning."},
  {"name":"mesh_bytag",    "kind":"mcp","title":"Find by tag",       "description":"Find documents by exact tag."},
  {"name":"mesh_get",      "kind":"mcp","title":"Read a document",   "description":"Read one document by id."},
  {"name":"mesh_add",      "kind":"mcp","title":"Remember",          "description":"Write a document to memory."},
  {"name":"mesh_update",   "kind":"mcp","title":"Correct a memory",  "description":"Update an existing document."},
  {"name":"mesh_delete",   "kind":"mcp","title":"Forget",            "description":"Delete a document."},
  {"name":"mesh_recent",   "kind":"mcp","title":"Recent",            "description":"Most recent documents, optionally by type."},
  {"name":"mesh_versions", "kind":"mcp","title":"History",           "description":"Version history of one document."},
  {"name":"mesh_focus",    "kind":"mcp","title":"Set workspace",     "description":"Set the active memory workspace."},
  {"name":"mesh_projects", "kind":"mcp","title":"Projects",          "description":"List known projects."},
  {"name":"mesh_stats",    "kind":"mcp","title":"Stats",             "description":"Counts and coverage of what is stored."},
  {"name":"mesh_tags",     "kind":"mcp","title":"Tags",              "description":"Tags in use."},
  {"name":"mesh_schema",   "kind":"mcp","title":"Tag schema",        "description":"The tag taxonomy."}
]
TOOLS
        sn_integration_register \
            --slug             mesh \
            --label            "Memory" \
            --purpose          "Notes agents write down and search later" \
            --kind             app \
            --status           available \
            --install-uri      "http://memory.${SMART_NODE_DOMAIN}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Semantic memory + workspaces." \
            --capabilities     '["memory"]' \
            --tools-json       "$tools_tmp" \
            --companion-slug   "$STAGE_ID" || true
        rm -f "$tools_tmp"
    fi
    if declare -F sn_mcp_server_register >/dev/null; then
        sn_mcp_server_register \
            --slug      "mesh" \
            --name      "Mesh" \
            --transport http \
            --url       "http://localhost:${MESH_MCP_PORT}/mcp" || true
    fi
    stage_mark "$STAGE_ID" done \
        "version=memory@$STAGE_VERSION" \
        "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
