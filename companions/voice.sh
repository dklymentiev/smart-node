#!/bin/bash
#
# voice.sh -- Smart Node installer, Layer C Phase 2 companion: voice.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B03 (Docker
# + Traefik) and the memory companion (mesh, for user profiles).
#
# As of v2.0.0 voice is a NATIVE systemd service, not a Docker container
# The companion ships its Python source (companions/voice/backend/
# and companions/voice/frontend/) alongside this script and installs it under
# a dedicated OS user with a Python venv.
#
# Why native: a hotfix is git pull + systemctl restart, under 60 seconds.
# Container builds took ~106 seconds per run and produced nothing reusable in
# the release tarball (no pre-built image). pip install into a venv is
# 30-60 s on first run and cache-hit near-instant on repeat.
#
# TRAEFIK ROUTING
# voice binds the Docker bridge gateway IP so Traefik (a Docker container) can
# reach it. The gateway IP is read from the smart-node Docker network at install
# time (same pattern as the dispatcher). A file-provider config is written to
# /etc/smart-node/traefik/dynamic/70-voice.yml.
#
# DATA MIGRATION
# The Docker volume smart-node_sn-voice-data (mounted at /data in the
# container) held voice.db and transcripts/. This installer copies them to
# /opt/smart-node/voice/data/ best-effort if the volume is present.
# History is not critical -- a fresh database is always safe to start.
#
# Creates:
#   - service account smart-node-voice (OS user)
#   - /opt/smart-node/voice     -- Python source (backend/ + frontend/) + venv
#   - /opt/smart-node/voice/data -- SQLite database + transcripts
#   - /opt/smart-node/voice/.env (0600 root:root) -- runtime config
#   - /etc/smart-node/traefik/dynamic/70-voice.yml -- Traefik file route
#   - smart-node-voice.service  -- systemd unit (uvicorn on VOICE_HOST:8001)
#   - "Companion voice" section in the install report
#   - /var/lib/smart-node/stages/voice.state
#
# UPGRADE FROM CONTAINER (v1.x)
# The smart-node-voice Docker container is stopped and removed automatically.
# The Docker volume data is migrated to /opt/smart-node/voice/data/ if found.
# The smart-node-voice Docker image is left for manual cleanup.
#
# Usage:
#   voice.sh             full run: probe -> intent -> verify -> mark
#   voice.sh --verify    idempotent post-check; exit 0 / N failures
#   voice.sh --status    read /var/lib/smart-node/stages/voice.state
#   voice.sh --uninstall reverse the install
#   voice.sh --help      this message
#
# Environment:
#   SMART_NODE_GEMINI_API_KEY   Gemini API key (required for live voice)
#   SMART_NODE_DOMAIN           node domain
#   SMART_NODE_VOICE_HOSTNAME   the public name to publish the voice route under
#                               (default: voice.<SMART_NODE_DOMAIN>). A node whose
#                               voice line is already known by another name says so
#                               here rather than having a second name invented for it.
#   SMART_NODE_VOICE_LANGUAGE   default call language (default: en-US, not changed)
#   SMART_NODE_VOICE_MESH_URL   mesh API override (default: read from mesh.env)
#   SMART_NODE_VOICE_MESH_WORKSPACE  mesh workspace for voice (default: voice)
#   SN_DISP_HOST                Docker bridge gateway IP override
#
#
# Exits 0 on success.

set -euo pipefail

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="voice"
STAGE_NAME="voice"
STAGE_VERSION="2.0.7"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/voice"

SERVICE_USER="smart-node-voice"
SERVICE_SHORT="voice"  # create_service_user prefixes smart-node- itself
INSTALL_DIR="/opt/smart-node/voice"
VENV_DIR="$INSTALL_DIR/venv"
DATA_DIR="$INSTALL_DIR/data"
LOG_DIR="/var/log/smart-node/voice"
VOICE_ENV="$INSTALL_DIR/.env"

SYSTEMD_UNIT="smart-node-voice.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"

VOICE_HTTP_PORT=8001
TRAEFIK_DYNAMIC="${SMART_NODE_TRAEFIK_DYNAMIC:-/etc/smart-node/traefik/dynamic}"

# Legacy Docker names (v1.x) -- only referenced for migration.
LEGACY_CT="smart-node-voice"
LEGACY_VOLUME="smart-node_sn-voice-data"
LEGACY_VOLUME_DATA="/var/lib/docker/volumes/${LEGACY_VOLUME}/_data"

PYTHON_BIN="python3"

LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

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

derive_domain() {
    # routing.env absent is fatal. SMART_NODE_DOMAIN
    # empty means a no-domain install; callers skip public route composition.
    # No hostname(1) fallback, no silent empty-string concatenation.
    [[ -n "${SMART_NODE_DOMAIN:-}" ]] && return 0
    if [[ ! -f /etc/smart-node/routing.env ]]; then
        die "no /etc/smart-node/routing.env -- run B03-routing first, or pass SMART_NODE_DOMAIN. Cannot write node identity."
    fi
    # shellcheck source=/dev/null
    source /etc/smart-node/routing.env
    if [[ -z "${SMART_NODE_DOMAIN:-}" ]]; then
        log "[voice] SMART_NODE_DOMAIN empty in routing.env -- public route will be skipped"
        return 0
    fi
}

# resolve_voice_host -- set the bind address for the voice native service.
# voice binds 0.0.0.0 (all interfaces). Access boundary is ufw alone:
# open_firewall() opens the port only to docker bridge subnets, never to Anywhere.
# Traefik (container) reaches voice via host.docker.internal; native callers use 127.0.0.1.
resolve_voice_host() {
    VOICE_HOST="0.0.0.0"
    log "voice will bind 0.0.0.0:$VOICE_HTTP_PORT"
}

# resolve_mesh_url -- read mesh URL from mesh.env (native mesh) or env override.
resolve_mesh_url() {
    MESH_API_URL="${SMART_NODE_VOICE_MESH_URL:-}"
    if [[ -z "$MESH_API_URL" && -r /etc/smart-node/mesh.env ]]; then
        MESH_API_URL=$(sed -n 's/^MESH_API_URL=//p' /etc/smart-node/mesh.env | tail -1)
    fi
    MESH_API_URL="${MESH_API_URL:-http://127.0.0.1:8000}"
    log "mesh API for voice: $MESH_API_URL"
}

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

probe_before() {
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1 \
       && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "smart-node-voice service already present -- idempotent re-run"
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

# install_snapshot -- rsync backend + frontend to INSTALL_DIR.
# Excludes the venv, data dir, and .env so they survive re-runs.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored voice snapshot missing ($SRC_SNAPSHOT)"
    [[ -f "$SRC_SNAPSHOT/backend/requirements.txt" ]] \
        || die "voice snapshot incomplete -- no backend/requirements.txt"
    [[ -d "$SRC_SNAPSHOT/frontend" ]] \
        || die "voice snapshot incomplete -- no frontend/"

    log "syncing voice snapshot -> $INSTALL_DIR"
    rsync -a --delete \
        --exclude 'venv/' \
        --exclude 'data/' \
        --exclude '.env' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude 'docker-compose*' \
        --exclude 'Dockerfile*' \
        --exclude 'systemd/' \
        --exclude '.git/' \
        --exclude 'CHANGELOG.md' \
        --exclude 'VENDORED' \
        "$SRC_SNAPSHOT/" "$INSTALL_DIR/"
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
    chmod -R go-w "$INSTALL_DIR"
}

# patch_frontend -- replace build-time tokens in index.html.
# The container Dockerfile did this with `sed -i` during `docker build`.
# We do it here instead, against the installed copy.
patch_frontend() {
    local idx="$INSTALL_DIR/frontend/index.html"
    [[ -f "$idx" ]] || return 0
    local build_ts; build_ts=$(date +%m%d-%H%M)
    sed -i \
        -e "s/__BUILD_TS__/${build_ts}/g" \
        -e "s/__APP_VERSION__/${STAGE_VERSION}/g" \
        -e "s/__ENV_LABEL__//g" \
        "$idx"
    log "patched frontend/index.html (ts=${build_ts}, ver=${STAGE_VERSION})"
}

# build_venv -- create or reuse the venv and install requirements.
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
    log "installing voice requirements"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet \
        -r "$INSTALL_DIR/backend/requirements.txt" \
        || die "pip install of voice requirements failed"
}

# ensure_data_dir -- create the data directory and set ownership.
ensure_data_dir() {
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$DATA_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$DATA_DIR/transcripts"
    manifest_record dir "$DATA_DIR"
}

# migrate_from_container -- stop v1.x Docker container and copy volume data.
# Non-fatal: a fresh node has no container or volume.
migrate_from_container() {
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    # Stop and remove the old container.
    if docker ps -aq -f "name=^${LEGACY_CT}\$" 2>/dev/null | grep -q .; then
        log "stopping and removing legacy Docker container '$LEGACY_CT'"
        docker rm -f "$LEGACY_CT" >/dev/null 2>&1 || true
    fi
    # Copy data from the Docker volume if present.
    if [[ -d "$LEGACY_VOLUME_DATA" ]]; then
        log "migrating voice data from Docker volume $LEGACY_VOLUME"
        if [[ -f "$LEGACY_VOLUME_DATA/voice.db" && ! -f "$DATA_DIR/voice.db" ]]; then
            cp "$LEGACY_VOLUME_DATA/voice.db" "$DATA_DIR/voice.db"
            chown "$SERVICE_USER:$SMART_NODE_GROUP" "$DATA_DIR/voice.db"
            log "migrated voice.db"
        fi
        if [[ -d "$LEGACY_VOLUME_DATA/transcripts" ]]; then
            rsync -a "$LEGACY_VOLUME_DATA/transcripts/" "$DATA_DIR/transcripts/" 2>/dev/null || true
            chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$DATA_DIR/transcripts/"
            log "migrated transcripts/"
        fi
    fi
}

# write_env -- write (or refresh) the .env file.
# Carries forward existing GEMINI/GOOGLE key if present.
write_env() {
    local existing_key=""
    if [[ -f "$VOICE_ENV" ]]; then
        existing_key=$(grep -E '^(GOOGLE|GEMINI)_API_KEY=' "$VOICE_ENV" | head -1 | cut -d= -f2- || true)
    fi
    local gemini_key="${SMART_NODE_GEMINI_API_KEY:-$existing_key}"

    # Dispatcher (brin) URL and key -- same logic as v1.x voice.sh.
    local disp_env="/opt/smart-node/dispatcher/.env"
    local brin_key="" brin_port="" brin_url=""
    if [[ -f "$disp_env" ]]; then
        brin_key=$(grep -E '^BRIN_API_KEY=' "$disp_env" 2>/dev/null | cut -d= -f2- || true)
        # brin binds 0.0.0.0; HOST in dispatcher .env is the bind address.
        # Voice is a native service, so the caller address is always 127.0.0.1.
        brin_port=$(grep -E '^PORT=' "$disp_env" 2>/dev/null | cut -d= -f2- || true)
        [[ -n "$brin_port" ]] || brin_port=8101
        if [[ -n "$brin_key" ]]; then
            brin_url="http://127.0.0.1:$brin_port"
        fi
    fi

    # Planner key minted for voice by the tracking companion.
    local planner_key=""
    if [[ -r /etc/smart-node/planner-key-voice.env ]]; then
        planner_key=$(sed -n 's/^PLANNER_API_KEY=//p' /etc/smart-node/planner-key-voice.env | head -n1)
    fi

    # Service auth secret from core-api.
    local svc_secret=""
    if declare -f _envfile_get >/dev/null 2>&1; then
        svc_secret=$(_envfile_get /etc/smart-node/core-api-service-auth.env SN_SERVICE_AUTH_SECRET 2>/dev/null || true)
    fi

    # The node owner, as the core-api companion recorded it. Voice reads the
    # node as the owner and must be told who that is, not assume a name.
    local owner_actor=""
    if declare -f _envfile_get >/dev/null 2>&1; then
        owner_actor=$(_envfile_get /etc/smart-node/operator.env SN_OPERATOR_ACTOR 2>/dev/null || true)
    fi
    if [[ -z "$owner_actor" ]]; then
        log "WARN: no owner in /etc/smart-node/operator.env -- voice cannot read agents, tools or events from core-api (run core-api first)"
    fi

    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node voice companion .env -- voice.sh@$STAGE_VERSION."
        echo "# Mode 0600 root:root."
        echo ""
        echo "SMART_NODE_DOMAIN=$SMART_NODE_DOMAIN"
        echo ""
        echo "# Gemini API key (required for live voice)."
        echo "GOOGLE_API_KEY=$gemini_key"
        echo "GEMINI_API_KEY=$gemini_key"
        echo ""
        echo "# SQLite data paths (native, replaces Docker volume)."
        echo "DB_PATH=$DATA_DIR/voice.db"
        echo "TRANSCRIPT_DIR=$DATA_DIR/transcripts"
        echo ""
        echo "# Mesh (memory) companion."
        echo "MESH_API_URL=$MESH_API_URL"
        echo "MESH_WORKSPACE=${SMART_NODE_VOICE_MESH_WORKSPACE:-voice}"
        echo ""
        echo "# Language for unknown callers (not changed -- en-US is the node default)."
        echo "VOICE_LANGUAGE=${SMART_NODE_VOICE_LANGUAGE:-en-US}"
        if [[ -n "$svc_secret" ]]; then
            echo ""
            echo "# Service auth (delegation to agents)."
            echo "SN_SERVICE_AUTH_SECRET=$svc_secret"
        fi
        if [[ -n "$owner_actor" ]]; then
            echo ""
            echo "# The node owner, for voice's reads of the node."
            echo "SN_OPERATOR_ACTOR=$owner_actor"
        fi
        if [[ -n "$brin_url" ]]; then
            echo ""
            echo "# Dispatcher (brin) for agent delegation tools."
            echo "BRIN_URL=$brin_url"
            echo "BRIN_API_KEY=$brin_key"
        fi
        if [[ -n "$planner_key" ]]; then
            echo ""
            echo "# Planner credential (IT ticket filing)."
            echo "PLANNER_API_KEY=$planner_key"
        fi
        echo ""
        # The avatar store. backend/main.py proxies /avatars/<path> to
        # STUDIO_URL so the phone shows the same faces the Studio does, and
        # STUDIO_URL comes from SN_STUDIO_URL -- which nothing ever wrote.
        # Unset, main.py:1209 leaves it "" and the proxy builds a host-less URL,
        # so every picture answered 502 on a node with the avatars on disk.
        # Studio is a native service; so is the caller here, which is why
        # 127.0.0.1 is the stable address -- the same reasoning BRIN_URL uses
        # above. SMART_NODE_STUDIO_URL overrides.
        # the address voice reports its spend to. Voice talks to its
        # provider directly, so without this the node total is wrong by however
        # much anybody has spoken, and a hard budget does not stop a call.
        echo "# Gateway (budgets: ask before a call, report after it)."
        echo "GATEWAY_URL=${SMART_NODE_GATEWAY_URL:-http://127.0.0.1:8000}"
        echo ""
        echo "# Studio (the avatar store the phone proxies)."
        echo "SN_STUDIO_URL=${SMART_NODE_STUDIO_URL:-http://127.0.0.1:8050}"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$VOICE_ENV"
    rm -f "$tmp"
    manifest_record file "$VOICE_ENV"

    if [[ -z "$gemini_key" ]]; then
        log "WARN: GEMINI_API_KEY not set -- voice installs degraded. Re-run with SMART_NODE_GEMINI_API_KEY=... to configure."
    fi
}

# derive_voice_public_host -- the name(s) the voice route is published under.
# SMART_NODE_VOICE_HOSTNAME from the environment wins; then routing.env (written
# by B03 carry_forward_voice_hostname); then voice.<domain>. Pure: sets
# VOICE_PUBLIC_HOST and writes nothing.
# WHY a function: the writer derived this inline and the standalone --verify
# process never derived it at all, so on the one node that names its line
# (a node in the field with its own line name) the check asked the gate about an empty name,
# concluded "no route", and failed the 1.12.36 update on a route the writer had
# correctly written. One derivation, used by both.
derive_voice_public_host() {
    local _voice_hostname="${SMART_NODE_VOICE_HOSTNAME:-}"
    if [[ -z "$_voice_hostname" && -r /etc/smart-node/routing.env ]]; then
        _voice_hostname=$(sed -n "s/^SMART_NODE_VOICE_HOSTNAME=//p" /etc/smart-node/routing.env | tail -1)
    fi
    # the phone line is line.<domain>; voice.<domain> redirects to it
    VOICE_PUBLIC_HOST="${_voice_hostname:-line.${SMART_NODE_DOMAIN:-}}"
}

# voice_route_expected -- 0 when the writer publishes a route: a domain is set
# and at least ONE of the names passes the gate. The writer filters name by name
# and writes if any survive; the check used to ask about the first name only.
voice_route_expected() {
    [[ -n "${SMART_NODE_DOMAIN:-}" ]] || return 1
    local _h
    for _h in ${VOICE_PUBLIC_HOST//,/ }; do
        [[ -n "$_h" ]] && public_route_eligible "$_h" && return 0
    done
    return 1
}

# write_traefik_route -- write the Traefik file-provider config for voice.
# Replaces the Docker label approach from v1.x.
write_traefik_route() {
    # no public domain means no route.
    # SMART_NODE_VOICE_HOSTNAME intentionally supports an empty domain.
    if [[ -z "${SMART_NODE_DOMAIN:-}" ]]; then
    # Skip means RETIRE, not walk past. Returning here without touching
    # the file leaves whatever the PREVIOUS release wrote sitting on disk, and
    # Traefik keeps serving it. That is how 1.12.29-era route files carrying a
    # bridge literal survived into a 1.12.33 node and failed the new bridge-literal
    # check -- the release had moved to host.docker.internal, the file had not,
    # and nothing in the update path was responsible for the difference. The
    # same guard also stranded a route written by an earlier bug on a node that
    # should have had none at all.
    #
    # manifest_retire_artifact exists for exactly this (1.12.32): the code that
    # removes an artifact withdraws the claim in the same breath, so validate
    # does not later fail on a file this release deleted on purpose.
        local stale="$TRAEFIK_DYNAMIC/70-voice.yml"
        if [[ -f "$stale" ]]; then
            rm -f "$stale"
            manifest_retire_artifact file "$stale" \
                "no public domain -- voice route retired"
            log "[voice] no public domain -- Traefik route retired ($stale)"
        else
            log "[voice] no public domain -- no Traefik route to write"
        fi
        return 0
    fi
    # The published name. Defaults to voice.<domain>, which is the convention, but a
    # node whose line is already known by another name (DNS handed out, printed on
    # something, dialled by somebody) must be able to keep that name instead of
    # gaining a second one. Derived once here so the route and the log agree.
    # Resolution order: SMART_NODE_VOICE_HOSTNAME env var wins; then routing.env
    # (written by B03 carry_forward_voice_hostname); then default voice.<domain>.
    # Empty at any level means "use the default" -- this name MAY differ, not MUST.
    derive_voice_public_host
    # More than one name is normal, not exotic: a node has a public face and an
    # internal one, and the Studio already ships as two files for exactly that.
    # Comma-separated in, one rule out, so the two names cannot drift apart.
    VOICE_RULE=""
    local _h _named=0
    for _h in ${VOICE_PUBLIC_HOST//,/ }; do
        [[ -z "$_h" ]] && continue
        _named=$((_named + 1))
        # A name with no DNS record is not a door. Filtered here rather
        # than refused at the top: voice may publish several names, and one of
        # them missing its record must not cost the others.
        if ! public_route_eligible "$_h"; then
            log "[voice] $PUBLIC_ROUTE_SKIP_REASON -- '$_h' left out of the route"
            continue
        fi
        [[ -n "$VOICE_RULE" ]] && VOICE_RULE="$VOICE_RULE || "
        VOICE_RULE="${VOICE_RULE}Host(\`${_h}\`)"
    done
    (( _named > 0 )) || die "SMART_NODE_VOICE_HOSTNAME resolved to no host at all"
    if [[ -z "$VOICE_RULE" ]]; then
        retire_route "voice" "$TRAEFIK_DYNAMIC/70-voice.yml" \
            "no name in '$VOICE_PUBLIC_HOST' has a DNS record"
        return 0
    fi
    # NAME THE CERTIFICATE RESOLVER. A bare `tls: {}` serves whatever certificate
    # already exists and asks for nothing, so a node publishing a name for the
    # first time answers with the default self-signed one and every client
    # refuses it. The resolver is the one B03 recorded in the routing contract,
    # not a constant: a node that does not use ACME leaves it empty and keeps
    # the old behaviour.
    local _resolver="${SMART_NODE_TLS_RESOLVER:-}"
    if [[ -z "$_resolver" && -r /etc/smart-node/routing.env ]]; then
        _resolver=$(sed -n "s/^SMART_NODE_TLS_RESOLVER=//p" /etc/smart-node/routing.env | tail -1)
    fi
    if [[ -n "$_resolver" ]]; then
        VOICE_TLS_BLOCK="        certResolver: $_resolver"
    else
        VOICE_TLS_BLOCK="        {}"
    fi
    VOICE_TLS_BLOCK="      tls:
$VOICE_TLS_BLOCK"
    web_local && VOICE_TLS_BLOCK="      $(web_tls_yaml)"
    [[ -n "${VOICE_HOST:-}" ]] \
        || die "VOICE_HOST is unset -- cannot write Traefik route"

    install -d -m 0755 -o root -g root "$TRAEFIK_DYNAMIC"
    local dst="$TRAEFIK_DYNAMIC/70-voice.yml"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node voice companion Traefik route.
# Generated by voice.sh@$STAGE_VERSION -- do not edit by hand.
# Voice is native; binds 0.0.0.0:$VOICE_HTTP_PORT.
# Traefik (container) reaches it via host.docker.internal.
http:
  routers:
    snvoice:
      rule: "${VOICE_RULE}"
      entryPoints:
        - $(web_entry)
      service: snvoice
      middlewares:
        - smart-node-auth@docker
${VOICE_TLS_BLOCK}
    # the old name redirects, the path and the query kept
    snvoice-old:
      rule: "Host(\`voice.${SMART_NODE_DOMAIN}\`)"
      entryPoints:
        - $(web_entry)
      service: snvoice
      middlewares:
        - snvoice-to-line
${VOICE_TLS_BLOCK}
  middlewares:
    snvoice-to-line:
      redirectRegex:
        regex: "^https?://voice\\\\.(.*)"
        replacement: "$(web_scheme)://line.\${1}"
        permanent: true
  services:
    snvoice:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:${VOICE_HTTP_PORT}/"
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$dst"
    rm -f "$tmp"
    manifest_record file "$dst"
    log "wrote Traefik file route -> $dst (${VOICE_PUBLIC_HOST} -> host.docker.internal:${VOICE_HTTP_PORT})"
}

# open_firewall -- let Traefik reach the port this companion binds.
#
# THE DEFECT THIS FIXES. Voice binds the docker gateway address, and Traefik is a
# CONTAINER, so its connection arrives at the host from the bridge subnet. UFW's
# INPUT policy is DROP, so without a rule that connection is dropped, Traefik
# waits, and the person gets a 504 -- but only AFTER logging in, because until
# then the forward-auth answers with a redirect and the backend is never dialled.
# So the service looked healthy from every direction except the one that matters,
# and the fault appeared to be somewhere between the browser and Cloudflare.
# companions/studio.sh has carried this rule since it was written; voice never did.
open_firewall() {
    local sn_cidr
    sn_cidr=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    if command -v ufw >/dev/null 2>&1 && [[ -n "$sn_cidr" ]]; then
        ufw allow from "$sn_cidr" to any port "$VOICE_HTTP_PORT" proto tcp >/dev/null 2>&1 || true
        log "ufw: allowed $sn_cidr -> :$VOICE_HTTP_PORT"
    fi
}

# render_unit -- write the systemd service unit.
render_unit() {
    cat > "$1" <<EOF
[Unit]
Description=Smart Node voice companion
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
EnvironmentFile=$VOICE_ENV
# The core-api address, written by the installer as a node contract. The
# service refuses to start without it; '-' means an older node that
# has no such file still boots and refuses loudly rather than silently.
EnvironmentFile=-/etc/smart-node/core-api.env
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python -m uvicorn backend.main:app --host ${VOICE_HOST} --port ${VOICE_HTTP_PORT}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$LOG_DIR $DATA_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
WantedBy=multi-user.target
EOF
}

install_service() {
    install -d -m 0755 -o root -g root /etc/systemd/system
    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record file "$SYSTEMD_PATH"
    manifest_record systemd-unit "$SYSTEMD_UNIT"

    systemctl daemon-reload

    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || log "WARN: could not enable $SYSTEMD_UNIT"

    if systemctl is-active "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "restarting $SYSTEMD_UNIT"
        systemctl restart "$SYSTEMD_UNIT" \
            || die "failed to restart $SYSTEMD_UNIT"
    else
        log "starting $SYSTEMD_UNIT"
        systemctl start "$SYSTEMD_UNIT" \
            || die "failed to start $SYSTEMD_UNIT"
    fi
}

wait_for_voice() {
    local deadline=$(( $(date +%s) + 120 )) code
    log "waiting for voice /health on 127.0.0.1:${VOICE_HTTP_PORT}"
    while (( $(date +%s) < deadline )); do
        if curl -sf "http://127.0.0.1:${VOICE_HTTP_PORT}/health" >/dev/null 2>&1; then
            log "voice API is healthy"
            return 0
        fi
        sleep 3
    done
    die "voice API did not become healthy within 120s -- check $LOG_DIR/service.log"
}

write_report_section() {
    local key_state="not set -- degraded (re-run to configure)"
    if [[ -f "$VOICE_ENV" ]] && grep -qE '^(GOOGLE|GEMINI)_API_KEY=.+' "$VOICE_ENV"; then
        key_state="configured"
    fi
    {
        echo ""
        echo "## Companion voice"
        echo ""
        echo "- Type:         native systemd service"
        echo "- Version:      voice@$STAGE_VERSION"
        echo "- Service:      $SYSTEMD_UNIT"
        echo "- Bind:         0.0.0.0:$VOICE_HTTP_PORT (all interfaces, ufw restricts to docker bridge subnets)"
        echo "- Route:        ${VOICE_PUBLIC_HOST:-line.${SMART_NODE_DOMAIN}}"
        echo "- Install dir:  $INSTALL_DIR"
        echo "- Data dir:     $DATA_DIR"
        echo "- Gemini key:   $key_state"
    } >> "$SMART_NODE_REPORT"
}

run_intent() {
    require_root
    log "companion voice -- start (v$STAGE_VERSION native)"
    log_init "$STAGE_ID"
    derive_domain
    resolve_voice_host
    resolve_mesh_url
    ensure_python
    probe_before
    ensure_dirs
    create_service_user "$SERVICE_SHORT"
    migrate_from_container
    install_snapshot
    patch_frontend
    build_venv
    ensure_data_dir
    write_env
    open_firewall
    write_traefik_route
    install_service
    wait_for_voice
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B03"
    write_report_section
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=voice@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=voice@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"voice\",\"native\":true,\"checks\":$VERIFY_TOTAL}"
    fi
    log "companion voice -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # voice binds 0.0.0.0; native callers (including verify) use 127.0.0.1.
    local vhost="127.0.0.1"

    # 1. install dir present.
    verify_check "install dir $INSTALL_DIR present" test -d "$INSTALL_DIR"
    # 2. backend present.
    verify_check "backend/main.py present" test -f "$INSTALL_DIR/backend/main.py"
    # 3. frontend present.
    verify_check "frontend/index.html present" test -f "$INSTALL_DIR/frontend/index.html"
    # 4. venv has uvicorn.
    verify_check "venv has uvicorn" test -x "$VENV_DIR/bin/uvicorn"
    # 5. .env present and 0600.
    verify_check ".env present 0600" bash -c "
        [[ -f '$VOICE_ENV' && \$(stat -c '%a' '$VOICE_ENV' 2>/dev/null) == 600 ]]"
    # 6. data dir present.
    verify_check "data dir $DATA_DIR present" test -d "$DATA_DIR"
    # 7. Traefik route present -- only when a public domain was configured.
    # Asks the writer's own question: a domain is not the same as a
    # published record, and a check that asks something slightly different from
    # the writer is how the previous writer mismatch happened.
    if voice_route_expected; then
        verify_check "Traefik route 70-voice.yml present" \
            test -f "$TRAEFIK_DYNAMIC/70-voice.yml"
    else
        verify_check "no voice route this node cannot serve" \
            bash -c "[[ ! -f \"$TRAEFIK_DYNAMIC/70-voice.yml\" ]]"
    fi
    # 8. systemd unit installed.
    verify_check "$SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # 9. service enabled.
    verify_check "$SYSTEMD_UNIT enabled" bash -c "
        systemctl is-enabled '$SYSTEMD_UNIT' >/dev/null 2>&1"
    # 10. service active.
    verify_check "$SYSTEMD_UNIT active" bash -c "
        systemctl is-active '$SYSTEMD_UNIT' >/dev/null 2>&1"
    # 11. /health answers. The unit may have been restarted a moment ago (a
    # contracts-only run restarts it and verifies straight away), and the service
    # takes a few seconds to bind, so ask for up to 30 s before calling it down.
    if [[ -n "$vhost" ]]; then
        verify_check "voice API answers /health" bash -c "
            for _i in \$(seq 1 30); do
                curl -sf 'http://${vhost}:${VOICE_HTTP_PORT}/health' >/dev/null 2>&1 && exit 0
                sleep 1
            done
            exit 1"
    fi
    # 12. companion manifest written.
    verify_check "companions.d/voice.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/voice.json"
    # 13. install-report has the voice section.
    verify_check "install-report has voice section" \
        bash -c "grep -qFx '## Companion voice' '$SMART_NODE_REPORT'"
    # 14. manifest records the systemd unit.
    verify_check "manifest records $SYSTEMD_UNIT" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$SYSTEMD_UNIT\",' \"\$m\""
}

run_verify_mode() {
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    resolve_voice_host 2>/dev/null || true
    # The route checks ask the writer's question, so this process must know the
    # writer's names: the domain and the published host(s).
    derive_domain
    derive_voice_public_host
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
    require_root
    log "companion voice -- uninstall"
    unregister_companion "$STAGE_ID"
    systemctl stop "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    systemctl disable "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    rm -f "$TRAEFIK_DYNAMIC/70-voice.yml"
    # Remove legacy Docker container if still around.
    if command -v docker >/dev/null 2>&1; then
        docker rm -f "$LEGACY_CT" >/dev/null 2>&1 || true
    fi
    rm -rf "$INSTALL_DIR"
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion voice uninstalled"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   create_service_user (OS account, one-time), ensure_dirs (directory
#   create, one-time), install_snapshot (extract application files,
#   version-gated), patch_frontend (frontend asset patch, version-gated),
#   build_venv (pip install, version-gated), ensure_data_dir (data dir
#   create, one-time), migrate_from_container (one-time data migration).
#
# CONTRACT (run): derive_domain + resolve_voice_host + resolve_mesh_url
#   (resolve dynamic env vars needed by write_env and write_traefik_route),
#   write_env (env file read by the voice service on every restart -- must
#   be current; includes VOICE_HOST, MESH_URL, domain), write_traefik_route
#   (Traefik route file -- must be current), install_service (systemd unit
#   file -- must be current), register_companion (companions.d record for
#   version tracking), stage_mark done.
run_contracts_only() {
    require_root
    log "companion voice -- contracts only (v$STAGE_VERSION)"
    log_init "$STAGE_ID"
    derive_domain
    resolve_voice_host
    resolve_mesh_url
    write_env
    write_traefik_route
    install_service
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B03"
    stage_mark "$STAGE_ID" done \
        "version=voice@$STAGE_VERSION" \
        "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
