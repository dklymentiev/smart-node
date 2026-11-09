#!/bin/bash
#
# B03-routing.sh -- Smart Node installer, Layer C bootstrap stage B03.
#
# Runs INSIDE the target host as root. Idempotent. Depends on B01-core.
#
# Installs the routing layer: the reverse proxy that gives every Smart
# Node web service a domain and HTTPS. Smart Node is an appliance -- it
# assumes a clean, dedicated host and owns the machine, so B03 installs
# and fully owns Docker and a Traefik proxy. A pre-existing Docker with
# foreign containers, or anything already bound to 80/443, is an abort
# condition, not a co-tenant.
#
# Decision: Docker is installed
# here, NOT deferred to the optional Layer A A7 container-engine step.
# Phase 2 companions are Docker containers, so Docker is required
# regardless; the `smart-node` Docker network is the routing fabric --
# Traefik and Docker-companions on one network route by container name,
# no host port mapping, isolation preserved.
#
# Creates:
#   - the Docker apt repository (download.docker.com) + signing key, and
#     the Docker Engine packages (docker-ce, docker-ce-cli, containerd.io,
#     docker-buildx-plugin, docker-compose-plugin)
#   - the `smart-node` Docker network -- the shared routing fabric every
#     Docker-companion attaches to
#   - /etc/smart-node/traefik/ -- the Traefik config tree:
#       traefik.yml   static config (entrypoints, providers, ACME)
#       dynamic/      file-provider directory; companions drop route
#                     fragments here via lib.sh register_route
#       certs/        the self-signed bootstrap certificate
#   - /var/lib/smart-node/traefik/acme.json -- ACME cert storage (0600)
#   - the `smart-node-traefik` Traefik container: ports 80 + 443, the
#     docker-provider (label routing) + file-provider, restart-on-boot
#   - ufw allow 80/tcp + 443/tcp (Layer A opens only SSH; Layer C owns
#     the product's ports)
#   - /etc/smart-node/routing.env -- the routing contract later stages
#     and companions read (network name, dynamic dir, domain, resolver)
#   - "Stage B03 -- routing" section in the install report
#   - /var/lib/smart-node/stages/B03.state
#
# TLS: with a node domain (SMART_NODE_DOMAIN, threaded from install-on.sh
# --node-domain) Traefik issues a real Let's Encrypt certificate via the
# ACME HTTP-01 challenge. Without one, only the self-signed bootstrap
# cert is configured -- the panel is still reachable over HTTPS, and real
# ACME is enabled later from the B06 web interface once a domain is set.
#
# The Traefik dashboard is built (api.dashboard) but NOT exposed
# (api.insecure=false, no router) -- it gets an internal, authenticated
# route once B05 identity exists.
#
# Usage:
#   B03-routing.sh            full run: probe -> intent -> probe -> mark
#   B03-routing.sh --verify   idempotent post-check; exit 0 on full pass,
#                             N (fail count) with [VERIFY-FAIL]
#   B03-routing.sh --status   read /var/lib/smart-node/stages/B03.state;
#                             exit 0=done 1=failed 2=missing
#   B03-routing.sh --help     this message
#
# Environment:
#   SMART_NODE_DOMAIN          node domain -> ACME (HTTP-01); unset -> self-signed
#   SMART_NODE_PUBLIC_DOMAIN   the name the OUTSIDE world uses for this node,
#                              when it differs from the node domain (optional).
#                              Carried forward across re-runs -- see
#                              carry_forward_public_domain.
#   SMART_NODE_ACME_EMAIL      ACME registration email (optional)
#   SMART_NODE_TRAEFIK_IMAGE   override the Traefik image (default traefik:v3.7.1)
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

STAGE_ID="B03"
STAGE_NAME="routing"
STAGE_VERSION="0.2.1"

# Node domain -- optional. Set -> Traefik issues a real Let's Encrypt
# cert via ACME HTTP-01. Unset -> only the self-signed bootstrap cert.
NODE_DOMAIN="${SMART_NODE_DOMAIN:-}"
# Public domain -- optional, and a DIFFERENT fact. A node can have two
# names: the one it is administered by (often internal, e.g. on a VPN) and the
# one its customers type. Until now the contract could hold only the first, so
# B05 could only build the sign-in identity from the internal name, and a
# routine update silently replaced a working public identity with a VPN-only
# one -- login unreachable from an ordinary browser, the session cookie scoped
# to a domain that does not cover the public host, and every registered passkey
# invalidated with it. Nothing reported any of that; validate.sh passed 197/197
# an hour later. Empty means "the node has one name", which is the common case.
PUBLIC_DOMAIN="${SMART_NODE_PUBLIC_DOMAIN:-}"
ACME_EMAIL="${SMART_NODE_ACME_EMAIL:-}"
# Internal (WireGuard) domain -- optional. When set, B03 writes the
# Studio.INTERNAL_DOMAIN and auth.INTERNAL_DOMAIN Traefik router config
# so a reinstall preserves the internal routing. Carried forward from
# routing.env across re-runs that do not restate the variable.
INTERNAL_DOMAIN="${SMART_NODE_INTERNAL_DOMAIN:-}"
# Voice hostname -- optional. The public name to publish the voice line under.
# When empty the companion defaults to voice.<SMART_NODE_DOMAIN>, which is the
# convention. Set this when the line is already known by a different name (e.g.
# line.<domain>) so an update does not silently revert to the default and lose
# the real DNS entry. Empty stays legal; this is carried forward from routing.env
# across re-runs that do not restate the variable.
VOICE_HOSTNAME="${SMART_NODE_VOICE_HOSTNAME:-}"
# Traefik image -- pinned. v3 carries the docker + file providers and the
# v3 ACME/redirect config this stage writes. Must be a current v3: the
# docker-provider client in older v3 tags (e.g. v3.3) negotiates down to
# Docker API 1.24 and a current docker-ce daemon (Engine 29+, min API
# 1.40) rejects it -- the provider then reads no container labels at all.
TRAEFIK_IMAGE="${SMART_NODE_TRAEFIK_IMAGE:-traefik:v3.7.1}"
# ACME resolver name; only wired into traefik.yml when a domain is set.
ACME_RESOLVER="letsencrypt"

# The shared routing fabric + the proxy container -- both namespaced.
DOCKER_NET="smart-node"
TRAEFIK_CT="smart-node-traefik"

# Subnet / gateway for the smart-node Docker network.
# Not compiled in. Discovery order in create_network():
#   1. SMART_NODE_DOCKER_SUBNET / SMART_NODE_DOCKER_GATEWAY env vars (operator override)
#   2. routing.env (SMART_NODE_DOCKER_SUBNET / SMART_NODE_DOCKER_GATEWAY) from a prior run
#   3. First install with no record: no --subnet (docker assigns); result is recorded
# On an existing network: the recorded subnet must match or the install refuses.
SN_DOCKER_SUBNET=""  # set by create_network(); written to routing.env as the pin
SN_DOCKER_GATEWAY="" # set by create_network(); written to routing.env as the pin

# Config tree under /etc (B01/Layer A created /etc/smart-node).
TRAEFIK_ETC="/etc/smart-node/traefik"
TRAEFIK_DYNAMIC="$TRAEFIK_ETC/dynamic"
TRAEFIK_CERTS="$TRAEFIK_ETC/certs"
TRAEFIK_STATIC="$TRAEFIK_ETC/traefik.yml"
BOOTSTRAP_CRT="$TRAEFIK_CERTS/bootstrap.crt"
BOOTSTRAP_KEY="$TRAEFIK_CERTS/bootstrap.key"
# State tree under /var/lib -- holds the ACME cert store.
TRAEFIK_VAR="/var/lib/smart-node/traefik"
TRAEFIK_ACME="$TRAEFIK_VAR/acme.json"
# The routing contract later stages + companions read.
ROUTING_ENV="/etc/smart-node/routing.env"

# Docker apt repository -- the Docker project's official channel; gives a
# current Engine instead of whatever the base distro happens to ship.
DOCKER_KEY="/etc/apt/keyrings/docker.asc"
DOCKER_LIST="/etc/apt/sources.list.d/docker.list"

# lib.sh is installed by B01-core; B03 has no sidecar fallback.
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

DOCKER_PKGS_INSTALLED=()   # packages this run actually installed (report)

# ---------- docker helpers ----------

# docker_available -- true if the docker CLI is present and the daemon
# answers. Never fails the caller under set -e.
docker_available() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

# traefik_http_code -- HTTP status of a plain request to the local web
# entrypoint. Any unknown host hits the global HTTP->HTTPS redirect, so a
# live Traefik answers 3xx. Empty/000 means it is not up yet.
traefik_http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        http://127.0.0.1/ 2>/dev/null || true
}

# ---------- modes ----------

# probe_before -- clean-server guard. A re-run (our network present)
# proceeds idempotently. Otherwise a host that already has something on
# 80/443, or a Docker with foreign containers, is not the clean appliance
# host B03 expects -- abort.
probe_before() {
    if docker_available && docker network inspect "$DOCKER_NET" >/dev/null 2>&1; then
        log "docker network '$DOCKER_NET' already present -- idempotent re-run"
        return 0
    fi
    # Nothing else may own the web ports -- Smart Node terminates them.
    local p
    if command -v ss >/dev/null 2>&1; then
        for p in 80 443; do
            if ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
                die "port $p already in use on this host. Smart Node is an appliance and owns 80/443 for routing. Aborting -- free the port or install on a clean server."
            fi
        done
    fi
    # A Docker that already runs foreign containers is a populated host.
    if docker_available; then
        local n
        n=$(docker ps -aq 2>/dev/null | wc -l)
        if (( n > 0 )); then
            die "Docker is already installed and has $n container(s). Smart Node expects a clean, dedicated host. Aborting -- remove the containers or install on a clean server."
        fi
    fi
}

# _assert_pkg_available PKG -- die unless apt has an installable candidate.
_assert_pkg_available() {
    local pkg="$1" cand
    cand=$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/{print $2}')
    if [[ -z "$cand" || "$cand" == "(none)" ]]; then
        die "package '$pkg' has no installable candidate -- Docker apt repo unreachable? Check $DOCKER_LIST."
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
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg"
    manifest_record package "$pkg"
    DOCKER_PKGS_INSTALLED+=("$pkg")
}

# _ensure_docker_repo -- add download.docker.com as an apt source. The
# Docker project's official channel; modern apt reads the ASCII-armored
# key directly via signed-by, so no gnupg/dearmor is needed. Idempotent.
# The key + sources file are recorded in the manifest.
_ensure_docker_repo() {
    if [[ -f "$DOCKER_KEY" && -f "$DOCKER_LIST" ]]; then
        log "Docker apt repository already configured"
    else
        log "configuring Docker apt repository ($DOCKER_LIST)"
        command -v curl >/dev/null 2>&1 || _install_pkg curl
        install -d -m 0755 /etc/apt/keyrings
        # Distro id/codename. Docker publishes repos for debian + ubuntu;
        # anything else falls back to debian (the appliance base).
        local os_id codename
        os_id=$(. /etc/os-release 2>/dev/null && echo "${ID:-debian}")
        codename=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-bookworm}")
        case "$os_id" in
            ubuntu) os_id="ubuntu" ;;
            *)      os_id="debian" ;;
        esac
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$DOCKER_KEY" "https://download.docker.com/linux/$os_id/gpg" \
            || die "failed to fetch the Docker signing key -- network down or download.docker.com unreachable?"
        chmod 0644 "$DOCKER_KEY"
        local arch
        arch=$(dpkg --print-architecture)
        echo "deb [arch=$arch signed-by=$DOCKER_KEY] https://download.docker.com/linux/$os_id ${codename} stable" \
            > "$DOCKER_LIST"
        manifest_record file "$DOCKER_KEY"
        manifest_record file "$DOCKER_LIST"
    fi
    apt-get update -qq
}

install_docker() {
    export DEBIAN_FRONTEND=noninteractive
    # apt-get update only if the cache is stale (same policy as B01/B02).
    local cache_age=999999
    if [[ -f /var/cache/apt/pkgcache.bin ]]; then
        cache_age=$(( $(date +%s) - $(stat -c %Y /var/cache/apt/pkgcache.bin) ))
    fi
    if (( cache_age > 3600 )); then
        log "apt-get update (cache age ${cache_age}s)"
        apt-get update -qq
    fi

    if docker_available \
            && dpkg-query -W -f='${Status}\n' docker-ce 2>/dev/null \
                | grep -q 'install ok installed'; then
        log "Docker Engine already installed"
    else
        _ensure_docker_repo
        # openssl is needed for the self-signed bootstrap cert.
        command -v openssl >/dev/null 2>&1 || _install_pkg openssl
        local pkg
        for pkg in docker-ce docker-ce-cli containerd.io \
                   docker-buildx-plugin docker-compose-plugin; do
            _assert_pkg_available "$pkg"
            _install_pkg "$pkg"
        done
    fi

    # The docker service is owned by the package -- enable + start it, but
    # do NOT record it as a manifest systemd-unit (the unit file ships
    # with the package; the package, not the manifest, owns its lifecycle).
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now docker >/dev/null 2>&1 \
            || die "failed to enable/start the docker service"
    fi
    # Wait for the daemon socket to answer.
    local i
    for i in $(seq 1 30); do
        docker_available && return 0
        sleep 1
    done
    die "Docker daemon did not become ready within 30s"
}

ensure_dirs() {
    # Parent /etc/smart-node + /var/lib/smart-node exist (Layer A / B01).
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$TRAEFIK_ETC"
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$TRAEFIK_CERTS"
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$TRAEFIK_VAR"
    # dynamic/ is a registry dir -- setgid so route files companions drop
    # here inherit the umbrella group (same pattern as mcp.d/companions.d).
    install -d -m 2750 -o root -g "$SMART_NODE_GROUP" "$TRAEFIK_DYNAMIC"
    manifest_record dir "$TRAEFIK_ETC"
    manifest_record dir "$TRAEFIK_CERTS"
    manifest_record dir "$TRAEFIK_DYNAMIC"
    manifest_record dir "$TRAEFIK_VAR"
}

create_network() {
    # Determine the configured (expected) subnet and gateway.
    # Priority: env vars > routing.env record > (empty = let docker assign on first install)
    local configured_subnet="${SMART_NODE_DOCKER_SUBNET:-}"
    local configured_gateway="${SMART_NODE_DOCKER_GATEWAY:-}"
    if [[ -z "$configured_subnet" && -r "$ROUTING_ENV" ]]; then
        configured_subnet=$(sed -n 's/^SMART_NODE_DOCKER_SUBNET=//p' "$ROUTING_ENV" | tail -1)
        configured_gateway=$(sed -n 's/^SMART_NODE_DOCKER_GATEWAY=//p' "$ROUTING_ENV" | tail -1)
    fi

    if docker network inspect "$DOCKER_NET" >/dev/null 2>&1; then
        # Network already exists. If we have a recorded subnet, verify the live
        # network matches it; a mismatch means the network was recreated and
        # renumbered, which invalidates every artifact written at install time.
        local actual_subnet
        actual_subnet=$(docker network inspect "$DOCKER_NET" \
            --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
            | tr ' ' '\n' | grep -m1 . || true)
        if [[ -n "$configured_subnet" && -n "$actual_subnet" \
              && "$actual_subnet" != "$configured_subnet" ]]; then
            die "docker network '$DOCKER_NET' has subnet $actual_subnet but the recorded pin is $configured_subnet.
  The network was recreated and renumbered. To proceed on this node:
  1. Run update-on.sh first -- it reconciles all artifacts to the actual gateway.
  2. Then remove the network: docker network rm $DOCKER_NET
  3. Then re-run this bootstrap -- it will create the network with the pinned subnet.
  Or set SMART_NODE_DOCKER_SUBNET=$actual_subnet to accept this subnet."
        fi
        log "docker network '$DOCKER_NET' already exists (subnet ${actual_subnet:-unknown})"
    else
        # Network does not exist -- create it.
        if [[ -n "$configured_subnet" && -n "$configured_gateway" ]]; then
            log "creating docker network '$DOCKER_NET' (subnet=$configured_subnet, gateway=$configured_gateway)"
            docker network create \
                --subnet "$configured_subnet" \
                --gateway "$configured_gateway" \
                "$DOCKER_NET" >/dev/null \
                || die "failed to create docker network '$DOCKER_NET'"
        elif [[ -n "$configured_subnet" ]]; then
            log "creating docker network '$DOCKER_NET' (subnet=$configured_subnet)"
            docker network create \
                --subnet "$configured_subnet" \
                "$DOCKER_NET" >/dev/null \
                || die "failed to create docker network '$DOCKER_NET'"
        else
            log "creating docker network '$DOCKER_NET' (no recorded subnet -- docker assigns)"
            docker network create "$DOCKER_NET" >/dev/null \
                || die "failed to create docker network '$DOCKER_NET'"
        fi
    fi
    # Read the actual gateway and subnet back from the live network.
    # Written to SN_DOCKER_GATEWAY / SN_DOCKER_SUBNET so write_routing_env()
    # records the live values, which become the pin for all future runs.
    local live_gw live_subnet
    live_gw=$(docker network inspect "$DOCKER_NET" \
        --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    live_subnet=$(docker network inspect "$DOCKER_NET" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    [[ -n "$live_gw" ]] \
        || die "could not read gateway from docker network '$DOCKER_NET' after create/verify"
    SN_DOCKER_GATEWAY="$live_gw"
    SN_DOCKER_SUBNET="${live_subnet:-}"
    manifest_record docker-network "$DOCKER_NET"
}

gen_bootstrap_cert() {
    # A self-signed certificate so the node answers HTTPS immediately --
    # before any ACME cert exists, and as the default cert for requests
    # that match no ACME domain (IP access, the dashboard). Regenerated
    # only if absent; a re-run keeps the existing pair.
    if [[ -s "$BOOTSTRAP_CRT" && -s "$BOOTSTRAP_KEY" ]]; then
        log "bootstrap certificate already present"
    else
        local cn
        cn="${NODE_DOMAIN:-$(hostname -f 2>/dev/null || hostname)}"
        [[ -n "$cn" ]] || cn="smart-node.local"
        log "generating self-signed bootstrap certificate (CN=$cn)"
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout "$BOOTSTRAP_KEY" -out "$BOOTSTRAP_CRT" \
            -days 3650 -subj "/CN=$cn" \
            -addext "subjectAltName=DNS:$cn" >/dev/null 2>&1 \
            || die "failed to generate the self-signed bootstrap certificate"
    fi
    chmod 0600 "$BOOTSTRAP_KEY"
    chmod 0644 "$BOOTSTRAP_CRT"
    manifest_record file "$BOOTSTRAP_CRT"
    manifest_record file "$BOOTSTRAP_KEY"
}

write_traefik_config() {
    # ACME cert store -- pre-created 0600 so Traefik does not refuse a
    # world-readable acme.json. Empty file = no certs yet, which is fine.
    if [[ ! -e "$TRAEFIK_ACME" ]]; then
        : > "$TRAEFIK_ACME"
    fi
    chmod 0600 "$TRAEFIK_ACME"
    manifest_record file "$TRAEFIK_ACME"

    log "writing Traefik static config ($TRAEFIK_STATIC)"
    # A local node answers on plain http (lib.sh web_local); every other node
    # sends http to https.
    local WEB_REDIRECT="    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https"
    web_local "$NODE_DOMAIN" && WEB_REDIRECT="    # plain http: a local node"
    local tmp; tmp=$(mktemp)
    {
        cat <<EOF
# Smart Node Traefik static configuration.
# Generated by B03-routing@$STAGE_VERSION -- do not edit by hand.

global:
  checkNewVersion: false
  sendAnonymousUsage: false

entryPoints:
  web:
    address: ":80"
$WEB_REDIRECT
  websecure:
    address: ":443"

providers:
  docker:
    endpoint: "unix:///var/run/docker.sock"
    exposedByDefault: false
    network: $DOCKER_NET
  file:
    directory: $TRAEFIK_DYNAMIC
    watch: true

# Dashboard is built but NOT exposed (insecure=false, no router). B05
# identity adds an internal, authenticated route to api@internal.
api:
  dashboard: true
  insecure: false

log:
  level: INFO
EOF
        # a local node (lib.sh web_local) has no certificate to ask for
        if [[ -n "$NODE_DOMAIN" ]] && ! web_local "$NODE_DOMAIN"; then
            cat <<EOF

# A node domain is set -> issue real certificates via Let's Encrypt
# (ACME HTTP-01). Routers opt in with tls.certResolver: $ACME_RESOLVER.
certificatesResolvers:
  $ACME_RESOLVER:
    acme:
      email: "$ACME_EMAIL"
      storage: $TRAEFIK_ACME
      httpChallenge:
        entryPoint: web
EOF
        fi
    } > "$tmp"
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$TRAEFIK_STATIC"
    rm -f "$tmp"
    manifest_record file "$TRAEFIK_STATIC"
}

write_dynamic_tls() {
    # The file-provider default-certificate store: every HTTPS request
    # that matches no ACME cert is served the self-signed bootstrap cert,
    # so there is never a Traefik throwaway default cert in the chain.
    local dst="$TRAEFIK_DYNAMIC/00-tls-default.yml"
    log "writing default-certificate config ($dst)"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node default TLS certificate (self-signed bootstrap cert).
# Generated by B03-routing@$STAGE_VERSION -- do not edit by hand.
tls:
  stores:
    default:
      defaultCertificate:
        certFile: $BOOTSTRAP_CRT
        keyFile: $BOOTSTRAP_KEY
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$dst"
    rm -f "$tmp"
    manifest_record file "$dst"
}

write_internal_routes() {
    # Write the WireGuard-internal Traefik router config so reinstalls do
    # not lose the hand-written 71-studio-internal.yml. Only written when
    # INTERNAL_DOMAIN is set (carried forward from routing.env on re-runs).
    # The smart-node-studio service is defined by studio.sh (70-studio.yml);
    # Traefik merges all file-provider configs, so the cross-file reference works.
    # The authum-internal container (AUTHUM_DOMAIN=.INTERNAL_DOMAIN) is launched
    # by B05-identity.sh -- it is not a routing concern.
    [[ -n "$INTERNAL_DOMAIN" ]] || return 0
    local dst="$TRAEFIK_DYNAMIC/71-studio-internal.yml"
    log "writing internal hostname route config ($dst, domain=$INTERNAL_DOMAIN)"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node internal (WireGuard) routing.
# Generated by B03-routing@$STAGE_VERSION -- do not edit by hand.
http:
  routers:
    smart-node-studio-internal:
      rule: "Host(\`studio.${INTERNAL_DOMAIN}\`)"
      entryPoints:
        - websecure
      service: smart-node-studio
      middlewares:
        - smart-node-auth-internal
      tls: {}
    smart-node-auth-internal:
      rule: "Host(\`auth.${INTERNAL_DOMAIN}\`)"
      entryPoints:
        - websecure
      service: authum-internal
      tls: {}
  middlewares:
    smart-node-auth-internal:
      forwardAuth:
        address: "http://authum-internal:80/"
        trustForwardHeader: true
        authResponseHeaders:
          - "Remote-User"
  services:
    authum-internal:
      loadBalancer:
        servers:
          - url: "http://authum-internal:80/"
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$dst"
    rm -f "$tmp"
    manifest_record file "$dst"
}

run_traefik() {
    # Recreate deterministically: a re-run drops the old container and
    # starts a fresh one with the current config + image.
    if docker ps -aq -f "name=^${TRAEFIK_CT}$" 2>/dev/null | grep -q .; then
        log "removing existing container '$TRAEFIK_CT' (will recreate)"
        docker rm -f "$TRAEFIK_CT" >/dev/null 2>&1 || true
    fi
    log "starting Traefik container '$TRAEFIK_CT' ($TRAEFIK_IMAGE)"
    # Plain http is for this machine only: a local node's ports listen on
    # loopback, never on the network the machine sits in. SMART_NODE_WEB_BIND
    # moves it for a node that is itself a container whose host publishes
    # loopback ports into it (the Docker test node).
    local PORTS=(-p 80:80 -p 443:443)
    if web_local "$NODE_DOMAIN"; then
        local bind="${SMART_NODE_WEB_BIND:-127.0.0.1}"
        PORTS=(-p "$bind:80:80" -p "$bind:443:443")
    fi
    # --add-host: the routing contract points every companion file route at
    # host.docker.internal, and nothing else teaches Traefik that name.
    # documents.sh maps it for its own compose services (extra_hosts); the
    # Traefik container was left out, so those routes resolve to nothing.
    docker run -d \
        --name "$TRAEFIK_CT" \
        --restart unless-stopped \
        --network "$DOCKER_NET" \
        "${PORTS[@]}" \
        --add-host host.docker.internal:host-gateway \
        -v /var/run/docker.sock:/var/run/docker.sock:ro \
        -v "$TRAEFIK_ETC:$TRAEFIK_ETC:ro" \
        -v "$TRAEFIK_VAR:$TRAEFIK_VAR" \
        "$TRAEFIK_IMAGE" \
        --configfile="$TRAEFIK_STATIC" >/dev/null \
        || die "failed to start the Traefik container"
    manifest_record docker-container "$TRAEFIK_CT"
}

open_firewall() {
    # Layer A opens only SSH. Layer C owns the product's ports -- B03
    # opens the web ports and records each rule so uninstall reverses it.
    if ! command -v ufw >/dev/null 2>&1; then
        log "ufw not installed -- skipping (firewall is nftables or none)"
        return 0
    fi
    local rule
    for rule in 80/tcp 443/tcp; do
        ufw allow "$rule" >/dev/null 2>&1 \
            || log "could not add ufw rule '$rule' (continuing)"
        manifest_record ufw-rule "$rule"
    done
    log "ufw: allowed 80/tcp + 443/tcp"
}

# carry_forward_public_domain -- keep the public name across a re-run that did
# not restate it; stale generated values must be retired explicitly.
#
# This is the half that matters in practice. The flag is passed once, when the
# node is first given its public face; every update after that is a routine
# `install-on.sh --all` typed from memory, and without this the contract would
# come back empty, B05 would rebuild the identity from the internal name, and
# the public door would close again. An operator who wants to CHANGE the public
# name passes the flag; an operator who wants to keep it does nothing, which is
# what people actually do.
#
# Deliberately one-directional: it never invents a value, only preserves one
# already on disk. To remove a public name, pass SMART_NODE_PUBLIC_DOMAIN=- .
carry_forward_public_domain() {
    if [[ "$PUBLIC_DOMAIN" == "-" ]]; then
        PUBLIC_DOMAIN=""
        log "public domain cleared by request -- this node has one name again"
        return 0
    fi
    [[ -z "$PUBLIC_DOMAIN" ]] || { log "public domain: $PUBLIC_DOMAIN"; return 0; }
    [[ -r "$ROUTING_ENV" ]] || return 0
    local prev
    prev=$(sed -n 's/^SMART_NODE_PUBLIC_DOMAIN=//p' "$ROUTING_ENV" | tail -1)
    if [[ -n "$prev" ]]; then
        PUBLIC_DOMAIN="$prev"
        log "public domain carried forward from the existing contract: $PUBLIC_DOMAIN"
    fi
    return 0
}

# carry_forward_node_domain -- keep the node domain (and its ACME cert resolver)
# across a re-run that did not restate SMART_NODE_DOMAIN.
#
# Without this, a routine update that passes no SMART_NODE_DOMAIN env var causes
# write_traefik_config to produce a static config WITHOUT the ACME stanza, and
# every Let's Encrypt certificate silently disappears on the next Traefik restart.
# The carry-forward is one-directional: it only preserves a value already on
# disk; it never invents one. To clear the node domain explicitly, pass
# SMART_NODE_DOMAIN=- (analogous to the public-domain clear convention).
carry_forward_node_domain() {
    if [[ "$NODE_DOMAIN" == "-" ]]; then
        NODE_DOMAIN=""
        log "node domain cleared by request -- ACME will not be configured"
        return 0
    fi
    [[ -z "$NODE_DOMAIN" ]] || { log "node domain: $NODE_DOMAIN"; return 0; }
    [[ -r "$ROUTING_ENV" ]] || return 0
    local prev
    prev=$(sed -n 's/^SMART_NODE_DOMAIN=//p' "$ROUTING_ENV" | tail -1)
    if [[ -n "$prev" ]]; then
        NODE_DOMAIN="$prev"
        log "node domain carried forward from the existing contract: $NODE_DOMAIN"
    fi
    return 0
}

# carry_forward_acme_email -- keep the ACME registration email across a re-run
# that did not restate SMART_NODE_ACME_EMAIL.
#
# Without this, a routine update without --acme-email writes the
# certificatesResolvers block with email: "" -- harmless for existing certs
# (Let's Encrypt does not require email for renewal) but incomplete. More
# importantly, this pairs with carry_forward_node_domain: both must be called
# before write_traefik_config so the resolver block is written correctly.
carry_forward_acme_email() {
    [[ -z "$ACME_EMAIL" ]] || { log "ACME email: $ACME_EMAIL"; return 0; }
    [[ -r "$ROUTING_ENV" ]] || return 0
    local prev
    prev=$(sed -n 's/^SMART_NODE_ACME_EMAIL=//p' "$ROUTING_ENV" | tail -1)
    if [[ -n "$prev" ]]; then
        ACME_EMAIL="$prev"
        log "ACME email carried forward from the existing contract: $ACME_EMAIL"
    fi
    return 0
}

# carry_forward_internal_domain -- keep the WireGuard internal domain across
# re-runs that do not restate SMART_NODE_INTERNAL_DOMAIN. Parallel to the
# other carry_forward_* functions. Set SMART_NODE_INTERNAL_DOMAIN=- to clear.
carry_forward_internal_domain() {
    if [[ "$INTERNAL_DOMAIN" == "-" ]]; then
        INTERNAL_DOMAIN=""
        log "internal domain cleared (SMART_NODE_INTERNAL_DOMAIN=-)"
        return 0
    fi
    [[ -z "$INTERNAL_DOMAIN" ]] || { log "internal domain: $INTERNAL_DOMAIN"; return 0; }
    [[ -r "$ROUTING_ENV" ]] || return 0
    local prev
    prev=$(sed -n 's/^SMART_NODE_INTERNAL_DOMAIN=//p' "$ROUTING_ENV" | tail -1)
    if [[ -n "$prev" ]]; then
        INTERNAL_DOMAIN="$prev"
        log "internal domain carried forward from the existing contract: $INTERNAL_DOMAIN"
    fi
    return 0
}

# carry_forward_voice_hostname -- keep the voice hostname across re-runs that
# do not restate SMART_NODE_VOICE_HOSTNAME. Parallel to carry_forward_internal_domain.
# Empty means "use the default voice.<domain>" -- this is a MAY-differ name, not a
# MUST-supply one. Set SMART_NODE_VOICE_HOSTNAME=- to clear (revert to default).
carry_forward_voice_hostname() {
    if [[ "$VOICE_HOSTNAME" == "-" ]]; then
        VOICE_HOSTNAME=""
        log "voice hostname cleared (SMART_NODE_VOICE_HOSTNAME=-) -- companion will use default voice.<domain>"
        return 0
    fi
    [[ -z "$VOICE_HOSTNAME" ]] || { log "voice hostname: $VOICE_HOSTNAME"; return 0; }
    [[ -r "$ROUTING_ENV" ]] || return 0
    local prev
    prev=$(sed -n 's/^SMART_NODE_VOICE_HOSTNAME=//p' "$ROUTING_ENV" | tail -1)
    if [[ -n "$prev" ]]; then
        VOICE_HOSTNAME="$prev"
        log "voice hostname carried forward from the existing contract: $VOICE_HOSTNAME"
    fi
    return 0
}


write_routing_env() {
    # The routing contract -- the parallel to persistence.env. Later
    # stages (B04..B06) and Phase 2 companions read it to learn the
    # network name, the dynamic-route dir, the domain and TLS resolver.
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node routing contract -- generated by B03-routing@$STAGE_VERSION."
        echo "# Do not edit by hand. Sourced by later stages and companions."
        echo "SMART_NODE_DOCKER_NETWORK=$DOCKER_NET"
        echo "SMART_NODE_TRAEFIK_CONTAINER=$TRAEFIK_CT"
        echo "SMART_NODE_TRAEFIK_DYNAMIC_DIR=$TRAEFIK_DYNAMIC"
        echo "SMART_NODE_DOMAIN=$NODE_DOMAIN"
        # ACME email persisted so carry_forward_acme_email can restore it on
        # an update that does not restate SMART_NODE_ACME_EMAIL.
        echo "SMART_NODE_ACME_EMAIL=$ACME_EMAIL"
        # write SMART_NODE_PUBLIC_DOMAIN and SMART_NODE_INTERNAL_DOMAIN
        # only when they have a non-empty value with a defined distinct meaning.
        # When empty they have no meaning distinct from SMART_NODE_DOMAIN and
        # must not appear in routing.env to avoid confusion. Carry-forward
        # functions still restore previously-set values on update runs.
        [[ -n "$PUBLIC_DOMAIN" ]] && echo "SMART_NODE_PUBLIC_DOMAIN=$PUBLIC_DOMAIN"
        if [[ -n "$NODE_DOMAIN" ]] && ! web_local "$NODE_DOMAIN"; then
            echo "SMART_NODE_TLS_RESOLVER=$ACME_RESOLVER"
        else
            echo "SMART_NODE_TLS_RESOLVER="
        fi
        [[ -n "$INTERNAL_DOMAIN" ]] && echo "SMART_NODE_INTERNAL_DOMAIN=$INTERNAL_DOMAIN"
        # SMART_NODE_VOICE_HOSTNAME: only written when explicitly set. Empty means
        # "use the default voice.<domain>", which the companion derives itself.
        [[ -n "$VOICE_HOSTNAME" ]] && echo "SMART_NODE_VOICE_HOSTNAME=$VOICE_HOSTNAME"
        # Subnet and live gateway for the smart-node Docker network, recorded here
        # so future B03 runs compare the live network against this pin, and companions
        # derive the gateway from the contract file rather than baking it.
        echo "SMART_NODE_DOCKER_SUBNET=$SN_DOCKER_SUBNET"
        echo "SMART_NODE_DOCKER_GATEWAY=$SN_DOCKER_GATEWAY"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$ROUTING_ENV"
    rm -f "$tmp"
    manifest_record file "$ROUTING_ENV"
}

wait_for_traefik() {
    # The first run pulls the image -- be generous. A 3xx from the web
    # entrypoint (the global HTTP->HTTPS redirect) means Traefik is live.
    local i code
    for i in $(seq 1 60); do
        code=$(traefik_http_code)
        [[ "$code" == 3* ]] && return 0
        # a local node has no redirect: any answer means Traefik is live
        web_local "$NODE_DOMAIN" && [[ "$code" != 000 && -n "$code" ]] && return 0
        sleep 1
    done
    die "Traefik did not become ready within 60s (container '$TRAEFIK_CT')"
}

write_report_section() {
    local pkgs="${DOCKER_PKGS_INSTALLED[*]:-(already present)}"
    local tls_mode
    if [[ -n "$NODE_DOMAIN" ]]; then
        tls_mode="Let's Encrypt (ACME HTTP-01) for $NODE_DOMAIN + self-signed default"
    else
        tls_mode="self-signed bootstrap cert (no domain set; ACME enabled later from B06)"
    fi
    local body
    body=$(cat <<EOF
**Version:** B03-routing@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
engine:    Docker Engine (managed, smart-node-owned)
source:    Docker apt repo (download.docker.com)
network:   $DOCKER_NET  (shared routing fabric for companions)
proxy:     $TRAEFIK_CT  ($TRAEFIK_IMAGE)
ports:     80 -> 443 redirect, 443 TLS
providers: docker (label routing, network=$DOCKER_NET) + file ($TRAEFIK_DYNAMIC)
tls:       $tls_mode
dashboard: built, not exposed (api.insecure=false; B05 adds an auth route)
contract:  $ROUTING_ENV
packages:  $pkgs
\`\`\`

Companions register routes by dropping a fragment in
\`$TRAEFIK_DYNAMIC\` via lib.sh \`register_route\`.
EOF
)
    report_section "Stage B03 -- routing" "$body"
}

run_intent() {
    require_root
    log "stage B03 (routing) -- start"
    require_stage B01
    log_init "$STAGE_ID"
    probe_before
    install_docker
    ensure_dirs
    create_network
    gen_bootstrap_cert
    # Carry forward identity BEFORE write_traefik_config so the
    # certificatesResolvers block is present on updates that omit
    # --node-domain / --acme-email (wrong order caused TLS outage).
    carry_forward_public_domain
    carry_forward_node_domain
    carry_forward_acme_email
    carry_forward_internal_domain
    carry_forward_voice_hostname
    write_traefik_config
    write_dynamic_tls
    write_internal_routes
    run_traefik
    open_firewall
    write_routing_env
    wait_for_traefik
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=B03-routing@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=B03-routing@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    # Record stage completion in the event log (B02 is up by now -> DB).
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "stage.done" "stage" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"network\":\"$DOCKER_NET\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "stage B03 (routing) -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # 1. B01 precondition.
    verify_check "stage B01 done" require_stage B01
    # 2. docker-ce package installed.
    verify_check "docker-ce installed" bash -c "
        dpkg-query -W -f='\${Status}\n' docker-ce 2>/dev/null \
            | grep -q 'install ok installed'"
    # 3. docker daemon answering.
    verify_check "docker daemon running" docker_available
    # 4. routing network exists.
    verify_check "docker network $DOCKER_NET exists" \
        docker network inspect "$DOCKER_NET"
    # 5. traefik container exists.
    verify_check "container $TRAEFIK_CT exists" bash -c "
        docker ps -aq -f 'name=^${TRAEFIK_CT}\$' 2>/dev/null | grep -q ."
    # 6. traefik container running.
    verify_check "container $TRAEFIK_CT running" bash -c "
        [[ \$(docker inspect -f '{{.State.Running}}' '$TRAEFIK_CT' 2>/dev/null) == true ]]"
    # 7. traefik runs the pinned image.
    verify_check "container uses image $TRAEFIK_IMAGE" bash -c "
        [[ \$(docker inspect -f '{{.Config.Image}}' '$TRAEFIK_CT' 2>/dev/null) == '$TRAEFIK_IMAGE' ]]"
    # 8. port 80 listening.
    verify_check "port 80 listening" bash -c "
        ss -ltnH 'sport = :80' 2>/dev/null | grep -q ."
    # 9. port 443 listening.
    verify_check "port 443 listening" bash -c "
        ss -ltnH 'sport = :443' 2>/dev/null | grep -q ."
    # 10. static config present.
    verify_check "traefik.yml present" test -f "$TRAEFIK_STATIC"
    # 11. dynamic-route registry dir present.
    verify_check "dynamic route dir present" test -d "$TRAEFIK_DYNAMIC"
    # 12. default-certificate config present.
    verify_check "default TLS cert config present" \
        test -f "$TRAEFIK_DYNAMIC/00-tls-default.yml"
    # 13. bootstrap certificate present.
    verify_check "bootstrap certificate present" test -s "$BOOTSTRAP_CRT"
    # 14. bootstrap key present and 0600.
    verify_check "bootstrap key present 0600" bash -c "
        [[ -s '$BOOTSTRAP_KEY' && \$(stat -c '%a' '$BOOTSTRAP_KEY' 2>/dev/null) == 600 ]]"
    # 15. acme storage present and 0600.
    verify_check "acme.json present 0600" bash -c "
        [[ -e '$TRAEFIK_ACME' && \$(stat -c '%a' '$TRAEFIK_ACME' 2>/dev/null) == 600 ]]"
    # 16. HTTP->HTTPS redirect answers 3xx; on a local node http answers
    # itself, with no redirect (lib.sh web_local).
    if web_local "$NODE_DOMAIN"; then
        verify_check "local node: http answers without a redirect" bash -c "
            code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/ 2>/dev/null)
            [[ \"\$code\" != 000 && \"\$code\" != 3* ]]"
    else
    verify_check "HTTP->HTTPS redirect active" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/ 2>/dev/null)
        [[ \"\$code\" == 3* ]]"
    fi
    # 17. dashboard NOT published (no 8080 on the container).
    verify_check "dashboard not published" bash -c "
        ! docker port '$TRAEFIK_CT' 2>/dev/null | grep -q 8080"
    # 18. ufw allows 80 (lenient: no ufw passes).
    verify_check "ufw allows 80/tcp" bash -c "
        command -v ufw >/dev/null 2>&1 || exit 0
        ufw show added 2>/dev/null | grep -qE 'allow 80(/tcp)?\b'"
    # 19. ufw allows 443 (lenient: no ufw passes).
    verify_check "ufw allows 443/tcp" bash -c "
        command -v ufw >/dev/null 2>&1 || exit 0
        ufw show added 2>/dev/null | grep -qE 'allow 443(/tcp)?\b'"
    # 20. routing contract written.
    verify_check "routing.env written" test -r "$ROUTING_ENV"
    # 21. install-report has the B03 section.
    verify_check "install-report has Stage B03 section" \
        bash -c "grep -qFx '## Stage B03 -- routing' '$SMART_NODE_REPORT'"
    # 22. manifest records the B03 network + container.
    verify_check "manifest records B03 artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"docker-network\",\"value\":\"$DOCKER_NET\",' \"\$m\" &&
        grep -qF '{\"type\":\"docker-container\",\"value\":\"$TRAEFIK_CT\",' \"\$m\""
    # 23. internal route config present (only when INTERNAL_DOMAIN is configured).
    if [[ -n "$INTERNAL_DOMAIN" ]]; then
        verify_check "internal hostname route config present" \
            test -f "$TRAEFIK_DYNAMIC/71-studio-internal.yml"
    fi
}

run_verify_mode() {
    log_init "$STAGE_ID"
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    # the domain this node was installed with decides what http must do
    carry_forward_node_domain >/dev/null
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
