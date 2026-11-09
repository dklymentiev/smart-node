#!/bin/bash
#
# core-api.sh -- Smart Node installer, Layer C Phase 2 companion: core-api.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B02.
#
# Installs the core-api companion: the panel's write-side controller. It
# is the FastAPI/uvicorn service that backs the B06 panel -- the CRUD for
# Tools / Agents / Workflows / Integrations / Specialists / Secrets, the
# events log + crons + MCP registry, and the /api/system/status
# observability snapshot the panel scrapes. Without it the panel renders
# but cannot write -- registries are read-only and /api/* 404s.
#
# Why a companion, not a bootstrap stage: the SCHEMA (sn_core / sn_config)
# is already baked by B02; this companion installs only the
# SERVICE that serves that schema. It depends on B02 (the smartnode
# cluster + the baked schema must exist before the service connects).
#
# Native systemd service, NOT a container: core-api is a Python app and
# connects to Postgres over the local unix socket via peer auth, exactly
# like the gateway companion. The component ships as a vendored snapshot
# in the sibling directory `core-api/` (the B05/B06 vendoring pattern).
#
# It must NOT recreate or migrate sn_core. main.py's lifespan only opens
# the connection pool and emits one boot event -- it runs no migrations.
# The vendored `core-api/migrations/` is reference only; nothing here
# applies it. ExecStartPre runs the bundled `seed-tools.py`, which is
# idempotent against the already-baked schema (ON CONFLICT DO UPDATE /
# DO NOTHING on the system integration + the four builtin tool rows), so
# it is safe to run on every (re)start of an already-populated node.
#
# Creates:
#   - service account smart-node-core-api (+ owned dirs) via lib.sh
#     create_service_user
#   - /opt/smart-node/core-api -- the install tree (vendored snapshot +
#     a Python venv with the pinned requirements)
#   - the `smartnode` pg_ident map line `smartnode  smart-node-core-api
#     smartnode` (OS user -> core role) so the service authenticates over
#     peer auth as the shared core role -- the earlier change pattern, identical to
#     the gateway companion's setup_db_ident
#   - smart-node-core-api.service -- the systemd unit (ExecStartPre seed,
#     uvicorn on the Docker bridge gateway IP)
#   - a Traefik file-provider route at panel.<domain>/api/* -> the service,
#     behind the smart-node-auth middleware (templated by SMART_NODE_DOMAIN
#     from the routing contract, NOT a hardcoded host)
#   - companions.d/core-api.json -- the manifest the B06 panel reads
#   - "Companion core-api" section in the install report
#   - /var/lib/smart-node/stages/core-api.state
#
# Binding: the service binds the Docker bridge gateway IP so the panel
# container reaches it across the bridge, and so auth.py's trusted-source
# allowlist (loopback + Docker network CIDR) is meaningful.
# Traefik forwards the Authum Remote-User header on every /api/* route.
#
# No .env: db.py connects with `user=smartnode dbname=smartnode` over the
# unix socket -- peer auth resolves it through the ident map this stage
# writes. There are no provider keys and no generated secrets, so unlike
# the gateway this companion writes no .env.
#
# Usage:
#   core-api.sh             full run: probe -> intent -> verify -> mark
#   core-api.sh --verify    idempotent post-check; exit 0 on full pass,
#                           N (fail count) with [VERIFY-FAIL]
#   core-api.sh --status    read /var/lib/smart-node/stages/core-api.state;
#                           exit 0=done 1=failed 2=missing
#   core-api.sh --uninstall reverse the install (service, tree, user,
#                           traefik route, companion manifest)
#   core-api.sh --help      this message
#
# Environment:
#   SMART_NODE_CORE_API_PORT  docker0-bind port (default 8030)
#   SMART_NODE_DOMAIN         node domain for the panel host (from the
#                             routing contract; falls back to hostname)
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

STAGE_ID="core-api"
STAGE_NAME="core-api"
STAGE_VERSION="0.4.28"

# Where this script lives -- the vendored core-api snapshot sits beside
# it. install-on.sh / the operator ship the two together onto the node.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/core-api"

# Install tree, namespaced like every Smart Node artifact.
SERVICE_SHORT="core-api"
SERVICE_USER="smart-node-core-api"
INSTALL_DIR="/opt/smart-node/core-api"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="/var/log/smart-node/core-api"
# Uploaded avatar bytes are node state, not part of either companion package.
# core-api is the sole application writer; Studio receives read-only access.
AVATAR_STATE_DIR="/var/lib/smart-node/agents/avatars/custom"

# systemd unit -- native service (core-api is a Python app).
SYSTEMD_UNIT="smart-node-core-api.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"

# core-api binds 0.0.0.0 (all interfaces). Access boundary is ufw alone:
# open_firewall() opens the port only to docker bridge subnets, never to Anywhere.
# Native callers use 127.0.0.1:PORT; container callers use host.docker.internal:PORT.
# Port overridable via SMART_NODE_CORE_API_PORT (default 8030).
CORE_API_HOST="0.0.0.0"
CORE_API_PORT="${SMART_NODE_CORE_API_PORT:-8030}"
# The second door. Agent-facing clients call core-api here, and only
# here does the caller's identity come from the kernel instead of a header it
# typed. Services keep the TCP bind above; they are not agents.
CORE_API_UDS="/run/smart-node/api/core-api.sock"

# Traefik dynamic route file -- dropped into the file-provider dir from the
# routing contract. Hot-reloaded by Traefik; no container restart.
TRAEFIK_ROUTE_BASENAME="30-core-api.yml"

# Proxy proof. The shared secret that lets core-api tell a request
# Traefik authenticated from one that merely ASSERTED an identity. Every agent
# on this node reaches the controller from the trusted network, so without this
# an agent sending `Remote-User: admin` IS the owner as far as the API is
# concerned -- which is how an agent could record an approval in the owner's
# name, the one control the owner relies on before anything commits them to a
# customer.
#
# Two copies, and BOTH must stay out of reach of an agent:
#   * the EnvironmentFile below -- 0600 root:root, injected by systemd into
#     core-api's process env before it drops to the service user
#   * the Traefik route -- 0640 root:$SMART_NODE_GROUP, and agents are in
#     smart-node-agents, never in the service group (the permissions e2e
#     asserts exactly that)
# Never put it in the unit file: that is 0644 and world-readable.
PROXY_AUTH_ENV="/etc/smart-node/core-api-proxy-auth.env"
PROXY_AUTH_HEADER="X-SN-Proxy-Auth"
PLANNER_OPERATOR_ENV="/etc/smart-node/planner-operator.env"

# The node owner, as a contract. core-api maps a root socket peer to this
# identity and accepts it as the one human a node service may act for; the
# planner and the dispatcher read the same file to know whom to name. Not a
# secret (0644): a name proves nothing without the service credential.
OPERATOR_ENV="/etc/smart-node/operator.env"

# The service-MCP door to the secret store. This is deliberately a
# separate, non-secret contract: socket reachability proves the OS user, while
# routes/secrets.py admits only users named here. The node installer supplies
# SMART_NODE_SECRETS_OPERATOR_USERS; an update with no override carries this
# rendered value forward. New nodes admit root only.
SECRETS_OPERATORS_ENV="/etc/smart-node/secrets-operators.env"
# Field hotfix used previously made the door installer-owned. Read it once on
# upgrade, then retire it after the equivalent contract has been rendered.
LEGACY_SECRETS_OPERATORS_DROPIN="/etc/systemd/system/smart-node-core-api.service.d/40-secrets-operators.conf"

# The service credential, and which services may act for which agents.
#
# Same reasoning as the proxy proof above and the same shape: a secret, so it
# lives in a 0600 file that systemd reads as root, never in the 0644 unit that
# every agent on the node can read. A service that could read this could act as
# any agent it is listed for, which is exactly the capability being withheld.
#
# SN_SERVICE_AGENTS is not a secret and could have gone in the unit, but it is
# kept beside the credential it governs: a permission list stored apart from the
# key it qualifies is a list somebody edits without noticing what it opens.
SERVICE_AUTH_ENV="/etc/smart-node/core-api-service-auth.env"
# Which service may originate as which agents. Refusal is the default: an
# unlisted service acts for nobody, so forgetting one fails closed and loudly.
# `voice` is listed because the voice line's entire purpose is to let a person
# speak to an agent, which means originating as that agent -- and it is a
# container, so it can never be the agent's own process.
# `brin` is listed for the same reason as `voice`: the dispatcher IS the thing
# that runs a turn on an agent's behalf, and it is a separate systemd service, so
# the kernel can never name it as the agent. Without it every ledger read brin
# makes -- inbox, tool calls, recent activity -- is refused 403 and read as
# "nothing happened", silently, because a ledger failure must not break a turn.
# That is how an agent came to answer a question it had answered two minutes
# earlier, and to contradict itself doing so. The wildcard is deliberate and is
# the same one voice carries: brin runs turns for every agent on the node.
SERVICE_AGENTS_DEFAULT="${SMART_NODE_SERVICE_AGENTS:-voice:*;brin:*}"
# Which service is admitted to which permission. Most are the callbacks no
# role holds: writing an event, recording an inbound message or a voice turn,
# the routing lookup. The rest are ordinary permissions a service needs as
# itself and has no account to hold: brin reads the turn bound and the workflow
# list, and makes sure a desk row exists before a desk turn. core-api admits a
# service only when it proves itself with the credential above AND is listed
# here for that permission. Refusal is the default and there is no wildcard, so
# the line says exactly what a holder of the credential can reach.
# The names are the ones the services send in X-SN-Service: `brin` is the
# dispatcher, its runner and its watchers; `voice` is the voice line; `flows`
# is the workflow launcher core-api itself starts, which writes a run's events;
# `gateway` reads an agent's assembled prompt when it opens a session;
# `core-api` is core-api's own prompt recomposer, run by this installer.
# brin's reads of agents, tools, prompts, MCP servers, budgets and people are
# what it needs to wake an agent and run a turn. They took no identity before
# the routes declared a permission, and brin has no account to hold one.
SERVICE_PERMISSIONS_DEFAULT="${SMART_NODE_SERVICE_PERMISSIONS:-brin:internal.events.write,internal.messages.inbound,internal.messages.routing,internal.messages.voice.write,limits.read,desks.create,workflows.read,agents.read,agents.runtime.control,agents.tools.read,agents.system_prompt.read,mcp.read,budgets.read,users.read;voice:internal.events.write,internal.messages.voice.write;flows:internal.events.write;gateway:agents.system_prompt.read,gateway.chatgpt.token;core-api:agents.regenerate,agents.system_prompt.read}"

# The fast-channel contract. FAST_CHANNELS names the channels the
# communication dispatcher answers ITSELF; /api/agents/with-pending-work
# subtracts them, so the Runner does not wake an agent for a message the
# dispatcher is already answering. core-api never received the value: it read
# an empty list, excluded nothing, and BOTH paths answered the same mail. On
# a node in the field that produced two turns per message, one replying in the
# channel and one replying with the internal messaging tools, which is why
# replies to mail arrived as internal notes with the letter carrying the
# leftover narration.
#
# The value BELONGS to the communication companion, which derives it from the
# channels actually wired. It is not recomputed here -- it is read from a
# contract file that companion writes, the same arrangement as the planner key
# below, and for the same reason: two derivations of one value drift.
FAST_CHANNELS_ENV="/etc/smart-node/fast-channels.env"

# Budget alert delivery contract, published by communication.sh after
# it has proved that SN_NOTIFY_SENDER names a mailbox with installed
# credentials.  Optional at unit-start level so a telegram-only node still
# starts; the endpoint refuses and leaves notified_at NULL when it is absent.
BUDGET_NOTIFICATIONS_ENV="/etc/smart-node/budget-notifications.env"

# The planner credential, minted and written by the tracking companion.
# core-api does not create it -- tracking owns the planner and its keys -- so
# this path is a contract between the two companions, and the install order
# (tracking before core-api on a fresh node, either order on a re-run) is why
# the unit reads it with a leading '-'.
PLANNER_KEY_ENV="/etc/smart-node/planner-key-core-api.env"

# DB->/srv projection roots. The newer core-api requires these env
# vars and fails loud if any is unset (no fallbacks in the app). Real-node
# layout; the srv-skeleton sidecar seeds _template + _specialists.
# Node-level operational tools, shared with the gateway's session reaper so an
# operator has one place to look rather than one per companion.
SN_OPS_DIR="/opt/smart-node/ops"
SN_AGENTS_ROOT="/srv/agents"
SN_AGENT_TEMPLATE="$SN_AGENTS_ROOT/_template"
SN_SPECIALISTS_SEED="$SN_AGENTS_ROOT/_specialists"
SN_TOOLS_OUT="/srv/tools/REGISTRY.md"
SN_CRONTAB_OUT="/srv/generated/smart-node-crons"
SN_SERVICES_SYSTEMD_GLOB="/etc/systemd/system/smart-node-*.service"
SRV_SKELETON="$SCRIPT_DIR/srv-skeleton"

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
# on 2026-08-14, which failed at "SMART_NODE_AGENT_GROUP: unbound variable"
# after the venv and pip steps had already run. Default rather than assume.
SMART_NODE_AGENT_GROUP="${SMART_NODE_AGENT_GROUP:-smart-node-agents}"
# CORE_API_HOST is always 0.0.0.0. No dynamic discovery needed.

# Optional integration helper -- provides sn_mcp_server_register and
# sn_integration_register. Sourced here because core-api registers the
# gdrive MCP server after the API is up.
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
    --uninstall)      MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)                die "unknown arg: $1 (use --help)" ;;
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

# derive_panel_host -- the panel hostname from the routing contract.
# routing.env absent is fatal. SMART_NODE_DOMAIN empty
# means a no-domain install: return "" so the caller skips the public route.
# No hostname(1) fallback, no silent empty-string concatenation.
derive_panel_host() {
    local base="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$base" && -r "$SMART_NODE_ROUTING_ENV" ]]; then
        base=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_DOMAIN)
    fi
    if [[ -z "$base" && -f "$SMART_NODE_ROUTING_ENV" ]]; then
        # shellcheck source=/dev/null
        source "$SMART_NODE_ROUTING_ENV"
        base="${SMART_NODE_DOMAIN:-}"
    fi
    if [[ -z "$base" ]]; then
        if [[ ! -f "${SMART_NODE_ROUTING_ENV:-}" ]]; then
            die "routing.env absent ($SMART_NODE_ROUTING_ENV) -- run B03 first"
        fi
        # log() writes to STDOUT (lib.sh:121) and this function returns its
        # value the same way, so an unredirected log here is captured by
        # panel_host=$(derive_panel_host) and ends up as the Traefik Host rule.
        log "[core-api] SMART_NODE_DOMAIN empty in routing.env -- public route skipped" >&2
        echo ""
        return 0
    fi
    echo "admin.$base"   # the admin console (panel.<domain> redirects)
}

# traefik_dynamic_dir -- the file-provider dir from the routing contract
# (B03). Companions drop route files here; Traefik hot-reloads them.
traefik_dynamic_dir() {
    local d=""
    [[ -r "$SMART_NODE_ROUTING_ENV" ]] \
        && d=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_TRAEFIK_DYNAMIC_DIR)
    [[ -n "$d" ]] \
        || die "routing contract missing SMART_NODE_TRAEFIK_DYNAMIC_DIR ($SMART_NODE_ROUTING_ENV) -- run B03 first"
    echo "$d"
}

# ---------- modes ----------

# probe_before -- a re-run (our service/tree present) proceeds
# idempotently: the tree is re-synced, the venv reused, the unit rewritten.
probe_before() {
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1 \
       && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "core-api service already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service unit does not -- treating as a resumed run"
    fi
}

# install_snapshot -- copy the vendored core-api snapshot into the install
# tree. --delete-style refresh: the tree mirrors the snapshot, but venv/
# (runtime state, not snapshot content) is kept.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/main.py" && -f "$SRC_SNAPSHOT/requirements.txt" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no main.py / requirements.txt"
    [[ -f "$SRC_SNAPSHOT/seed-tools.py" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no seed-tools.py (ExecStartPre)"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    log "syncing core-api snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' --exclude '__pycache__/' --exclude 'logs/' --exclude 'role-library/' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        # rsync-free fallback: wipe everything but venv/, recopy.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$INSTALL_DIR"/
    fi
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

# setup_db_ident -- peer auth for core-api's DB connection. The service
# connects over the local unix socket as the OS user smart-node-core-api,
# authenticating as the shared core role (db.py: user=smartnode). For
# Postgres peer auth to resolve that, the `smartnode` ident map must carry
# the line `smartnode  smart-node-core-api  smartnode` (OS user -> core
# role). create_service_user's generic OS-user->same-name line does not
# apply here (the service maps onto the existing core role), so this stage
# owns the bespoke mapping.
#
# Mirrors the gateway companion's setup_db_ident: append the line
# idempotently, then reload the cluster so it takes effect before the
# service starts and connects. Reads the ident path + cluster from the B02
# persistence contract.
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

# ensure_proxy_auth_secret -- mint the shared proof once, then leave it alone.
#
# GENERATE-ONCE, like every other credential this installer makes. A re-run
# that rotated it would be correct only if both copies were rewritten in the
# same instant; they are not (Traefik hot-reloads its file on its own clock),
# and the gap is a window in which the owner cannot approve anything. Rotating
# is deliberate work: delete the file and re-run.
#
# Echoes the secret on stdout so the two writers below share one value.
ensure_proxy_auth_secret() {
    if [[ -s "$PROXY_AUTH_ENV" ]]; then
        local existing
        existing=$(_envfile_get "$PROXY_AUTH_ENV" SN_PROXY_AUTH_SECRET)
        if [[ -n "$existing" ]]; then
            printf '%s' "$existing"
            return 0
        fi
        log "$PROXY_AUTH_ENV exists but carries no secret -- regenerating" >&2
    fi
    local secret
    secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    [[ ${#secret} -eq 64 ]] || die "could not generate the proxy proof secret"
    install -d -m 0755 -o root -g root /etc/smart-node
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node core-api -- proxy proof. Written once by core-api.sh.
#
# Traefik stamps this value on requests it has authenticated, as the
# $PROXY_AUTH_HEADER header. core-api treats a request carrying it as a proven
# session and everything else as an asserted identity, which is refused for
# approvals. Anyone who can read this file can approve as the owner.
#
# To rotate: delete this file and re-run core-api.sh (it rewrites the Traefik
# route with the new value in the same run).
SN_PROXY_AUTH_SECRET=$secret
EOF
    install -m 0600 -o root -g root "$tmp" "$PROXY_AUTH_ENV"
    rm -f "$tmp"
    manifest_record file "$PROXY_AUTH_ENV"
    log "minted the proxy proof secret ($PROXY_AUTH_ENV, 0600 root:root)" >&2
    printf '%s' "$secret"
}

# ensure_planner_operator_secret -- dedicated proof for the owner-only TCP
# planner bridge. It is not the browser/session proof and is readable only by
# the bridge's admin service account.
ensure_planner_operator_secret() {
    if [[ -s "$PLANNER_OPERATOR_ENV" ]]; then
        local existing
        existing=$(_envfile_get "$PLANNER_OPERATOR_ENV" SN_PLANNER_OPERATOR_TOKEN)
        [[ -n "$existing" ]] && { printf '%s' "$existing"; return 0; }
    fi
    local secret; secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    [[ ${#secret} -eq 64 ]] || die "could not generate planner operator token"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Dedicated owner proof for the work-mcp-planner TCP bridge. Written by core-api.sh.
SN_PLANNER_OPERATOR_TOKEN=$secret
EOF
    install -m 0600 -o root -g root "$tmp" "$PLANNER_OPERATOR_ENV"
    rm -f "$tmp"
    manifest_record file "$PLANNER_OPERATOR_ENV"
    printf '%s' "$secret"
}

# desired_secrets_operator_users -- canonical CSV on stdout.
#
# Root is always retained for installer/operator work. Every other entry must
# be an exact Unix username. Preserve first-seen order while removing duplicate
# entries, so an idempotent update does not rewrite the contract needlessly.
desired_secrets_operator_users() {
    local raw="${SMART_NODE_SECRETS_OPERATOR_USERS:-}"
    if [[ -z "$raw" && -r "$SECRETS_OPERATORS_ENV" ]]; then
        raw=$(_envfile_get "$SECRETS_OPERATORS_ENV" SN_SECRETS_OPERATOR_USERS)
    fi
    if [[ -z "$raw" && -r "$LEGACY_SECRETS_OPERATORS_DROPIN" ]]; then
        raw=$(sed -n -E \
            's/^Environment="?SN_SECRETS_OPERATOR_USERS=([^"[:space:]]+)"?$/\1/p' \
            "$LEGACY_SECRETS_OPERATORS_DROPIN" | tail -n1)
    fi
    [[ -n "$raw" ]] || raw="root"

    local out="root" user old_ifs="$IFS"
    IFS=','
    for user in $raw; do
        IFS="$old_ifs"
        [[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] \
            || die "invalid secrets operator '$user' (expected a Unix username)"
        if [[ "$user" != "root" && ",$out," != *",$user,"* ]]; then
            out="$out,$user"
        fi
        IFS=','
    done
    IFS="$old_ifs"
    printf '%s' "$out"
}

# render_secrets_operator_contract -- render both halves of the service door.
#
# The foreign service owns creation of its account. Smart Node only grants an
# existing, explicitly named account access to its socket and /resolve. Missing
# users are fatal: silently writing an allow-list for a typo leaves a configured
# service dead while making the install look successful.
render_secrets_operator_contract() {
    local users user old_ifs="$IFS"
    users=$(desired_secrets_operator_users)
    getent group "$SMART_NODE_AGENT_GROUP" >/dev/null 2>&1 \
        || die "secrets operator socket group '$SMART_NODE_AGENT_GROUP' is absent"

    IFS=','
    for user in $users; do
        IFS="$old_ifs"
        if [[ "$user" != "root" ]]; then
            id -u "$user" >/dev/null 2>&1 \
                || die "configured secrets operator '$user' does not exist"
            if ! id -nG "$user" 2>/dev/null | tr ' ' '\n' \
                    | grep -qx "$SMART_NODE_AGENT_GROUP"; then
                usermod -a -G "$SMART_NODE_AGENT_GROUP" "$user"
            fi
        fi
        IFS=','
    done
    IFS="$old_ifs"

    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node service-MCP secret-store door. Generated; do not edit.
# Socket group membership and this allow-list come from the same installer input.
SN_SECRETS_OPERATOR_USERS=$users
EOF
    install -m 0644 -o root -g root "$tmp" "$SECRETS_OPERATORS_ENV"
    rm -f "$tmp"
    manifest_record file "$SECRETS_OPERATORS_ENV"
    if [[ -f "$LEGACY_SECRETS_OPERATORS_DROPIN" ]]; then
        rm -f "$LEGACY_SECRETS_OPERATORS_DROPIN"
        rmdir "$(dirname "$LEGACY_SECRETS_OPERATORS_DROPIN")" 2>/dev/null || true
        log "retired legacy hand-written secrets operator drop-in"
    fi
    log "secrets operator contract rendered ($users)"
}

# _owner_query SQL -- one unaligned value from the core database, on stdout.
_owner_query() {
    local cluster db
    cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    db=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CORE_DB)
    [[ -n "$cluster" && -n "$db" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    runuser -u postgres -- psql --cluster "$cluster" -d "$db" \
        -X -qAt -v ON_ERROR_STOP=1 -c "$1"
}

# render_operator_contract -- write the owner this node is configured with.
#
# Runs on every pass of this companion, full or contracts-only, so a node
# installed before this file existed gets it on its next update, before the
# services that need it are restarted. Without it core-api refuses every call a
# service makes for the owner, and task notifications stop waking anybody.
#
# The name is READ from the users table, never assumed: the owner is the active
# human super-admin, which is exactly the test core-api applies to a delegated
# owner. A value already on the node is kept while it still passes that test,
# so a node with several super-admins keeps the one it was told. A node with
# none is refused here, by name, instead of being left to fail one call at a time.
render_operator_contract() {
    local is_owner="u.active AND u.role = 'super-admin' AND a.kind = 'human'"
    local from="FROM sn_config.users u JOIN sn_core.actors a ON a.id = u.actor_id"
    local owner="" current=""
    current=$(_envfile_get "$OPERATOR_ENV" SN_OPERATOR_ACTOR)
    if [[ "$current" =~ ^[A-Za-z0-9._@+-]+$ ]]; then
        owner=$(_owner_query "SELECT u.email $from WHERE $is_owner AND u.email = '$current' LIMIT 1") \
            || die "could not read the node owner from the core database"
        [[ -n "$owner" ]] \
            || log "owner '$current' in $OPERATOR_ENV is no longer an active human super-admin -- resolving again"
    fi
    if [[ -z "$owner" ]]; then
        owner=$(_owner_query "SELECT u.email $from WHERE $is_owner ORDER BY u.id LIMIT 1") \
            || die "could not read the node owner from the core database"
    fi
    [[ -n "$owner" ]] \
        || die "no active human super-admin in sn_config.users -- this node has no owner a service could act for (re-run B02, or restore the owner account)"
    [[ "$owner" =~ ^[A-Za-z0-9._@+-]+$ ]] \
        || die "owner identity '$owner' cannot be written to $OPERATOR_ENV"

    install -d -m 0755 -o root -g root /etc/smart-node
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node owner contract. Generated by core-api.sh from sn_config.users;
# do not edit. Read by core-api, the planner and the dispatcher.
SN_OPERATOR_ACTOR=$owner
EOF
    install -m 0644 -o root -g root "$tmp" "$OPERATOR_ENV"
    rm -f "$tmp"
    manifest_record file "$OPERATOR_ENV"
    log "owner contract rendered ($OPERATOR_ENV: $owner)"
}

# ensure_service_auth_secret -- the credential a node service presents when it
# acts for an agent. Generate-once, for the same reason as the proxy
# proof: rotating it needs both holders rewritten in the same instant, and they
# are not. Rotating is deliberate work -- delete the file, re-run this and the
# voice companion.
#
# WHY IT EXISTS AT ALL. TCP identity refusal makes an agent's name unforgeable by refusing it
# over TCP, correctly. But the voice line's whole job is to let a person speak to
# an agent, which means originating messages AS that agent, and it runs in a
# container that can never be the agent's own process. From 2026-08-14 every
# voice delegation answered 403 and the caller was told, in prose, that something
# had gone wrong; it went unnoticed for seven weeks because nobody delegated by
# voice in between. The service proves itself here and names the agent
# separately, and core-api checks both.
#
# Echoes the secret on stdout so the voice companion can be handed the same value.
# _merge_service_agents EXISTING DEFAULT -- the allow list an existing node
# should carry, on stdout.
#
# A service that ALREADY has a clause keeps the one it has: an operator who
# narrowed `voice:*` to `voice:alex` meant it, and a release must not re-widen
# it. A service with no clause at all gets the shipped default, which is how a
# new service (brin) reaches a node that was installed before it existed.
# Only ever adds.
_merge_service_agents() {
    local existing="$1" default="$2" out="$1" clause svc
    local old_ifs="$IFS"
    IFS=';'
    for clause in $default; do
        IFS="$old_ifs"
        [[ -n "$clause" ]] || { IFS=';'; continue; }
        svc="${clause%%:*}"
        case ";$existing;" in
            *";$svc:"*) : ;;                       # the node already decides this one
            *) out="${out:+$out;}$clause" ;;
        esac
        IFS=';'
    done
    IFS="$old_ifs"
    printf '%s' "$out"
}

ensure_service_auth_secret() {
    # The list is written into an env file and through sed below; a value that
    # is not the plain clause shape would corrupt one or the other.
    [[ "$SERVICE_PERMISSIONS_DEFAULT" =~ ^[a-z0-9_.,:\;-]+$ ]]         || die "SMART_NODE_SERVICE_PERMISSIONS is not a 'service:permission,...;service:...' list"
    if [[ -s "$SERVICE_AUTH_ENV" ]]; then
        local existing
        existing=$(_envfile_get "$SERVICE_AUTH_ENV" SN_SERVICE_AUTH_SECRET)
        if [[ -n "$existing" ]]; then
            # THE SECRET IS WRITTEN ONCE; THE ALLOW LIST IS NOT THE SECRET.
            # This file's own header says "Written once by core-api.sh", and
            # that is right for the credential -- rotating it needs both holders
            # rewritten in the same instant. But it meant SN_SERVICE_AGENTS was
            # also frozen at whatever the release that first installed the node
            # shipped, so adding a service to the default changed nothing on any
            # node that already existed. brin would have stayed refused, and its
            # ledger reads silently empty, on every node but a brand new one --
            # which is the defect this list is being changed to fix.
            local agents_now merged
            agents_now=$(_envfile_get "$SERVICE_AUTH_ENV" SN_SERVICE_AGENTS)
            merged=$(_merge_service_agents "$agents_now" "$SERVICE_AGENTS_DEFAULT")
            if [[ "$merged" != "$agents_now" ]]; then
                if sed -i "s|^SN_SERVICE_AGENTS=.*|SN_SERVICE_AGENTS=$merged|" \
                        "$SERVICE_AUTH_ENV" 2>/dev/null; then
                    log "SN_SERVICE_AGENTS reconciled: ${agents_now:-(none)} -> $merged" >&2
                else
                    log "WARN: could not update SN_SERVICE_AGENTS in $SERVICE_AUTH_ENV" >&2
                fi
            fi
            # The permission list is the release's, not the node's: it names
            # product callbacks, which do not differ from node to node. So it is
            # rewritten on every pass, and a node installed before the list
            # existed gets it on its next update. Without it core-api refuses
            # every service callback and names SN_SERVICE_PERMISSIONS.
            local perms_now
            perms_now=$(_envfile_get "$SERVICE_AUTH_ENV" SN_SERVICE_PERMISSIONS)
            if [[ "$perms_now" != "$SERVICE_PERMISSIONS_DEFAULT" ]]; then
                if grep -q '^SN_SERVICE_PERMISSIONS=' "$SERVICE_AUTH_ENV" 2>/dev/null; then
                    sed -i "s|^SN_SERVICE_PERMISSIONS=.*|SN_SERVICE_PERMISSIONS=$SERVICE_PERMISSIONS_DEFAULT|" \
                        "$SERVICE_AUTH_ENV" \
                        || die "could not update SN_SERVICE_PERMISSIONS in $SERVICE_AUTH_ENV"
                else
                    printf 'SN_SERVICE_PERMISSIONS=%s\n' "$SERVICE_PERMISSIONS_DEFAULT" \
                        >> "$SERVICE_AUTH_ENV" \
                        || die "could not write SN_SERVICE_PERMISSIONS to $SERVICE_AUTH_ENV"
                fi
                log "SN_SERVICE_PERMISSIONS set: $SERVICE_PERMISSIONS_DEFAULT" >&2
            fi
            printf '%s' "$existing"
            return 0
        fi
        log "$SERVICE_AUTH_ENV exists but carries no secret -- regenerating" >&2
    fi
    local secret
    secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    [[ ${#secret} -eq 64 ]] || die "could not generate the service credential"
    install -d -m 0755 -o root -g root /etc/smart-node
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node core-api -- service credential. Written once by core-api.sh.
#
# A node service that must act FOR an agent presents this value as the
# X-SN-Service-Auth header, names itself in X-SN-Service, and names the agent in
# X-SN-On-Behalf-Of. core-api verifies the secret (constant-time), checks the
# (service, agent) pair against SN_SERVICE_AGENTS below, and records the acting
# service on the message so the row can say later that a line caused it.
#
# Anyone who can read this file can act as any agent listed below. That is why
# it is 0600 root:root and not an Environment= line in the 0644 unit.
#
# SN_SERVICE_AGENTS: one clause per service, ';' separated. '*' means any agent.
# An UNLISTED service may act for nobody -- the default is refusal.
#
# SN_SERVICE_PERMISSIONS: which service is admitted to which internal
# permission, same shape, no wildcard. Rewritten by core-api.sh on every run.
#
# To rotate: delete this file, re-run core-api.sh, then re-run the voice
# companion so both ends carry the new value.
SN_SERVICE_AUTH_SECRET=$secret
SN_SERVICE_AGENTS=$SERVICE_AGENTS_DEFAULT
SN_SERVICE_PERMISSIONS=$SERVICE_PERMISSIONS_DEFAULT
EOF
    install -m 0600 -o root -g root "$tmp" "$SERVICE_AUTH_ENV"
    rm -f "$tmp"
    manifest_record file "$SERVICE_AUTH_ENV"
    log "minted the service credential ($SERVICE_AUTH_ENV, 0600 root:root; agents: $SERVICE_AGENTS_DEFAULT)" >&2
    printf '%s' "$secret"
}

# install_socket_dir -- declare the unix socket's directory.
#
# tmpfiles.d rather than RuntimeDirectory=: systemd re-applies a unit's exec
# context before every command it runs, so a RuntimeDirectory is re-owned to the
# unit's own User:Group immediately before ExecStart and any attempt to hand it
# to the agent group from an ExecStartPre is silently undone. That was measured
# here, not guessed -- the chgrp exited clean and the directory still came up
# with the wrong group.
#
# 2750 and setgid: the agent group may traverse and connect, nobody else can
# reach the path at all, and the socket INHERITS the group when core-api binds
# it -- which is how the socket ends up reachable by agents without core-api
# holding any privilege, and without joining the agent group.
install_socket_dir() {
    local conf="/etc/tmpfiles.d/smart-node-core-api.conf"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node core-api unix socket directory. Generated; do not edit.
# The agent group may traverse this directory and connect to the socket in it.
# Everyone else cannot reach the path, which is the whole access control: the
# socket is where an agent's identity stops being a header it typed.
d /run/smart-node 0755 root root -
d /run/smart-node/api 2750 $SERVICE_USER $SMART_NODE_AGENT_GROUP -
EOF
    install -m 0644 -o root -g root "$tmp" "$conf"
    rm -f "$tmp"
    manifest_record file "$conf"
    systemd-tmpfiles --create "$conf" >/dev/null 2>&1 \
        || log "WARN: systemd-tmpfiles could not create the socket directory now; it will exist at next boot"
    log "socket directory declared: /run/smart-node/api (2750 $SERVICE_USER:$SMART_NODE_AGENT_GROUP)"
}

# install_service -- write and activate the systemd unit. The service runs
# as the unprivileged service user. ExecStartPre runs the bundled
# idempotent seed-tools.py; ExecStart launches uvicorn on the docker0 IP.
# The unit Requires/After the smartnode Postgres cluster so the service
# does not start before its DB is up.
# render_unit <path> -- write the unit THIS version of the companion wants.
#
# Split out of install_service so --verify can render the same text and diff it
# against the unit the node is actually running. A release that changes
# a unit is deployed as files; nothing re-runs the stage that generates it, so a
# node keeps the old unit while running the new code and says nothing. Two seeds
# shipped exactly that way and had each run once, by hand.
#
# Everything here is READ-ONLY on purpose. The one side effect in the original
# install_service -- minting the proxy proof, which is generate-once -- stays in
# install_service, because a verify run must not create anything. The unit text
# references that file by path, never by value, so rendering does not need it.

# The memory companion's Mesh container -- the same names dispatcher.sh and
# memory.sh use. core-api reaches Mesh over the smart-node network, not by DNS.
MESH_CT="smart-node-mesh"
MESH_PORT=8000

# resolve_mesh_url -- the memory companion's address on the smart-node network.
#
# core-api WRITES to Mesh (the durable snapshot behind every knowledge entry,
# the document store) and READS from it (the documents it composes into an agent's
# prompt). Previously the unit set eleven addresses and not this one, so the
# code fell back to its own default -- https://mesh.local, which resolves
# nowhere -- and every knowledge write became a silent no-op: HTTP 201, a chip
# in the panel, a row in the database, and nothing the agent could ever read.
# Measured on reference-node before the fix: 4 of 19 rows carried a mesh_guid, all four
# old. After: 15 of 19, and the documents appear in the prompts.
#
# Same shape as dispatcher.sh's resolver, including its lesson: return 0
# explicitly. `[[ -n "$ip" ]] && echo` as the last command makes the FUNCTION
# return 1 when there is no ip, and `set -e` then kills the caller at the
# assignment -- silently, and before the WARN written for exactly that case
# "No url" is an answer, not a failure.
resolve_mesh_url() {
    local net="${SMART_NODE_DOCKER_NETWORK:-smart-node}" ip=""

    # Try the Docker container first (container-based install, or mixed node).
    if command -v docker >/dev/null 2>&1; then
        ip=$(docker inspect -f \
            "{{with index .NetworkSettings.Networks \"$net\"}}{{.IPAddress}}{{end}}" \
            "$MESH_CT" 2>/dev/null || true)
        if [[ -z "$ip" ]]; then
            ip=$(docker inspect -f \
                '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
                "$MESH_CT" 2>/dev/null || true)
        fi
        if [[ -n "$ip" ]]; then
            echo "http://$ip:$MESH_PORT"
            return 0
        fi
    fi

    # Fall back to the native service contract written by memory.sh.
    # The contract file carries MESH_API_URL=http://127.0.0.1:8000 when mesh
    # is installed as a native systemd service instead of a container.
    local contract="/etc/smart-node/mesh.env"
    if [[ -r "$contract" ]]; then
        local native_url=""
        native_url=$(_envfile_get "$contract" MESH_API_URL)
        if [[ -n "$native_url" ]]; then
            echo "$native_url"
            return 0
        fi
    fi

    return 0
}

# resolve_planner_url -- the tracking (planner) companion's address.
#
# Container-based (v0.4.x): the container published its port on the Docker
# bridge gateway, which varies by network. Ask docker for the gateway IP of
# the smart-node network and append :8080.
#
# Native (v0.5.0+): tracking.sh writes /etc/smart-node/planner.env
# with PLANNER_API_URL=http://127.0.0.1:8080. Read that contract file as the
# fallback so a node that has no Docker container still resolves the service.
#
# Same shape and same caution as resolve_mesh_url: return 0 explicitly so
# `set -e` does not kill the caller when the result is empty.
resolve_planner_url() {
    local net="${SMART_NODE_DOCKER_NETWORK:-smart-node}"

    # Try the Docker container first (container-based install, or mixed node).
    if command -v docker >/dev/null 2>&1; then
        if docker ps -aq -f "name=^smart-node-planner$" 2>/dev/null | grep -q .; then
            local gw=""
            gw=$(docker network inspect "$net" \
                --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' 2>/dev/null \
                | tr ' ' '\n' | grep -m1 . || true)
            if [[ -n "$gw" ]]; then
                echo "http://$gw:8080"
                return 0
            fi
        fi
    fi

    # Fall back to the native service contract (tracking.sh v0.5.0+).
    local contract="/etc/smart-node/planner.env"
    if [[ -r "$contract" ]]; then
        local native_url=""
        native_url=$(_envfile_get "$contract" PLANNER_API_URL)
        if [[ -n "$native_url" ]]; then
            echo "$native_url"
            return 0
        fi
    fi

    return 0
}

render_unit() {
    local pg_cluster pg_unit
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    [[ -n "$pg_cluster" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    pg_unit="postgresql@${pg_cluster/\//-}.service"

    # Native HOST systemd services (the Studio user panel) reach the bind
    # via the node's primary interface, so core-api sees their source as the
    # node LAN IP -- NOT a Docker-bridge address, hence untrusted by the allowlist.
    # Declare that LAN IP as trusted so get_principal() honours the Authum
    # Remote-User from host-process callers. Empty -> only loopback + Docker
    # network CIDR (containers) are trusted (auth.py handles an empty value).
    # `|| true`: a node with no default route makes `ip route get` fail, and an
    # unusual route line makes the grep match nothing -- either one ends the
    # script here under `set -euo pipefail`, silently, instead of leaving the
    # empty value the next line is written to handle.
    local lan_ip="" trusted_cidrs=""
    lan_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1 || true)
    [[ -n "$lan_ip" ]] && trusted_cidrs="${lan_ip}/32"

    # ...and the network the COMPANIONS are on, asked of docker rather than assumed.
    # The Docker CIDR is not always the same range -- on a host where that range
    # is already in use by another bridge, Docker assigns a different one and every
    # companion is refused. Same correction as ask, do not type the number.
    local sn_net="${SMART_NODE_DOCKER_NETWORK:-smart-node}" sn_subnets=""
    sn_subnets=$(docker network inspect "$sn_net" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null || true)
    local subnet
    for subnet in $sn_subnets; do
        [[ -n "$subnet" ]] || continue
        case ",$trusted_cidrs," in
            *",$subnet,"*) continue ;;
        esac
        trusted_cidrs="${trusted_cidrs:+$trusted_cidrs,}$subnet"
    done

    # Resolve the planner's address. Native nodes
    # use 127.0.0.1 via the contract file. resolve_planner_url() reads it; fall
    # back to 127.0.0.1 for nodes where the contract file is not yet written.
    local planner_url=""
    planner_url=$(resolve_planner_url)
    [[ -n "$planner_url" ]] || planner_url="http://127.0.0.1:8080"  # native callers use loopback

    # The memory companion's address. core-api is a HOST process, so it
    # cannot use docker's own DNS for the container name -- the IP on the
    # smart-node network is the only route, which is why dispatcher.sh resolves
    # it the same way.
    #
    # AND NOTHING HERE MAY WRITE TO STDOUT except the unit. unit_diff_text calls
    # render_unit inside a command substitution to compare the live unit against
    # what this version wants, so a `log` line in this function is captured as if
    # it were part of that difference -- and the check then reports a drift that
    # does not exist. It did, on the first from-nothing install of this release,
    # which is precisely the node where an unresolvable mesh is normal. The
    # operator-facing line lives in install_service instead.
    local mesh_url mesh_line=""
    mesh_url=$(resolve_mesh_url)
    [[ -n "$mesh_url" ]] && mesh_line="Environment=MESH_API_URL=$mesh_url"

    # Resolve the node's OWN domain (raw, not the panel host) so the Python
    # service can derive its notification sender, task deep-links and OAuth
    # redirect from it via os.environ["SMART_NODE_DOMAIN"].
    # routing.env absent is fatal. SMART_NODE_DOMAIN empty
    # means a no-domain install: the Environment= line is ABSENT, not empty -- same
    # reason as mesh_line above. An empty Environment= overrides os.environ.get
    # defaults with the empty string, which is a silent third failure mode.
    local node_domain="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$node_domain" && -r "$SMART_NODE_ROUTING_ENV" ]]; then
        node_domain=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_DOMAIN)
    fi
    if [[ -z "$node_domain" && -f "$SMART_NODE_ROUTING_ENV" ]]; then
        # shellcheck source=/dev/null
        source "$SMART_NODE_ROUTING_ENV"
        node_domain="${SMART_NODE_DOMAIN:-}"
    fi
    if [[ -z "$node_domain" ]]; then
        if [[ ! -f "${SMART_NODE_ROUTING_ENV:-}" ]]; then
            die "routing.env absent ($SMART_NODE_ROUTING_ENV) -- run B03 first"
        fi
        # stderr only: this function must not write to stdout (see comment above
        # on mesh_line). unit_diff_text calls render_unit inside $() and any
        # stdout leaks into $unit_diff, causing a false-positive verify failure.
        echo "[$(date '+%H:%M:%S')] [core-api] SMART_NODE_DOMAIN empty -- unit will start without a public domain" >&2
    fi
    local domain_line=""
    [[ -n "$node_domain" ]] && domain_line="Environment=SMART_NODE_DOMAIN=$node_domain"

    cat > "$1" <<EOF
[Unit]
Description=Smart Node core-api companion (admin write-side controller; Tools/Agents/Workflows CRUD)
After=network-online.target $pg_unit
Wants=network-online.target
Requires=$pg_unit

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
Environment=SN_AGENTS_ROOT=$SN_AGENTS_ROOT
Environment=SN_AGENT_TEMPLATE=$SN_AGENT_TEMPLATE
Environment=SN_SPECIALISTS_SEED=$SN_SPECIALISTS_SEED
# how often an agent may be woken without a person behind the work,
# before the node holds it back. Money limits what a wake costs; these limit how
# often one happens by itself. Defaults chosen ~1.5x above the highest rate seen
# on a working node, so ordinary work never meets them. A message from a person
# is never held by this.
Environment=SN_TOOLS_OUT=$SN_TOOLS_OUT
Environment=SN_CRONTAB_OUT=$SN_CRONTAB_OUT
Environment=SN_SERVICES_SYSTEMD_GLOB=$SN_SERVICES_SYSTEMD_GLOB
Environment=PLANNER_API_URL=$planner_url
Environment=SN_TRUSTED_CIDRS=$trusted_cidrs
$domain_line
# path to the inbound attachment byte store, shared with
# the communication companion. core-api reads here for the retrieval API.
Environment=ATTACHMENT_DIR=/srv/data/attachments
# Reply obligation : how long a request that asked for a reply waits
# between reminders, and how many wakes it gets before its sender is told it
# went unanswered. core-api has no default for either: without both the
# feature is off and a send that asks for a reply is refused with the reason.
Environment=SN_REPLY_WAKE_WINDOW_SECONDS=${SN_REPLY_WAKE_WINDOW_SECONDS:-600}
Environment=SN_REPLY_MAX_ATTEMPTS=${SN_REPLY_MAX_ATTEMPTS:-3}
# Mesh. The line is ABSENT, not empty, when the memory companion could
# not be resolved -- an empty Environment= would override the code's own default
# with the empty string, which is a third failure mode nobody reads. Either way
# the API now reports an undelivered knowledge write instead of answering 201
# for a fact no agent will ever see.
$mesh_line
# The proxy proof. An EnvironmentFile and NOT an Environment= line:
# this unit is 0644 and readable by every agent on the node, and a secret in
# it would hand over exactly the capability it exists to withhold. systemd
# reads the file as root and injects it before dropping to $SERVICE_USER.
#
# Leading '-' so an absent file degrades PROPORTIONATELY. install_service
# mints it above, so it is always there on a node this script has run; but if
# it is ever lost, the right outcome is "approvals are refused and say why"
# (auth.py answers 503 naming this setting), not "the node's entire API will
# not start". A mandatory EnvironmentFile makes one gated operation able to
# take down every endpoint.
EnvironmentFile=-$PROXY_AUTH_ENV
EnvironmentFile=-$PLANNER_OPERATOR_ENV
# The owner contract. Same leading '-': without the file the API still starts,
# and each call that needs the owner is refused naming SN_OPERATOR_ACTOR.
EnvironmentFile=-$OPERATOR_ENV
EnvironmentFile=-$SECRETS_OPERATORS_ENV
# The service credential and its authorisation list. Same leading
# '-': without it, losing this file would stop the whole API rather than
# refusing the one thing it governs.
EnvironmentFile=-$SERVICE_AUTH_ENV
# The admin-runner installer writes this root-read environment file before
# core-api starts. Both services receive the same private client credential;
# a bare Remote-User on loopback no longer opens admin-runner operations.
EnvironmentFile=-/etc/smart-node/admin-runner.env
# The planner credential. Written by the tracking companion, which mints
# it as the ONE delegating key on the node: core-api may present this credential
# and still name the caller it is acting for, so an entry made through the panel
# is authored by the person who made it and not by the door. Same leading '-'
# and the same reason -- without it the planner answers 401 and this API says so,
# which is a better failure than an API that will not start.
EnvironmentFile=-$PLANNER_KEY_ENV
# The fast-channel list, written by the communication companion. Same
# leading '-', and here it is load-bearing rather than defensive: this
# companion installs BEFORE communication does (install-on.sh runs core-api at
# stage B04 and communication after it), so on a fresh node this file does not
# exist yet when the unit is written. communication.sh creates it and restarts
# core-api, so the value arrives when the channel it describes does.
EnvironmentFile=-$FAST_CHANNELS_ENV
# Owner budget alerts: real sender selected by communication.sh.  The same file
# gives gateway its actor/channel, keeping both halves on one installer-owned
# decision instead of hand-written systemd drop-ins.
EnvironmentFile=-$BUDGET_NOTIFICATIONS_ENV
# Minted by gateway.sh after core-api's initial install; gateway.sh restarts
# this service so both sides receive the same private notification proof.
EnvironmentFile=-/etc/smart-node/gateway-core-api-auth.env
WorkingDirectory=$INSTALL_DIR
ExecStartPre=$VENV_DIR/bin/python $INSTALL_DIR/seed-tools.py
ExecStartPre=-$VENV_DIR/bin/python $INSTALL_DIR/seed-specialists.py
ExecStartPre=-$VENV_DIR/bin/python $INSTALL_DIR/seed-policies.py
ExecStartPre=-$VENV_DIR/bin/python $INSTALL_DIR/seed-user-channels.py
ExecStartPre=-$VENV_DIR/bin/python $INSTALL_DIR/seed-agent-workspaces.py
# The unix socket's directory is declared in tmpfiles.d, NOT with
# RuntimeDirectory=. systemd re-applies the exec context before EVERY command in
# the unit, so a RuntimeDirectory is re-owned to the unit's own User:Group
# immediately before ExecStart -- an ExecStartPre that chgrp's it to the agent
# group runs, succeeds, and is undone a moment later. Measured on test-node: the
# chgrp recorded a clean exit and the directory still came up group smart-node,
# and agents could not reach the socket. tmpfiles.d owns it once instead.
# serve.py, not "python -m uvicorn": the CLI binds --host/--port OR --uds and
# this service must answer on BOTH. One process, two doors, and a request knows
# which one it arrived through -- on the socket the kernel names the caller and
# a forged Remote-User has nothing to stand on.
#
# NO BACKTICKS ANYWHERE IN THIS HEREDOC. It is unquoted, so a backtick inside a
# COMMENT is still command substitution: it runs, prints "command not found",
# and leaves an empty space where the words were. Cost a run today and cost
# dispatcher.sh one yesterday.
Environment=CORE_API_HOST=$CORE_API_HOST
Environment=CORE_API_PORT=$CORE_API_PORT
Environment=CORE_API_UDS=$CORE_API_UDS
Environment=CORE_API_UDS_GROUP=$SMART_NODE_AGENT_GROUP
# The group of the node's service accounts. A socket peer whose uid belongs to
# it is a node component, and is admitted to the internal permissions on that
# ground. Agents are never members of it, which is what makes it a boundary.
Environment=SN_COMPONENTS_GROUP=$SMART_NODE_GROUP
# run-workflow.py (spawned by the /api/workflows/{name}/run route) reads this
# to emit flow lifecycle events. It inherits core-api's environment, so this
# is the right place to supply it. The address is core-api's own events
# endpoint -- the same process, reached via its bind address.
Environment=SN_CORE_EVENTS=http://127.0.0.1:$CORE_API_PORT/api/events
# seed-tools.py (ExecStartPre) reads SN_CORE_API to write the core-api endpoint
# into sn_config.integrations. Without it the row held empty string and the
# smoke phase failed with "core-api endpoint empty" on every clean node.
# native callers (including this process itself) use 127.0.0.1.
Environment=SN_CORE_API=http://127.0.0.1:$CORE_API_PORT
Environment=SN_AVATAR_STATE_DIR=$AVATAR_STATE_DIR
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/serve.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR $SN_AGENTS_ROOT /srv/tools /srv/generated /srv/data/attachments $AVATAR_STATE_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
# Also wanted by the database: Requires= stops this unit when the cluster is
# stopped, and nothing would start it again when the cluster comes back.
WantedBy=multi-user.target $pg_unit
EOF
}

# unit_diff_text -- how the live unit differs from what this version generates,
# or nothing at all if they agree. It PRINTS rather than returning a verdict
# because verify_check swallows both streams of what it runs, and a difference
# nobody can read is only half of removing the silence.
unit_diff_text() {
    if [[ ! -f "$SYSTEMD_PATH" ]]; then
        echo "the unit is not installed at all ($SYSTEMD_PATH)"
        return 0
    fi
    local want; want=$(mktemp)
    render_unit "$want"
    if ! diff -q "$want" "$SYSTEMD_PATH" >/dev/null 2>&1; then
        diff -u "$SYSTEMD_PATH" "$want" 2>/dev/null | sed -n '3,30p' | sed 's/^/      /'
    fi
    rm -f "$want"
}

install_service() {
    # Render the application allow-list and the socket-group membership from
    # one declaration before the unit consumes it.
    render_secrets_operator_contract
    # Mint the proxy proof BEFORE the unit that references it. install_route
    # also calls this (it needs the same value for the Traefik middleware) and
    # it is generate-once, so the second call reads the file the first wrote.
    #
    # Learned by running it: with this call only in install_route, the unit was
    # written and restarted first and systemd refused to start the service at
    # all -- "Failed to load environment files" -- because EnvironmentFile
    # without a leading '-' is mandatory. install_service runs before
    # install_route in main().
    ensure_proxy_auth_secret >/dev/null
    ensure_planner_operator_secret >/dev/null
    # Same ordering rule: mint before the unit names it.
    ensure_service_auth_secret >/dev/null
    # And the owner those credentials may be used for, before the restart
    # below puts the refusal of unproved owner calls into effect.
    render_operator_contract

    # Say out loud whether this unit will carry the memory companion's address
    # render_unit resolves it too and stays silent, because its output
    # is compared byte for byte; this is the one place a person reads. An
    # unresolvable mesh is not fatal -- the API now reports each undelivered
    # knowledge write rather than answering 201 -- but it is worth a line,
    # because the previous failure was silent.
    local _mesh; _mesh=$(resolve_mesh_url)
    if [[ -n "$_mesh" ]]; then
        log "memory companion resolved -- MESH_API_URL=$_mesh"
    else
        log "WARN: could not resolve the $MESH_CT container IP -- knowledge writes will NOT reach agents until this stage is re-run with memory up"
    fi

    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record systemd-unit "$SYSTEMD_UNIT"
    systemctl daemon-reload
    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || die "systemctl enable $SYSTEMD_UNIT failed"
    systemctl restart "$SYSTEMD_UNIT" \
        || die "systemctl restart $SYSTEMD_UNIT failed"
}

# install_reconcile_timer -- a periodic safety net that re-projects the enabled
# cron registry to /etc/cron.d (DB -> core-api render -> admin-runner materialize)
# every 5 min, so the node crontab cannot silently drift from sn_core.crons even
# with no panel "Apply to crontab". Idempotent: materialize is a no-op when the
# rendered body already matches the live file. Identity is the node owner
# (forwarded as Remote-User; core-api trusts it on the docker0 bind). SN_CRONTAB_OUT
# on the service is now best-effort staging only -- /etc/cron.d is authoritative.
# install_retention_sweep -- prune the per-wake agent logs and measure journal
# growth. core-api owns $SN_AGENTS_ROOT and the journal, so it owns this.
#
# Source is companions/node-ops/: cross-cutting housekeeping that belongs to no
# single companion. (companions/gateway-ops/ is the other one and is deliberately
# separate -- the session reaper is specific to what the gateway spawns.)
# Both install into /opt/smart-node/ops, one place an operator can look.
install_retention_sweep() {
    local src="$SCRIPT_DIR/node-ops"
    [[ -f "$src/retention.py" ]] \
        || die "retention sweep missing ($src/retention.py) -- the companion ships it as a sibling dir"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$SN_OPS_DIR"
    manifest_record dir "$SN_OPS_DIR"
    install -m 0755 -o root -g root "$src/retention.py" "$SN_OPS_DIR/retention.py"
    manifest_record file "$SN_OPS_DIR/retention.py"
    install -m 0644 -o root -g root "$src/systemd/smart-node-retention.service" \
        "/etc/systemd/system/smart-node-retention.service"
    install -m 0644 -o root -g root "$src/systemd/smart-node-retention.timer" \
        "/etc/systemd/system/smart-node-retention.timer"
    manifest_record systemd-unit "smart-node-retention.service"
    manifest_record systemd-unit "smart-node-retention.timer"
    systemctl daemon-reload
    # The TIMER is what gets enabled. Enabling the oneshot service instead would
    # run it once at boot and never again, and a check on the .service would pass
    # in that state -- so the verify checks the timer.
    systemctl enable --now smart-node-retention.timer >/dev/null 2>&1 \
        || log "could not enable smart-node-retention.timer (non-fatal)"
}

# install_classes_verify -- does the node still match its own declaration.
#
# /etc/smart-node/classes.json says where every class lives on a node of this
# release. This installs the thing that asks the node whether that is still
# true. It ships beside the retention sweep for the same reason: cross-cutting
# housekeeping belonging to no single companion, in the one directory an
# operator can look at.
#
# Why core-api owns it: the declaration is about core-api's own schemas
# (sn_config, sn_core), and core-api is the only component that may write them.
install_classes_verify() {
    local src="$SCRIPT_DIR/node-ops"
    [[ -f "$src/classes-verify.py" ]] \
        || die "class verifier missing ($src/classes-verify.py) -- the companion ships it as a sibling dir"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$SN_OPS_DIR"
    manifest_record dir "$SN_OPS_DIR"
    install -m 0755 -o root -g root "$src/classes-verify.py" "$SN_OPS_DIR/classes-verify.py"
    manifest_record file "$SN_OPS_DIR/classes-verify.py"
    install -m 0644 -o root -g root "$src/systemd/smart-node-classes-verify.service" \
        "/etc/systemd/system/smart-node-classes-verify.service"
    install -m 0644 -o root -g root "$src/systemd/smart-node-classes-verify.timer" \
        "/etc/systemd/system/smart-node-classes-verify.timer"
    manifest_record systemd-unit "smart-node-classes-verify.service"
    manifest_record systemd-unit "smart-node-classes-verify.timer"
    systemctl daemon-reload
    # The TIMER is enabled, not the oneshot -- same reasoning as the retention
    # sweep above, and for the same reason the verify checks the timer.
    #
    # --now, and the timer must be ACTIVE by the time validate runs: validate
    # walks the manifest and requires every unit recorded there to be healthy,
    # and it runs BEFORE the installer's phase 3.5. An enabled-but-unstarted
    # timer fails that check and stops the install one step short of laying the
    # declaration -- which is how archive-node ended up running new code under an old
    # release stamp.
    #
    # Safe to start now only because the timer counts from OnActiveSec, not
    # OnBootSec: it will not fire during the install and find no declaration.
    systemctl reset-failed smart-node-classes-verify.service >/dev/null 2>&1 || true
    systemctl enable --now smart-node-classes-verify.timer >/dev/null 2>&1 \
        || log "could not enable smart-node-classes-verify.timer (non-fatal)"

    # The verifier is INSTALLED here and RUN by install-on.sh in phase 3.5,
    # immediately after /etc/smart-node/classes.json is laid down. Running it
    # here would be wrong twice over: the declaration does not exist yet at
    # Phase 2 on a fresh node, so it would always report "cannot run"; and
    # capturing a non-zero exit into `out=$(...)` under `set -e` aborts the
    # whole companion with nothing printed, which is exactly how this was
    # discovered -- an install that died after "socket directory declared" and
    # named no failing step.
    log "class verifier installed; it runs in phase 3.5, after the declaration is laid"
}

# install_event_digest -- daily summary of hard-failure events, e-mailed to the
# owner at 09:00 local. Companion of the tick-side event-monitor: the monitor
# alerts within one 5-minute tick on hot defects, the digest sends one e-mail
# per day with the pattern view (counts by type, top repeats, affected actors
# and channels, deltas vs the previous day). Clean days still mail with a
# "clean" subject so absence of e-mail is never confused with a broken digest.
#
# Off until the owner switches it on in Settings; no timer at all with no mail.
#
# Ships beside retention and the class verifier under /opt/smart-node/ops, as
# a sibling systemd unit (same cadence shape as classes-verify). The script
# reads sn_core.events directly via the core-api psql role so it never hits
# the /api/events page cap.
install_event_digest() {
    local src="$SCRIPT_DIR/node-ops"
    [[ -f "$src/event-digest.py" ]] \
        || die "event digest missing ($src/event-digest.py) -- the companion ships it as a sibling dir"
    [[ -f "$src/_events_classify.py" ]] \
        || die "shared classifier missing ($src/_events_classify.py) -- event-digest.py imports it on first run"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$SN_OPS_DIR"
    manifest_record dir "$SN_OPS_DIR"
    install -m 0755 -o root -g root "$src/event-digest.py" "$SN_OPS_DIR/event-digest.py"
    manifest_record file "$SN_OPS_DIR/event-digest.py"
    # Shared classification module imported by event-digest.py (and by the
    # tick-side event monitor from 5301). The service runs
    # /usr/bin/python3 /opt/smart-node/ops/event-digest.py, so Python puts
    # /opt/smart-node/ops at sys.path[0] and the import resolves against it.
    # If this file is not copied, the first run fails with ModuleNotFoundError.
    # The install is idempotent with install_event_monitor: both copy the same
    # bytes and manifest_record the same path, so whichever lands first wins.
    install -m 0644 -o root -g root "$src/_events_classify.py" \
        "$SN_OPS_DIR/_events_classify.py"
    manifest_record file "$SN_OPS_DIR/_events_classify.py"
    install -m 0644 -o root -g root "$src/systemd/smart-node-event-digest.service" \
        "/etc/systemd/system/smart-node-event-digest.service"
    install -m 0644 -o root -g root "$src/systemd/smart-node-event-digest.timer" \
        "/etc/systemd/system/smart-node-event-digest.timer"
    manifest_record systemd-unit "smart-node-event-digest.service"
    # State directory: fallback cooldowns and last-run bookkeeping. Shared with
    # the monitor on purpose: one node-ops data root, one operator inspection
    # path. The directory is also created by install_event_monitor; here it is
    # idempotent in case the digest ships ahead of the monitor on an upgrade.
    install -d -m 0750 -o root -g root /var/lib/smart-node/node-ops
    install -d -m 0750 -o root -g root /var/lib/smart-node/node-ops/state
    manifest_record dir /var/lib/smart-node/node-ops
    manifest_record dir /var/lib/smart-node/node-ops/state
    # Fallback log file. install_event_monitor also creates this; we only
    # ensure the parent dir exists and never touch an existing file so the
    # monitor's permissions are preserved.
    install -d -m 0755 -o root -g root /var/log/smart-node
    systemctl daemon-reload
    # The digest is an e-mail. With no sender communication.sh has proved, nothing
    # can carry it: no timer, so nothing runs and nothing fails every day.
    if grep -qE '^SN_NOTIFY_SENDER=.+' "$BUDGET_NOTIFICATIONS_ENV" 2>/dev/null; then
        # The TIMER is enabled. Enabling the oneshot would run once at boot and
        # never again, same reasoning as retention and classes-verify. It runs
        # only if the owner switched the digest on (node_control.daily_digest).
        # Recorded only here: validate fails a recorded timer that is not running,
        # and a node with no mail has it off on purpose.
        manifest_record systemd-unit "smart-node-event-digest.timer"
        systemctl enable --now smart-node-event-digest.timer >/dev/null 2>&1 \
            || log "could not enable smart-node-event-digest.timer (non-fatal)"
        log "event digest installed -> $SN_OPS_DIR/event-digest.py (timer daily at 09:00 local)"
    else
        systemctl disable --now smart-node-event-digest.timer >/dev/null 2>&1 || true
        systemctl reset-failed smart-node-event-digest.service >/dev/null 2>&1 || true
        log "event digest installed, timer off: no mail sender on this node"
    fi
}

install_reconcile_timer() {
    local svc="/etc/systemd/system/smart-node-cron-reconcile.service"
    local tmr="/etc/systemd/system/smart-node-cron-reconcile.timer"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node cron reconcile (re-materialize /etc/cron.d from sn_core.crons via core-api -> admin-runner)
After=network-online.target $SYSTEMD_UNIT
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -fsS -m 30 --unix-socket $CORE_API_UDS -X POST http://core-api/api/crons/render
EOF
    install -m 0644 -o root -g root "$tmp" "$svc"
    cat > "$tmp" <<EOF
[Unit]
Description=Periodic Smart Node cron reconcile (DB -> /etc/cron.d safety net)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    install -m 0644 -o root -g root "$tmp" "$tmr"
    rm -f "$tmp"
    manifest_record systemd-unit "smart-node-cron-reconcile.service"
    manifest_record systemd-unit "smart-node-cron-reconcile.timer"
    systemctl daemon-reload
    systemctl enable --now smart-node-cron-reconcile.timer >/dev/null 2>&1 \
        || log "could not enable smart-node-cron-reconcile.timer (non-fatal)"
}

# install_route -- drop the Traefik file-provider route for the panel's
# /api/* prefix. Host is templated from the routing contract's domain (or
# the node hostname) -- NEVER hardcoded. Behind the smart-node-auth
# middleware so every /api/* call carries the Authum Remote-User header.
# priority 100 outranks the panel's catch-all so /api/* lands on the
# controller. Traefik's file provider hot-reloads -- no restart.
install_route() {
    local dyn_dir panel_host route_file middleware proxy_secret
    dyn_dir=$(traefik_dynamic_dir)
    panel_host=$(derive_panel_host)
    if [[ -z "$panel_host" ]]; then
        # Skip means RETIRE, not walk past. See the same guard in
        # studio.sh and voice.sh: returning without touching the file leaves
        # whatever an earlier release wrote sitting on disk, and Traefik keeps
        # serving it. On our node this path is what stranded a 30-core-api.yml
        # whose Host rule was a captured log line -- written by a bug, then
        # preserved by this guard on every subsequent run.
        local stale="$dyn_dir/$TRAEFIK_ROUTE_BASENAME"
        if [[ -f "$stale" ]]; then
            rm -f "$stale"
            manifest_retire_artifact file "$stale" \
                "no public domain -- core-api route retired"
            log "[core-api] no public domain -- Traefik route retired ($stale)"
        else
            log "[core-api] no public domain -- no Traefik route to write"
        fi
        return 0
    fi
    # A derived name with no DNS record is not a door.
    if ! public_route_eligible "$panel_host"; then
        retire_route "core-api" "$dyn_dir/$TRAEFIK_ROUTE_BASENAME" \
            "$PUBLIC_ROUTE_SKIP_REASON"
        return 0
    fi
    middleware="${SMART_NODE_AUTH_MIDDLEWARE:-smart-node-auth@docker}"
    route_file="$dyn_dir/$TRAEFIK_ROUTE_BASENAME"
    proxy_secret=$(ensure_proxy_auth_secret)
    [[ -d "$dyn_dir" ]] \
        || die "Traefik dynamic dir missing ($dyn_dir) -- run B03 first"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node core-api -- write-side controller (Tools/Agents/Workflows CRUD).
# Native service on the host, bound to 0.0.0.0:$CORE_API_PORT.
# Routed at $panel_host/api/* behind the same Authum middleware as the panel.
# Written by core-api.sh@$STAGE_VERSION (host templated from SMART_NODE_DOMAIN).
#
# CONTAINS A SECRET -- installed 0640 root:$SMART_NODE_GROUP. Agents are in
# smart-node-agents and never in the service group, so they cannot read it.
http:
  middlewares:
    # The proxy proof. Stamped only on the router that sits behind
    # Authum, so its presence means "a browser session was authenticated".
    # customRequestHeaders OVERWRITES, which is the load-bearing half: a
    # client that sends this header itself has its value replaced here rather
    # than passed through.
    smart-node-proxy-proof:
      headers:
        customRequestHeaders:
          $PROXY_AUTH_HEADER: "$proxy_secret"

  routers:
    smart-node-core-api:
      rule: "Host(\`$panel_host\`) && PathPrefix(\`/api\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-core-api"
      # Order matters: authenticate first, stamp the proof second. Stamping
      # ahead of the auth middleware would vouch for requests Authum has not
      # yet accepted.
      middlewares: ["$middleware", "smart-node-proxy-proof"]
      priority: 100
      $(web_tls_yaml)

    # First-run onboarding endpoint. Reachable BEFORE any operator exists, so it
    # must NOT carry the smart-node-auth middleware (the setup token is the
    # credential). Higher priority than the /api router above so /api/setup/* is
    # matched here and skips auth; every other /api/* stays gated. Mirrors B06's
    # snpanelsetup router that exempts the panel's /setup path.
    smart-node-core-api-setup:
      rule: "Host(\`$panel_host\`) && PathPrefix(\`/api/setup\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-core-api"
      priority: 200
      $(web_tls_yaml)

  services:
    smart-node-core-api:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:$CORE_API_PORT"
        passHostHeader: true
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$route_file"
    rm -f "$tmp"
    manifest_record file "$route_file"
    log "wrote Traefik route $route_file (host $panel_host/api/*)"
}

# wait_for_core_api -- poll the health endpoint (auth-exempt on the bind IP)
# until the uvicorn app answers, against a 60s deadline.
wait_for_core_api() {
    local deadline=$(( $(date +%s) + 60 )) code
    log "waiting for core-api to answer /api/health"
    while (( $(date +%s) < deadline )); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            "http://127.0.0.1:$CORE_API_PORT/api/health" 2>/dev/null || true)
        if [[ "$code" =~ ^2 ]]; then
            log "core-api is up (HTTP $code)"
            return 0
        fi
        sleep 2
    done
    die "core-api did not answer /api/health within 60s (unit $SYSTEMD_UNIT)"
}

# A release can add global prompt contracts without touching an agent row. Do
# this once after the updated API is healthy, not in ExecStartPre on every
# ordinary restart. Going through the canonical regenerate endpoint also
# recycles live gateway sessions, so the new contract takes effect immediately.
recompose_agent_prompts() {
    log "recomposing all provisioned agent prompts"
    # The recomposer runs as core-api's own account and proves itself with the
    # credential that account already holds. It is exported and inherited, not
    # written into the command: a command line is readable by every user on
    # the node, an environment only by its owner.
    local svc_secret
    svc_secret=$(_envfile_get "$SERVICE_AUTH_ENV" SN_SERVICE_AUTH_SECRET)
    [[ -n "$svc_secret" ]] \
        || die "no service credential in $SERVICE_AUTH_ENV -- cannot recompose agent prompts"
    (
        export SN_SERVICE_AUTH_SECRET="$svc_secret"
        runuser -u "$SERVICE_USER" -- env \
            SN_AGENTS_ROOT="$SN_AGENTS_ROOT" \
            SN_CORE_API_URL="http://127.0.0.1:$CORE_API_PORT" \
            "$VENV_DIR/bin/python" \
            "$INSTALL_DIR/companions/recompose-affected.py" --all
    ) || die "agent prompt recomposition failed"
}

write_report_section() {
    local panel_host; panel_host=$(derive_panel_host 2>/dev/null || echo "panel.<domain>")
    local body
    body=$(cat <<EOF
**Version:** core-api@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  core-api (vendored snapshot, FastAPI)
service:    $SYSTEMD_UNIT  (native systemd, user $SERVICE_USER)
install:    $INSTALL_DIR
bind:       0.0.0.0:$CORE_API_PORT  (all interfaces, ufw restricts to docker bridge subnets)
seed:       ExecStartPre seed-tools.py (idempotent; baked schema)
route:      $panel_host/api/*  (Traefik file provider, smart-node-auth)
db:         peer auth -- $SERVICE_USER -> smartnode (pg_ident map)
\`\`\`

core-api is the panel's write-side controller: Tools / Agents / Workflows
/ Integrations / Specialists / Secrets CRUD, the events log + crons + MCP
registry, and /api/system/status. The schema is baked by B02 -- this
companion installs only the service and never migrates sn_core. seed-tools
re-seeds the system integration + builtin tools idempotently on every
start.
EOF
)
    report_section "Companion core-api" "$body"
}

run_intent() {
    require_root
    log "companion core-api -- start"
    require_stage B02
    log_init "$STAGE_ID"
    ensure_python
    probe_before
    # Service account + its owned home/log dirs.
    create_service_user "$SERVICE_SHORT" >/dev/null
    install_snapshot
    lay_srv_skeleton
    lay_role_library
    build_venv
    # Peer-auth ident map for the DB connection -- must be in place and the
    # cluster reloaded before the service starts and connects.
    setup_db_ident
    # Before the unit: serve.py binds the socket at startup and the directory
    # has to be there, with the right group, for the socket to inherit it.
    install_socket_dir
    install_service
    install_reconcile_timer
    install_retention_sweep
    install_classes_verify
    install_event_digest
    install_health_probe_cron
    install_integration_cli
    build_gdrive_wheel
    provision_host_utils
    install_route
    open_firewall
    wait_for_core_api
    # Refresh the release-owned setup contract before composing existing agents,
    # including a renamed John. Seeding on service restart still skips live rows.
    runuser -u "$SERVICE_USER" -- env SN_SPECIALISTS_SEED="$SN_SPECIALISTS_SEED" \
        "$VENV_DIR/bin/python" "$INSTALL_DIR/seed-specialists.py" --refresh-configurator \
        || die "configurator specialist refresh failed"
    # before the recompose: it writes each agent's .mcp.json, and reminders is in every one
    register_agents_mcp_server
    register_reminders_mcp_server
    recompose_agent_prompts
    register_secrets_mcp_server
    register_gdrive_mcp
    provision_mcp_autoinstall
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=core-api@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=core-api@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"core-api\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "companion core-api -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

# retire_agent_runners -- take the retired run.sh off an updated node.
#
# WHY. run.sh was the per-agent cron entry point. the Runner became the only
# waker, but the file stayed: copied into every new agent, re-converged into old
# ones on every update, and still runnable. Run by hand it spends a model call
# outside brin and the gateway, so no session, no per-request usage, no budget
# rule and no claim; and a reader of the folder concludes that is how agents
# wake. Removing it from the template does not remove it from a node, because
# the template is copied additively and agent folders are create-only. So the
# update removes it.
#
# NOT DELETED, ARCHIVED. reference-node carries hand corrections in run.sh that live
# nowhere else, and converge kept earlier versions as run.sh.pre-*.bak. All of
# them move to $SN_AGENTS_ROOT/.archive/run-sh-retired-<ts>/<slug>/, not
# executable, where an operator can still read them and nothing can run them.
#
# The disabled wake crons that pointed at run.sh go too (sn_core.crons rows whose
# command is an agent's run.sh). Enabling one would now run a missing file.
# Each removal is an event, so the cleanup is visible, not silent.
retire_agent_runners() {
    local ts arch d slug f moved=0
    ts="$(date +%Y%m%d-%H%M%S)"
    arch="$SN_AGENTS_ROOT/.archive/run-sh-retired-$ts"
    for d in "$SN_AGENTS_ROOT"/*/; do
        [[ -d "$d" ]] || continue
        slug="$(basename "$d")"
        for f in "$d"run.sh "$d"run.sh.*; do
            [[ -f "$f" ]] || continue
            install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$arch/$slug"
            mv -f "$f" "$arch/$slug/"
            chmod 0640 "$arch/$slug/$(basename "$f")"
            moved=$((moved + 1))
        done
    done
    if (( moved > 0 )); then
        log "retired run.sh: $moved file(s) moved to $arch"
        log_event "system:installer" "agent.runner_retired" "agent" "run.sh" \
            "{\"files\":$moved,\"archive\":\"$arch\"}"
    fi

    # The cluster and database come from the B02 contract file, not from the
    # environment: the installer's shell does not carry them (the first stand
    # run of this step failed on exactly that).
    local cluster db rows
    cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    db=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CORE_DB)
    if [[ -z "$cluster" || -z "$db" ]]; then
        log "WARN: persistence contract incomplete ($SMART_NODE_PERSISTENCE_ENV) -- wake crons that point at run.sh left in place"
        return 0
    fi
    if ! rows=$(runuser -u postgres -- psql --cluster "$cluster" -d "$db" \
            -X -qAt -v ON_ERROR_STOP=1 -c \
            "DELETE FROM sn_core.crons WHERE command ~ '^$SN_AGENTS_ROOT/[a-z][a-z0-9-]*/run\.sh\$' RETURNING slug" \
            2>&1); then
        log "WARN: could not remove wake crons that point at run.sh -- left in place: ${rows//$'\n'/ }"
        return 0
    fi
    local c
    while IFS= read -r c; do
        [[ -n "$c" ]] || continue
        log "retired wake cron '$c' (it ran the removed run.sh)"
        log_event "system:installer" "cron.deleted" "cron" "$c" \
            "{\"reason\":\"run.sh retired\"}"
    done <<< "$rows"
    return 0
}

# lay_role_library -- the release's ready-made roles, one Markdown file each, for the
# setup agent to read and narrow to an owner (GET /api/specialists/library, and the
# files themselves). Release-owned: an update replaces the whole folder; an agent made
# from a role keeps its own copy in sn_config.specialists. Root-owned and read-only to
# everyone else, so no agent can rewrite a role another agent is then made from.
ROLE_LIBRARY_DIR="/srv/smart-node/specialists"
lay_role_library() {
    local src="$SRC_SNAPSHOT/role-library"
    [[ -d "$src" ]] || { log "WARN: no role library in this release ($src)"; return 0; }
    install -d -m 0755 -o root -g root /srv/smart-node "$ROLE_LIBRARY_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --chmod=D0755,F0644 "$src"/ "$ROLE_LIBRARY_DIR"/
    else
        find "$ROLE_LIBRARY_DIR" -mindepth 1 -delete
        cp -a "$src"/. "$ROLE_LIBRARY_DIR"/
        find "$ROLE_LIBRARY_DIR" -type f -exec chmod 0644 {} +
    fi
    chown -R root:root "$ROLE_LIBRARY_DIR"
    log "role library: $(find "$ROLE_LIBRARY_DIR" -name '*.md' | wc -l) roles -> $ROLE_LIBRARY_DIR"
}

lay_srv_skeleton() {
    # Provision the DB->/srv projection skeleton the newer core-api needs
    # Idempotent: dirs ensured; _template + agnostic specialists
    # seeded only when missing, except the release-owned configurator contract.
    # 2755, not 2775: core-api is the only writer of this projection tree.
    # It was group-writable previously, and since agents joined that group
    # they could drop files into /srv/tools -- the directory holding
    # the tools they are then trusted to run -- and into /srv/agents, where
    # agent definitions live, one OS user per agent (agent-<slug>).
    # setgid stays so per-agent subdirs keep inheriting the group.
    install -d -m 2755 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP"         "$SN_AGENTS_ROOT" "$SN_AGENT_TEMPLATE" "$SN_SPECIALISTS_SEED"         /srv/tools /srv/generated
    # ATTACHMENT_DIR. The unit names /srv/data/attachments in
    # ReadWritePaths, and systemd refuses to start a unit whose ReadWritePaths
    # entry does not exist -- it fails at step NAMESPACE with 226, before the
    # process runs, so the service never gets to create the directory itself.
    # Nothing created it, so a node taking this release for the first time lost
    # core-api entirely. 0750: core-api is the only writer, and the
    # group needs traverse to read what agents were sent.
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data
    # /srv/data/attachments is ALSO laid by communication.sh, owned by the
    # communication account, which writes channel files into it. Both installers
    # used to `install -d -o <self>` it, so whichever ran last took it from the
    # other and that one could no longer write. Create it
    # only when it is missing; ownership of the shared directory is the
    # dedicated-group step of the permissions policy, not a race between two scripts.
    [[ -d /srv/data/attachments ]] || install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data/attachments
    # Files attached in the Studio chat, one directory per
    # desk underneath. core-api is the only writer here; the group can traverse.
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data/attachments/desks
    # Files on a planner task (routes/task_attachments.py), one directory per
    # task underneath. Same owner and mode as desks/: core-api writes, the
    # group traverses, nothing is writable beyond its owner.
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data/attachments/tasks
    # Uploaded avatars are durable node state. Do not manifest this directory:
    # package uninstall/update must leave user bytes behind. Studio serves it
    # through the stable /avatars/custom URL but never receives write access.
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" \
        /var/lib/smart-node/agents /var/lib/smart-node/agents/avatars
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$AVATAR_STATE_DIR"
    if [[ -d "$SRV_SKELETON" ]]; then
        cp -a "$SRV_SKELETON/agents/_template/." "$SN_AGENT_TEMPLATE/" 2>/dev/null || true
        local f dest
        for f in "$SRV_SKELETON"/agents/_specialists/*.md; do
            [[ -e "$f" ]] || continue
            dest="$SN_SPECIALISTS_SEED/$(basename "$f")"
            if [[ "$(basename "$f")" == configurator.md || ! -e "$dest" ]]; then
                cp -a "$f" "$dest"
            fi
        done
        [[ -f "$SRV_SKELETON/agents/CLAUDE.md" && ! -f "$SN_AGENTS_ROOT/CLAUDE.md" ]] && cp -a "$SRV_SKELETON/agents/CLAUDE.md" "$SN_AGENTS_ROOT/CLAUDE.md" || true
        [[ -f "$SRV_SKELETON/tools/CLAUDE.md" && ! -f /srv/tools/CLAUDE.md ]] && cp -a "$SRV_SKELETON/tools/CLAUDE.md" /srv/tools/CLAUDE.md || true
    else
        log "WARN: srv skeleton sidecar missing ($SRV_SKELETON) -- dirs created empty"
    fi
    # Health-checker scripts. Installed from the node-ops sidecar into
    # /srv/tools/health-checker/ so every node has them from first boot.
    # rsync -a so permissions and timestamps are preserved; chown follows.
    local hc_src="$SCRIPT_DIR/node-ops/health-checker"
    local hc_dst="/srv/tools/health-checker"
    if [[ -d "$hc_src" ]]; then
        install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$hc_dst"
        rsync -a --delete "$hc_src"/ "$hc_dst"/
        chown -R root:"$SMART_NODE_GROUP" "$hc_dst"
        chmod 0755 "$hc_dst"/status.sh "$hc_dst"/health-probe.py 2>/dev/null || true
        log "health-checker scripts installed -> $hc_dst"
    else
        log "WARN: health-checker sidecar missing ($hc_src) -- skipping"
    fi
    # Agent attachment retrieval. The helper runs as the agent, asks
    # core-api over its kernel-authenticated Unix socket, then materializes a
    # correctly suffixed file in gateway PrivateTmp for the built-in Read tool.
    local attachment_reader_src="$SRV_SKELETON/tools/attachments/attachment-read.py"
    local attachment_reader_dir="/srv/tools/attachments"
    if [[ -f "$attachment_reader_src" ]]; then
        install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$attachment_reader_dir"
        install -m 0755 -o root -g "$SMART_NODE_GROUP" \
            "$attachment_reader_src" "$attachment_reader_dir/attachment-read.py"
        log "attachment reader installed -> $attachment_reader_dir/attachment-read.py"
    else
        die "attachment reader missing from package: $attachment_reader_src"
    fi
    retire_agent_runners
    chown -R "$SERVICE_USER":"$SMART_NODE_GROUP" "$SN_AGENTS_ROOT" /srv/tools /srv/generated 2>/dev/null || true
    # That chown also took each agent's own runtime folders away from the agent. They are the agent's: the wake runs
    # as the agent and writes its logs/, state/ and work/ there (bootstrap/permissions.list declares them 2750 with
    # the group only, so the owner is the only writer). admin-runner's ensure_agent_users hands them over once, when
    # the agent is created, and nothing did it again: after every install or update an agent could no longer write
    # its own work/ (found on a stand: the e2e permissions check failed right after an update, and on a node that had been
    # updated every agent was refused). Hand them back, to the agent whose folder it is.
    local agent_dir agent_slug agent_sub
    for agent_dir in "$SN_AGENTS_ROOT"/*/; do
        agent_slug="$(basename "$agent_dir")"
        id -u "agent-$agent_slug" >/dev/null 2>&1 || continue          # _template, specialists, a retired agent
        for agent_sub in logs state work; do
            [[ -d "$agent_dir$agent_sub" ]] || continue
            chown -R "agent-$agent_slug":"$SMART_NODE_AGENT_GROUP" "$agent_dir$agent_sub" 2>/dev/null || true
        done
    done
    # Record the projection tree. It was created here but never recorded, so
    # the node's own install manifest -- and validate.sh, which walks it --
    # did not know these directories existed. That is why the exposure in
    # the regression was invisible to every check the node runs on itself.
    manifest_record dir "$SN_AGENTS_ROOT"
    manifest_record dir "$SN_AGENT_TEMPLATE"
    manifest_record dir "$SN_SPECIALISTS_SEED"
    manifest_record dir /srv/tools
    manifest_record dir /srv/generated
    manifest_record dir /srv/data
    manifest_record dir /srv/data/attachments
    manifest_record dir /srv/data/attachments/desks
    manifest_record dir /srv/data/attachments/tasks
    log "laid /srv projection skeleton (agents/_template + _specialists + tools + generated + data/attachments + desks + tasks)"
}

# install_health_probe_cron -- wire health-probe.py as a 15-minute system cron.
#
# Runs as root, which has sudo access to postgres and docker. The probe writes
# to sn_core.agent_health_checks via psql; the panel reads that table but never
# writes it (architect decision). Using /etc/cron.d so a wedged
# core-api process cannot also silence its own health checker.
#
# Cron file: /etc/cron.d/smart-node-health-probe (every 15 minutes, root).
install_health_probe_cron() {
    local cron_file="/etc/cron.d/smart-node-health-probe"
    local log_file="/var/log/smart-node/health-probe.log"
    local probe="/srv/tools/health-checker/health-probe.py"
    touch "$log_file" 2>/dev/null && chmod 0644 "$log_file" || true
    cat > "$cron_file" <<EOF
# Agent health probe: check every agent's granted tools and channels,
# write results to sn_core.agent_health_checks every 15 minutes.
# The panel reads this table read-only; this cron is the only writer.
*/15 * * * *  root  python3 $probe >> $log_file 2>&1
EOF
    chmod 0644 "$cron_file"
    log "health probe cron installed -> $cron_file (every 15 minutes)"
}

# install_integration_cli -- install the `integration` provisioner CLI at
# /usr/local/bin/integration. This is the tool that provisions Pattern A
# (pip package) MCP servers on behalf of the kernel installer and operators.
#
#
# The script is sourced from companions/tools/integration, which arrives as
# the "tools" sidecar beside this companion script.
install_integration_cli() {
    local src="$SCRIPT_DIR/tools/integration"
    local dst="/usr/local/bin/integration"

    if [[ ! -f "$src" ]]; then
        log "WARN: tools/integration sidecar missing ($src) -- skipping integration CLI install"
        return 0
    fi

    install -m 0755 -o root -g root "$src" "$dst"
    log "integration CLI installed -> $dst"
}

# build_gdrive_wheel -- build a pip wheel from the gdrive-mcp source sidecar
# and store it at /srv/integrations/packages/ so the provisioner can install
# from it without a network connection. the permissions policy Pattern A, first-install path.
#
# The wheel is the versioned artifact. After this build, companions/gdrive-mcp
# source is no longer needed on the node -- future updates replace the wheel.
#
# Skipped gracefully when the sidecar is absent (e.g. a re-run on a node
# whose gdrive-mcp is already at the right version).
# provision_host_utils -- the document extractor every upload path shells out to
# core-api (routes/agents.py, routes/knowledge.py) and the Studio
# (src/server.js) run /srv/tools/host-utils/venv/bin/markitdown to turn an
# uploaded PDF/DOCX/XLSX/PPTX into markdown. Nothing installed it, so every
# upload answered 503 "extractor unavailable (host-utils not provisioned)" on
# every node. Owned like the rest of /srv/tools: core-api is the only writer and
# the group has no write, so an agent cannot replace the tool it is trusted to run.
# Idempotent: a venv that already reports the pinned version is left alone.
HOST_UTILS_DIR="${HOST_UTILS_DIR:-/srv/tools/host-utils}"
MARKITDOWN_SPEC="${MARKITDOWN_SPEC:-markitdown[pdf,docx,xlsx,pptx]==0.1.8}"
provision_host_utils() {
    local want="${MARKITDOWN_SPEC##*==}"
    local have=""
    if [[ -x "$HOST_UTILS_DIR/venv/bin/python" ]]; then
        have=$("$HOST_UTILS_DIR/venv/bin/python" -c \
            "import importlib.metadata as m; print(m.version('markitdown'))" 2>/dev/null || true)
    fi
    if [[ "$have" == "$want" && -x "$HOST_UTILS_DIR/venv/bin/markitdown" ]]; then
        log "host-utils: markitdown $have present"
        return 0
    fi
    install -d -m 2755 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$HOST_UTILS_DIR"
    [[ -x "$HOST_UTILS_DIR/venv/bin/python" ]] || \
        runuser -u "$SERVICE_USER" -- python3 -m venv "$HOST_UTILS_DIR/venv"
    runuser -u "$SERVICE_USER" -- env HOME=/tmp "$HOST_UTILS_DIR/venv/bin/pip" \
        install --no-cache-dir -q "$MARKITDOWN_SPEC" \
        || { log "WARN: host-utils: markitdown install failed -- document uploads will answer 503"; return 0; }
    log "host-utils: markitdown $want installed at $HOST_UTILS_DIR"
}

build_gdrive_wheel() {
    local src="$SCRIPT_DIR/gdrive-mcp"
    local pkg_dir="/srv/integrations/packages"

    if [[ ! -d "$src" ]]; then
        # Normal on subsequent runs once the sidecar stops shipping.
        log "gdrive-mcp sidecar absent ($src) -- wheel build skipped"
        return 0
    fi

    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$pkg_dir"
    log "building gdrive-mcp wheel from $src"
    # Build a wheelhouse, not only the application wheel: the provisioner uses
    # --no-index on the node, so every declared dependency must be beside it.
    # pip wheel is available on the system Python even where the `build` module
    # cannot be installed in an externally-managed/offline environment.
    python3 -m pip wheel --wheel-dir "$pkg_dir" "$src" \
        || { log "WARN: gdrive-mcp wheelhouse build failed -- provisioner will not find local wheels"; return 0; }
    chown root:"$SMART_NODE_GROUP" "$pkg_dir"/gdrive_mcp-*.whl 2>/dev/null || true
    log "gdrive-mcp wheel built -> $pkg_dir"
}

# register_secrets_mcp_server -- upsert the secrets MCP server row.
#
# mcp_secrets.py is already installed by install_snapshot() -- it lives in
# companions/core-api/ and the snapshot copies all source files to INSTALL_DIR.
# This call only registers the row so the runtime knows to spawn it for agents
# that hold `secrets: r--` in their grants.json.
register_secrets_mcp_server() {
    if ! declare -F sn_mcp_server_register >/dev/null; then
        log "WARN: integration helper not loaded -- secrets mcp_servers row not registered"
        return 0
    fi
    local secrets_mcp="$INSTALL_DIR/mcp_secrets.py"
    [[ -f "$secrets_mcp" ]] || { log "WARN: $secrets_mcp not found -- skipping secrets MCP registration"; return 0; }
    local args_json
    args_json=$(python3 -c "import json,sys; print(json.dumps([sys.argv[1]]))" "$secrets_mcp")
    sn_mcp_server_register \
        --slug      "secrets" \
        --name      "Secrets" \
        --transport stdio \
        --command   "$VENV_DIR/bin/python" \
        --args      "$args_json" \
        || log "WARN: secrets mcp_servers registration returned non-zero (non-fatal)"
    log "secrets mcp_servers row registered"
}

# register_reminders_mcp_server -- upsert the reminders MCP server row.
#
# mcp_reminders.py ships in the snapshot like mcp_secrets.py. Unlike secrets it
# needs no grant: core-api renders it into every agent's .mcp.json
# (BASE_MCP_SERVERS in routes/agents.py), because "remind me" is basic.
register_reminders_mcp_server() {
    if ! declare -F sn_mcp_server_register >/dev/null; then
        log "WARN: integration helper not loaded -- reminders mcp_servers row not registered"
        return 0
    fi
    local mcp_py="$INSTALL_DIR/mcp_reminders.py"
    [[ -f "$mcp_py" ]] || { log "WARN: $mcp_py not found -- skipping reminders MCP registration"; return 0; }
    sn_mcp_server_register         --slug      "reminders"         --name      "Reminders"         --transport stdio         --default-on         --command   "$VENV_DIR/bin/python"         --args      "$(python3 -c "import json,sys; print(json.dumps([sys.argv[1]]))" "$mcp_py")"         || log "WARN: reminders mcp_servers registration returned non-zero (non-fatal)"
    log "reminders mcp_servers row registered"
}

# register_agents_mcp_server -- upsert the agents MCP server row.
#
# mcp_agents.py ships in the snapshot like mcp_reminders.py. It needs no grant:
# core-api renders it into the configurator's .mcp.json only (routes/agents.py).
register_agents_mcp_server() {
    if ! declare -F sn_mcp_server_register >/dev/null; then
        log "WARN: integration helper not loaded -- agents mcp_servers row not registered"
        return 0
    fi
    local mcp_py="$INSTALL_DIR/mcp_agents.py"
    [[ -f "$mcp_py" ]] || { log "WARN: $mcp_py not found -- skipping agents MCP registration"; return 0; }
    local args_json
    args_json=$(python3 -c "import json,sys; print(json.dumps([sys.argv[1]]))" "$mcp_py")
    sn_mcp_server_register         --slug      "agents"         --name      "Agents"         --transport stdio         --command   "$VENV_DIR/bin/python"         --args      "$args_json"         || log "WARN: agents mcp_servers registration returned non-zero (non-fatal)"
    log "agents mcp_servers row registered"
}

# register_gdrive_mcp -- upsert the gdrive mcp_servers row with full
# provisioning metadata. Called after wait_for_core_api.
#
# The config_jsonb carries Pattern A fields alongside the runtime command so
# `integration provision gdrive` knows what to install and where.
register_gdrive_mcp() {
    if ! declare -F sn_mcp_server_register >/dev/null; then
        log "WARN: integration helper not loaded -- gdrive mcp_servers row not registered"
        return 0
    fi
    sn_mcp_server_register \
        --slug           gdrive \
        --name           "Google Drive" \
        --transport      stdio \
        --command        "/srv/integrations/gdrive/venv/bin/gdrive-mcp" \
        --args           "[]" \
        --autoinstall \
        --package-name   gdrive-mcp \
        --version-pin    0.1.1 \
        --package-source /srv/integrations/packages \
        || log "WARN: gdrive mcp_servers registration returned non-zero (non-fatal)"
    log "gdrive mcp_servers row registered"
}

# provision_mcp_autoinstall -- run `integration provision --all` to install
# every autoinstall=true MCP server. Idempotent. Called after core-api is up
# and after mcp_servers rows are registered.
provision_mcp_autoinstall() {
    if ! command -v integration >/dev/null 2>&1; then
        log "WARN: integration CLI not found -- skipping autoinstall provisioning"
        return 0
    fi
    log "provisioning autoinstall MCP servers"
    if ! integration provision --all; then
        log "ERROR: integration provision --all failed -- MCP server installation incomplete, deployment is broken"
        return 1
    fi
}

open_firewall() {
    # core-api binds 0.0.0.0. The access boundary is ufw alone.
    # Open the port only to docker bridge subnets, never to Anywhere.
    # A rule broader than the bridge subnet would expose core-api's Remote-User
    # trust to the public internet.
    command -v ufw >/dev/null 2>&1 || return 0
    ufw status 2>/dev/null | grep -q "Status: active" || return 0
    local sn_subnets subnet
    sn_subnets=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null | tr ' ' '\n' | grep -v '^$' || true)
    [[ -n "$sn_subnets" ]] || { log "ufw: cannot determine bridge subnets -- skipping firewall rule"; return 0; }
    for subnet in $sn_subnets; do
        if ufw status | grep -qE "${CORE_API_PORT}.*${subnet}"; then
            log "ufw: bridge $subnet -> :$CORE_API_PORT already allowed"
            continue
        fi
        ufw allow from "$subnet" to any port "$CORE_API_PORT" proto tcp \
            comment "core-api: docker bridge only" >/dev/null 2>&1 || true
        log "ufw: allowed $subnet -> :$CORE_API_PORT"
    done
}

run_verify_checks() {
    verify_check "role library laid at $ROLE_LIBRARY_DIR" bash -c \
        "[ -n \"\$(find '$ROLE_LIBRARY_DIR' -name '*.md' 2>/dev/null | head -1)\" ] && [ \"\$(stat -c %U '$ROLE_LIBRARY_DIR')\" = root ]"
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. service user exists.
    verify_check "service user $SERVICE_USER exists" id -u "$SERVICE_USER"
    # 3. install tree present.
    verify_check "install dir $INSTALL_DIR exists" test -d "$INSTALL_DIR"
    verify_check "avatar state dir is owned by the core-api writer" bash -c \
        "[[ \$(stat -c '%a %U:%G' '$AVATAR_STATE_DIR' 2>/dev/null) == '750 $SERVICE_USER:$SMART_NODE_GROUP' ]]"
    # 4. vendored app present in the tree.
    verify_check "core-api main.py installed" test -f "$INSTALL_DIR/main.py"
    # 5. seed script present (ExecStartPre).
    verify_check "seed-tools.py installed" test -f "$INSTALL_DIR/seed-tools.py"
    # 6. venv interpreter present.
    verify_check "venv interpreter present" test -x "$VENV_DIR/bin/python"
    # 7. core deps importable in the venv.
    verify_check "venv has uvicorn + fastapi + psycopg2" \
        "$VENV_DIR/bin/python" -c "import uvicorn, fastapi, psycopg2"
    # 8. systemd unit file installed.
    verify_check "unit $SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # Document uploads: the extractor exists and runs as the Studio user,
    # the other caller of it.
    verify_check "host-utils markitdown present" test -x "$HOST_UTILS_DIR/venv/bin/markitdown"
    # "installed" and "active" are both true of a node whose unit is a release
    # behind -- which is how two ExecStartPre seeds ran exactly once each and
    # nothing noticed. Render what this version wants and compare.
    local unit_diff; unit_diff="$(unit_diff_text)"
    verify_check "unit matches what this version generates (not a release behind)" \
        test -z "$unit_diff"
    if [[ -n "$unit_diff" ]]; then
        echo "       the live unit differs from what this version generates." >&2
        echo "       Re-run this companion to regenerate it; nothing else will." >&2
        echo "$unit_diff" >&2
    fi
    # The service-MCP door is useful only when both halves agree: the user can
    # reach the Unix socket and core-api names it as a secrets operator.
    # Without an owner here core-api refuses every call a service makes for
    # the owner, so an install that lost the file must not verify clean.
    verify_check "owner contract names the node owner" bash -c \
        "grep -Eq '^SN_OPERATOR_ACTOR=.+' '$OPERATOR_ENV'"
    verify_check "secrets operator contract is rendered" \
        test -s "$SECRETS_OPERATORS_ENV"
    local _secrets_users _secrets_user _secrets_old_ifs="$IFS"
    _secrets_users=$(_envfile_get "$SECRETS_OPERATORS_ENV" SN_SECRETS_OPERATOR_USERS)
    verify_check "secrets operator contract retains root" bash -c \
        "[[ ',$_secrets_users,' == *,root,* ]]"
    IFS=','
    for _secrets_user in $_secrets_users; do
        IFS="$_secrets_old_ifs"
        [[ "$_secrets_user" == "root" ]] || \
            verify_check "secrets operator $_secrets_user is in group $SMART_NODE_AGENT_GROUP" bash -c \
                "id -nG '$_secrets_user' | tr ' ' '\\n' | grep -qx '$SMART_NODE_AGENT_GROUP'"
        IFS=','
    done
    IFS="$_secrets_old_ifs"
    # 9. unit enabled.
    verify_check "unit $SYSTEMD_UNIT enabled" \
        systemctl is-enabled "$SYSTEMD_UNIT"
    # 10. unit active.
    verify_check "unit $SYSTEMD_UNIT active" \
        systemctl is-active "$SYSTEMD_UNIT"
    # 11. core-api answers /api/health.
    verify_check "core-api answers /api/health" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            'http://127.0.0.1:$CORE_API_PORT/api/health' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    # 11a. The second door: core-api answers on the unix socket too, and
    # the socket is reachable by agents and by nobody else. Checked as a PAIR --
    # "the socket exists" says nothing about whether an agent can use it, and
    # "an agent name over TCP is refused" would pass equally on a node where the
    # socket answers nothing at all.
    verify_check "socket directory declared in tmpfiles.d" \
        test -f /etc/tmpfiles.d/smart-node-core-api.conf
    verify_check "socket directory is 2750 $SERVICE_USER:$SMART_NODE_AGENT_GROUP" bash -c "
        [[ \$(stat -c '%a %U:%G' /run/smart-node/api 2>/dev/null) == '2750 $SERVICE_USER:$SMART_NODE_AGENT_GROUP' ]]"
    verify_check "socket exists, 0660, group $SMART_NODE_AGENT_GROUP (inherited by setgid)" bash -c "
        [[ \$(stat -c '%a %G' '$CORE_API_UDS' 2>/dev/null) == '660 $SMART_NODE_AGENT_GROUP' ]]"
    verify_check "core-api answers /api/health on the socket" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            --unix-socket '$CORE_API_UDS' 'http://core-api/api/health' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    # Without the list every callback a node service makes is refused, so an
    # install that lost the line must not verify clean.
    verify_check "service permission list is rendered" bash -c "
        grep -Eq '^SN_SERVICE_PERMISSIONS=.+' '$SERVICE_AUTH_ENV'"
    # The functional pair needs a real agent to speak as, and a node can be
    # installed before its first agent exists. When there is one, both halves run
    # here; when there is not, say so rather than counting a skipped check as a
    # pass -- a verification that reports success for an untested boundary is the
    # failure mode this project keeps finding.
    # `|| true` is load-bearing, not defensive noise: this script runs under
    # `set -e`, and an assignment whose command substitution fails takes the
    # whole verify pass down with it -- silently, since the output is a header
    # and nothing else. Measured on test-node: a fresh node has no agents, the query
    # returns nothing, and the installer reported the companion as failed with
    # no failing check named.
    local _ag="" _agu=""
    _ag=$(su -s /bin/bash -c "psql -tAc \"SELECT slug FROM sn_config.agents WHERE archived_at IS NULL AND os_user IS NOT NULL LIMIT 1\"" postgres 2>/dev/null | tr -d ' ' || true)
    if [[ -n "$_ag" ]]; then
        _agu=$(su -s /bin/bash -c "psql -tAc \"SELECT os_user FROM sn_config.agents WHERE slug='$_ag'\"" postgres 2>/dev/null | tr -d ' ' || true)
    fi
    if [[ -n "$_ag" && -n "$_agu" ]] && id -u "$_agu" >/dev/null 2>&1; then
        verify_check "agent '$_ag' is REFUSED when it asserts its name over TCP" bash -c "
            code=\$(sudo -n -u '$_agu' curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
                -H 'Remote-User: $_ag' 'http://127.0.0.1:$CORE_API_PORT/api/planner/api/v1/tasks?limit=1' 2>/dev/null)
            [[ \"\$code\" == '403' ]]"
        verify_check "agent '$_ag' is ACCEPTED over the socket (the other half)" bash -c "
            code=\$(sudo -n -u '$_agu' curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
                --unix-socket '$CORE_API_UDS' 'http://core-api/api/planner/api/v1/tasks?limit=1' 2>/dev/null)
            [[ \"\$code\" =~ ^2 ]]"
    else
        log "NOTE: no agent with an os_user on this node yet, so the identity pair" \
            "(refused over TCP / accepted over the socket) was NOT exercised."
    fi
    # 11b. retention sweep installed and SCHEDULED. The timer is the
    # check that matters: the script sitting on disk prunes nothing, and enabling
    # the oneshot service instead would run it once at boot and never again.
    verify_check "retention sweep installed executable" test -x "$SN_OPS_DIR/retention.py"
    verify_check "timer smart-node-retention.timer enabled" \
        systemctl is-enabled smart-node-retention.timer
    verify_check "timer smart-node-retention.timer active" \
        systemctl is-active smart-node-retention.timer
    # 11c. daily event digest installed and SCHEDULED. Same shape as retention:
    # the script sitting on disk mails nothing, only the timer does. The
    # timer runs daily, so verifying it is active (started) also proves the
    # systemctl enable --now took, which is how a disabled timer would get
    # caught here even though it is written to disk.
    verify_check "event digest installed executable" test -x "$SN_OPS_DIR/event-digest.py"
    # test -x only proves the file is executable, not that it can start.
    # Load the module through importlib so a missing sibling (for example
    # _events_classify.py not copied by the installer) fails install
    # instead of surfacing at the first run 24h later.
    verify_check "event digest imports" \
        /usr/bin/python3 -c "import importlib.util, sys; sys.path.insert(0, '$SN_OPS_DIR'); spec = importlib.util.spec_from_file_location('event_digest', '$SN_OPS_DIR/event-digest.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"
    verify_check "event digest shared classifier present" \
        test -f "$SN_OPS_DIR/_events_classify.py"
    # The digest is an e-mail: its timer runs only where a mail sender was proved.
    if grep -qE '^SN_NOTIFY_SENDER=.+' "$BUDGET_NOTIFICATIONS_ENV" 2>/dev/null; then
        verify_check "timer smart-node-event-digest.timer enabled" \
            systemctl is-enabled smart-node-event-digest.timer
        verify_check "timer smart-node-event-digest.timer active" \
            systemctl is-active smart-node-event-digest.timer
    fi
    # 12. /api/system/status reachable (db-backed observability snapshot).
    verify_check "core-api /api/system/status reachable" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
            --unix-socket '$CORE_API_UDS' 'http://core-api/api/system/status' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    # 13. peer-auth ident map carries the core-api OS-user -> core-role line.
    verify_check "pg_ident has smartnode map ($SERVICE_USER -> core role)" bash -c "
        ident=\$(sed -n -E 's/^SMART_NODE_PG_IDENT=//p' '$SMART_NODE_PERSISTENCE_ENV' | tail -n1)
        role=\$(sed -n -E 's/^SMART_NODE_PG_CORE_ROLE=//p' '$SMART_NODE_PERSISTENCE_ENV' | tail -n1)
        [[ -n \"\$ident\" && -f \"\$ident\" ]] || exit 1
        grep -qF \"smartnode  $SERVICE_USER  \${role:-smartnode}\" \"\$ident\""
    # 14. Traefik route file in the dynamic dir. A no-domain install is a
    #     supported shape and install_route skips the file on
    #     it, so requiring the file unconditionally fails a correct install.
    #     This check passed on a no-domain node only because of the defect fixed
    #     earlier in this release: derive_panel_host logged to stdout, the log
    #     line landed in panel_host, the empty-guard never fired and a route was
    #     written anyway with the log text as its hostname. So on the no-domain
    #     path assert the file is ABSENT -- that is the assertion that would have
    #     caught it, and it also holds the line for the route retirement.
    #     Resolve the domain the way derive_panel_host does. It reads routing.env
    #     with _envfile_get and only source-s the file in a later branch, so
    #     SMART_NODE_DOMAIN is NOT a shell variable here on a node that has one --
    #     asking ${SMART_NODE_DOMAIN:-} sends a domain node down the no-domain
    #     path and fails it on the route it just wrote correctly.
    local _route_domain="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$_route_domain" && -r "$SMART_NODE_ROUTING_ENV" ]]; then
        _route_domain=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_DOMAIN)
    fi
    local _panel_host; _panel_host=$(derive_panel_host 2>/dev/null || true)
    if [[ -n "$_route_domain" ]] && public_route_eligible "$_panel_host"; then
        verify_check "Traefik core-api route written" bash -c "
            dyn=\$(sed -n -E 's/^SMART_NODE_TRAEFIK_DYNAMIC_DIR=//p' '$SMART_NODE_ROUTING_ENV' | tail -n1)
            [[ -n \"\$dyn\" && -f \"\$dyn/$TRAEFIK_ROUTE_BASENAME\" ]]"
    else
        verify_check "no core-api route on a node with no domain" bash -c "
            dyn=\$(sed -n -E 's/^SMART_NODE_TRAEFIK_DYNAMIC_DIR=//p' '$SMART_NODE_ROUTING_ENV' | tail -n1)
            [[ -z \"\$dyn\" ]] || [[ ! -f \"\$dyn/$TRAEFIK_ROUTE_BASENAME\" ]]"
    fi
    # 15. companion manifest written.
    verify_check "companions.d/core-api.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/core-api.json"
    # 16. install-report has the core-api section.
    verify_check "install-report has the core-api section" \
        bash -c "grep -qFx '## Companion core-api' '$SMART_NODE_REPORT'"
    # 17. manifest records the install dir + the unit.
    verify_check "manifest records the core-api artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"dir\",\"value\":\"$INSTALL_DIR\",' \"\$m\" &&
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$SYSTEMD_UNIT\",' \"\$m\""
    # 18. health-checker probe installed and cron present.
    verify_check "health-checker probe installed" \
        test -x /srv/tools/health-checker/health-probe.py
    verify_check "health-checker status.sh installed" \
        test -x /srv/tools/health-checker/status.sh
    verify_check "attachment reader installed" \
        test -x /srv/tools/attachments/attachment-read.py
    verify_check "health probe cron present" \
        test -f /etc/cron.d/smart-node-health-probe
    # 19. integration provisioner CLI installed.
    verify_check "integration provisioner CLI installed" \
        test -x /usr/local/bin/integration
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
# operator-reversible (the Cloudron card model), so the companion owns an
# explicit --uninstall in addition to the manifest replay that
# uninstall-from.sh performs for a whole-node teardown. The pg_ident map
# line is left in place -- removing it is the persistence stage's concern,
# and an orphan ident line is harmless.
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion core-api -- uninstall"
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        systemctl disable --now "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    fi
    rm -f "$SYSTEMD_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    # Remove the Traefik route (hot-reloaded away by the file provider).
    local dyn_dir=""
    [[ -r "$SMART_NODE_ROUTING_ENV" ]] \
        && dyn_dir=$(_envfile_get "$SMART_NODE_ROUTING_ENV" SMART_NODE_TRAEFIK_DYNAMIC_DIR)
    [[ -n "$dyn_dir" ]] && rm -f "$dyn_dir/$TRAEFIK_ROUTE_BASENAME"
    rm -rf "$INSTALL_DIR"
    # Health probe cron and scripts.
    rm -f /etc/cron.d/smart-node-health-probe || true
    rm -rf /srv/tools/health-checker || true
    # Daily event digest timer, units, script, and the shared classifier.
    # The 5301 branch (event monitor) also removes _events_classify.py; the
    # second rm is a no-op either way. Safe to remove here even when the
    # monitor is still installed: the next install_event_monitor re-copies
    # the same bytes.
    if systemctl list-unit-files smart-node-event-digest.timer >/dev/null 2>&1; then
        systemctl disable --now smart-node-event-digest.timer >/dev/null 2>&1 || true
    fi
    rm -f /etc/systemd/system/smart-node-event-digest.timer || true
    rm -f /etc/systemd/system/smart-node-event-digest.service || true
    rm -f "$SN_OPS_DIR/event-digest.py" || true
    rm -f "$SN_OPS_DIR/_events_classify.py" || true
    rm -f "$SN_OPS_DIR/__pycache__/_events_classify."*.pyc 2>/dev/null || true
    # Integration CLI.
    rm -f /usr/local/bin/integration || true
    # Old gdrive-mcp install path (legacy). Clean up on nodes that ran
    # the previous installer which put files at /srv/tools/gdrive-mcp/.
    rm -rf /srv/tools/gdrive-mcp || true
    unregister_companion "$STAGE_ID"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" >/dev/null 2>&1 || true
    fi
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion core-api -- uninstalled (log dir $LOG_DIR left for inspection)"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   probe_before (log-only, no state written), create_service_user (OS
#   account, one-time), install_snapshot (extract application files,
#   version-gated), lay_srv_skeleton (copy /srv skeleton, version-gated),
#   build_venv (pip install, version-gated), setup_db_ident (pg_ident.conf
#   line -- this IS a contract write but requires a cluster reload; leave
#   in install for now, file a separate task if ident drift is observed),
#   install_socket_dir (directory create, idempotent but only needed once),
#   build_gdrive_wheel (wheel build, version-gated), install_integration_cli
#   (binary install, version-gated), wait_for_core_api (health probe, no
#   state written), provision_mcp_autoinstall (MCP server provisioning --
#   requires core-api to be listening and is a heavier install operation;
#   safe to skip on an unchanged-version update where the MCP list is the
#   same as the previous run).
#
# CONTRACT (run): ensure_proxy_auth_secret, ensure_service_auth_secret
#   (verify secrets exist in the store -- fast, no state written if already
#   present), install_service (systemd unit file -- must be current),
#   install_reconcile_timer (cron/timer unit -- must be current),
#   install_retention_sweep (retention cron entry -- must be current),
#   install_classes_verify (classes-verify unit -- must be current),
#   install_health_probe_cron (health probe cron -- must be current),
#   install_route (Traefik route file -- must be current), open_firewall
#   (ufw rule for docker bridge -> CORE_API_PORT -- if removed the panel
#   and agents cannot reach the API; idempotent), register_secrets_mcp_server
#   (sn_config mcp row -- must be current), register_gdrive_mcp (sn_config
#   mcp row -- must be current), register_companion (companions.d record for
#   version tracking), stage_mark done.
run_contracts_only() {
    require_root
    log "companion core-api -- contracts only (v$STAGE_VERSION)"
    # the roles change with the release even when core-api's own version does not
    lay_role_library
    ensure_proxy_auth_secret >/dev/null
    ensure_planner_operator_secret >/dev/null
    ensure_service_auth_secret >/dev/null
    install_service
    install_reconcile_timer
    install_retention_sweep
    install_classes_verify
    install_event_digest
    install_health_probe_cron
    install_route
    open_firewall
    register_secrets_mcp_server
    register_reminders_mcp_server
    register_agents_mcp_server
    register_gdrive_mcp
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    stage_mark "$STAGE_ID" done \
        "version=core-api@$STAGE_VERSION"
    log "companion core-api -- contracts only done"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
