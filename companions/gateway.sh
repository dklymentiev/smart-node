#!/bin/bash
#
# gateway.sh -- Smart Node installer, Layer C Phase 2 companion: gateway.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B04.
#
# Installs the gateway companion: the node's single egress to external
# LLMs. The component is ai-gateway -- a FastAPI/uvicorn service that
# fronts the Anthropic / Gemini / OpenRouter cloud APIs behind one
# HTTP interface with rate limiting, request logging and a shadow cache.
#
# Why a companion, not a bootstrap stage: per installer-stages.md
# the gateway depends only on B04 (secrets) and is
# installed, operator-triggered, in Phase 2. The comms shake-out
# is the motivating evidence -- a node with no local gateway
# was IP-blocked from a remote one; each node must own its egress.
#
# Scope -- cloud egress only. ai-gateway also carries a `cli` provider
# that spawns a local `claude` binary; that is agent-runtime territory,
# not external-LLM egress, and is left dormant here (no `claude` on a
# fresh node). This keeps the declared dependency `gateway <- B04`
# honest, with no hidden coupling to agent-runtimes.
#
# Native systemd service, NOT a container: ai-gateway is a Python app
# and B02 already establishes that not every Smart Node component is
# Docker. The component ships as a vendored snapshot in the sibling
# directory `ai-gateway/` (the B05/B06 vendoring pattern).
#
# Creates:
#   - service account smart-node-gateway (+ owned dirs) via lib.sh
#     create_service_user
#   - /opt/smart-node/gateway -- the install tree (vendored snapshot +
#     a Python venv with the pinned requirements)
#   - /opt/smart-node/gateway/.env (0600, service-user owned) -- the
#     runtime config: localhost bind, port, a generate-once
#     GATEWAY_API_KEY, the log dir, and -- if supplied -- the Anthropic
#     API key (SMART_NODE_ANTHROPIC_API_KEY); absent, the gateway
#     installs degraded.
#   - smart-node-gateway.service -- the systemd unit
#   - companions.d/gateway.json -- the manifest the B06 panel reads
#   - "Companion gateway" section in the install report
#   - /var/lib/smart-node/stages/gateway.state
#
# Binding: the service binds 127.0.0.1 only and ALLOWED_IPS is
# localhost. Native companions reach it on the loopback. Reaching the
# gateway from inside other companions' containers (bind to the docker
# bridge IP + a CIDR allowlist) is a deliberate follow-up -- it needs
# confirmation that ai-gateway's ALLOWED_IPS accepts CIDR, not just
# exact IPs. v0.1.0 ships the safe loopback-only bind.
#
# LLM API keys: the companion does not invent provider keys. The
# Anthropic key may be supplied at install via the environment variable
# SMART_NODE_ANTHROPIC_API_KEY -- it is written to .env and serves both
# the gateway's Anthropic egress and the `claude` CLI the agent-runtimes
# companion spawns (passed through in the session env). With no key the
# gateway still installs and runs degraded -- providers report
# unconfigured; a re-run with the variable set reconfigures it. --verify
# checks the service answers /api/v1/health; it does NOT check that any
# provider key works.
#
# MCP: ai-gateway exposes a REST API, not an MCP server, so no mcp.d/
# shim is registered in v0.1.0. If the gateway later grows an MCP
# facet, register_mcp is added then.
#
# Usage:
#   gateway.sh             full run: probe -> intent -> probe -> mark
#   gateway.sh --verify    idempotent post-check; exit 0 on full pass,
#                          N (fail count) with [VERIFY-FAIL]
#   gateway.sh --status    read /var/lib/smart-node/stages/gateway.state;
#                          exit 0=done 1=failed 2=missing
#   gateway.sh --uninstall reverse the install (service, tree, user,
#                          companion manifest)
#   gateway.sh --help      this message
#
# Environment:
#   SMART_NODE_GATEWAY_PORT       loopback port to bind (default 8000)
#   SMART_NODE_ANTHROPIC_API_KEY  Anthropic API key (optional; written to
#                                 .env, preserved across re-runs)
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

STAGE_ID="gateway"
STAGE_NAME="gateway"
STAGE_VERSION="0.2.12"

# Where this script lives -- the vendored smart-node-gateway snapshot sits beside
# it. install-on.sh / the operator ship the two together onto the node.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/gateway"

# Install tree, namespaced like every Smart Node artifact.
SERVICE_SHORT="gateway"
SERVICE_USER="smart-node-gateway"
INSTALL_DIR="/opt/smart-node/gateway"
VENV_DIR="$INSTALL_DIR/venv"
ENV_FILE="$INSTALL_DIR/.env"
LOG_DIR="/var/log/smart-node/gateway"

# systemd unit -- native service (ai-gateway is a Python app).
SYSTEMD_UNIT="smart-node-gateway.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"
# The runtime: the sibling process that owns the agent CLI processes
# (runtime_main.py). Same service account, own unit, so the door can be
# restarted without ending a single agent's turn, and the one process that
# needs sudo is the one that spawns.
RUNTIME_UNIT="smart-node-runtime.service"
RUNTIME_SYSTEMD_PATH="/etc/systemd/system/$RUNTIME_UNIT"

# Per-agent privilege drop. The gateway spawns each agent's CLI as that
# agent's own OS user rather than as its own service account, so that what an
# agent writes is written BY the agent and the group membership on a project
# work area applies to it. Two paths, one behaviour.
RUNAS_SUDOERS_PATH="/etc/sudoers.d/smart-node-gateway-runas"

# Credential wrapper. sudo logs the full environment; the wrapper is
# what sudo actually calls. It reads the credential from the agent-runtime.env
# file (root:smart-node-agents, mode 0640) and execs the real CLI binary, so
# the token never appears in the journal.
WRAPPER_BIN="$INSTALL_DIR/claude-agent-wrapper"

# Flint is optional. The installed binary must be a reviewed stdio build;
# merely updating the gateway never changes an agent's runtime.
FLINT_ABS_PATH="/usr/local/bin/flint"

# Session reaper -- node-level, root, deliberately outside $INSTALL_DIR.
REAPER_DIR="/opt/smart-node/ops"
REAPER_BIN="$REAPER_DIR/session-reaper.py"
REAPER_UNIT="smart-node-session-reaper.service"
REAPER_TIMER="smart-node-session-reaper.timer"

# Loopback bind. Port overridable; ai-gateway's own config default is
# 8000. localhost-only -- see the binding note in the header.
GW_HOST="127.0.0.1"
GW_PORT="${SMART_NODE_GATEWAY_PORT:-8000}"
# The runtime listens for the door only; loopback, never a published port.
# 8001 is the voice companion's (companions/voice.sh), hence 8002.
RT_HOST="127.0.0.1"
RT_PORT="${SMART_NODE_RUNTIME_PORT:-8002}"

# Published by communication.sh once a real outbound mailbox is known.  It is
# non-secret (actor, channel, address) and shared with core-api.
BUDGET_NOTIFICATIONS_ENV="/etc/smart-node/budget-notifications.env"
GATEWAY_CORE_API_ENV="/etc/smart-node/gateway-core-api-auth.env"

# lib.sh is installed by B01-core; the companion has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# The agent group. Current lib.sh defines it; a node installed before it did
# does not, and an update runs against exactly those nodes. Under `set -u` an
# absent variable stops the companion dead partway through -- measured on reference-node
# on 2026-08-14. core-api.sh carries the same line for the same reason.
SMART_NODE_AGENT_GROUP="${SMART_NODE_AGENT_GROUP:-smart-node-agents}"

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

# python_bin -- the interpreter to build the venv with. Plain python3;
# ensure_python guarantees it plus the venv module exist.
PYTHON_BIN="python3"

# ensure_python -- the node needs python3 and the stdlib venv module.
# Debian splits venv into its own package; install it if `python3 -m
# venv` is unavailable. pip arrives inside the venv via ensurepip.
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

# probe_before -- a re-run (our service/tree present) proceeds
# idempotently: the tree is re-synced, the venv reused, the unit
# rewritten. The generate-once GATEWAY_API_KEY in .env is preserved.
probe_before() {
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1 \
       && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "gateway service already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service unit does not -- treating as a resumed run"
    fi
}

# install_snapshot -- copy the vendored smart-node-gateway snapshot into the
# install tree. --delete-style refresh: the tree mirrors the snapshot,
# but .env and venv/ (runtime state, not snapshot content) are kept.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/main.py" && -f "$SRC_SNAPSHOT/requirements.txt" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no main.py / requirements.txt"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    log "syncing ai-gateway snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' --exclude '.env' --exclude 'logs/' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        # rsync-free fallback: wipe everything but venv/ + .env, recopy.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv ! -name .env -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$INSTALL_DIR"/
    fi
    # One supervisor implementation is shipped to both Python services.
    rm -f "$INSTALL_DIR/supervisor.py"  # the source tree uses a relative symlink
    install -m 0644 "$SCRIPT_DIR/core-api/lib/supervisor.py" "$INSTALL_DIR/supervisor.py"
    # Commit hash marker for sn-version (vendored snapshot has no .git).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$INSTALL_DIR/.sn-commit"
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
}

# build_venv -- create (or reuse) the venv and install the pinned
# requirements. pip runs as the service user; the venv is owned by it.
build_venv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "creating venv -> $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" \
            || die "venv creation failed ($VENV_DIR)"
    else
        log "venv already present -- reusing"
    fi
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$VENV_DIR"
    log "installing requirements (pip)"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip install of requirements failed"
}

# setup_db_ident -- peer auth for the gateway's DB connection. The gateway
# connects over the local unix socket as the OS user smart-node-gateway,
# authenticating as the shared core role (DSN `user=smartnode`). For
# Postgres peer auth to resolve that, the `smartnode` ident map must carry
# the line `smartnode  smart-node-gateway  smartnode` (OS user -> core
# role). Unlike a per-component role, the gateway maps onto the existing
# core role, so create_service_user's generic OS-user->same-name line does
# not apply here -- this stage owns the gateway's bespoke mapping.
#
# Mirrors the communication companion's setup_db_role: append the line
# idempotently, then reload the cluster so it takes effect. Reads the
# ident path + cluster from the B02 persistence contract.
setup_db_ident() {
    local pg_ident pg_cluster
    pg_ident=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_IDENT)
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    [[ -n "$pg_ident" && -n "$pg_cluster" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    [[ -f "$pg_ident" ]] \
        || die "pg_ident.conf missing ($pg_ident) -- cluster not created?"

    local core_role="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
    local ident_line="smartnode  $SERVICE_USER  $core_role"
    if ! grep -qF "$ident_line" "$pg_ident"; then
        echo "$ident_line" >> "$pg_ident"
        log "added the $SERVICE_USER line to the smartnode ident map"
    else
        log "smartnode ident map already carries the $SERVICE_USER line"
    fi

    # Activate the map edit. Prefer the systemd unit reload; fall back to
    # pg_ctlcluster if the unit is not the active manager (e.g. a manually
    # started cluster). cluster "16/smartnode" -> unit postgresql@16-smartnode.
    local pg_unit="postgresql@${pg_cluster/\//-}"
    if systemctl is-active "$pg_unit" >/dev/null 2>&1; then
        systemctl reload "$pg_unit" >/dev/null 2>&1 \
            || systemctl restart "$pg_unit" >/dev/null 2>&1 \
            || die "failed to reload $pg_unit after the ident-map change"
    else
        pg_ctlcluster "${pg_cluster%%/*}" "${pg_cluster##*/}" reload >/dev/null 2>&1 || true
    fi
}

# write_env -- the runtime config. Generate-once for GATEWAY_API_KEY: a
# re-run re-reads the existing .env and keeps the key, so clients that
# stored it keep working. The file is 0600, service-user owned.
# The gateway resolves provider credentials (`secret:<ref>` in cli_providers /
# providers config) from sn_config.secrets, the node's one secret store, decrypting them IN SQL with a
# symmetric master key read from a root-only key file. Both pieces are node-local
# state, not app code, so the companion must create them -- without the key file the
# gateway raises FileNotFoundError on the FIRST provider-key lookup and never answers
# /health, i.e. the whole install fails. Idempotent: an existing key is never rotated
# (that would orphan every secret already encrypted with it).
setup_secret_store() {
    local key_file="/etc/smart-node/gateway-secret.key"
    if [[ ! -s "$key_file" ]]; then
        install -d -m 0755 /etc/smart-node
        openssl rand -hex 32 | tr -d '\n' > "$key_file"
        chown root:"$SMART_NODE_GROUP" "$key_file"
        chmod 0640 "$key_file"
        log "gateway secret master key created ($key_file)"
    else
        log "gateway secret master key already present -- kept"
    fi
    # pgp_sym_encrypt/decrypt live in pgcrypto; the secret store is unusable without it.
    sudo -u postgres psql -d smartnode -qc "CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null 2>&1 \
        && log "pgcrypto ready (gateway secret store)" \
        || log "WARN: could not enable pgcrypto -- gateway secrets will not decrypt"
}

write_env() {
    local api_key=""
    if [[ -f "$ENV_FILE" ]]; then
        api_key=$(sed -n -E 's/^GATEWAY_API_KEY=//p' "$ENV_FILE" | tail -n1)
    fi
    if [[ -z "$api_key" ]]; then
        command -v openssl >/dev/null 2>&1 \
            || die "openssl not found -- B03 installs it; run B03 first"
        api_key=$(openssl rand -hex 24)
        log "generated a new GATEWAY_API_KEY"
    else
        log "preserving the existing GATEWAY_API_KEY (generate-once)"
    fi
    # Anthropic API key -- the operator supplies it via
    # SMART_NODE_ANTHROPIC_API_KEY; otherwise it is preserved from an
    # existing .env across re-runs. Absent -> the gateway installs
    # degraded (cloud egress + the spawned claude have no key).
    local anthropic_key="${SMART_NODE_ANTHROPIC_API_KEY:-}"
    if [[ -z "$anthropic_key" && -f "$ENV_FILE" ]]; then
        anthropic_key=$(sed -n -E 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | tail -n1)
    fi
    if [[ -n "$anthropic_key" ]]; then
        log "Anthropic API key configured"
    else
        log "WARN: no Anthropic API key -- installing degraded (re-run with SMART_NODE_ANTHROPIC_API_KEY to configure)"
    fi
    # OpenRouter API key -- same lifecycle as the Anthropic key. brin's
    # default model id is openrouter/... so a node expected to answer via
    # cloud egress needs this; absent -> openrouter egress is degraded.
    local openrouter_key="${SMART_NODE_OPENROUTER_API_KEY:-}"
    if [[ -z "$openrouter_key" && -f "$ENV_FILE" ]]; then
        openrouter_key=$(sed -n -E 's/^OPENROUTER_API_KEY=//p' "$ENV_FILE" | tail -n1)
    fi
    if [[ -n "$openrouter_key" ]]; then
        log "OpenRouter API key configured"
    else
        log "WARN: no OpenRouter API key -- openrouter egress degraded (re-run with SMART_NODE_OPENROUTER_API_KEY to configure)"
    fi
    local flint_enabled="${SMART_NODE_FLINT_ENABLED:-}"
    local flint_skip_permissions="${SMART_NODE_FLINT_SKIP_PERMISSIONS:-}"
    local flint_skip_permissions_users="${SMART_NODE_FLINT_SKIP_PERMISSIONS_USERS:-}"
    [[ -z "$flint_skip_permissions" || "$flint_skip_permissions" == true || "$flint_skip_permissions" == false ]] \
        || die "SMART_NODE_FLINT_SKIP_PERMISSIONS must be true or false"
    if [[ -f "$ENV_FILE" ]]; then
        [[ -n "$flint_enabled" ]] || flint_enabled=$(_envfile_get "$ENV_FILE" FLINT_ENABLED 2>/dev/null || true)
        [[ -n "$flint_skip_permissions" ]] || flint_skip_permissions=$(_envfile_get "$ENV_FILE" FLINT_SKIP_PERMISSIONS 2>/dev/null || true)
        [[ -n "$flint_skip_permissions_users" ]] || flint_skip_permissions_users=$(_envfile_get "$ENV_FILE" FLINT_SKIP_PERMISSIONS_USERS 2>/dev/null || true)
    fi
    # Installing the flint companion is the person's choice of Flint, so it turns
    # Flint on (flint.manifest.json; re-run as its dependent). Only an explicit
    # SMART_NODE_FLINT_ENABLED=false keeps it off then.
    if [[ -z "${SMART_NODE_FLINT_ENABLED:-}" ]] && stage_status flint >/dev/null 2>&1; then
        flint_enabled=true
    fi
    flint_enabled="${flint_enabled:-false}"
    flint_skip_permissions="${flint_skip_permissions:-false}"
    [[ "$flint_enabled" == true || "$flint_enabled" == false ]] \
        || die "FLINT_ENABLED must be true or false"
    [[ "$flint_skip_permissions" == true || "$flint_skip_permissions" == false ]] \
        || die "FLINT_SKIP_PERMISSIONS must be true or false"
    [[ -z "$flint_skip_permissions_users" || "$flint_skip_permissions_users" =~ ^agent-[a-z0-9-]+(,agent-[a-z0-9-]+)*$ ]] \
        || die "FLINT_SKIP_PERMISSIONS_USERS must be comma-separated agent OS users"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node gateway companion runtime config -- gateway.sh@$STAGE_VERSION."
        echo "# Mode 0600. Loaded by ai-gateway via python-dotenv."
        echo "HOST=$GW_HOST"
        echo "PORT=$GW_PORT"
        # The runtime (runtime_main.py) and where the door forwards to it.
        echo "RUNTIME_HOST=$RT_HOST"
        echo "RUNTIME_PORT=$RT_PORT"
        echo "GATEWAY_API_KEY=$api_key"
        echo "ALLOWED_IPS=127.0.0.1"
        echo "LOGS_DIR=$LOG_DIR"
        echo "ENABLE_DOCS=false"
        # Session-first gateway (RFC v0.3) writes ai_sessions / messages
        # rows in the B02 core DB. Peer auth: the service runs as
        # SERVICE_USER and connects over the local unix socket -- no
        # password. The B05-identity stage GRANTed the smart-node-gateway
        # role on the relevant schemas.
        echo "SMARTNODE_DB_DSN=user=${SMART_NODE_PG_CORE_ROLE:-smartnode} dbname=${SMART_NODE_PG_CORE_DB:-smartnode}"
        if [[ -n "$anthropic_key" ]]; then
            # Serves the gateway's Anthropic egress and is passed through
            # to the claude CLI the agent-runtimes companion spawns.
            echo "ANTHROPIC_API_KEY=$anthropic_key"
        else
            echo "# ANTHROPIC_API_KEY unset -- gateway degraded. Re-run"
            echo "# gateway.sh with SMART_NODE_ANTHROPIC_API_KEY set."
        fi
        if [[ -n "$openrouter_key" ]]; then
            echo "OPENROUTER_API_KEY=$openrouter_key"
        else
            echo "# OPENROUTER_API_KEY unset -- openrouter egress degraded."
        fi
        # The CLI backend spawns `claude` for the node's OWN agents, acting on the node
        # they administer. Without this the spawned agent has no tool permissions: it
        # answers in character and then cannot read the planner or touch a file ("no permission
        # for network calls"), so a chat-driven onboarding cannot write anything. Prod
        # (reference-node) has always run with it; the installer now sets it too.
        echo "SKIP_PERMISSIONS=true"
        echo "FLINT_ENABLED=$flint_enabled"
        echo "FLINT_SKIP_PERMISSIONS=$flint_skip_permissions"
        echo "FLINT_SKIP_PERMISSIONS_USERS=$flint_skip_permissions_users"
    } > "$tmp"
    install -m 0600 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    manifest_record file "$ENV_FILE"
}

ensure_gateway_core_api_secret() {
    local existing secret tmp
    existing=$(_envfile_get "$GATEWAY_CORE_API_ENV" SN_GATEWAY_CORE_API_SECRET 2>/dev/null || true)
    [[ -n "$existing" ]] && return 0
    secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    [[ ${#secret} -eq 64 ]] || die "could not generate gateway core-api proof"
    install -d -m 0755 /etc/smart-node
    tmp=$(mktemp)
    printf 'SN_GATEWAY_CORE_API_SECRET=%s\n' "$secret" > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$GATEWAY_CORE_API_ENV"
    rm -f "$tmp"
    manifest_record file "$GATEWAY_CORE_API_ENV"
}

reload_core_api_for_gateway_proof() {
    # Optional EnvironmentFile may have been absent when core-api started.
    if systemctl is-active --quiet smart-node-core-api.service 2>/dev/null; then
        systemctl restart smart-node-core-api.service \
            || die "core-api did not restart with gateway owner-notice proof"
    fi
}

# resolve_cli_path -- absolute path of the agent CLI, empty if it is not here.
# A node can be installed before the CLI is, so this is not fatal on its own;
# it only decides whether the rule below points at something real.
resolve_cli_path() {
    local p
    p=$(command -v claude 2>/dev/null || true)
    [[ -z "$p" && -x /usr/local/bin/claude ]] && p=/usr/local/bin/claude
    printf '%s' "$p"
}

resolve_flint_path() {
    local p
    p=$(command -v flint 2>/dev/null || true)
    printf '%s' "${p:-/usr/local/bin/flint}"
}

# install_wrapper -- install the credential wrapper that sudo calls in place of
# the CLI binary. The wrapper reads CLAUDE_CODE_OAUTH_TOKEN from
# /etc/smart-node/agent-runtime.env (root:smart-node-agents, mode 0640) and
# execs the real CLI, so the token never passes through sudo's environment
# logger. Must run AFTER install_snapshot so $INSTALL_DIR exists.
install_wrapper() {
    local src="$SCRIPT_DIR/gateway-ops/claude-agent-wrapper"
    [[ -f "$src" ]] \
        || die "credential wrapper missing ($src) -- the companion ships it as a sibling file"
    # 0755: the file is in $INSTALL_DIR (owned by the service user), but sudo
    # runs it as the target agent user, who must be able to execute it.
    install -m 0755 -o root -g root "$src" "$WRAPPER_BIN"
    manifest_record file "$WRAPPER_BIN"
    log "credential wrapper installed: $WRAPPER_BIN"
}

# install_runas_sudoers -- let the gateway run the credential wrapper as an
# agent, and nothing else as anybody else. Must run BEFORE
# install_service, which writes the wrapper path into the unit.
install_runas_sudoers() {
    CLI_ABS_PATH="$(resolve_cli_path)"
    FLINT_ABS_PATH="$(resolve_flint_path)"
    if [[ -z "$CLI_ABS_PATH" ]]; then
        CLI_ABS_PATH="/usr/local/bin/claude"
        log "[WARN] agent CLI not found on PATH; the runas rule is written for" \
            "$CLI_ABS_PATH. Until the CLI exists there, an agent-backed chat" \
            "fails at spawn -- loudly, which is the point: the alternative is" \
            "running every agent as the gateway again without saying so."
    fi
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node -- gateway per-agent privilege drop. Generated; do not edit.
#
# The gateway spawns each agent's CLI as that agent's own OS user, so that a
# project work area granted by unix group applies to the agent on the gateway
# path exactly as it already does on the timer path.
#
# TWO commands, and the second is not decoration:
#   the wrapper -- the spawn itself. The wrapper reads the credential from disk
#               and execs the real CLI binary, so the token never appears in
#               sudo's environment log.
#   kill     -- a process the gateway no longer owns cannot be signalled by it.
#               Measured on a node: a direct kill answers "Operation not
#               permitted" and the child lives on. Without this line every
#               reclaim path (idle sweep, turn timeout, cap eviction, shutdown)
#               becomes a no-op that logs success while leaving an orphaned process.
#
# Runas is the GROUP, so adding an agent needs no edit here, and root is not a
# member of it. SETENV is required so non-credential env vars from the gateway
# are preserved; the credential itself is NOT in the preserved set -- it is
# read from the agent-runtime.env file by the wrapper.
Defaults:$SERVICE_USER !requiretty
$SERVICE_USER ALL=(%$SMART_NODE_AGENT_GROUP) NOPASSWD:SETENV: $WRAPPER_BIN, /usr/bin/kill
EOF
    if ! visudo -c -f "$tmp" >/dev/null 2>&1; then
        rm -f "$tmp"
        die "generated sudoers rule failed visudo validation -- refusing to install it"
    fi
    install -m 0440 -o root -g root "$tmp" "$RUNAS_SUDOERS_PATH"
    rm -f "$tmp"
    manifest_record file "$RUNAS_SUDOERS_PATH"
    log "per-agent privilege drop armed: $SERVICE_USER may run $WRAPPER_BIN as %$SMART_NODE_AGENT_GROUP"
}

# install_service -- write and activate the systemd unit. The service
# runs as the unprivileged service user; ai-gateway reads .env from its
# WorkingDirectory via python-dotenv.
# render_unit <path> -- write the unit THIS version of the companion wants.
#
# Split out of install_service so --verify can render the same text to a temp
# file and diff it against the one the node is actually running. A
# release that changes a unit is deployed as files; nothing re-runs the stage
# that generates it, so a node keeps the old unit while running the new code and
# says nothing. Two seeds shipped that way in August and had each run exactly
# once, by hand.
render_unit_env() {
    # The environment both units load. The door needs the credential files and
    # the core-api proofs for its own egress and notices; the runtime needs the
    # CLI, wrapper and memory contracts for the processes it spawns. One block,
    # so neither unit can fall a contract behind the other.
    CLI_ABS_PATH="${CLI_ABS_PATH:-$(resolve_cli_path)}"
    [[ -n "$CLI_ABS_PATH" ]] || CLI_ABS_PATH="/usr/local/bin/claude"
    cat <<EOF
# The node's LLM credential (CLAUDE_CODE_OAUTH_TOKEN) lives in agent-runtime.env.
# The gateway itself no longer reads this for spawned agents -- the credential
# wrapper reads it directly as the target agent user, so the token never appears
# in sudo's environment log. The EnvironmentFile is kept Optional (-) and
# still loaded so the gateway's OWN Anthropic egress (non-sudo path) continues to
# work.
EnvironmentFile=-/etc/smart-node/agent-runtime.env
# The core-api address, as a node contract. cli_claude.py refreshes an agent's
# system prompt from core-api before every run; with no address that call failed
# with "Request URL is missing an 'http://' protocol", the backend kept the
# stored prompt (44 bytes on a fresh node), and the agent came back with an
# empty answer while the gateway logged the chat as ok.
EnvironmentFile=-/etc/smart-node/core-api.env
# Budget exhaustion delivery.  Optional so gateway still starts on a
# node without email; absent config leaves notified_at retryable and logs why.
EnvironmentFile=-$BUDGET_NOTIFICATIONS_ENV
# Dedicated proof for the gateway's three owner-notification calls to core-api.
EnvironmentFile=-$GATEWAY_CORE_API_ENV
# The service credential, for the gateway's own reads of core-api (an agent's
# assembled prompt). Root-only; systemd reads it before dropping privileges.
# '-' so a node without it still starts, and each such read then fails naming
# the setting.
EnvironmentFile=-/etc/smart-node/core-api-service-auth.env
# Memory companion contract.  Optional because the gateway can be installed
# before memory; a handoff attempt without memory fails loud and keeps the old
# session alive.
EnvironmentFile=-/etc/smart-node/mesh.env
# Absolute, because a sudoers rule matches the command as written and the
# gateway's own default for this is the bare name \`claude\` resolved through
# PATH, which no rule can match. Resolved at install time; if the CLI
# ever moves, this is the line that has to move with it.
Environment=CLI_PATH=$CLI_ABS_PATH
Environment=FLINT_PATH=$FLINT_ABS_PATH
# Absolute path to the credential wrapper. The gateway calls this via
# sudo in place of the CLI directly. The wrapper reads the credential from
# /etc/smart-node/agent-runtime.env and execs the real CLI with it set.
Environment=CLI_AGENT_WRAPPER_PATH=$WRAPPER_BIN
EOF
}

render_unit() {
    # Self-sufficient on purpose: this is called from install_service, where
    # install_runas_sudoers has already resolved the CLI path, AND from
    # unit_is_current during --verify, where nothing has. Left to inherit, the
    # verify path renders an empty CLI_PATH and reports a difference that is not
    # there -- a check failing on itself, which is worse than one that passes.
    {
    cat <<EOF
[Unit]
Description=Smart Node gateway companion (the door -- every model call of the node)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EOF
    render_unit_env
    cat <<EOF
# The door passes chats' live lines through (desk-live) and a graceful stop waits
# for every open response; five seconds, then they are cut and reconnect.
ExecStart=$VENV_DIR/bin/python -m uvicorn main:app --host $GW_HOST --port $GW_PORT --timeout-graceful-shutdown 5
Restart=always
RestartSec=5
# The door spawns nothing: the agent processes are the runtime's
# ($RUNTIME_UNIT), and sudo with them. This is the hardening line the single
# process had to give up; it is back.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
WantedBy=multi-user.target
EOF
    } > "$1"
}

# render_runtime_unit <path> -- the runtime's unit: same account, same
# environment, its own process. The door forwards to it; it is never published.
render_runtime_unit() {
    {
    cat <<EOF
[Unit]
Description=Smart Node runtime (the agent CLI processes, reached through the gateway)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EOF
    render_unit_env
    cat <<EOF
# A chat on screen holds its live line open here (desk-live), and a graceful stop
# waits for every open response: a restart hung 90 s. Five seconds, then open
# responses are cut; a turn running at that moment is lost either way, because
# the shutdown ends the agent processes (owner's OK, 2026-10-30).
ExecStart=$VENV_DIR/bin/python -m uvicorn runtime_main:app --host $RT_HOST --port $RT_PORT --timeout-graceful-shutdown 5
Restart=always
RestartSec=5
# DELIBERATELY false, and this is the one hardening line this unit gives up:
# dropping to the agent's own user goes through sudo, which is setuid;
# NoNewPrivileges=true forbids exactly that and would make every agent run as
# the service account again -- silently, since the spawn would simply fail
# closed on a path nobody watches. What it can gain is bounded by
# $RUNAS_SUDOERS_PATH: one binary, run only as a member of
# $SMART_NODE_AGENT_GROUP, never as root.
NoNewPrivileges=false
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR
StandardOutput=append:$LOG_DIR/runtime.log
StandardError=append:$LOG_DIR/runtime.log

[Install]
WantedBy=multi-user.target
EOF
    } > "$1"
}

# unit_is_current -- does the node run the unit this version would write?
#
# Not "does a unit exist" and not "is it active": both are true of a node whose
# unit is a release behind. Renders and compares, so anything missing, stale or
# hand-edited shows as a difference rather than as silence.
# unit_diff_text -- print how the live unit differs from what this version
# generates, or nothing at all if they agree.
#
# It PRINTS rather than returning a verdict because verify_check swallows both
# streams of what it runs, so a diff produced inside it is lost and the operator
# is told only that something differs. "Silence is the part that must go" is the
# point of the rendered-unit comparison; a bare "the unit differs" is half of it.
unit_diff_text() {
    # unit_diff_text [render_fn live_path] -- the door's unit by default.
    local render="${1:-render_unit}" live="${2:-$SYSTEMD_PATH}"
    if [[ ! -f "$live" ]]; then
        echo "the unit is not installed at all ($live)"
        return 0
    fi
    local want; want=$(mktemp)
    "$render" "$want"
    if ! diff -q "$want" "$live" >/dev/null 2>&1; then
        diff -u "$live" "$want" 2>/dev/null \
            | sed -n '3,30p' | sed 's/^/      /'
    fi
    rm -f "$want"
}

install_service() {
    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    render_runtime_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$RUNTIME_SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record systemd-unit "$SYSTEMD_UNIT"
    manifest_record systemd-unit "$RUNTIME_UNIT"
    systemctl daemon-reload
    local unit
    for unit in "$SYSTEMD_UNIT" "$RUNTIME_UNIT"; do
        systemctl enable "$unit" >/dev/null 2>&1 \
            || die "systemctl enable $unit failed"
        # The runtime restart ends every agent process it owns; an install
        # is the one moment that is expected.
        systemctl restart "$unit" \
            || die "systemctl restart $unit failed"
    done
}

# install_session_reaper -- lay down the sweep for agent CLI processes the
# gateway spawns and does not own, plus its timer.
#
# Source is companions/gateway-ops/, a SIBLING of the vendored snapshot and not a
# directory inside it: install_snapshot syncs companions/gateway/ with
# `rsync -a --delete`, so anything of ours placed in there is destroyed by the
# next re-vendor, silently.
#
# Target is /opt/smart-node/ops/, a node-level path rather than a gateway-private
# one: it runs as root (the gateway does not) and reaps processes the gateway no
# longer tracks. That is also where it already lives on reference-node, the node this was
# extracted from, so the fleet keeps one path.
install_session_reaper() {
    local src="$SCRIPT_DIR/gateway-ops"
    [[ -f "$src/session-reaper.py" ]] \
        || die "session reaper missing ($src/session-reaper.py) -- the companion ships it as a sibling dir"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$REAPER_DIR"
    manifest_record dir "$REAPER_DIR"
    install -m 0755 -o root -g root "$src/session-reaper.py" "$REAPER_BIN"
    manifest_record file "$REAPER_BIN"
    install -m 0644 -o root -g root "$src/systemd/$REAPER_UNIT" "/etc/systemd/system/$REAPER_UNIT"
    install -m 0644 -o root -g root "$src/systemd/$REAPER_TIMER" "/etc/systemd/system/$REAPER_TIMER"
    manifest_record systemd-unit "$REAPER_UNIT"
    manifest_record systemd-unit "$REAPER_TIMER"
    systemctl daemon-reload
    # The TIMER is what gets enabled; the service is oneshot and is triggered by
    # it. Enabling the service instead would run it once at boot and never again.
    systemctl enable --now "$REAPER_TIMER" >/dev/null 2>&1 \
        || die "systemctl enable --now $REAPER_TIMER failed"
    log "session reaper installed ($REAPER_BIN, every 5min)"
}

# wait_for_gateway -- poll the health endpoint (auth-exempt) until the
# uvicorn app answers, against a 60s deadline.
wait_for_gateway() {
    # Both processes: the door answers at once; the runtime is what the
    # door's cli health is read from, so a node is not up until it answers too.
    local deadline=$(( $(date +%s) + 60 )) code rcode
    log "waiting for the gateway and the runtime to answer /api/v1/health"
    while (( $(date +%s) < deadline )); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            "http://$GW_HOST:$GW_PORT/api/v1/health" 2>/dev/null || true)
        rcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            "http://$RT_HOST:$RT_PORT/api/v1/health" 2>/dev/null || true)
        if [[ "$code" =~ ^2 && "$rcode" =~ ^2 ]]; then
            log "gateway is up (door HTTP $code, runtime HTTP $rcode)"
            return 0
        fi
        sleep 2
    done
    die "gateway did not answer /api/v1/health within 60s (door $code, runtime $rcode; units $SYSTEMD_UNIT, $RUNTIME_UNIT)"
}

write_report_section() {
    local anthropic_state="not set -- degraded (re-run to configure)"
    if [[ -f "$ENV_FILE" ]] && grep -qE '^ANTHROPIC_API_KEY=.+' "$ENV_FILE"; then
        anthropic_state="configured"
    fi
    local body
    body=$(cat <<EOF
**Version:** gateway@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  ai-gateway (vendored snapshot)
service:    $SYSTEMD_UNIT  (the door; native systemd, user $SERVICE_USER)
runtime:    $RUNTIME_UNIT  (the agent CLI processes; same user)
install:    $INSTALL_DIR
bind:       http://$GW_HOST:$GW_PORT  (loopback only; the runtime at $RT_HOST:$RT_PORT behind it)
config:     $ENV_FILE  (0600; GATEWAY_API_KEY generate-once)
anthropic:  $anthropic_state
providers:  anthropic / gemini / openrouter -- cloud egress only
\`\`\`

The gateway is the node's single egress to external LLMs. The Anthropic
API key is supplied via SMART_NODE_ANTHROPIC_API_KEY at install (or a
re-run); absent, the gateway runs degraded. The \`cli\` provider becomes
live once the agent-runtimes companion wires \`claude\`.
Container-reachable binding is a follow-up.
EOF
)
    report_section "Companion gateway" "$body"
}

run_intent() {
    require_root
    log "companion gateway -- start"
    require_stage B04
    log_init "$STAGE_ID"
    ensure_python
    probe_before
    # Service account + its owned home/log dirs (+ pg ident map line).
    create_service_user "$SERVICE_SHORT" >/dev/null
    install_snapshot
    build_venv
    # Peer-auth ident map for the gateway's DB connection -- must be in
    # place and the cluster reloaded before the service starts and connects.
    setup_db_ident
    setup_secret_store
    write_env
    ensure_gateway_core_api_secret
    # Credential wrapper first: it must exist at $WRAPPER_BIN before the
    # sudoers rule and the unit reference it.
    install_wrapper
    # Before install_service on purpose: it resolves the wrapper path that the
    # unit is written with, and arms the rule the gateway needs to use it
    install_runas_sudoers
    install_service
    wait_for_gateway
    reload_core_api_for_gateway_proof
    # The sweep for what the gateway spawns and stops tracking. After the
    # gateway itself, because there is nothing to reap without it.
    install_session_reaper
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B04"
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=gateway@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=gateway@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"ai-gateway\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "companion gateway -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # 1. B04 precondition.
    verify_check "stage B04 done" require_stage B04
    # 2. service user exists.
    verify_check "service user $SERVICE_USER exists" id -u "$SERVICE_USER"
    # 3. install tree present.
    verify_check "install dir $INSTALL_DIR exists" test -d "$INSTALL_DIR"
    # 4. vendored app present in the tree.
    verify_check "ai-gateway main.py installed" test -f "$INSTALL_DIR/main.py"
    # 5. venv interpreter present.
    verify_check "venv interpreter present" test -x "$VENV_DIR/bin/python"
    # 6. core deps importable in the venv.
    verify_check "venv has uvicorn + fastapi" \
        "$VENV_DIR/bin/python" -c "import uvicorn, fastapi"
    # 7. .env present and 0600.
    verify_check ".env present 0600" bash -c "
        [[ -f '$ENV_FILE' && \$(stat -c '%a' '$ENV_FILE' 2>/dev/null) == 600 ]]"
    # 8. .env owned by the service user.
    verify_check ".env owned by $SERVICE_USER" bash -c "
        [[ \$(stat -c '%U' '$ENV_FILE' 2>/dev/null) == '$SERVICE_USER' ]]"
    # 9. .env carries a GATEWAY_API_KEY.
    verify_check ".env holds GATEWAY_API_KEY" bash -c "
        grep -qE '^GATEWAY_API_KEY=.+' '$ENV_FILE'"
    verify_check "private core-api notice proof exists" bash -c "
        [[ -f '$GATEWAY_CORE_API_ENV' \
           && \$(stat -c '%a %U:%G' '$GATEWAY_CORE_API_ENV' 2>/dev/null) == '600 root:root' ]] \
        && grep -qE '^SN_GATEWAY_CORE_API_SECRET=[a-f0-9]{64}$' '$GATEWAY_CORE_API_ENV'"
    # 10. systemd unit file installed.
    verify_check "unit $SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # "installed" and "active" are both true of a node whose unit is a release
    # behind, which is why neither caught This one renders the unit this
    # version wants and compares it with the one the node is running -- and
    # prints the difference, which is the half that makes it actionable.
    local unit_diff; unit_diff="$(unit_diff_text)"
    verify_check "unit matches what this version generates (not a release behind)" \
        test -z "$unit_diff"
    if [[ -n "$unit_diff" ]]; then
        echo "       the live unit differs from what this version generates." >&2
        echo "       Re-run this companion to regenerate it; nothing else will." >&2
        echo "$unit_diff" >&2
    fi
    # 11. unit enabled.
    verify_check "unit $SYSTEMD_UNIT enabled" \
        systemctl is-enabled "$SYSTEMD_UNIT"
    # 12. unit active.
    verify_check "unit $SYSTEMD_UNIT active" \
        systemctl is-active "$SYSTEMD_UNIT"
    # 12a. The runtime unit, checked the same way: installed, current,
    # enabled, active. A node with the door alone has no agents.
    verify_check "unit $RUNTIME_UNIT installed" test -f "$RUNTIME_SYSTEMD_PATH"
    local runtime_diff; runtime_diff="$(unit_diff_text render_runtime_unit "$RUNTIME_SYSTEMD_PATH")"
    verify_check "runtime unit matches what this version generates" \
        test -z "$runtime_diff"
    if [[ -n "$runtime_diff" ]]; then
        echo "       the live runtime unit differs from what this version generates." >&2
        echo "$runtime_diff" >&2
    fi
    verify_check "unit $RUNTIME_UNIT enabled" \
        systemctl is-enabled "$RUNTIME_UNIT"
    verify_check "unit $RUNTIME_UNIT active" \
        systemctl is-active "$RUNTIME_UNIT"
    # 12b. Credential wrapper and per-agent privilege drop,
    # checked as a group. "sudo refuses root" passes just as well on a rule
    # that permits nothing at all, and on a missing file, so the permitted
    # case is proved in the same run. Neither half needs an agent to exist
    # yet -- the rule names the GROUP, so a node installed before its first
    # agent is still fully checked here.
    [[ -z "${CLI_ABS_PATH:-}" ]] && CLI_ABS_PATH="$(resolve_cli_path)"
    [[ -z "$CLI_ABS_PATH" ]] && CLI_ABS_PATH="/usr/local/bin/claude"
    verify_check "credential wrapper installed executable" test -x "$WRAPPER_BIN"
    verify_check "runas sudoers installed 0440 root:root" bash -c "
        [[ \$(stat -c '%a %U:%G' '$RUNAS_SUDOERS_PATH' 2>/dev/null) == '440 root:root' ]]"
    verify_check "runas sudoers parses" visudo -c -f "$RUNAS_SUDOERS_PATH"
    # The spawning process is the runtime; these are checked on its unit.
    verify_check "runtime unit carries an ABSOLUTE CLI_PATH (a bare name matches no rule)" \
        grep -qE '^Environment=CLI_PATH=/' "$RUNTIME_SYSTEMD_PATH"
    verify_check "runtime unit carries an ABSOLUTE FLINT_PATH" \
        grep -qE '^Environment=FLINT_PATH=/' "$RUNTIME_SYSTEMD_PATH"
    verify_check "runtime unit carries an ABSOLUTE CLI_AGENT_WRAPPER_PATH" \
        grep -qE '^Environment=CLI_AGENT_WRAPPER_PATH=/' "$RUNTIME_SYSTEMD_PATH"
    verify_check "runtime unit carries the optional memory contract" \
        grep -qF 'EnvironmentFile=-/etc/smart-node/mesh.env' "$RUNTIME_SYSTEMD_PATH"
    verify_check "runtime unit may use sudo (NoNewPrivileges=false)" \
        grep -qx 'NoNewPrivileges=false' "$RUNTIME_SYSTEMD_PATH"
    verify_check "door unit may NOT gain privileges (NoNewPrivileges=true)" \
        grep -qx 'NoNewPrivileges=true' "$SYSTEMD_PATH"
    verify_check "gateway MAY run the wrapper as %$SMART_NODE_AGENT_GROUP" bash -c "
        sudo -n -l -U '$SERVICE_USER' 2>/dev/null \
          | grep -q '(%$SMART_NODE_AGENT_GROUP).*$WRAPPER_BIN'"
    verify_check "gateway may NOT run the wrapper as root" bash -c "
        ! sudo -n -l -U '$SERVICE_USER' -u root '$WRAPPER_BIN' >/dev/null 2>&1"
    # 13. gateway answers /api/v1/health.
    verify_check "gateway answers /api/v1/health" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            'http://$GW_HOST:$GW_PORT/api/v1/health' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    verify_check "runtime answers /api/v1/health" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            'http://$RT_HOST:$RT_PORT/api/v1/health' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    verify_check "the door reports the runtime's cli health (not unavailable)" bash -c "
        curl -s --max-time 4 'http://$GW_HOST:$GW_PORT/api/v1/health' 2>/dev/null \
          | grep -q '\"live_children\"'"
    # 13b. session reaper installed, executable, and SCHEDULED. The
    # timer is the check that matters: the script being present reaps nothing,
    # and enabling the oneshot service instead of the timer would run it once at
    # boot and never again -- a check on the .service would pass in that state.
    verify_check "session reaper installed executable" test -x "$REAPER_BIN"
    verify_check "timer $REAPER_TIMER enabled" \
        systemctl is-enabled "$REAPER_TIMER"
    verify_check "timer $REAPER_TIMER active with a next run" bash -c "
        systemctl is-active '$REAPER_TIMER' >/dev/null \
        && systemctl list-timers '$REAPER_TIMER' --no-pager 2>/dev/null \
           | grep -qE '$REAPER_TIMER'"
    # 14. peer-auth ident map carries the gateway OS-user -> core-role line.
    verify_check "pg_ident has smartnode map ($SERVICE_USER -> core role)" bash -c "
        ident=\$(sed -n -E 's/^SMART_NODE_PG_IDENT=//p' '$SMART_NODE_PERSISTENCE_ENV' | tail -n1)
        role=\$(sed -n -E 's/^SMART_NODE_PG_CORE_ROLE=//p' '$SMART_NODE_PERSISTENCE_ENV' | tail -n1)
        [[ -n \"\$ident\" && -f \"\$ident\" ]] || exit 1
        grep -qF \"smartnode  $SERVICE_USER  \${role:-smartnode}\" \"\$ident\""
    # 15. companion manifest written.
    verify_check "companions.d/gateway.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/gateway.json"
    # 15. install-report has the gateway section.
    verify_check "install-report has the gateway section" \
        bash -c "grep -qFx '## Companion gateway' '$SMART_NODE_REPORT'"
    # 16. manifest records the install dir + the unit.
    verify_check "manifest records the gateway artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"dir\",\"value\":\"$INSTALL_DIR\",' \"\$m\" &&
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$SYSTEMD_UNIT\",' \"\$m\""
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

# run_uninstall_mode -- reverse the install. Companion installs are
# operator-reversible (the Cloudron card model), so the companion owns
# an explicit --uninstall in addition to the manifest replay that
# uninstall-from.sh performs for a whole-node teardown.
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion gateway -- uninstall"
    local unit
    for unit in "$RUNTIME_UNIT" "$SYSTEMD_UNIT"; do
        if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
        fi
    done
    rm -f "$SYSTEMD_PATH" "$RUNTIME_SYSTEMD_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    # Before the user is deleted: a sudoers rule naming an account that no
    # longer exists is a rule nobody can read the intent of later.
    rm -f "$RUNAS_SUDOERS_PATH"
    rm -rf "$INSTALL_DIR"
    unregister_companion "$STAGE_ID"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" >/dev/null 2>&1 || true
    fi
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion gateway -- uninstalled (log dir $LOG_DIR left for inspection)"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   create_service_user (OS account + pg ident line, one-time),
#   install_snapshot (extract application files, version-gated),
#   build_venv (pip install, version-gated), setup_db_ident (pg_ident.conf
#   line -- contract write but requires cluster reload; leave in install,
#   file separately if ident drift is observed), setup_secret_store (secret
#   vault init, one-time), wait_for_gateway (health probe, no state written).
#
# CONTRACT (run): write_env (env file read by the gateway on every restart
#   -- must be current), install_wrapper (credential wrapper script at
#   $WRAPPER_BIN -- the sudoers rule and the unit file both reference this
#   exact path; if absent or stale the per-agent privilege drop breaks and
#   agents cannot be spawned; install_wrapper must run BEFORE
#   install_runas_sudoers because the sudoers rule references $WRAPPER_BIN),
#   install_runas_sudoers (sudoers rule granting the gateway its NOPASSWD
#   scope -- must be current), install_service (systemd unit file -- must
#   be current), install_session_reaper (reaper cron entry -- must be
#   current), register_companion (companions.d record for version tracking),
#   stage_mark done.
run_contracts_only() {
    require_root
    log "companion gateway -- contracts only (v$STAGE_VERSION)"
    require_stage B04
    log_init "$STAGE_ID"
    write_env
    ensure_gateway_core_api_secret
    install_wrapper
    install_runas_sudoers
    install_service
    reload_core_api_for_gateway_proof
    install_session_reaper
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B04"
    stage_mark "$STAGE_ID" done \
        "version=gateway@$STAGE_VERSION" \
        "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
