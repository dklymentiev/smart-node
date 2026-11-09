#!/bin/bash
#
# B05-identity.sh -- Smart Node installer, Layer C bootstrap stage B05.
#
# Runs INSIDE the target host as root. Idempotent. Depends on B02 + B03 + B04.
#
# Installs the identity layer for INTERNAL STAFF: a self-hosted Authum SSO
# service (WebAuthn/passkey + Argon2id password fallback) as the AuthN gate,
# plus an identity/roles schema in the B02 core database as the AuthZ store.
# From B05 onward the B06 web interface and every authenticated route sit
# behind one SSO login.
#
# Three identity planes; B05 owns exactly one:
#   - OS isolation (smart-node-* / per-agent Linux users)  -- NOT B05
#   - internal staff (operators: super-admin + roles)      -- THIS stage
#   - external email/call/chat contacts                    -- Phase 2 (Entis)
#
# AuthN / AuthZ split:
#   - AuthN = Authum. Proves WHO an operator is; file-backed users.json +
#     credentials.json; emits the `Remote-User` header. A Docker service
#     (it needs the B03 smart-node network + Traefik). Authum never talks
#     to PostgreSQL -- so B05, unlike B04, needs no grant_pg_docker_access.
#   - AuthZ = PostgreSQL. Roles/permissions are relational data and live in
#     the B02 `smartnode` core DB (B05-schema.sql). Product code resolves
#     Remote-User -> role.
#
# The super-admin account is NOT seeded here. Per installer-stages.md
# (canonical) it is created by the operator after B06 via a one-time setup
# token. B05 ships Authum with an empty users.json; B05-schema.sql seeds
# the role catalogue only.
#
# Creates:
#   - the `smart-node-authum` image, built locally from the vendored
#     bootstrap/authum/ snapshot (reproducible, offline-capable; an
#     operator-supplied SMART_NODE_AUTHUM_IMAGE skips the build)
#   - /var/lib/smart-node/authum/{auth,data} -- bind-mounted persistence
#     for users.json, credentials.json (passkeys) and the rate-limit dir,
#     so operator identities survive a container recreate
#   - /var/lib/smart-node/authum/auth/users.json -- empty `{}` (awaiting
#     the B06 setup-token flow)
#   - the `smart-node-authum` container: on the smart-node network, no
#     published port, routed by Traefik docker-provider labels at
#     Host(auth.<domain-or-hostname>), AND carrying the labels that DEFINE
#     the `smart-node-auth` forwardAuth middleware every later web service
#     attaches to gate itself
#   - the identity schema in the B02 core DB (roles / identity_users /
#     user_roles), role catalogue seeded
#   - a Traefik file-provider route for the dashboard (api@internal),
#     protected by the smart-node-auth middleware -- the dashboard B03
#     built but deliberately left unexposed
#   - on a node with a PUBLIC name (SMART_NODE_PUBLIC_DOMAIN in the B03
#     routing contract): the sign-in identity is built from that name, and
#     11-public-auth.yml routes it to Authum -- a file, not a label, so it
#     survives the container recreate this stage performs
#   - /etc/smart-node/identity.env -- the identity contract later stages
#     and companions read (provider, middleware name, auth URL)
#   - "Stage B05 -- identity" section in the install report
#   - /var/lib/smart-node/stages/B05.state
#
# uninstall-from.sh needs no B05-specific handler: the container is a
# `docker-container` manifest entry, the data dirs / dashboard route file /
# identity.env are `dir`/`file` entries, and the identity schema dies with
# the B02 cluster. The locally built image is left behind (harmless; no
# manifest type for images).
#
# Usage:
#   B05-identity.sh            full run: probe -> intent -> probe -> mark
#   B05-identity.sh --verify   idempotent post-check; exit 0 on full pass,
#                              N (fail count) with [VERIFY-FAIL]
#   B05-identity.sh --status   read /var/lib/smart-node/stages/B05.state;
#                              exit 0=done 1=failed 2=missing
#   B05-identity.sh --help     this message
#
# Environment:
#   SMART_NODE_AUTHUM_IMAGE  use a pre-built Authum image instead of
#                            building from the vendored snapshot
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

STAGE_ID="B05"
STAGE_NAME="identity"
STAGE_VERSION="0.3.2"

# Resolve script dir so the vendored authum/ snapshot and the
# B05-schema.sql sidecar can be found alongside this script.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
AUTHUM_SRC_DIR="$SCRIPT_DIR/authum"
SCHEMA_FILE="$SCRIPT_DIR/B05-schema.sql"

# The Authum image. Default is a local tag built from the vendored snapshot,
# and the tag is the snapshot's OWN version -- read here, never repeated by
# hand. This line used to carry the number literally, and it went stale: it
# said 1.0.2 while the vendored source was 1.0.5, so a node built from canon
# ran 1.0.5 under a label announcing 1.0.2. sn-version reads the label, not
# the contents, which put the wrong number in the node's release lock.
# Setting SMART_NODE_AUTHUM_IMAGE supplies a pre-built image and skips the build.
AUTHUM_VENDORED_VERSION="$(cat "$AUTHUM_SRC_DIR/VERSION" 2>/dev/null || true)"
AUTHUM_IMAGE="${SMART_NODE_AUTHUM_IMAGE:-smart-node-authum:$AUTHUM_VENDORED_VERSION}"
AUTHUM_IMAGE_SUPPLIED="${SMART_NODE_AUTHUM_IMAGE:+yes}"

# The B05 container -- namespaced like every Smart Node artifact.
AUTHUM_CT="smart-node-authum"
# Authum's in-container HTTP port (the vendored nginx listens here).
AUTHUM_HTTP_PORT=80
# The forwardAuth middleware Authum defines and later web services use.
AUTH_MIDDLEWARE="smart-node-auth"

# Config + state tree (Layer A / B01 created /etc/smart-node and
# /var/lib/smart-node).
AUTHUM_DATA_DIR="/var/lib/smart-node/authum"
AUTHUM_AUTH_DIR="$AUTHUM_DATA_DIR/auth"
AUTHUM_RT_DIR="$AUTHUM_DATA_DIR/data"
# The uid the vendored Authum image writes its bind-mounted state as (alpine
# nginx/php-fpm). The two leaves below are owned by it instead of being opened
# to the world -- see ensure_dirs.
AUTHUM_CT_UID="${SMART_NODE_AUTHUM_UID:-82}"
USERS_FILE="$AUTHUM_AUTH_DIR/users.json"
IDENTITY_ENV="/etc/smart-node/identity.env"

# Derived routing -- set by derive_site() from the B03 routing contract.
AUTH_HOST=""
DASH_HOST=""
AUTH_URL=""
COOKIE_DOMAIN=""
RP_ID=""
ROUTER_RULE=""
DEFAULT_REDIRECT=""
POLL_HOST=""
# The outside world's auth host, when this node has a second name.
# Empty on a node with one name, which is the common case.
PUB_AUTH_HOST=""

# lib.sh is installed by B01-core; B05 has no sidecar fallback.
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

# ---------- contract + helper functions ----------

# load_contracts -- source the B02 persistence + B03 routing contracts so
# their SMART_NODE_PG_* / SMART_NODE_DOCKER_* / SMART_NODE_DOMAIN values
# are in scope. Tolerates a missing file (verify checks then fail with a
# clear name); require_stage in run mode guarantees both exist.
load_contracts() {
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] && source "$SMART_NODE_PERSISTENCE_ENV"
    [[ -f "$SMART_NODE_ROUTING_ENV"     ]] && source "$SMART_NODE_ROUTING_ENV"
    return 0
}

# derive_site -- compute Authum's own hostname, the dashboard hostname,
# the auth URL, the WebAuthn RP id and cookie domain from the routing
# contract. Authum is always routed by a dedicated `auth.` host; the
# dashboard gets a `dashboard.` host. With a node domain the routers
# opt into the ACME resolver; without one they serve the self-signed
# bootstrap cert.
derive_site() {
    local base="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$base" ]]; then
        base=$(hostname -f 2>/dev/null || hostname 2>/dev/null || true)
        [[ -n "$base" ]] || base="smart-node.local"
    fi
    # The IDENTITY base is the name a browser types. A node can have
    # two: the one it is administered by and the one its customers use. Before
    # this, every value below came from the administrative name, so on a node
    # whose public face lives elsewhere a routine update pointed sign-in at a
    # host reachable only from the VPN, scoped the session cookie to a domain
    # that does not cover the public host, and reset the WebAuthn relying
    # party -- which invalidates every registered passkey. Nothing reported it:
    # every gate on this node reads internal names. Falls back to the node
    # domain, so a node with one name behaves exactly as it did.
    local ident="${SMART_NODE_PUBLIC_DOMAIN:-}"
    [[ -n "$ident" ]] || ident="$base"

    AUTH_HOST="auth.$ident"
    DASH_HOST="dashboard.$base"
    # https, or plain http on a local node (lib.sh web_local)
    WEB="$(web_scheme "$base")"
    AUTH_URL="$WEB://$AUTH_HOST"
    # WebAuthn RP id is the registrable base; the session cookie is shared
    # across auth./dashboard./the panel via the leading-dot parent domain.
    RP_ID="$ident"
    COOKIE_DOMAIN=".$ident"
    # The container's own label keeps routing the INTERNAL name only. The
    # public name gets a dynamic FILE instead (write_public_auth_route), for
    # the reason container labels cannot be a durable identity: a label lives and dies with its container,
    # and the installer recreates this container on every run. A file survives.
    # Belt and braces here -- B05 re-applies its own labels each run -- but the
    # public door is the one whose failure nobody notices, so it gets the
    # durable half.
    ROUTER_RULE="Host(\`auth.$base\`)"
    # Where Authum sends a person after login. The app's own fallback is a
    # placeholder, so always supply it from the node's domain.
    DEFAULT_REDIRECT="$WEB://studio.$base/"   # the Studio
    PUB_AUTH_HOST=""
    [[ "$ident" != "$base" ]] && PUB_AUTH_HOST="auth.$ident"
    # The poll forges a Host header against 127.0.0.1, so it needs a name this
    # node's own Traefik matches without leaving the machine -- and one that
    # does not depend on public DNS having propagated yet.
    POLL_HOST="auth.$base"
}

# ---------- modes ----------

# probe_before -- a re-run (our container present) proceeds idempotently:
# the container is recreated, the schema re-applied, persistence reused.
probe_before() {
    if docker ps -aq -f "name=^${AUTHUM_CT}\$" 2>/dev/null | grep -q .; then
        log "smart-node identity container already present -- idempotent re-run"
    fi
}

# ensure_dirs -- the Authum persistence tree. auth/ + data/ are bind-mounted
# into the container, which writes credentials.json (passkeys) and the
# rate-limit files as in-container uid AUTHUM_CT_UID.
#
# These two leaves were 0777 previously. The comment here used to argue they
# were contained by AUTHUM_DATA_DIR (0750, root:smart-node). That argument was
# void from 2026-07-10: agent users were added to the smart-node group, so they
# traversed the parent freely, and any agent could rewrite the account store
# and replace the passkey file of the identity provider that fronts the public
# door. Demonstrated by action as an agent account on a live node.
#
# Own the leaves by the uid that actually writes them, rather than opening them
# to everyone. The service group keeps write because core-api writes users.json.
ensure_dirs() {
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$AUTHUM_DATA_DIR"
    install -d -m 2770 -o "$AUTHUM_CT_UID" -g "$SMART_NODE_GROUP" "$AUTHUM_AUTH_DIR"
    install -d -m 2770 -o "$AUTHUM_CT_UID" -g "$SMART_NODE_GROUP" "$AUTHUM_RT_DIR"
    manifest_record dir "$AUTHUM_DATA_DIR"
    manifest_record dir "$AUTHUM_AUTH_DIR"
    manifest_record dir "$AUTHUM_RT_DIR"
    # Empty operator store -- B05 ships no accounts; the B06 setup-token
    # flow writes the first one. Never overwrite an existing users.json
    # (that would wipe operators on a re-run). Authum only reads this
    # file -- as the in-container uid, so it must OWN it. core-api
    # (unprivileged smart-node-core-api, in the smart-node service group) must
    # WRITE it from /api/setup/complete, hence group write. No world read: it
    # carries the operator password hashes.
    #
    # It was root:smart-node 0660 for a few hours, which locked Authum out of
    # its own account store: password login returned a PHP warning instead of
    # JSON, while passkey login kept working because credentials.json is
    # already owned by that uid. Owner must be the container, not root.
    if [[ ! -f "$USERS_FILE" ]]; then
        log "writing empty operator store -> $USERS_FILE"
        install -m 0660 -o "$AUTHUM_CT_UID" -g "$SMART_NODE_GROUP" /dev/null "$USERS_FILE"
        printf '{}\n' > "$USERS_FILE"
    else
        log "users.json present -- preserving existing operator store"
    fi
    manifest_record file "$USERS_FILE"
}

# build_authum_image -- build the Authum image from the vendored snapshot.
# An operator-supplied image is used as-is (must already be present). The
# default local tag is built once and reused on re-runs.
build_authum_image() {
    if [[ -n "$AUTHUM_IMAGE_SUPPLIED" ]]; then
        docker image inspect "$AUTHUM_IMAGE" >/dev/null 2>&1 \
            || die "SMART_NODE_AUTHUM_IMAGE=$AUTHUM_IMAGE is not present on the host"
        log "using operator-supplied Authum image $AUTHUM_IMAGE -- build skipped"
        return 0
    fi
    [[ -n "$AUTHUM_VENDORED_VERSION" ]] \
        || die "vendored Authum snapshot has no VERSION file ($AUTHUM_SRC_DIR/VERSION) -- refusing to build an image with an empty tag"
    if docker image inspect "$AUTHUM_IMAGE" >/dev/null 2>&1; then
        log "Authum image $AUTHUM_IMAGE already built"
        return 0
    fi
    [[ -r "$AUTHUM_SRC_DIR/Dockerfile" ]] \
        || die "vendored Authum snapshot missing ($AUTHUM_SRC_DIR/Dockerfile)"
    log "building Authum image $AUTHUM_IMAGE from $AUTHUM_SRC_DIR"
    docker build -t "$AUTHUM_IMAGE" "$AUTHUM_SRC_DIR" >/dev/null \
        || die "failed to build the Authum image"
}

run_authum() {
    # Recreate deterministically (B03/B04 pattern): drop the old container,
    # start a fresh one on the current image. The auth/ + data/ bind-mounts
    # carry operator identities across the recreate.
    if docker ps -aq -f "name=^${AUTHUM_CT}\$" 2>/dev/null | grep -q .; then
        log "removing existing container '$AUTHUM_CT' (will recreate)"
        docker rm -f "$AUTHUM_CT" >/dev/null 2>&1 || true
    fi

    # TLS: with a node domain the router opts into the ACME resolver;
    # without one it serves the self-signed default cert (tls=true).
    local tls_label
    if [[ -n "${SMART_NODE_TLS_RESOLVER:-}" ]]; then
        tls_label="traefik.http.routers.authum.tls.certresolver=${SMART_NODE_TLS_RESOLVER}"
    else
        tls_label="traefik.http.routers.authum.tls=true"
    fi
    web_local && tls_label="smart-node.web=http"
    # A local node: the browser on this machine stays signed in for the whole
    # session lifetime (30 days), not 12 idle hours. Nobody else reaches
    # *.localhost, and the way back in is a launcher on this computer, not a
    # passkey (2026-10-30: a skipped passkey left only a server command).
    local session_env=()
    web_local && session_env=(-e "AUTHUM_SESSION_LIFETIME=2592000")

    log "starting Authum container '$AUTHUM_CT' ($AUTHUM_IMAGE)"
    # The middleware labels DEFINE smart-node-auth (forwardAuth ->
    # Authum's own verify endpoint). Carrying the definition on the
    # Authum container is the upstream Traefik pattern; later web routers
    # reference it as `smart-node-auth@docker`.
    docker run -d \
        --name "$AUTHUM_CT" \
        --restart unless-stopped \
        --network "$SMART_NODE_DOCKER_NETWORK" \
        -e "AUTHUM_RP_NAME=Smart Node" \
        -e "AUTHUM_DOMAIN=$COOKIE_DOMAIN" \
        -e "AUTHUM_RP_ID=$RP_ID" \
        -e "AUTHUM_AUTH_URL=$AUTH_URL" \
        -e "AUTHUM_DEFAULT_REDIRECT=$DEFAULT_REDIRECT"         "${session_env[@]}" \
        -v "$AUTHUM_AUTH_DIR:/app/auth" \
        -v "$AUTHUM_RT_DIR:/app/data" \
        --label "traefik.enable=true" \
        --label "traefik.http.routers.authum.rule=$ROUTER_RULE" \
        --label "traefik.http.routers.authum.entrypoints=$(web_entry)" \
        --label "traefik.http.services.authum.loadbalancer.server.port=$AUTHUM_HTTP_PORT" \
        --label "$tls_label" \
        --label "traefik.http.middlewares.${AUTH_MIDDLEWARE}.forwardauth.address=http://${AUTHUM_CT}/?action=verify" \
        --label "traefik.http.middlewares.${AUTH_MIDDLEWARE}.forwardauth.trustForwardHeader=true" \
        --label "traefik.http.middlewares.${AUTH_MIDDLEWARE}.forwardauth.authResponseHeaders=Remote-User" \
        "$AUTHUM_IMAGE" >/dev/null \
        || die "failed to start the Authum container"
    manifest_record docker-container "$AUTHUM_CT"
}

# apply_identity_schema -- apply B05-schema.sql into the B02 core DB over
# the host-local peer-auth socket, as the postgres OS user (cluster
# superuser). Single transaction; idempotent.
apply_identity_schema() {
    [[ -r "$SCHEMA_FILE" ]] \
        || die "identity schema sidecar not found ($SCHEMA_FILE)"
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" && -n "${SMART_NODE_PG_CORE_DB:-}" ]] \
        || die "persistence contract incomplete -- run B02 first"
    log "applying identity schema -> database $SMART_NODE_PG_CORE_DB"
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d "$SMART_NODE_PG_CORE_DB" -v ON_ERROR_STOP=1 -X -1 -f - \
        < "$SCHEMA_FILE" >/dev/null \
        || die "identity schema apply failed"
}

# write_dashboard_route -- expose the Traefik dashboard (api@internal),
# which B03 built but left unrouted, behind the smart-node-auth
# middleware. A file-provider router: api@internal cannot carry docker
# labels. Cross-provider middleware reference -> `@docker` suffix.
write_dashboard_route() {
    local dir="${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-}"
    [[ -n "$dir" && -d "$dir" ]] \
        || die "routing contract incomplete -- no Traefik dynamic dir (run B03 first)"
    local dst="$dir/10-dashboard.yml"
    # dashboard.<domain> is the sharpest case of a name the installer invents
    #: nobody asks for the Traefik dashboard, it appears because a
    # domain was declared. On this node it never had a record, and because the
    # router carries a certResolver it spent the ACME budget for that name on
    # every reload until the account was rate limited.
    if ! public_route_eligible "$DASH_HOST"; then
        retire_route "B05" "$dst" "$PUBLIC_ROUTE_SKIP_REASON"
        return 0
    fi
    local tls_block
    if [[ -n "${SMART_NODE_TLS_RESOLVER:-}" ]]; then
        tls_block="      tls:
        certResolver: ${SMART_NODE_TLS_RESOLVER}"
    else
        tls_block="      tls: {}"
    fi
    web_local && tls_block="      $(web_tls_yaml)"
    log "writing dashboard route ($dst)"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node Traefik dashboard route -- generated by B05-identity@$STAGE_VERSION.
# Do not edit by hand. Exposes api@internal at https://$DASH_HOST behind
# the $AUTH_MIDDLEWARE forwardAuth middleware (defined on the Authum
# container's docker labels).
http:
  routers:
    smart-node-dashboard:
      rule: "Host(\`$DASH_HOST\`)"
      entryPoints: ["$(web_entry)"]
      service: "api@internal"
      middlewares: ["${AUTH_MIDDLEWARE}@docker"]
$tls_block
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$dst"
    rm -f "$tmp"
    manifest_record file "$dst"
}

# write_public_auth_route -- route the node's PUBLIC auth host to Authum
#, as a dynamic file rather than a container label.
#
# The identity this stage hands Authum (AUTH_URL, RP_ID, cookie domain) is now
# the public one when the node has a second name, so the public host must reach
# this container or sign-in answers 404 at the only address a customer has. The
# container's own label keeps the internal name; this file keeps the public one,
# for the reason recorded in a label comes back carrying only what the
# installer puts there, and the public name disappears with nothing saying so.
#
# TLS is the DEFAULT certificate deliberately, never the ACME resolver. A node
# with a public face is typically fronted by something that already terminates
# TLS for that name, and asking this node to solve an HTTP-01 challenge for a
# host that resolves elsewhere fails quietly and repeatedly. The internal
# routers keep whatever resolver B03 configured.
#
# Removed, not left behind, when the node has one name: a stale public router
# outliving its domain is the same silent-wrong-answer this task is about.
write_public_auth_route() {
    local dir="${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-}"
    [[ -n "$dir" && -d "$dir" ]] \
        || die "routing contract incomplete -- no Traefik dynamic dir (run B03 first)"
    local dst="$dir/11-public-auth.yml"
    if [[ -z "$PUB_AUTH_HOST" ]]; then
        if [[ -e "$dst" ]]; then
            log "no public domain configured -- removing the public auth route ($dst)"
            rm -f "$dst"
        fi
        return 0
    fi
    if ! public_route_eligible "$PUB_AUTH_HOST"; then
        # The sign-in door, so the gate is deliberately the same one: a name
        # with no record cannot receive a browser either way, and the verdict
        # is only reached when the parent domain DOES resolve -- a resolver
        # that cannot answer leaves the door alone.
        retire_route "B05" "$dst" "$PUBLIC_ROUTE_SKIP_REASON"
        return 0
    fi
    log "writing public auth route for $PUB_AUTH_HOST ($dst)"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Smart Node public auth route -- generated by B05-identity@$STAGE_VERSION.
# Do not edit by hand. This node answers to two names: ${SMART_NODE_DOMAIN:-(none)}
# internally and $PUB_AUTH_HOST on the outside, and the sign-in identity is
# built from the public one. A FILE and not a container label: the installer
# recreates the Authum container on every run.
#
# No middleware: auth. is the door. Everything else sits behind it.
http:
  routers:
    smart-node-public-auth:
      rule: "Host(\`$PUB_AUTH_HOST\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-public-auth"
      $(web_tls_yaml)

  services:
    smart-node-public-auth:
      loadBalancer:
        servers: [{url: "http://${AUTHUM_CT}:${AUTHUM_HTTP_PORT}"}]
        passHostHeader: true
EOF
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$dst"
    rm -f "$tmp"
    manifest_record file "$dst"
}

write_identity_contract() {
    # The identity contract -- the B05 parallel to routing.env / vault.env.
    # Later stages + the B06 web interface read it to wire SSO. Not secret
    # (0644): no credentials, only names and URLs.
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node identity contract -- generated by B05-identity@$STAGE_VERSION."
        echo "# Do not edit by hand. Sourced by later stages and the B06 web interface."
        echo "SMART_NODE_AUTH_PROVIDER=authum"
        echo "SMART_NODE_AUTH_CONTAINER=$AUTHUM_CT"
        echo "SMART_NODE_AUTH_MIDDLEWARE=${AUTH_MIDDLEWARE}@docker"
        echo "SMART_NODE_AUTH_URL=$AUTH_URL"
        echo "SMART_NODE_AUTH_VERIFY_URL=http://$AUTHUM_CT/?action=verify"
        echo "SMART_NODE_AUTH_USERS_FILE=$USERS_FILE"
        echo "SMART_NODE_DASHBOARD_URL=$(web_scheme)://$DASH_HOST"
    } > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$IDENTITY_ENV"
    rm -f "$tmp"
    manifest_record file "$IDENTITY_ENV"
}

wait_for_authum() {
    # Authum is a small PHP app -- no DB migrations, it answers in seconds
    # once the container is up and Traefik has read its labels. An
    # unauthenticated GET / returns the 200 login page. Poll through
    # Traefik against a 120s deadline.
    local deadline=$(( $(date +%s) + 120 )) code
    log "waiting for Authum to answer through Traefik"
    while (( $(date +%s) < deadline )); do
        code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H "Host: $POLL_HOST" "$(web_scheme)://127.0.0.1/" 2>/dev/null || true)
        if [[ "$code" =~ ^[23] ]]; then
            log "Authum is up (HTTP $code)"
            return 0
        fi
        sleep 3
    done
    die "Authum did not become ready within 120s (container '$AUTHUM_CT')"
}

write_report_section() {
    local route_note
    if [[ -n "${SMART_NODE_DOMAIN:-}" ]]; then
        route_note="ACME cert ($SMART_NODE_TLS_RESOLVER)"
    else
        route_note="self-signed bootstrap cert (no domain set)"
    fi
    local body
    body=$(cat <<EOF
**Version:** B05-identity@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
sso:        Authum  ($AUTHUM_CT, $AUTHUM_IMAGE, no published port)
auth URL:   $AUTH_URL
dashboard:  https://$DASH_HOST  (api@internal, behind $AUTH_MIDDLEWARE)
middleware: ${AUTH_MIDDLEWARE}@docker  (forwardAuth -- later web routes attach this)
tls:        $route_note
authz:      roles / identity_users / user_roles in the B02 core DB
operators:  none -- empty users.json; super-admin created via the B06 setup token
persist:    $AUTHUM_DATA_DIR  (auth/ + data/ bind-mounts -- passkeys survive recreate)
contract:   $IDENTITY_ENV
\`\`\`

Authum (AuthN -- WebAuthn/passkey + password) proves operator identity and
emits \`Remote-User\`; roles (AuthZ) live in the core DB. No operator
accounts are seeded here -- the super-admin is created after B06 via a
one-time setup token. External email/call/chat contacts are a Phase 2
concern, not part of this identity layer.
EOF
)
    report_section "Stage B05 -- identity" "$body"
}

run_intent() {
    require_root
    log "stage B05 (identity) -- start"
    require_stage B02
    require_stage B03
    require_stage B04
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    [[ -n "${SMART_NODE_DOCKER_NETWORK:-}" ]] \
        || die "routing contract incomplete -- no SMART_NODE_DOCKER_NETWORK (run B03 first)"
    command -v docker >/dev/null 2>&1 \
        || die "docker not available -- B03 should have installed it"
    derive_site
    probe_before
    ensure_dirs
    build_authum_image
    run_authum
    apply_identity_schema
    write_dashboard_route
    write_public_auth_route
    write_identity_contract
    wait_for_authum
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=B05-identity@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=B05-identity@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "stage.done" "stage" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"sso\":\"authum\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "stage B05 (identity) -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    local cluster="${SMART_NODE_PG_CLUSTER:-}" coredb="${SMART_NODE_PG_CORE_DB:-}"
    # 1-3. preconditions.
    verify_check "stage B02 done" require_stage B02
    verify_check "stage B03 done" require_stage B03
    verify_check "stage B04 done" require_stage B04
    # 4. routing network exists.
    verify_check "docker network ${SMART_NODE_DOCKER_NETWORK:-smart-node} exists" \
        docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}"
    # 5. Authum image present.
    verify_check "Authum image $AUTHUM_IMAGE present" \
        docker image inspect "$AUTHUM_IMAGE"
    # 6. container exists.
    verify_check "container $AUTHUM_CT exists" bash -c "
        docker ps -aq -f 'name=^${AUTHUM_CT}\$' 2>/dev/null | grep -q ."
    # 7. container running.
    verify_check "container $AUTHUM_CT running" bash -c "
        [[ \$(docker inspect -f '{{.State.Running}}' '$AUTHUM_CT' 2>/dev/null) == true ]]"
    # 8. container runs the expected image.
    verify_check "container $AUTHUM_CT uses image $AUTHUM_IMAGE" bash -c "
        [[ \$(docker inspect -f '{{.Config.Image}}' '$AUTHUM_CT' 2>/dev/null) == '$AUTHUM_IMAGE' ]]"
    # 9. publishes no host port.
    verify_check "$AUTHUM_CT publishes no port" bash -c "
        [[ -z \$(docker port '$AUTHUM_CT' 2>/dev/null) ]]"
    # 10. persistence dirs exist.
    verify_check "Authum persistence dirs exist" bash -c "
        [[ -d '$AUTHUM_AUTH_DIR' && -d '$AUTHUM_RT_DIR' ]]"
    # 11. operator store present.
    verify_check "operator store users.json present" test -f "$USERS_FILE"
    # 12. the forwardAuth middleware is defined on the container labels.
    verify_check "forwardAuth middleware $AUTH_MIDDLEWARE defined" bash -c "
        [[ -n \$(docker inspect -f \
            '{{ index .Config.Labels \"traefik.http.middlewares.${AUTH_MIDDLEWARE}.forwardauth.address\" }}' \
            '$AUTHUM_CT' 2>/dev/null) ]]"
    # 13. Authum answers the login page through Traefik.
    verify_check "Authum answers through Traefik" bash -c "
        code=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H 'Host: ${POLL_HOST:-localhost}' $(web_scheme)://127.0.0.1/ 2>/dev/null)
        [[ \"\$code\" =~ ^[23] ]]"
    # 14. dashboard route file written -- but only on a node that can serve
    #     the name. It asks the writer's own question; before this it
    #     demanded the file unconditionally, so a node whose dashboard.<domain>
    #     has no record could only pass by keeping a router nobody can reach.
    if public_route_eligible "$DASH_HOST"; then
        verify_check "dashboard route file written" \
            test -r "${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-/nonexistent}/10-dashboard.yml"
    else
        verify_check "no dashboard route this node cannot serve" \
            bash -c "[[ ! -f \"${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-/nonexistent}/10-dashboard.yml\" ]]"
    fi
    # 14a. the container ANNOUNCES the identity this stage intends.
    #      This is the check whose absence let a routine update replace a
    #      working public identity with a VPN-only one while every other gate
    #      reported a pass: they all read internal names, and this reads the
    #      one value a browser is sent to.
    verify_check "Authum announces $AUTH_URL" bash -c "
        [[ \$(docker inspect -f \
            '{{range .Config.Env}}{{println .}}{{end}}' '$AUTHUM_CT' 2>/dev/null \
            | sed -n 's/^AUTHUM_AUTH_URL=//p') == '$AUTH_URL' ]]"
    # 14b-c. only on a node that HAS a public name -- a check that passes
    #        vacuously on every other node would be worse than none.
    if [[ -n "$PUB_AUTH_HOST" ]] && public_route_eligible "$PUB_AUTH_HOST"; then
        verify_check "public auth route file written" \
            test -r "${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-/nonexistent}/11-public-auth.yml"
        verify_check "Authum answers on $PUB_AUTH_HOST" bash -c "
            code=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
                -H 'Host: $PUB_AUTH_HOST' https://127.0.0.1/ 2>/dev/null)
            [[ \"\$code\" =~ ^[23] ]]"
    elif [[ -n "$PUB_AUTH_HOST" ]]; then
        # The writer retired the route for a name with no record;
        # demanding the file here would fail the node for doing that.
        verify_check "no public auth route this node cannot serve" \
            bash -c "[[ ! -f \"${SMART_NODE_TRAEFIK_DYNAMIC_DIR:-/nonexistent}/11-public-auth.yml\" ]]"
    fi
    # 15. dashboard is gated -- an unauthenticated request is bounced by
    #     the forwardAuth middleware (302 to login / 401). A 2xx here
    #     would mean the dashboard is exposed unauthenticated.
    #     Asked only where the route exists. The writer retires it for a name
    #     with no record, and while this check still demanded it every
    #     update of such a node stopped at B05 (release 1.12.36 stand).
    if public_route_eligible "$DASH_HOST"; then
        verify_check "dashboard route gated by $AUTH_MIDDLEWARE" bash -c "
            code=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
                -H 'Host: ${DASH_HOST:-localhost}' $(web_scheme)://127.0.0.1/dashboard/ 2>/dev/null)
            [[ \"\$code\" == 302 || \"\$code\" == 401 ]]"
    fi
    # 16-18. identity schema tables exist.
    local t
    for t in roles identity_users user_roles; do
        verify_check "identity table $t exists" bash -c "
            [[ \$(runuser -u postgres -- psql --cluster '$cluster' -d '$coredb' -tAc \
                \"SELECT to_regclass('public.$t') IS NOT NULL\" 2>/dev/null) == t ]]"
    done
    # 19. the role catalogue is seeded (super-admin present).
    verify_check "role catalogue seeded (super-admin)" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$cluster' -d '$coredb' -tAc \
            \"SELECT 1 FROM roles WHERE id='super-admin'\" 2>/dev/null) == 1 ]]"
    # 20. identity contract written.
    verify_check "identity.env written" test -r "$IDENTITY_ENV"
    # 21. install-report has the B05 section.
    verify_check "install-report has Stage B05 section" \
        bash -c "grep -qFx '## Stage B05 -- identity' '$SMART_NODE_REPORT'"
    # 22. manifest records the B05 container.
    verify_check "manifest records the Authum container" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"docker-container\",\"value\":\"$AUTHUM_CT\",' \"\$m\""
}

run_verify_mode() {
    log_init "$STAGE_ID"
    load_contracts
    derive_site
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
