#!/bin/bash
#
# documents.sh -- Smart Node installer, Layer C Phase 2 companion: documents.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B02 + B03.
#
# v0.3.0 changes vs v0.2.0-rc:
#   - MCP sidecar (smart-node-doci-mcp) via docker compose, streamable-http :8200
#   - Traefik dynamic route registered via register_route
#   - AI_GATEWAY_URL + AI_API_KEY passed to the app container
#   - POST /api/integrations: slug=doci, label=Documents, capabilities=[knowledge]
#     12 MCP tools inline
#   - POST /api/mcp/servers: slug=doci, transport=http (streamable-http)
#   - Source of truth: the Doci repository (not companions/doci/ monorepo)
#
# Component: Doci: a PHP app served by Apache with mod_php, plus the Python
# FastMCP sidecar. Both run NATIVELY as systemd services; neither is a Docker
# container any more.
#
# Why native. The app is about a megabyte of PHP. The image that carried it was
# 801 MB, nearly all of it the php:8.2-apache base and its build toolchain, and
# the MCP sidecar image was another 261 MB for a 555-line script. A node that
# installed from source spent minutes building both. The distribution packages
# (Apache, mod_php, php-pgsql, php-mbstring, php-curl) are a few dozen MB, the
# sidecar is a small venv on the host Python, and both are what memory and
# planner already do.
#
# Runtime layout:
#   /opt/smart-node/documents/app        the vendored Doci tree (root, read-only)
#   /opt/smart-node/documents/app/files  symlink to the document store below
#   /opt/smart-node/documents/files      git-backed document store (www-data)
#   /opt/smart-node/documents/mcp        mcp_server.py + venv (smart-node-doci-mcp)
#   /opt/smart-node/documents/httpd.conf the Apache instance's own config
#   /etc/smart-node/doci-runtime.env     runtime env for the app (0600 root)
#   /etc/smart-node/doci-mcp-runtime.env runtime env for the sidecar (0600 root)
#   /etc/smart-node/doci.env             DOCI_URL contract for native callers
#   The runtime envs carry the node domain (DOCI_DOMAIN) and secrets, so they
#   live in /etc/smart-node with the node's other settings: validate refuses a
#   domain literal in a component env file under /opt/smart-node.
#   smart-node-doci.service       Apache, as www-data, 0.0.0.0:8060 (ufw: the
#                                 smart-node docker subnet only, for Traefik)
#   smart-node-doci-mcp.service   streamable-http on 127.0.0.1:8200
#
# The Apache instance is not the distribution's apache2 service: only
# apache2-bin is installed (no Recommends), and the unit starts the binary with
# its own config file, so nothing listens on :80, where Traefik already is.
#
# UPGRADE FROM THE CONTAINERS
# A node that still runs the smart-node-doci and smart-node-doci-mcp containers
# is migrated in place: the containers are removed, then the native services
# start on the same document store, database and documents.env. The store is
# already owned by uid 33, which is www-data on the host as well, so nothing is
# copied or re-owned. The two doci images are removed once the native service
# answers. An update that would otherwise run --contracts-only runs the full
# install instead while the native service is not there yet.
#
# Usage:
#   documents.sh             full install (probe -> intent -> verify -> mark)
#   documents.sh --verify    idempotent post-check; exit 0 / N failures
#   documents.sh --status    read stage state file
#   documents.sh --uninstall reverse the install
#   documents.sh --help      this message
#
# Exits 0 on success.

set -euo pipefail

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="documents"
STAGE_VERSION="0.3.6"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The vendored Doci tree ships beside this script.
SRC_SNAPSHOT="$SCRIPT_DIR/doci"

# Containers and images of the Docker-based releases. Referenced only to
# migrate a node away from them.
LEGACY_CT="smart-node-doci"
LEGACY_MCP_CT="smart-node-doci-mcp"
LEGACY_IMAGE_REPOS="smart-node-doci smart-node-doci-mcp ghcr.io/dklymentiev/smart-node-doci ghcr.io/dklymentiev/smart-node-doci-mcp"
# The bases those images were built FROM on a node that built them itself.
LEGACY_BUILD_BASES="php:8.2-apache python:3.11-slim"

# Ports. The app binds all interfaces (ufw admits only the smart-node docker
# subnet, for Traefik); native callers use loopback. 8060 is free on the host:
# gateway 8000, voice 8001, mesh 8010, core-api 8030, Studio 8050, planner 8080.
DOCI_HTTP_PORT=8060
DOCI_MCP_PORT=8200
DOCI_LOCAL_URL="http://127.0.0.1:${DOCI_HTTP_PORT}"

# systemd units.
DOCI_UNIT="smart-node-doci.service"
DOCI_UNIT_PATH="/etc/systemd/system/$DOCI_UNIT"
DOCI_MCP_UNIT="smart-node-doci-mcp.service"
# The address the MCP server listens on: loopback, unless this node's own override for the
# unit (EnvironmentFile=-/etc/smart-node/local/<unit>.env, read after the runtime env) says
# otherwise -- a node that routes it to its VPN through Traefik sets DOCI_MCP_HOST=0.0.0.0.
# The checks below look where the unit was told to listen; without an override they stay
# strict (loopback only). Before, they demanded loopback and failed such a node's update.
DOCI_MCP_LOCAL_ENV="/etc/smart-node/local/${DOCI_MCP_UNIT%.service}.env"
# (no override file is the usual case: sed fails then, and under set -euo pipefail that
# would end the whole script without a word, as it did on every node without one)
DOCI_MCP_LISTEN=$(sed -n "s/^DOCI_MCP_HOST=[\"']\{0,1\}\([^\"']*\)[\"']\{0,1\}\$/\1/p" "$DOCI_MCP_LOCAL_ENV" 2>/dev/null | tail -1 || true)
DOCI_MCP_LISTEN=${DOCI_MCP_LISTEN:-127.0.0.1}
DOCI_MCP_UNIT_PATH="/etc/systemd/system/$DOCI_MCP_UNIT"

# Service accounts. The app runs as www-data because the document store is
# declared www-data in bootstrap/permissions.list and has been owned by uid 33
# since the container days. The sidecar gets its own account: it holds only
# the API key and talks HTTP to the app.
DOCI_USER="www-data"
DOCI_GROUP="www-data"
MCP_SHORT="doci-mcp"
MCP_USER="smart-node-doci-mcp"

# Distribution packages for the app. Version-neutral names, so Debian 12
# (PHP 8.2) and Ubuntu 24.04 (PHP 8.3) both resolve; Doci needs PHP >= 8.2.
DOCI_PACKAGES="apache2-bin libapache2-mod-php php-cli php-pgsql php-mbstring php-curl curl git"

# Database in the shared B02 cluster.
DOCI_DB="doci"
DOCI_ROLE="doci_app"

# Install tree.
INSTALL_DIR="/opt/smart-node/documents"
DOCUMENTS_ENV="$INSTALL_DIR/documents.env"
FILES_DIR="$INSTALL_DIR/files"
# Starter documents shipped with the installer, mirrored into FILES_DIR on first
# install (see seed_documents). Verticals add their own by dropping files here.
SEED_DIR="${SMART_NODE_DOCI_SEED_DIR:-$SCRIPT_DIR/doci-seed}"
ROUTE_FILE="$INSTALL_DIR/traefik-route.yml"
APP_DIR="$INSTALL_DIR/app"
MCP_DIR="$INSTALL_DIR/mcp"
MCP_VENV="$MCP_DIR/venv"
BIN_DIR="$INSTALL_DIR/bin"
HTTPD_CONF="$INSTALL_DIR/httpd.conf"
DOCI_RUNTIME_ENV="/etc/smart-node/doci-runtime.env"
MCP_RUNTIME_ENV="/etc/smart-node/doci-mcp-runtime.env"
# Where the first native cut wrote them; removed on install (see write_runtime_env).
LEGACY_RUNTIME_ENVS=("$INSTALL_DIR/doci.env" "$INSTALL_DIR/doci-mcp.env")
LOG_DIR="/var/log/smart-node/documents"
MCP_LOG_DIR="/var/log/smart-node/$MCP_SHORT"   # created by create_service_user
RUN_DIR_NAME="smart-node-doci"
DOCI_CONTRACT="/etc/smart-node/doci.env"
# Left behind by the container releases; removed on migration.
LEGACY_COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
LEGACY_EMPTY_MIGRATIONS="$INSTALL_DIR/empty-migrations"

# www-data is uid/gid 33 on Debian and Ubuntu, the same as in the old image.
WWW_DATA_UID=33
WWW_DATA_GID=33

LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

INTG_HELPER="$SCRIPT_DIR/_shared/integration.sh"
if [[ -r "$INTG_HELPER" ]]; then
    # shellcheck source=/dev/null
    source "$INTG_HELPER"
fi

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")          MODE="run"       ;;
    --verify)    MODE="verify"    ;;
    --status)    MODE="status"    ;;
    --uninstall)      MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)                die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------

load_contracts() {
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] && source "$SMART_NODE_PERSISTENCE_ENV"
    [[ -f "$SMART_NODE_ROUTING_ENV"     ]] && source "$SMART_NODE_ROUTING_ENV"
    return 0
}

psql_pg() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d postgres -v ON_ERROR_STOP=1 -X "$@"
}

psql_doci() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d "$DOCI_DB" -v ON_ERROR_STOP=1 -X "$@"
}

docker_subnet() {
    # `|| true`: both ends of this pipeline fail on a node whose smart-node
    # network is not up yet: inspect exits 1, and grep exits 1 on empty input.
    docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 / || true
}

# derive_doci_host -- hostname for the Doci service (shared agent links).
# routing.env absent is fatal. SMART_NODE_DOMAIN empty
# means a no-domain install: return "" so the caller skips the public route.
# No hostname(1) fallback, no silent empty-string concatenation.
derive_doci_host() {
    local domain="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$domain" && -f /etc/smart-node/routing.env ]]; then
        # shellcheck source=/dev/null
        source /etc/smart-node/routing.env
        domain="${SMART_NODE_DOMAIN:-}"
    fi
    if [[ -z "$domain" ]]; then
        if [[ ! -f /etc/smart-node/routing.env ]]; then
            die "routing.env absent -- run B03 first"
        fi
        # log() writes to STDOUT (lib.sh:121) and this function returns its
        # value the same way, so an unredirected log here is captured by
        # doci_host=$(derive_doci_host) and ends up as the Traefik Host rule,
        # which Traefik then rejects on every reload without failing the
        # install. Same defect as derive_panel_host, 2a6dce9.
        log "[documents] SMART_NODE_DOMAIN empty in routing.env -- public route skipped" >&2
        echo ""
        return 0
    fi
    echo "doci.$domain"
}

# check_core_api_compat -- pre-flight gate.
#
# This companion (doci, STAGE_VERSION 0.3.3) changed how it reaches core-api:
# it now uses host.docker.internal, which requires core-api to bind on
# 0.0.0.0. Running 0.3.3 against an older
# core-api fails inside the container with a confusing connection error.
#
# This function runs BEFORE the companion touches anything. If core-api is
# absent, incomplete, or too old, it fails with a clear diagnostic and exits
# non-zero, stopping the run before it starts.
#
_version_ge() {
    # Return 0 (true) if version string $1 >= $2, using sort -V semantics.
    # Both arguments must be dotted-decimal strings, e.g. "0.4.3".
    [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$2" ]]
}

check_core_api_compat() {
    local state_file="/var/lib/smart-node/stages/core-api.state"
    # core-api 0.4.3 is the first version that binds 0.0.0.0 and
    # exposes host services via host.docker.internal. Earlier versions do not
    # accept connections from inside the doci container.
    local min_version="0.4.3"

    if [[ ! -f "$state_file" ]]; then
        echo "ERROR: core-api is not installed (no state file at $state_file)." >&2
        echo "       Run the full update before running this companion alone." >&2
        exit 1
    fi

    local status version_tag version=""
    status=$(sed -nE 's/^status=//p' "$state_file" | tail -n1)
    version_tag=$(sed -nE 's/^version=//p' "$state_file" | tail -n1)
    # version_tag is "core-api@0.4.3"; strip the prefix.
    version="${version_tag#*@}"

    if [[ "$status" != "done" ]]; then
        echo "ERROR: core-api stage status is '$status' (expected 'done')." >&2
        echo "       Run the full update before running this companion alone." >&2
        exit 1
    fi

    if [[ -z "$version" ]]; then
        echo "ERROR: cannot parse core-api version from state file ($state_file)." >&2
        exit 1
    fi

    if ! _version_ge "$version" "$min_version"; then
        echo "ERROR: core-api is at version $version; this companion requires >= $min_version." >&2
        echo "       Run the full update (core-api included) before running this companion alone." >&2
        exit 1
    fi

    # Confirm the process is alive and answering.
    local health_url="http://127.0.0.1:8030/api/health"
    if ! curl -sf --max-time 3 "$health_url" >/dev/null 2>&1; then
        echo "ERROR: core-api health check failed ($health_url did not answer)." >&2
        echo "       core-api state is done@$version but the process is not running." >&2
        echo "       Restart core-api (systemctl start smart-node-core-api) and retry." >&2
        exit 1
    fi
}

# ---------- install steps ----------

# legacy_containers_present: true while either container of the Docker-based
# releases still exists on this node. Missing docker means there are none.
legacy_containers_present() {
    command -v docker >/dev/null 2>&1 || return 1
    local ct
    for ct in "$LEGACY_CT" "$LEGACY_MCP_CT"; do
        docker ps -aq -f "name=^${ct}\$" 2>/dev/null | grep -q . && return 0
    done
    return 1
}

probe_before() {
    if legacy_containers_present; then
        log "doci containers present: this run migrates them to native services"
    elif [[ -f "$DOCI_UNIT_PATH" ]]; then
        log "$DOCI_UNIT already installed: idempotent re-run"
    fi
}

ensure_dirs() {
    # 0751, not 0750: the app runs as www-data, which is not in the service
    # group, and has to traverse this directory to reach app/ and files/. The
    # execute bit lets it pass through without listing anything here; the
    # secrets beside it are 0600 root. In the container the store was a bind
    # mount and the question never came up.
    install -d -m 0751 -o root -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    chmod 0751 "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    install -d -m 0755 -o root -g root "$BIN_DIR"
    install -d -m 0750 -o "$DOCI_USER" -g "$DOCI_GROUP" "$LOG_DIR"
    # Logs written under an earlier, looser umask stay owner-writable only.
    chmod -R go-w "$LOG_DIR"
    manifest_record dir "$LOG_DIR"
}

# install_packages: the distribution packages the app needs. Skips apt when
# every package is already installed, so a re-run does not touch the network.
install_packages() {
    local missing=() p
    for p in $DOCI_PACKAGES; do
        [[ "$(dpkg-query -W -f='${Status}' "$p" 2>/dev/null || true)" == "install ok installed" ]] \
            || missing+=("$p")
    done
    if (( ${#missing[@]} == 0 )); then
        log "packages present: $DOCI_PACKAGES"
    else
        log "installing packages: ${missing[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        # --no-install-recommends keeps the apache2 package out: it would start
        # the distribution's Apache on :80, which Traefik already holds.
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
            "${missing[@]}" >/dev/null \
            || die "apt-get install failed: ${missing[*]}"
    fi
    command -v php >/dev/null 2>&1 || die "php not on PATH after install"
    php_module_path >/dev/null || die "mod_php not found under /usr/lib/apache2/modules"
    # Read the module list once: `php -m | grep -q` under pipefail fails at
    # random, when grep exits early and php dies of SIGPIPE.
    local mods ext
    mods=$(php -m 2>/dev/null || true)
    for ext in pdo_pgsql mbstring curl; do
        grep -qx "$ext" <<<"$mods" || die "PHP extension $ext missing"
    done
}

# php_module_path: the mod_php shared object, whatever PHP version provides it.
php_module_path() {
    local so
    so=$(ls /usr/lib/apache2/modules/libphp*.so 2>/dev/null | sort -V | tail -n1 || true)
    [[ -n "$so" ]] || return 1
    echo "$so"
}

# install_app: copy the vendored Doci tree into APP_DIR, root-owned and
# read-only to the service. Files the node does not serve stay out: the
# Dockerfiles, tests, CI config and the image entrypoint. files/ inside the
# tree is a symlink to the store: Doci resolves the store relative to its own
# root in several places (index.php, lib/render.php, lib/git.php).
install_app() {
    [[ -d "$SRC_SNAPSHOT" ]] || die "vendored Doci tree missing: $SRC_SNAPSHOT"
    install -d -m 0755 -o root -g root "$APP_DIR"
    rsync -a --delete \
        --exclude '/files' \
        --exclude '/.git*' \
        --exclude '/.github' \
        --exclude '/Dockerfile*' \
        --exclude '/docker-compose*' \
        --exclude '/.dockerignore' \
        --exclude '/tests' \
        --exclude '/Makefile' \
        --exclude '/VENDORED' \
        --exclude '/scripts/docker-entrypoint.sh' \
        "$SRC_SNAPSHOT/" "$APP_DIR/" \
        || die "copying the Doci tree to $APP_DIR failed"
    chown -R root:root "$APP_DIR"
    chmod -R u+rwX,go+rX,go-w "$APP_DIR"
    ln -sfn "$FILES_DIR" "$APP_DIR/files"
    # sn-version reads the build commit from here (node-versioning/sn-version).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$APP_DIR/.sn-commit"
    manifest_record dir "$APP_DIR"
    log "Doci $(tr -d '[:space:]' < "$APP_DIR/VERSION" 2>/dev/null || echo '?') installed -> $APP_DIR"
}

# install_mcp: the MCP sidecar, mcp_server.py and a venv on the host Python.
install_mcp() {
    create_service_user "$MCP_SHORT"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$MCP_DIR"
    install -m 0644 -o root -g root "$SRC_SNAPSHOT/mcp_server.py" "$MCP_DIR/mcp_server.py"
    install -m 0644 -o root -g root "$SRC_SNAPSHOT/requirements.txt" "$MCP_DIR/requirements.txt"
    if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
        log "python3-venv missing: installing"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null 2>&1 \
            || die "python3-venv install failed"
    fi
    if [[ ! -x "$MCP_VENV/bin/python" ]]; then
        log "creating venv -> $MCP_VENV"
        python3 -m venv "$MCP_VENV" || die "venv creation failed ($MCP_VENV)"
    fi
    "$MCP_VENV/bin/pip" install --quiet --disable-pip-version-check \
        -r "$MCP_DIR/requirements.txt" \
        || die "pip install of the doci-mcp requirements failed"
    "$MCP_VENV/bin/python" -c 'import mcp.server.fastmcp, httpx' \
        || die "doci-mcp venv cannot import its requirements"
    chown -R root:root "$MCP_VENV"
    manifest_record dir "$MCP_DIR"
}

ensure_files_store() {
    if [[ ! -d "$FILES_DIR" ]]; then
        log "creating git-backed document store -> $FILES_DIR"
        install -d -m 0775 "$FILES_DIR"
    fi
    if [[ ! -d "$FILES_DIR/.git" ]]; then
        log "git-init document store"
        git init -q "$FILES_DIR"
        git -C "$FILES_DIR" config user.email "doci@smart-node"
        git -C "$FILES_DIR" config user.name "Doci"
    else
        log "document store git repo present -- keeping history"
    fi
    chown -R "$WWW_DATA_UID:$WWW_DATA_GID" "$FILES_DIR"
    manifest_record dir "$FILES_DIR"
}

# seed_documents -- copy the bundled starter documents into the node's document
# store and register them with Doci so they get a guid and are browsable.
#
# Source: $SEED_DIR (companions/doci-seed/), mirrored verbatim into the store, so
# a vertical can ship additional starter docs (e.g. an example Company Card) by
# dropping files in there -- no code change.
#
# NEVER overwrites: a path that already exists is left alone. The Company Card is
# the owner's document; once the node has started filling it in, a re-run of the
# installer must not reset it.
seed_documents() {
    [[ -d "$SEED_DIR" ]] || { log "no bundled seed documents -- skip"; return 0; }

    local doci_url="$DOCI_LOCAL_URL"
    curl -sf -m 5 -o /dev/null "$doci_url/api/health.php" 2>/dev/null \
        || { log "WARN: $DOCI_UNIT not answering on $doci_url: seeding files only, no registration"; doci_url=""; }

    # FILES_DIR is owned by www-data (ensure_files_store chowns it) while the
    # installer runs as root, so plain git here trips "dubious ownership".
    local -a git_store=(git -C "$FILES_DIR" -c "safe.directory=$FILES_DIR")

    local seeded=0 src rel dst title code
    while IFS= read -r src; do
        rel="${src#"$SEED_DIR"/}"
        dst="$FILES_DIR/$rel"
        if [[ -e "$dst" ]]; then
            log "seed doc already present, keeping owner's copy -- $rel"
            continue
        fi
        install -D -m 0664 -o "$WWW_DATA_UID" -g "$WWW_DATA_GID" "$src" "$dst"
        "${git_store[@]}" add -- "$rel"
        seeded=$(( seeded + 1 ))
        log "seeded document $rel"

        # Register so the doc has a guid + is listed. Non-fatal: the file is on
        # disk either way, and Doci picks up unregistered files on its next scan.
        [[ -n "$doci_url" ]] || continue
        title=$(sed -n 's/^# //p' "$src" | head -1)
        [[ -n "$title" ]] || title="$rel"
        code=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' \
            -X POST "${doci_url}/api/register.php" \
            -H "X-API-Key: ${SMART_NODE_DOCI_API_KEY}" \
            -H "Content-Type: application/json" \
            -d "$(printf '{"path":"%s","title":"%s"}' "$rel" "$title")" 2>/dev/null) || code="000"
        if [[ "$code" == 2* ]]; then
            log "registered document $rel (HTTP $code)"
        else
            log "WARN: register $rel returned HTTP $code -- file is on disk, register later"
        fi
    done < <(find "$SEED_DIR" -type f -name '*.md' | sort)

    if (( seeded > 0 )); then
        "${git_store[@]}" -c user.email="doci@smart-node" -c user.name="Doci" \
            commit -q -m "seed: starter documents (documents@$STAGE_VERSION)" \
            || log "WARN: seed commit reported nothing to do"
        chown -R "$WWW_DATA_UID:$WWW_DATA_GID" "$FILES_DIR"
    fi
    log "seed documents: $seeded new"
}

# doci_bcrypt_hash KEY: bcrypt hash of KEY via Doci's canonical
# scripts/hash-api-key.php, run by the host PHP. Doci v0.2+ rejects SHA-256
# (src/config.php), so the stored DOCI_API_KEY_HASH MUST be bcrypt. Requires the
# app installed (install_app) and PHP present (install_packages).
doci_bcrypt_hash() {
    [[ -r "$APP_DIR/scripts/hash-api-key.php" ]] \
        || die "doci_bcrypt_hash: $APP_DIR/scripts/hash-api-key.php missing (install the app before secrets)"
    # `|| die`: this one SHOULD stop the install -- there is no usable fallback
    # for a missing hash -- but it must say so. A bare failing pipeline inside a
    # command substitution just exits the caller with no message at all.
    php "$APP_DIR/scripts/hash-api-key.php" "$1" \
        | sed -n 's/^DOCI_API_KEY_HASH=//p' \
        || die "doci_bcrypt_hash: hashing the API key failed"
}

load_or_generate_secrets() {
    if [[ -f "$DOCUMENTS_ENV" ]]; then
        log "documents.env present -- loading (generate-once)"
        # shellcheck source=/dev/null
        source "$DOCUMENTS_ENV"
        [[ -n "${SMART_NODE_DOCI_DB_PW:-}"       ]] || die "SMART_NODE_DOCI_DB_PW missing in $DOCUMENTS_ENV"
        [[ -n "${SMART_NODE_DOCI_API_KEY:-}"      ]] || die "SMART_NODE_DOCI_API_KEY missing in $DOCUMENTS_ENV"
        [[ -n "${SMART_NODE_DOCI_API_KEY_HASH:-}" ]] || die "SMART_NODE_DOCI_API_KEY_HASH missing in $DOCUMENTS_ENV"
        # Migrate a legacy sha256 hash -> bcrypt (Doci v0.2+ rejects sha256;
        # API-key auth 401s until rehashed). Bcrypt hashes start with '$2'.
        if [[ "$SMART_NODE_DOCI_API_KEY_HASH" != '$2'* ]]; then
            log "DOCI_API_KEY_HASH is legacy sha256 -- rehashing to bcrypt"
            SMART_NODE_DOCI_API_KEY_HASH=$(doci_bcrypt_hash "$SMART_NODE_DOCI_API_KEY")
            sed -i "s|^SMART_NODE_DOCI_API_KEY_HASH=.*|SMART_NODE_DOCI_API_KEY_HASH='$SMART_NODE_DOCI_API_KEY_HASH'|" "$DOCUMENTS_ENV"
        fi
        return 0
    fi
    command -v openssl >/dev/null 2>&1 || die "openssl not found"
    log "generating documents secrets -> $DOCUMENTS_ENV"
    SMART_NODE_DOCI_DB_PW=$(openssl rand -hex 24)
    SMART_NODE_DOCI_API_KEY=$(openssl rand -hex 24)
    SMART_NODE_DOCI_API_KEY_HASH=$(doci_bcrypt_hash "$SMART_NODE_DOCI_API_KEY")
    local tmp
    tmp=$(mktemp)
    {
        echo "# Smart Node documents companion secrets -- documents.sh@$STAGE_VERSION"
        echo "# 0600 root:root. Generate-once -- re-run re-reads this file."
        echo "SMART_NODE_DOCI_DB_PW=$SMART_NODE_DOCI_DB_PW"
        echo "SMART_NODE_DOCI_API_KEY=$SMART_NODE_DOCI_API_KEY"
        echo "SMART_NODE_DOCI_API_KEY_HASH='$SMART_NODE_DOCI_API_KEY_HASH'"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$DOCUMENTS_ENV"
    rm -f "$tmp"
    manifest_record file "$DOCUMENTS_ENV"
}

# read_gateway_api_key -- read the AI gateway key from the gateway companion.
# Non-fatal: Doci runs without AI features if absent.
read_gateway_api_key() {
    local gw_env="/opt/smart-node/gateway/.env"
    if [[ -r "$gw_env" ]]; then
        grep -oP '(?<=^GATEWAY_API_KEY=).+' "$gw_env" 2>/dev/null | head -1 || true
    fi
}

create_doci_db() {
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DOCI_ROLE'")" == 1 ]]; then
        psql_pg -c "ALTER ROLE \"$DOCI_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_DOCI_DB_PW';"
    else
        log "creating role $DOCI_ROLE"
        psql_pg -c "CREATE ROLE \"$DOCI_ROLE\" WITH LOGIN PASSWORD '$SMART_NODE_DOCI_DB_PW';"
    fi
    if [[ "$(psql_pg -tAc "SELECT 1 FROM pg_database WHERE datname='$DOCI_DB'")" == 1 ]]; then
        log "database $DOCI_DB already exists"
    else
        log "creating database $DOCI_DB (owner $DOCI_ROLE)"
        psql_pg -c "CREATE DATABASE \"$DOCI_DB\" OWNER \"$DOCI_ROLE\";"
    fi
    psql_doci -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
    psql_doci -c "GRANT ALL ON SCHEMA public TO \"$DOCI_ROLE\";"
    # Apply the document schema (skip if already applied). The schema SoT is
    # the versioned migrations shipped in the doci/ sidecar -- the same files
    # the container applies on first boot (docker-entrypoint.sh). We apply
    # them out-of-band here, in filename order, and mask the container's
    # migrations dir (see write_compose_file) so 001 is never re-applied on a
    # restart (CREATE OR REPLACE FUNCTION breaks on a populated DB -- caught on
    # test-node 2026-05-28). SET SESSION AUTHORIZATION is prepended into the same
    # psql stream so every object ends up owned by $DOCI_ROLE; content is fed
    # on stdin (not psql -f) because the postgres OS user may not be able to
    # read the companion's staging path (e.g. under /root, 0700).
    local migrations_dir="$SCRIPT_DIR/doci/migrations"
    if [[ "$(psql_doci -tAc \
            "SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='documents'")" == 1 ]]; then
        log "document schema already applied -- skipping"
        return 0
    fi
    [[ -d "$migrations_dir" ]] \
        || die "Doci migrations not found at $migrations_dir -- doci/ sidecar missing"
    local m applied=0
    for m in "$migrations_dir"/*.sql; do
        [[ -f "$m" ]] || continue
        log "applying migration $(basename "$m") as $DOCI_ROLE"
        { echo "SET SESSION AUTHORIZATION \"$DOCI_ROLE\";"; cat "$m"; } \
            | runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
                -d "$DOCI_DB" -v ON_ERROR_STOP=1 -X \
            || die "migration $(basename "$m") apply failed"
        applied=$((applied + 1))
    done
    (( applied > 0 )) || die "no migrations found in $migrations_dir"
}

# migrate_from_container: remove the containers of the Docker-based releases.
# The document store, the database and documents.env are what they carry over;
# the containers themselves hold no state. Images are removed later, once the
# native service answers (remove_legacy_images). Non-fatal by design: a fresh
# node has no containers, and a node without docker has none either.
migrate_from_container() {
    command -v docker >/dev/null 2>&1 || return 0
    if [[ -f "$LEGACY_COMPOSE_FILE" ]]; then
        log "stopping the doci containers ($LEGACY_COMPOSE_FILE)"
        docker compose -f "$LEGACY_COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
    fi
    local ct
    for ct in "$LEGACY_CT" "$LEGACY_MCP_CT"; do
        if docker ps -aq -f "name=^${ct}\$" 2>/dev/null | grep -q .; then
            log "removing legacy container $ct"
            docker rm -f "$ct" >/dev/null 2>&1 || true
            manifest_retire_artifact docker-container "$ct" \
                "documents runs natively; container retired" 2>/dev/null || true
        fi
    done
    if [[ -f "$LEGACY_COMPOSE_FILE" ]]; then
        rm -f "$LEGACY_COMPOSE_FILE"
        manifest_retire_artifact file "$LEGACY_COMPOSE_FILE" \
            "documents runs natively; compose file retired" 2>/dev/null || true
    fi
    rm -rf "$LEGACY_EMPTY_MIGRATIONS"
}

# remove_legacy_images: drop the doci images once nothing needs them. Called
# only after the native service has answered, so a failed migration leaves the
# images in place for a rollback. Every tag and digest ref of the two
# repositories goes. So do the base images a node pulled only to BUILD them
# (LEGACY_BUILD_BASES), unless a container still runs from one; nothing else
# on a node builds from them.
remove_legacy_images() {
    command -v docker >/dev/null 2>&1 || return 0
    legacy_containers_present && return 0
    local repo ids base used
    for repo in $LEGACY_IMAGE_REPOS; do
        ids=$(docker images -q "$repo" 2>/dev/null | sort -u || true)
        [[ -n "$ids" ]] || continue
        log "removing legacy image(s) $repo"
        # shellcheck disable=SC2086
        docker image rm -f $ids >/dev/null 2>&1 || true
    done
    for base in $LEGACY_BUILD_BASES; do
        docker image inspect "$base" >/dev/null 2>&1 || continue
        used=$(docker ps -aq --filter "ancestor=$base" 2>/dev/null || true)
        if [[ -n "$used" ]]; then
            log "keeping build base $base: a container uses it"
            continue
        fi
        log "removing build base image $base (pulled only to build doci)"
        docker image rm "$base" >/dev/null 2>&1 || true
    done
}

# write_runtime_env: the environment of the two services. Rewritten on every
# run, from documents.env (the generate-once secrets) and the node contracts.
# Values are single-quoted: systemd then takes them literally, and the bcrypt
# hash is full of dollar signs.
write_runtime_env() {
    local port doci_host ai_api_key doci_trusted sn_cidr tmp
    port="${SMART_NODE_PG_PORT:-5432}"
    doci_host=$(derive_doci_host)
    ai_api_key="$(read_gateway_api_key)"
    # Who may assert Remote-User: Traefik, from the smart-node docker subnet,
    # and Studio, a native process on this host calling 127.0.0.1. In the
    # container Studio arrived from the bridge gateway, inside that subnet.
    sn_cidr=$(docker_subnet)
    doci_trusted="127.0.0.1/32"
    [[ -n "$sn_cidr" ]] && doci_trusted="${sn_cidr},127.0.0.1/32"
    # on a no-domain install doci_host is "". Doci requires a non-empty
    # DOCI_DOMAIN, so fall back to "localhost". Git commit emails become
    # author@localhost. No public route is written on no-domain nodes
    # (derive_doci_host + public_route_eligible already guard that path).
    [[ -z "$doci_host" ]] && \
        log "[documents] no public domain: DOCI_DOMAIN will be 'localhost'"

    tmp=$(mktemp)
    {
        echo "# Smart Node documents companion: generated by documents.sh@${STAGE_VERSION}."
        echo "# 0600 root:root, read by systemd for $DOCI_UNIT. Do not edit by hand."
        echo "DB_HOST='127.0.0.1'"
        echo "DB_PORT='${port}'"
        echo "DB_NAME='${DOCI_DB}'"
        echo "DB_USER='${DOCI_ROLE}'"
        echo "DB_PASS='${SMART_NODE_DOCI_DB_PW}'"
        echo "DOCI_API_KEY_HASH='${SMART_NODE_DOCI_API_KEY_HASH}'"
        echo "DOCI_DOMAIN='${doci_host:-localhost}'"
        echo "DOCI_ENV='production'"
        echo "DOCI_DEBUG='false'"
        echo "DOCI_DEV_AUTO_AUTH='false'"
        echo "DOCI_TRUSTED_PROXIES='${doci_trusted}'"
        echo "DOCI_LOG_FILE='${LOG_DIR}/app.log'"
        echo "FILES_PATH='${FILES_DIR}'"
        echo "AI_GATEWAY_URL='http://127.0.0.1:8000/api/v1'"
        echo "AI_API_KEY='${ai_api_key}'"
        echo "TZ='$(host_zone)'"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$DOCI_RUNTIME_ENV"
    rm -f "${LEGACY_RUNTIME_ENVS[@]}"
    rm -f "$tmp"
    manifest_record file "$DOCI_RUNTIME_ENV"

    tmp=$(mktemp)
    {
        echo "# Smart Node documents MCP sidecar: generated by documents.sh@${STAGE_VERSION}."
        echo "# 0600 root:root, read by systemd for $DOCI_MCP_UNIT. Do not edit by hand."
        echo "DOCI_API_URL='${DOCI_LOCAL_URL}/api'"
        echo "DOCI_API_KEY='${SMART_NODE_DOCI_API_KEY}'"
        echo "DOCI_MCP_HOST='127.0.0.1'"
        echo "DOCI_MCP_PORT='${DOCI_MCP_PORT}'"
    } > "$tmp"
    install -m 0600 -o root -g root "$tmp" "$MCP_RUNTIME_ENV"
    rm -f "$tmp"
    manifest_record file "$MCP_RUNTIME_ENV"
}

# write_httpd_conf: the Apache instance's own configuration. It loads only
# the modules Doci uses and serves APP_DIR, honouring Doci's .htaccess (rewrite
# to index.php, the security headers, the 404 on .git and files/.data).
write_httpd_conf() {
    local mods=/usr/lib/apache2/modules php_so tmp
    php_so=$(php_module_path) || die "mod_php not found under $mods"
    tmp=$(mktemp)
    cat > "$tmp" <<HTTPD
# Smart Node documents companion: Apache for Doci.
# Generated by documents.sh@${STAGE_VERSION}. Do not edit by hand.
# Started by ${DOCI_UNIT} as ${DOCI_USER}; not the distribution's apache2 service.
ServerRoot "${INSTALL_DIR}"
ServerName localhost
Listen 0.0.0.0:${DOCI_HTTP_PORT}
PidFile /run/${RUN_DIR_NAME}/httpd.pid
DefaultRuntimeDir /run/${RUN_DIR_NAME}
Mutex file:/run/${RUN_DIR_NAME} default

LoadModule mpm_prefork_module ${mods}/mod_mpm_prefork.so
LoadModule authz_core_module  ${mods}/mod_authz_core.so
LoadModule alias_module       ${mods}/mod_alias.so
LoadModule dir_module         ${mods}/mod_dir.so
LoadModule mime_module        ${mods}/mod_mime.so
LoadModule rewrite_module     ${mods}/mod_rewrite.so
LoadModule headers_module     ${mods}/mod_headers.so
LoadModule php_module         ${php_so}

StartServers            2
MinSpareServers         2
MaxSpareServers         4
MaxRequestWorkers       16
MaxConnectionsPerChild  1000
Timeout                 120
KeepAlive               On
ServerTokens            Prod
ServerSignature         Off
TraceEnable             Off

TypesConfig /etc/mime.types
ErrorLog ${LOG_DIR}/apache-error.log
LogLevel warn
LogFormat "%a %l %u %t \"%r\" %>s %b %D" doci
CustomLog ${LOG_DIR}/apache-access.log doci

DocumentRoot "${APP_DIR}"
DirectoryIndex index.php index.html

<Directory />
    Options None
    AllowOverride None
    Require all denied
</Directory>

<Directory "${APP_DIR}">
    Options -Indexes +FollowSymLinks
    AllowOverride All
    Require all granted
</Directory>

# Maintenance scripts and schema are for the installer, not for HTTP.
<DirectoryMatch "^${APP_DIR}/(scripts|migrations)(/|\$)">
    Require all denied
</DirectoryMatch>

<FilesMatch "^\.ht">
    Require all denied
</FilesMatch>

<FilesMatch "\.php\$">
    SetHandler application/x-httpd-php
</FilesMatch>
HTTPD
    install -m 0644 -o root -g root "$tmp" "$HTTPD_CONF"
    rm -f "$tmp"
    manifest_record file "$HTTPD_CONF"
    # The config test insists that DefaultRuntimeDir exists. systemd creates it
    # (RuntimeDirectory=) when the unit starts, and removes it when it stops;
    # create it here so the test can run before the first start.
    install -d -m 0750 -o "$DOCI_USER" -g "$DOCI_GROUP" "/run/${RUN_DIR_NAME}"
    /usr/sbin/apache2 -t -f "$HTTPD_CONF" >/dev/null 2>&1 \
        || die "Apache rejects $HTTPD_CONF: $(/usr/sbin/apache2 -t -f "$HTTPD_CONF" 2>&1 | tail -n 3)"
}

# write_service_scripts: what the image entrypoint did, as two small scripts
# the unit runs: the pre-start makes sure the store is a git repository and
# commits anything that arrived while the service was down; the post-start
# indexes the store and normalises links, as the container did on every start.
# Schema migrations are NOT here: create_doci_db applies them, once.
write_service_scripts() {
    local tmp
    tmp=$(mktemp)
    cat > "$tmp" <<'PRESTART'
#!/bin/sh
# Smart Node documents companion: pre-start for smart-node-doci.service.
# Generated by documents.sh. Runs as the service user, before Apache starts.
set -eu
: "${DOCI_DOMAIN:?DOCI_DOMAIN is required}"
: "${FILES_PATH:?FILES_PATH is required}"
cd "$FILES_PATH"
if [ ! -d .git ]; then
    echo "[doci-prestart] initialising git repo at $FILES_PATH"
    git init -q
    git config user.name "Doci"
    git config user.email "doci@$DOCI_DOMAIN"
fi
# Pick up content added while the service was down, so History reflects it.
if ! git diff --quiet HEAD 2>/dev/null \
        || [ -n "$(git ls-files --others --exclude-standard 2>/dev/null)" ]; then
    git add -A 2>/dev/null || true
    git -c user.name=Doci -c user.email="doci@$DOCI_DOMAIN" \
        commit -q -m "Sync content on service start" 2>/dev/null || true
fi
PRESTART
    install -m 0755 -o root -g root "$tmp" "$BIN_DIR/doci-prestart.sh"

    cat > "$tmp" <<POSTSTART
#!/bin/sh
# Smart Node documents companion: post-start for smart-node-doci.service.
# Generated by documents.sh. Non-fatal: the service is already serving.
APP_DIR="${APP_DIR}"
POSTSTART
    cat >> "$tmp" <<'POSTSTART'
php "$APP_DIR/scripts/index-documents.php" 2>&1 | tail -n 20 || true
php "$APP_DIR/scripts/normalize-links.php" 2>&1 | tail -n 10 || true
cd "$FILES_PATH" || exit 0
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A 2>/dev/null
    git -c user.name=Doci -c user.email="doci@$DOCI_DOMAIN" \
        commit -q -m "Normalise internal links to GUID form" 2>/dev/null || true
fi
exit 0
POSTSTART
    install -m 0755 -o root -g root "$tmp" "$BIN_DIR/doci-poststart.sh"
    rm -f "$tmp"
    manifest_record file "$BIN_DIR/doci-prestart.sh"
    manifest_record file "$BIN_DIR/doci-poststart.sh"
}

# install_units: write both units and enable them. Starting is the caller's.
install_units() {
    local pg_cluster pg_unit tmp
    pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    [[ -n "$pg_cluster" ]] || die "persistence contract incomplete: run B02 first"
    pg_unit="postgresql@${pg_cluster/\//-}.service"

    tmp=$(mktemp)
    cat > "$tmp" <<UNIT
[Unit]
Description=Smart Node documents companion (Doci on Apache, 0.0.0.0:${DOCI_HTTP_PORT})
After=network-online.target ${pg_unit}
Wants=network-online.target
Requires=${pg_unit}

[Service]
Type=simple
User=${DOCI_USER}
Group=${DOCI_GROUP}
EnvironmentFile=${DOCI_RUNTIME_ENV}
Environment=HOME=/run/${RUN_DIR_NAME}
RuntimeDirectory=${RUN_DIR_NAME}
RuntimeDirectoryMode=0750
# 0022, as Apache had in the image: the log files must not be group-writable
# (bootstrap/permissions.sh refuses that outside the declared paths).
UMask=0022
ExecStartPre=${BIN_DIR}/doci-prestart.sh
ExecStart=/usr/sbin/apache2 -f ${HTTPD_CONF} -DFOREGROUND
ExecStartPost=-${BIN_DIR}/doci-poststart.sh
# Apache stops gracefully on SIGWINCH; SIGTERM would cut requests in flight.
KillSignal=SIGWINCH
KillMode=mixed
TimeoutStartSec=300
TimeoutStopSec=30
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
StandardOutput=append:${LOG_DIR}/service.log
StandardError=append:${LOG_DIR}/service.log

[Install]
# Also wanted by the database: Requires= stops this unit when the cluster is
# stopped, and nothing would start it again when the cluster comes back.
WantedBy=multi-user.target ${pg_unit}
UNIT
    install -m 0644 -o root -g root "$tmp" "$DOCI_UNIT_PATH"
    manifest_record file "$DOCI_UNIT_PATH"

    cat > "$tmp" <<UNIT
[Unit]
Description=Smart Node documents MCP sidecar (streamable-http on 127.0.0.1:${DOCI_MCP_PORT})
After=${DOCI_UNIT}
Wants=${DOCI_UNIT}

[Service]
Type=simple
User=${MCP_USER}
Group=${SMART_NODE_GROUP}
EnvironmentFile=${MCP_RUNTIME_ENV}
WorkingDirectory=${MCP_DIR}
ExecStart=${MCP_VENV}/bin/python ${MCP_DIR}/mcp_server.py --transport http
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${MCP_LOG_DIR}
StandardOutput=append:${MCP_LOG_DIR}/mcp.log
StandardError=append:${MCP_LOG_DIR}/mcp.log

[Install]
WantedBy=multi-user.target
UNIT
    install -m 0644 -o root -g root "$tmp" "$DOCI_MCP_UNIT_PATH"
    rm -f "$tmp"
    manifest_record file "$DOCI_MCP_UNIT_PATH"

    systemctl daemon-reload
    systemctl enable "$DOCI_UNIT" "$DOCI_MCP_UNIT" >/dev/null 2>&1 \
        || die "systemctl enable $DOCI_UNIT $DOCI_MCP_UNIT failed"
    manifest_record systemd-unit "$DOCI_UNIT"
    manifest_record systemd-unit "$DOCI_MCP_UNIT"
}

# runtime_fingerprint: one hash over everything the running services read at
# start. contracts-only compares it before and after rewriting, and restarts
# only when something actually changed.
runtime_fingerprint() {
    cat "$DOCI_UNIT_PATH" "$DOCI_MCP_UNIT_PATH" "$DOCI_RUNTIME_ENV" "$MCP_RUNTIME_ENV" \
        "$HTTPD_CONF" "$BIN_DIR/doci-prestart.sh" "$BIN_DIR/doci-poststart.sh" \
        2>/dev/null | sha256sum | cut -d' ' -f1
}

# start_doci [restart]: start the app, or restart it when asked.
start_doci() {
    if [[ "${1:-}" == restart ]] || ! systemctl is-active --quiet "$DOCI_UNIT"; then
        log "starting $DOCI_UNIT"
        systemctl restart "$DOCI_UNIT" \
            || die "$DOCI_UNIT failed to start: $(tail -n 5 "$LOG_DIR/apache-error.log" 2>/dev/null)"
    fi
}

# start_mcp [restart]: start the sidecar, after the app answers.
start_mcp() {
    if [[ "${1:-}" == restart ]] || ! systemctl is-active --quiet "$DOCI_MCP_UNIT"; then
        log "starting $DOCI_MCP_UNIT"
        systemctl restart "$DOCI_MCP_UNIT" || die "$DOCI_MCP_UNIT failed to start"
    fi
}

# open_firewall: Traefik (a container) reaches the app via
# host.docker.internal; ufw default-deny needs the smart-node subnet admitted
# to the app port, and nothing wider. The sidecar binds loopback and needs no
# rule.
open_firewall() {
    local sn_cidr
    sn_cidr=$(docker_subnet)
    if command -v ufw >/dev/null 2>&1 && [[ -n "$sn_cidr" ]]; then
        ufw allow from "$sn_cidr" to any port "$DOCI_HTTP_PORT" proto tcp >/dev/null 2>&1 || true
        manifest_record ufw-rule "from $sn_cidr to any port $DOCI_HTTP_PORT proto tcp"
        log "ufw: allowed $sn_cidr -> :$DOCI_HTTP_PORT"
    else
        log "WARN: no ufw or no smart-node subnet: Traefik may not reach :$DOCI_HTTP_PORT"
    fi
}

# write_doci_contract: /etc/smart-node/doci.env, the address native callers
# use (Studio, core-api, brin). It replaces reading a container IP at start.
write_doci_contract() {
    local tmp
    install -d -m 0755 -o root -g root /etc/smart-node
    tmp=$(mktemp)
    {
        echo "# Smart Node documents (Doci) service contract: generated by documents.sh@$STAGE_VERSION."
        echo "# The address native callers use. Read by Studio via EnvironmentFile=-."
        echo "DOCI_URL=${DOCI_LOCAL_URL}"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$DOCI_CONTRACT"
    rm -f "$tmp"
    manifest_record file "$DOCI_CONTRACT"
}

# write_traefik_route -- Traefik file-provider fragment.
# Routes doci.<SMART_NODE_DOMAIN> -> Doci container (public route).
# When SMART_NODE_INTERNAL_DOMAIN is set in routing.env, also adds a router
# for doci.<SMART_NODE_INTERNAL_DOMAIN> for internal/VPN access.
# all host derivation reads from routing.env; no hostname(1) calls.
write_traefik_route() {
    local doci_host
    doci_host=$(derive_doci_host)
    if [[ -z "$doci_host" ]]; then
        # Skip means RETIRE, not walk past. See the same guard in
        # studio.sh and voice.sh: returning without touching the file leaves
        # whatever an earlier release wrote sitting on disk, and Traefik keeps
        # serving it. On our node this path is what stranded a 30-core-api.yml
        # whose Host rule was a captured log line -- written by a bug, then
        # preserved by this guard on every subsequent run.
        local stale="$SMART_NODE_TRAEFIK_DYNAMIC_DIR/80-documents.yml"
        if [[ -f "$stale" ]]; then
            rm -f "$stale"
            manifest_retire_artifact file "$stale" \
                "no public domain -- documents route retired"
            log "[documents] no public domain -- Traefik route retired ($stale)"
        else
            log "[documents] no public domain -- no Traefik route to write"
        fi
        return 0
    fi

    # A derived name with no DNS record is not a door. doci.<domain> is
    # invented by this companion, not asked for by the operator, so it is the
    # exact shape the gate exists for.
    if ! public_route_eligible "$doci_host"; then
        retire_route "documents" "$SMART_NODE_TRAEFIK_DYNAMIC_DIR/80-documents.yml" \
            "$PUBLIC_ROUTE_SKIP_REASON"
        return 0
    fi

    # Internal/VPN route: only when SMART_NODE_INTERNAL_DOMAIN is defined.
    # read from routing.env; hostname(1) is not used.
    local internal_domain="${SMART_NODE_INTERNAL_DOMAIN:-}"
    if [[ -z "$internal_domain" && -f /etc/smart-node/routing.env ]]; then
        # shellcheck source=/dev/null
        source /etc/smart-node/routing.env
        internal_domain="${SMART_NODE_INTERNAL_DOMAIN:-}"
    fi
    local internal_router=""
    if [[ -n "$internal_domain" ]]; then
        local sn_internal_host="doci.${internal_domain}"
        internal_router="    smart-node-doci:
      rule: \"Host(\`${sn_internal_host}\`)\"
      entryPoints: [\"websecure\"]
      service: \"smart-node-doci\"
      middlewares: [\"smart-node-auth@docker\"]
      tls: {}
"
    fi

    local tmp
    tmp=$(mktemp)
    cat > "$tmp" << ROUTE
# Smart Node Documents companion -- Traefik route.
# Generated by documents.sh@${STAGE_VERSION}. Do not edit by hand.
# Shared agent links: $(web_scheme)://${doci_host}/docs/<slug>
http:
  routers:
${internal_router}    smart-node-doci-sf:
      rule: "Host(\`${doci_host}\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-doci"
      middlewares: ["smart-node-auth@docker"]
      $(web_tls_yaml)

  services:
    smart-node-doci:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:${DOCI_HTTP_PORT}"
        passHostHeader: true
ROUTE
    # register_route copies the file into SMART_NODE_TRAEFIK_DYNAMIC_DIR
    # and names it 80-documents.yml -- Traefik picks it up live.
    cp "$tmp" "$ROUTE_FILE"
    # register_route rejects numeric-prefixed names; the established route
    # convention (NN-name.yml, e.g. 60-voice/70-Studio) is a direct file install.
    install -m 0640 -g "$SMART_NODE_GROUP" "$ROUTE_FILE" "$SMART_NODE_TRAEFIK_DYNAMIC_DIR/80-documents.yml"
    manifest_record file "$SMART_NODE_TRAEFIK_DYNAMIC_DIR/80-documents.yml"
    rm -f "$tmp"
    local route_desc="${doci_host}"
    [[ -n "$internal_domain" ]] && route_desc="${route_desc} + doci.${internal_domain}"
    log "Traefik route registered: ${route_desc} -> host.docker.internal:${DOCI_HTTP_PORT}"
}

wait_for_doci() {
    local deadline
    deadline=$(( $(date +%s) + 90 ))
    log "waiting for $DOCI_UNIT to answer on $DOCI_LOCAL_URL (up to 90s)"
    while (( $(date +%s) < deadline )); do
        if curl -sf -m 3 "$DOCI_LOCAL_URL/api/health.php" 2>/dev/null | grep -q '"status"'; then
            log "$DOCI_UNIT is answering"
            return 0
        fi
        sleep 2
    done
    die "$DOCI_UNIT did not answer within 90s (see $LOG_DIR/apache-error.log)"
}

wait_for_mcp() {
    local deadline
    deadline=$(( $(date +%s) + 60 ))
    while (( $(date +%s) < deadline )); do
        if ss -ltn 2>/dev/null | grep -qF "${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT} "; then
            log "$DOCI_MCP_UNIT is listening on ${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT}"
            return 0
        fi
        sleep 2
    done
    die "$DOCI_MCP_UNIT is not listening on ${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT} after 60s (see $MCP_LOG_DIR/mcp.log)"
}

register_integration() {
    declare -F sn_integration_register >/dev/null || return 0
    local doci_host_ri install_uri
    doci_host_ri=$(derive_doci_host)
    if [[ -z "$doci_host_ri" ]]; then
        log "[documents] no public domain -- integration registration skipped"
        return 0
    fi
    install_uri="$(web_scheme)://${doci_host_ri}"
    local tools_tmp
    tools_tmp=$(mktemp)
    cat > "$tools_tmp" << 'TOOLS'
[
  {"name":"doci","kind":"mcp","title":"Documents","description":"The node's document store: written things that someone will open again.",
   "usage_md":"## doci -- this node's documents\n\nA document is something a person or a colleague will open and read: a specification, a report, a record, an agreed procedure. Memory is for recall; documents are for reading. If nobody would ever open it on purpose, it is a memory, not a document.\n\nContent lives in a git tree, metadata in an index. A document's **GUID is its identity** and never changes; its path can. Link by GUID, not by path, or the link breaks the first time something is filed better.\n\n**`doci_search` looks up ONE document by its exact path. It is not a content search** -- the name is misleading and has already cost time. Give it `notes/first-check.md`, not `first-check` and not a word you hope appears inside. To read a document you know, use `doci_get` with the GUID or the path. Paths end in `.md`; it is appended if you forget.\n\n**Update, do not recreate.** Versions are kept, so `doci_update` preserves the history and the GUID. Creating a second document at a new path loses both and leaves two truths.\n\n**Threads belong on the document they are about** (`doci_thread` to open one, `doci_threads` to list, `doci_reply` to continue). A decision argued in a thread and never folded back into the document is a decision nobody will find.\n\n**`doci_inbox` is for capture** -- something worth keeping that is not yet a document. Do not let the inbox become the filing system. `doci_inbox_list` shows what is waiting in it.\n\n**To find a document you cannot name, list the folder it should be in** with `doci_list_folder` (every document under a path prefix). `doci_create` makes a new document, `doci_versions` shows the history of one, `doci_delete` removes one, and `doci_health` tells you whether the service answers.\n\nWriting it is not proof. Read it back before you report it done."},
  {"name":"doci_create",     "kind":"mcp","title":"Create document",  "description":"Create a new markdown document in the store."},
  {"name":"doci_get",        "kind":"mcp","title":"Get document",     "description":"Retrieve document metadata and content by GUID or path."},
  {"name":"doci_update",     "kind":"mcp","title":"Update document",  "description":"Modify document content, title, summary or tags."},
  {"name":"doci_delete",     "kind":"mcp","title":"Delete document",  "description":"Remove a document and cascade-delete related items."},
  {"name":"doci_search",     "kind":"mcp","title":"Search documents", "description":"Find documents by path or keyword."},
  {"name":"doci_thread",     "kind":"mcp","title":"Create thread",    "description":"Open a discussion thread anchored to a document passage."},
  {"name":"doci_threads",    "kind":"mcp","title":"List threads",     "description":"List all discussion threads for a document."},
  {"name":"doci_reply",      "kind":"mcp","title":"Reply to thread",  "description":"Add a reply to an existing discussion thread."},
  {"name":"doci_inbox",      "kind":"mcp","title":"Inbox capture",    "description":"Quick-capture a note to the inbox."},
  {"name":"doci_inbox_list", "kind":"mcp","title":"List inbox",       "description":"Retrieve recent inbox items."},
  {"name":"doci_versions",   "kind":"mcp","title":"List versions",    "description":"List all versions of a document."},
  {"name":"doci_health",     "kind":"mcp","title":"Health check",     "description":"Check Doci system health and database connectivity."}
]
TOOLS
    sn_integration_register \
        --slug           doci \
        --label          "Documents" \
        --purpose        "Documents" \
        --kind           app \
        --status         available \
        --install-uri    "$install_uri" \
        --install-path   "$INSTALL_DIR" \
        --description    "Git-backed markdown document store with AI discussion threads and versioning." \
        --companion-slug "$STAGE_ID" \
        --capabilities   '["knowledge"]' \
        --tools-json     "$tools_tmp" || true
    rm -f "$tools_tmp"
}

register_mcp_server() {
    declare -F sn_mcp_server_register >/dev/null || return 0
    # Register under slug "doci" (the slug agents are granted) with the loopback
    # URL, overriding any central baseline doci record. Container-name / FQDN
    # URLs are unreachable for node-host agents and Host-rejected by FastMCP.
    # streamable-http, not the older SSE transport. SSE splits the stream and the
    # calls across two connections, and a current client sends its first call
    # before the server has finished initialising: the server logs "Received
    # request before initialization was complete" and the agent sees
    # "MCP error -32602: Invalid request parameters" on EVERY call. Every doci
    # tool was unusable on a node that otherwise reported itself healthy, and
    # nothing in the install said so.
    local mcp_url="http://localhost:${DOCI_MCP_PORT}/mcp"
    sn_mcp_server_register \
        --slug      "doci" \
        --name      "Doci" \
        --transport http \
        --url       "$mcp_url" || true
}

write_report_section() {
    local doci_host
    doci_host=$(derive_doci_host)
    # on no-domain installs doci_host is empty.
    # Report the fact rather than emitting a broken URL.
    local route_line shared_links_line
    if [[ -n "$doci_host" ]]; then
        route_line="route:       $(web_scheme)://${doci_host}/docs/<slug>"
        shared_links_line="Agent shared links: $(web_scheme)://${doci_host}/docs/<slug>"
    else
        route_line="route:       n/a -- no public domain"
        shared_links_line="Agent shared links: n/a -- no public domain configured"
    fi
    local body
    body=$(cat << EOF
**Version:** documents@${STAGE_VERSION}
**Marked done:** $(date -Iseconds)

\`\`\`
component:   Doci $(tr -d '[:space:]' < "$APP_DIR/VERSION" 2>/dev/null || echo '?') at ${APP_DIR} (native, Apache + mod_php)
service:     ${DOCI_UNIT}  (${DOCI_USER}, 0.0.0.0:${DOCI_HTTP_PORT}, ufw: smart-node subnet only)
mcp-sidecar: ${DOCI_MCP_UNIT}  (${MCP_USER}, streamable-http on 127.0.0.1:${DOCI_MCP_PORT})
contract:    ${DOCI_CONTRACT}  (DOCI_URL=${DOCI_LOCAL_URL})
database:    ${DOCI_DB} + role ${DOCI_ROLE} in B02 cluster, over 127.0.0.1
store:       ${FILES_DIR}  (git-backed, www-data-owned)
secrets:     ${DOCUMENTS_ENV}  (0600 root:root, generate-once)
logs:        ${LOG_DIR}, ${MCP_LOG_DIR}
${route_line}
integration: slug=doci, label=Documents, capabilities=[knowledge], 12 tools
mcp-server:  slug=doci, transport=http (streamable-http)
\`\`\`

The node document store -- git-backed Markdown with versioning and AI threads.
${shared_links_line}
EOF
)
    report_section "Companion documents" "$body"
}

# ---------- verify checks ----------

# _doci_api_answers CODE KEY: true when an API read with KEY (none if empty)
# answers CODE. The key goes to curl on stdin, never on its command line.
_doci_api_answers() {
    local want="$1" key="$2" got
    got=$(printf 'X-API-Key: %s\n' "$key" \
        | curl -s -m 5 -o /dev/null -w '%{http_code}' ${key:+-H @-} \
            "$DOCI_LOCAL_URL/api/list.php?recent=1" 2>/dev/null || true)
    [[ "$got" == "$want" ]]
}

run_verify_checks() {
    local pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    local traefik_dir="${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-/etc/smart-node/traefik/dynamic}"

    verify_check "stage B02 done"   require_stage B02
    verify_check "stage B03 done"   require_stage B03

    verify_check "documents.env present 0600" bash -c \
        "[[ -f '$DOCUMENTS_ENV' && \$(stat -c '%a' '$DOCUMENTS_ENV' 2>/dev/null) == 600 ]]"

    verify_check "document store git repo present" test -d "$FILES_DIR/.git"

    verify_check "database $DOCI_DB exists" bash -c \
        "[[ \$(runuser -u postgres -- psql --cluster '$pg_cluster' \
            -d postgres -tAc \
            \"SELECT 1 FROM pg_database WHERE datname='$DOCI_DB'\" 2>/dev/null) == 1 ]]"

    verify_check "role $DOCI_ROLE exists" bash -c \
        "[[ \$(runuser -u postgres -- psql --cluster '$pg_cluster' \
            -d postgres -tAc \
            \"SELECT 1 FROM pg_roles WHERE rolname='$DOCI_ROLE'\" 2>/dev/null) == 1 ]]"

    verify_check "documents table present in $DOCI_DB" bash -c \
        "[[ \$(runuser -u postgres -- psql --cluster '$pg_cluster' \
            -d '$DOCI_DB' -tAc \
            \"SELECT 1 FROM information_schema.tables WHERE table_name='documents'\" \
            2>/dev/null) == 1 ]]"

    verify_check "app installed at $APP_DIR" test -r "$APP_DIR/index.php"

    verify_check "app VERSION matches the vendored tree" bash -c \
        "[[ ! -r '$SRC_SNAPSHOT/VERSION' ]] || [[ \"\$(tr -d '[:space:]' < '$APP_DIR/VERSION' 2>/dev/null)\" == \"\$(tr -d '[:space:]' < '$SRC_SNAPSHOT/VERSION')\" ]]"

    verify_check "app files/ points at the document store" bash -c \
        "[[ \"\$(readlink '$APP_DIR/files')\" == '$FILES_DIR' ]]"

    verify_check "app tree not writable by the service" bash -c \
        "! runuser -u '$DOCI_USER' -- test -w '$APP_DIR/index.php'"

    verify_check "document store writable by the service" \
        runuser -u "$DOCI_USER" -- test -w "$FILES_DIR"

    verify_check "runtime env files present 0600" bash -c \
        "[[ \$(stat -c '%a' '$DOCI_RUNTIME_ENV' 2>/dev/null) == 600 && \$(stat -c '%a' '$MCP_RUNTIME_ENV' 2>/dev/null) == 600 ]]"

    verify_check "Apache config valid" /usr/sbin/apache2 -t -f "$HTTPD_CONF"

    verify_check "$DOCI_UNIT active"      systemctl is-active  --quiet "$DOCI_UNIT"
    verify_check "$DOCI_UNIT enabled"     systemctl is-enabled --quiet "$DOCI_UNIT"
    verify_check "$DOCI_MCP_UNIT active"  systemctl is-active  --quiet "$DOCI_MCP_UNIT"
    verify_check "$DOCI_MCP_UNIT enabled" systemctl is-enabled --quiet "$DOCI_MCP_UNIT"

    verify_check "$DOCI_UNIT runs as $DOCI_USER" bash -c \
        "[[ \$(systemctl show -p User --value '$DOCI_UNIT') == '$DOCI_USER' ]]"

    verify_check "Doci health endpoint reports ok" bash -c \
        "curl -sf -m 5 '$DOCI_LOCAL_URL/api/health.php' 2>/dev/null | grep -q '\"status\"'"

    verify_check "Doci API accepts the node API key" \
        _doci_api_answers 200 "${SMART_NODE_DOCI_API_KEY:-}"

    verify_check "Doci API refuses a request without a key" \
        _doci_api_answers 401 ""

    verify_check "the distribution's Apache is not running" bash -c \
        "! systemctl is-active --quiet apache2 2>/dev/null"

    if [[ "$DOCI_MCP_LISTEN" == 127.0.0.1 ]]; then
        verify_check "$DOCI_MCP_UNIT listens on loopback port $DOCI_MCP_PORT only" bash -c \
            "ss -ltn 2>/dev/null | grep -q '127.0.0.1:${DOCI_MCP_PORT} ' && ! ss -ltn 2>/dev/null | grep -qE '(0\.0\.0\.0|\*|\[::\]):${DOCI_MCP_PORT} '"
    else
        log "note: $DOCI_MCP_UNIT listens on ${DOCI_MCP_LISTEN} by this node's override ($DOCI_MCP_LOCAL_ENV)"
        verify_check "$DOCI_MCP_UNIT listens where this node's override says (${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT})" bash -c \
            "ss -ltn 2>/dev/null | grep -qF '${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT} '"
    fi

    verify_check "ufw admits the smart-node subnet to :$DOCI_HTTP_PORT" bash -c \
        "! command -v ufw >/dev/null 2>&1 || iptables -S ufw-user-input 2>/dev/null | grep -q 'dport $DOCI_HTTP_PORT'"

    verify_check "DOCI_URL contract written" bash -c \
        "grep -qx 'DOCI_URL=$DOCI_LOCAL_URL' '$DOCI_CONTRACT'"

    verify_check "no doci container left" bash -c \
        "! command -v docker >/dev/null 2>&1 || ! docker ps -aq -f 'name=^${LEGACY_CT}\$' -f 'name=^${LEGACY_MCP_CT}\$' 2>/dev/null | grep -q ."

    # Public route is only written when SMART_NODE_DOMAIN is set
    # AND the name it would claim resolves. The check asks the same
    # function the writer asked, so the two cannot drift apart.
    local _doci_host; _doci_host=$(derive_doci_host 2>/dev/null || true)
    if [[ -n "${SMART_NODE_DOMAIN:-}" ]] && public_route_eligible "$_doci_host"; then
        verify_check "Traefik route file present" \
            test -f "${traefik_dir}/80-documents.yml"
    else
        verify_check "no documents route this node cannot serve" \
            bash -c "[[ ! -f \"${traefik_dir}/80-documents.yml\" ]]"
    fi

    verify_check "Company Card seeded into the document store" \
        test -f "$FILES_DIR/templates/company-card.md"

    verify_check "companions.d/documents.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/documents.json"

    verify_check "install-report has documents section" bash -c \
        "grep -qFx '## Companion documents' '$SMART_NODE_REPORT'"

    verify_check "manifest records $DOCI_UNIT" bash -c \
        "m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] && \
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$DOCI_UNIT\",' \"\$m\""
}

# ---------- modes ----------

run_intent() {
    require_root
    check_core_api_compat
    log "companion documents v$STAGE_VERSION -- start"
    local t0=$SECONDS
    require_stage B02
    require_stage B03
    log_init "$STAGE_ID"
    load_contracts

    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- run B02 first"
    [[ -n "${SMART_NODE_DOCKER_NETWORK:-}" ]] \
        || die "routing contract incomplete -- run B03 first"

    probe_before
    ensure_dirs
    install_packages
    command -v git >/dev/null 2>&1 || die "git not available"
    ensure_files_store
    install_app
    install_mcp
    load_or_generate_secrets
    create_doci_db
    write_runtime_env
    write_httpd_conf
    write_service_scripts
    # Everything slow is done; only now take the containers down, so the
    # documents are unavailable for seconds rather than for the whole run.
    migrate_from_container
    install_units
    open_firewall
    start_doci restart
    wait_for_doci
    start_mcp restart
    wait_for_mcp
    write_doci_contract
    seed_documents
    write_traefik_route
    remove_legacy_images
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,B03"
    write_report_section
    register_integration
    register_mcp_server

    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=documents@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=documents@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"doci\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "companion documents v$STAGE_VERSION: done ($VERIFY_TOTAL/$VERIFY_TOTAL checks, $((SECONDS - t0))s)"
}

run_verify_mode() {
    log_init "$STAGE_ID"
    load_contracts
    [[ -f "$DOCUMENTS_ENV" ]] && source "$DOCUMENTS_ENV"
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
    log "companion documents -- uninstall"

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
            "SELECT pg_size_pretty(pg_database_size('$DOCI_DB'))" 2>/dev/null \
            | tr -d '[:space:]' || echo "unknown")
    else
        db_size="unknown"
    fi
    echo ""
    echo "About to PERMANENTLY uninstall the documents companion (doci) from this node."
    echo "  Node FQDN:  $node_fqdn"
    echo "  Database:   $DOCI_DB (${db_size})"
    echo "  Services:   $DOCI_UNIT, $DOCI_MCP_UNIT"
    echo "  Files:      $INSTALL_DIR (includes the git-backed document store)"
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
    dump_file="$backup_dir/documents-${DOCI_DB}-${dump_stamp}.dump"
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
    log "dumping $DOCI_DB -> $dump_file"
    if ! runuser -u postgres -- pg_dump \
            --cluster "$SMART_NODE_PG_CLUSTER" -Fc "$DOCI_DB" \
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
    if declare -F sn_mcp_server_unregister  >/dev/null; then
        sn_mcp_server_unregister  --slug doci     || true
    fi
    if declare -F sn_integration_unregister >/dev/null; then
        sn_integration_unregister --slug doci     || true
    fi
    unregister_route "80-documents" 2>/dev/null || true
    systemctl disable --now "$DOCI_MCP_UNIT" "$DOCI_UNIT" 2>/dev/null || true
    rm -f "$DOCI_UNIT_PATH" "$DOCI_MCP_UNIT_PATH"
    systemctl daemon-reload 2>/dev/null || true
    local sn_cidr; sn_cidr=$(docker_subnet)
    if command -v ufw >/dev/null 2>&1 && [[ -n "$sn_cidr" ]]; then
        ufw --force delete allow from "$sn_cidr" to any port "$DOCI_HTTP_PORT" proto tcp >/dev/null 2>&1 || true
    fi
    rm -f "$DOCI_CONTRACT"
    # A node that never migrated still has the containers and images.
    migrate_from_container
    remove_legacy_images
    # Step 5: Drop -- only after the dump is confirmed written.
    psql_pg -c "DROP DATABASE IF EXISTS \"$DOCI_DB\";" 2>/dev/null || true
    psql_pg -c "DROP ROLE IF EXISTS \"$DOCI_ROLE\";"   2>/dev/null || true
    rm -rf "$INSTALL_DIR" "$LOG_DIR"
    state_file="/var/lib/smart-node/stages/${STAGE_ID}.state"
    [[ -f "$state_file" ]] && rm -f "$state_file"
    log "companion documents -- uninstalled"
}

# run_contracts_only -- write contracts without the install steps.
#
# FALLS THROUGH TO THE FULL INSTALL when the native service is not there yet:
# the doci containers still exist, the unit is missing, or the installed app
# carries a different VERSION from the vendored tree. The orchestrator picks
# contracts-only when STAGE_VERSION is unchanged, and an architecture change
# that lands before the next version bump must still reach the node. The same
# test caught the old failure mode as well: a re-vendored tree under an
# unchanged stage version (the reason obtain_image compared VERSION files).
#
# INSTALL (skip): ensure_dirs, install_packages (apt), install_app (copy the
#   tree), install_mcp (venv + pip), load_or_generate_secrets (contracts-only
#   sources the existing file directly), create_doci_db, seed_documents.
#
# CONTRACT (run): load_contracts, source documents.env, write_runtime_env,
#   write_httpd_conf, write_service_scripts, install_units (units must be
#   current), open_firewall (idempotent), the services restarted ONLY when one
#   of the files they read changed, write_doci_contract, write_traefik_route,
#   register_companion, register_integration, register_mcp_server, stage_mark.
run_contracts_only() {
    require_root
    check_core_api_compat
    log "companion documents -- contracts only (v$STAGE_VERSION)"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- run B02 first"
    [[ -n "${SMART_NODE_DOCKER_NETWORK:-}" ]] \
        || die "routing contract incomplete -- run B03 first"
    [[ -f "$DOCUMENTS_ENV" ]] \
        || die "documents secrets not found ($DOCUMENTS_ENV) -- run full install first"

    local reason=""
    if legacy_containers_present; then
        reason="the doci containers are still on this node"
    elif [[ ! -f "$DOCI_UNIT_PATH" || ! -f "$DOCI_MCP_UNIT_PATH" ]]; then
        reason="the native units are not installed"
    elif [[ -r "$SRC_SNAPSHOT/VERSION" ]] && [[ "$(tr -d '[:space:]' < "$SRC_SNAPSHOT/VERSION")" \
            != "$(tr -d '[:space:]' < "$APP_DIR/VERSION" 2>/dev/null)" ]]; then
        reason="the installed app VERSION differs from the vendored tree"
    fi
    if [[ -n "$reason" ]]; then
        log "contracts-only is not enough ($reason), running the full install"
        run_intent
        return
    fi

    # shellcheck source=/dev/null
    source "$DOCUMENTS_ENV"
    local before after
    before=$(runtime_fingerprint)
    write_runtime_env
    write_httpd_conf
    write_service_scripts
    install_units
    open_firewall
    after=$(runtime_fingerprint)
    if [[ "$before" != "$after" ]]; then
        log "service configuration changed: restarting $DOCI_UNIT and $DOCI_MCP_UNIT"
        start_doci restart
        wait_for_doci
        start_mcp restart
    else
        start_doci
        wait_for_doci
        start_mcp
    fi
    wait_for_mcp
    write_doci_contract
    write_traefik_route
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,B03"
    register_integration
    register_mcp_server
    stage_mark "$STAGE_ID" done \
        "version=documents@$STAGE_VERSION"
    log "companion documents -- contracts only done"
}

case "$MODE" in
    run)             run_intent        ;;
    verify)          run_verify_mode   ;;
    status)          run_status_mode   ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
