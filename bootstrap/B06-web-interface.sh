#!/bin/bash
#
# B06-web-interface.sh -- Smart Node installer, Layer C bootstrap stage B06.
#
# Runs INSIDE the target host as root. Idempotent. Depends on B03 + B05.
# The final Phase-1 bootstrap stage: after B06 the node has a working
# control panel and the operator continues in the browser.
#
# Installs the Smart Node control panel (smart-node-ui) -- a PHP web app,
# built from the vendored bootstrap/smart-node-ui/ snapshot. The panel:
#   - shows node status (Server page: bootstrap stages, companions,
#     install report);
#
# B06 owns the AuthN/AuthZ split for the super-admin:
#   - AuthZ: this stage seeds the super-admin's rows in the B02 core DB
#     (identity_users / user_roles / actors) over local peer auth -- the
#     username is fixed (the GitLab "root" pattern), display name preset.
#   - AuthN: this stage generates a one-time setup token; Studio's first-run
#     page (/welcome) presents it, core-api writes the Authum users.json entry,
#     consumes the token and hands the person their password once.
#     The panel had its own /setup form for this until 2026-11-07.
#
# Routing: the panel runs on the smart-node network, no published port,
# routed by Traefik at Host(panel.<domain-or-hostname>). Every route
# carries the B05 smart-node-auth forwardAuth middleware EXCEPT the panel's
# own static files (/ui, /assets): a second, higher-priority router omits it
# for those, which is also what the readiness poll reads.
#
# Creates:
#   - the smart-node-ui image, built locally from the vendored snapshot
#     (an operator-supplied SMART_NODE_UI_IMAGE skips the build)
#   - the super-admin AuthZ rows in the B02 core DB
#   - /var/lib/smart-node/panel -- the panel data dir; holds the one-time
#     setup-token file (deleted by the panel when setup completes)
#   - the smart-node-panel container, Traefik-routed, behind smart-node-auth
#   - "Stage B06 -- web-interface" section in the install report
#   - /var/lib/smart-node/stages/B06.state
#
# uninstall-from.sh needs no B06-specific handler: the container is a
# docker-container manifest entry, the panel data dir a dir entry, the
# image is left behind (no manifest type), and the DB rows die with the
# B02 cluster.
#
# Usage:
#   B06-web-interface.sh            full run: probe -> intent -> probe -> mark
#   B06-web-interface.sh --verify   idempotent post-check; exit 0 / N
#   B06-web-interface.sh --status   read /var/lib/smart-node/stages/B06.state
#   B06-web-interface.sh --help     this message
#
# Environment:
#   SMART_NODE_UI_IMAGE         use a pre-built panel image (skip build)
#   SMART_NODE_SUPERADMIN_USER  super-admin username (default admin)
#   SMART_NODE_OWNER_PASSWORD   seed the super-admin password into Authum (AuthN),
#                               so the node is born logged-in-able and no /setup
#                               token is emitted. Set by install-on.sh
#                               --owner-password. Idempotent; existing operator
#                               preserved.
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

STAGE_ID="B06"
STAGE_NAME="web-interface"
STAGE_VERSION="0.6.2"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
UI_SRC_DIR="$SCRIPT_DIR/smart-node-ui"

# The panel image. Default is a local tag built from the vendored snapshot,
# and the tag is the snapshot's OWN version -- read here, never repeated by
# hand. The same line in B05 carried its number literally and drifted two
# releases behind the source it built, which is how a label ends up naming
# something other than what is inside it.
# SMART_NODE_UI_IMAGE supplies a pre-built image instead.
UI_VENDORED_VERSION="$(cat "$UI_SRC_DIR/VERSION" 2>/dev/null || true)"
UI_IMAGE="${SMART_NODE_UI_IMAGE:-smart-node-ui:$UI_VENDORED_VERSION}"
UI_IMAGE_SUPPLIED="${SMART_NODE_UI_IMAGE:+yes}"

PANEL_CT="smart-node-panel"
PANEL_HTTP_PORT=80

# The fixed super-admin username (GitLab "root" pattern). Validated below.
SUPERADMIN_USER="${SMART_NODE_SUPERADMIN_USER:-admin}"
SUPERADMIN_NAME="Super Administrator"
# Optional AuthN seed: when set (install-on.sh --owner-password), B06 writes the
# super-admin password into the Authum store so the node is born logged-in-able
# and no /setup token is emitted. Empty -> the /setup token flow owns AuthN.
SUPERADMIN_PASSWORD="${SMART_NODE_OWNER_PASSWORD:-}"

PANEL_DATA_DIR="/var/lib/smart-node/panel"
SETUP_TOKEN_FILE="$PANEL_DATA_DIR/setup-token"
# The uid the panel / authum containers write bind-mounted state as. The dirs
# below are owned by it instead of being world-writable.
PANEL_CT_UID="${SMART_NODE_PANEL_UID:-82}"

# Where sn-panel-data.sh writes the runtime JSON snapshots (overview/events/
# messages/...). Mounted read-only into the panel webroot so nginx serves
# /data/*.json. Must match OUTDIR in host-tools/sn-panel-data.sh.
PANEL_RENDER_DIR="/srv/smart-node-hq/data"

# Derived -- set by derive_site() / load_contracts().
PANEL_HOST=""
OLD_PANEL_HOST=""
ROUTER_RULE=""
POLL_HOST=""
AUTH_MIDDLEWARE=""
AUTH_USERS_FILE=""
AUTH_AUTH_DIR=""
SETUP_DONE=0

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
    "")        MODE="run" ;;
    --verify)  MODE="verify" ;;
    --status)  MODE="status" ;;
    *)         die "unknown arg: $1 (use --help)" ;;
esac

[[ "$SUPERADMIN_USER" =~ ^[a-z][a-z0-9_-]*$ ]] \
    || die "invalid SMART_NODE_SUPERADMIN_USER '$SUPERADMIN_USER' (lowercase letter, then alnum/_/-)"

# ---------- contract + helpers ----------

# load_contracts -- source the B02 persistence + B03 routing + B05
# identity contracts. Tolerates a missing file (verify checks then fail
# with a clear name); require_stage in run mode guarantees they exist.
load_contracts() {
    [[ -f "$SMART_NODE_PERSISTENCE_ENV" ]] && source "$SMART_NODE_PERSISTENCE_ENV"
    [[ -f "$SMART_NODE_ROUTING_ENV"     ]] && source "$SMART_NODE_ROUTING_ENV"
    [[ -f /etc/smart-node/identity.env  ]] && source /etc/smart-node/identity.env
    AUTH_MIDDLEWARE="${SMART_NODE_AUTH_MIDDLEWARE:-smart-node-auth@docker}"
    AUTH_USERS_FILE="${SMART_NODE_AUTH_USERS_FILE:-/var/lib/smart-node/authum/auth/users.json}"
    AUTH_AUTH_DIR="$(dirname "$AUTH_USERS_FILE")"
    return 0
}

# derive_site -- the panel hostname + router rule from the routing
# contract, mirroring B05's auth.<host>.
derive_site() {
    local base="${SMART_NODE_DOMAIN:-}"
    if [[ -z "$base" ]]; then
        base=$(hostname -f 2>/dev/null || hostname 2>/dev/null || true)
        [[ -n "$base" ]] || base="smart-node.local"
    fi
    # the admin console is admin.<domain>; panel.<domain> redirects to it
    PANEL_HOST="admin.$base"
    OLD_PANEL_HOST="panel.$base"
    ROUTER_RULE="Host(\`$PANEL_HOST\`)"
    POLL_HOST="$PANEL_HOST"
}

# psql_core -- psql against the B02 core DB as the postgres OS user.
psql_core() {
    runuser -u postgres -- psql --cluster "$SMART_NODE_PG_CLUSTER" \
        -d "$SMART_NODE_PG_CORE_DB" -v ON_ERROR_STOP=1 -X "$@"
}

# superadmin_exists -- true once the first-run flow has written the
# super-admin into the Authum users.json (AuthN side complete).
superadmin_exists() {
    [[ -f "$AUTH_USERS_FILE" ]] \
        && grep -q "\"$SUPERADMIN_USER\"" "$AUTH_USERS_FILE" 2>/dev/null
}

# ---------- modes ----------

probe_before() {
    if docker ps -aq -f "name=^${PANEL_CT}\$" 2>/dev/null | grep -q .; then
        log "smart-node panel container already present -- idempotent re-run"
    fi
}

# ensure_dirs -- the panel data dir. The panel container, a non-root uid, must
# delete the setup token from here when the first-run flow completes.
#
# Was 0777 previously, justified as contained by the 0750 /var/lib/smart-node
# parent. That containment ended when agents joined the smart-node group
# and could traverse it. Own it by the container uid instead.
ensure_dirs() {
    install -d -m 2770 -o "$PANEL_CT_UID" -g "$SMART_NODE_GROUP" "$PANEL_DATA_DIR"
    manifest_record dir "$PANEL_DATA_DIR"
}

build_ui_image() {
    if [[ -n "$UI_IMAGE_SUPPLIED" ]]; then
        docker image inspect "$UI_IMAGE" >/dev/null 2>&1 \
            || die "SMART_NODE_UI_IMAGE=$UI_IMAGE is not present on the host"
        log "using operator-supplied panel image $UI_IMAGE -- build skipped"
        return 0
    fi
    [[ -n "$UI_VENDORED_VERSION" ]] \
        || die "vendored smart-node-ui snapshot has no VERSION file ($UI_SRC_DIR/VERSION) -- refusing to build an image with an empty tag"
    if docker image inspect "$UI_IMAGE" >/dev/null 2>&1; then
        log "panel image $UI_IMAGE already built"
        return 0
    fi
    [[ -r "$UI_SRC_DIR/Dockerfile" ]] \
        || die "vendored smart-node-ui snapshot missing ($UI_SRC_DIR/Dockerfile)"
    log "building panel image $UI_IMAGE from $UI_SRC_DIR"
    docker build -t "$UI_IMAGE" "$UI_SRC_DIR" >/dev/null \
        || die "failed to build the panel image"
}

# seed_superadmin_authz -- the AuthZ half of the super-admin: its rows in
# the B02 core DB. The AuthN half (the password) is set by the operator
# through the first-run flow. Idempotent (ON CONFLICT DO NOTHING).
seed_superadmin_authz() {
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" && -n "${SMART_NODE_PG_CORE_DB:-}" ]] \
        || die "persistence contract incomplete -- run B02 first"
    log "seeding super-admin AuthZ rows for '$SUPERADMIN_USER'"
    psql_core >/dev/null <<SQL || die "failed to seed super-admin AuthZ rows"
SET ROLE smartnode;
INSERT INTO actors (id, display_name, type, layer)
  VALUES ('human:$SUPERADMIN_USER', '$SUPERADMIN_NAME', 'operator', 'human')
  ON CONFLICT (id) DO NOTHING;
INSERT INTO identity_users (username, display_name, actor_id)
  VALUES ('$SUPERADMIN_USER', '$SUPERADMIN_NAME', 'human:$SUPERADMIN_USER')
  ON CONFLICT (username) DO NOTHING;
INSERT INTO user_roles (username, role_id)
  VALUES ('$SUPERADMIN_USER', 'super-admin')
  ON CONFLICT DO NOTHING;
SQL
}

# seed_superadmin_authn -- the AuthN half: write the super-admin password into
# the Authum users.json so the node is born able to log in with no manual
# /setup step. Gated on SMART_NODE_OWNER_PASSWORD (install-on.sh
# --owner-password); absent it this is a no-op and ensure_setup_token emits the
# one-time /setup token instead. Idempotent: an existing operator entry is
# preserved (never resets a password on re-run, and never clobbers a /setup
# that already ran). bcrypt via the Authum container's PHP -- the same recipe
# operator-seed recipe uses; Authum is up from B05.
# Sets SETUP_DONE so ensure_setup_token skips the token and clears any stale one.
seed_superadmin_authn() {
    [[ -n "$SUPERADMIN_PASSWORD" ]] || return 0
    if superadmin_exists; then
        log "super-admin '$SUPERADMIN_USER' already in the Authum store -- preserving (no password reset)"
        SETUP_DONE=1
        return 0
    fi
    local ct="${SMART_NODE_AUTH_CONTAINER:-smart-node-authum}"
    # Fallback image only (the line below prefers `docker exec` into B05's running
    # container). Do NOT pin a version here: this defaulted to 1.0.0 while B05 shipped
    # 1.0.2, so the fallback would have asked docker for an image the host does not
    # have, docker would try the public registry, and the failure would surface as the
    # misleading "Authum container not up? run B05 first". Resolve what is actually
    # present; B05 stays the only place that decides which authum a node carries.
    local img="${SMART_NODE_AUTHUM_IMAGE:-$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^smart-node-authum:' | sort -V | tail -1)}"
    log "seeding super-admin '$SUPERADMIN_USER' password into the Authum store (--owner-password)"
    local hash
    hash=$(docker exec -e P="$SUPERADMIN_PASSWORD" "$ct" \
             php -r 'echo password_hash(getenv("P"), PASSWORD_BCRYPT);' 2>/dev/null) \
      || hash=$(docker run --rm --entrypoint php -e P="$SUPERADMIN_PASSWORD" "$img" \
             -r 'echo password_hash(getenv("P"), PASSWORD_BCRYPT);' 2>/dev/null)
    [[ "$hash" == \$2* ]] \
        || die "failed to bcrypt the owner password (Authum container '$ct' not up? run B05 first)"
    # Owned by the container uid, group-writable for core-api. Was 0777.
    install -d -m 2770 -o "$PANEL_CT_UID" -g "$SMART_NODE_GROUP" "$AUTH_AUTH_DIR"
    SN_U="$SUPERADMIN_USER" SN_H="$hash" SN_F="$AUTH_USERS_FILE" python3 - <<'PY' \
        || die "failed to write the operator into $AUTH_USERS_FILE"
import json, os, datetime
u, h, f = os.environ["SN_U"], os.environ["SN_H"], os.environ["SN_F"]
try:
    d = json.load(open(f))
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}
d[u] = {"password": h, "role": "superadmin",
        "created": datetime.datetime.now().astimezone().isoformat()}
json.dump(d, open(f, "w"), indent=2)
PY
    # Authum only reads users.json; keep it 0664 root:smart-node (group write,
    # world read) per the B05 contract, so the core-api /api/setup/complete flow
    # can also write it. Not world-writable.
    chown "root:$SMART_NODE_GROUP" "$AUTH_USERS_FILE" 2>/dev/null || true
    chmod 0664 "$AUTH_USERS_FILE"
    SETUP_DONE=1
    log "super-admin '$SUPERADMIN_USER' password seeded -- node is born logged-in-able"
}

# ensure_setup_token -- the one-time token for the first-run flow.
# If the super-admin already exists (a re-run after setup completed),
# there is nothing to do and a stale token is cleared. Otherwise an
# existing token is kept and a missing one generated.
ensure_setup_token() {
    if superadmin_exists; then
        SETUP_DONE=1
        rm -f "$SETUP_TOKEN_FILE"
        log "super-admin '$SUPERADMIN_USER' already exists -- setup complete, no token"
        return 0
    fi
    if [[ -s "$SETUP_TOKEN_FILE" ]]; then
        log "setup token already present -- keeping it"
        return 0
    fi
    command -v openssl >/dev/null 2>&1 || die "openssl not found (B03 installs it)"
    local tmp; tmp=$(mktemp)
    openssl rand -hex 16 > "$tmp"
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$tmp" "$SETUP_TOKEN_FILE"
    rm -f "$tmp"
    log "generated one-time setup token -> $SETUP_TOKEN_FILE"
}

run_panel() {
    if docker ps -aq -f "name=^${PANEL_CT}\$" 2>/dev/null | grep -q .; then
        log "removing existing container '$PANEL_CT' (will recreate)"
        docker rm -f "$PANEL_CT" >/dev/null 2>&1 || true
    fi

    # TLS: ACME resolver with a node domain, self-signed default cert
    # without. Both panel routers need the label.
    local tls_main tls_setup tls_old tls_app
    if [[ -n "${SMART_NODE_TLS_RESOLVER:-}" ]]; then
        tls_main="traefik.http.routers.snpanel.tls.certresolver=${SMART_NODE_TLS_RESOLVER}"
        tls_setup="traefik.http.routers.snpanelsetup.tls.certresolver=${SMART_NODE_TLS_RESOLVER}"
        tls_old="traefik.http.routers.snpanelold.tls.certresolver=${SMART_NODE_TLS_RESOLVER}"
        tls_app="traefik.http.routers.snpanelapp.tls.certresolver=${SMART_NODE_TLS_RESOLVER}"
    else
        tls_main="traefik.http.routers.snpanel.tls=true"
        tls_setup="traefik.http.routers.snpanelsetup.tls=true"
        tls_old="traefik.http.routers.snpanelold.tls=true"
        tls_app="traefik.http.routers.snpanelapp.tls=true"
    fi
    # a local node: plain http, a no-op label where the tls one was (lib.sh web_local)
    if web_local; then tls_main="smart-node.web=http"; tls_setup="$tls_main"; tls_old="$tls_main"; tls_app="$tls_main"; fi

    log "starting panel container '$PANEL_CT' ($UI_IMAGE)"
    # Two routers, one service. `snpanel` carries the smart-node-auth
    # middleware -- the whole panel is gated. `snpanelsetup` (the name is
    # older than its job) matches the static files at a higher priority and
    # omits the middleware. Bind-mounts: the node state read-only; the Authum
    # auth dir + the panel data dir read-write.
    docker run -d \
        --name "$PANEL_CT" \
        --restart unless-stopped \
        --network "$SMART_NODE_DOCKER_NETWORK" \
        --add-host host.docker.internal:host-gateway \
        -e "SMART_NODE_SUPERADMIN_USER=$SUPERADMIN_USER" \
        -e "SMART_NODE_SETUP_TOKEN_FILE=$SETUP_TOKEN_FILE" \
        -e "SMART_NODE_AUTH_USERS_FILE=$AUTH_USERS_FILE" \
        -v "/etc/smart-node:/etc/smart-node:ro" \
        -v "/var/lib/smart-node/stages:/var/lib/smart-node/stages:ro" \
        -v "$AUTH_AUTH_DIR:$AUTH_AUTH_DIR" \
        -v "$PANEL_DATA_DIR:$PANEL_DATA_DIR" \
        -v "$PANEL_RENDER_DIR:/usr/share/nginx/html/data:ro" \
        --label "traefik.enable=true" \
        --label "traefik.http.routers.snpanel.rule=$ROUTER_RULE" \
        --label "traefik.http.routers.snpanel.entrypoints=$(web_entry)" \
        --label "traefik.http.routers.snpanel.service=snpanel" \
        --label "traefik.http.routers.snpanel.middlewares=$AUTH_MIDDLEWARE" \
        --label "$tls_main" \
        --label "traefik.http.routers.snpanelsetup.rule=($ROUTER_RULE) && (PathPrefix(\`/ui\`) || PathPrefix(\`/assets\`))" \
        --label "traefik.http.routers.snpanelsetup.entrypoints=$(web_entry)" \
        --label "traefik.http.routers.snpanelsetup.service=snpanel" \
        --label "traefik.http.routers.snpanelsetup.priority=200" \
        --label "$tls_setup" \
        --label "traefik.http.routers.snpanelapp.rule=($ROUTER_RULE) && (Path(\`/manifest.webmanifest\`) || Path(\`/sw.js\`) || Path(\`/offline.html\`) || PathPrefix(\`/icons/\`))" \
        --label "traefik.http.routers.snpanelapp.entrypoints=$(web_entry)" \
        --label "traefik.http.routers.snpanelapp.service=snpanel" \
        --label "traefik.http.routers.snpanelapp.priority=200" \
        --label "$tls_app" \
        --label "traefik.http.routers.snpanelold.rule=Host(\`$OLD_PANEL_HOST\`)" \
        --label "traefik.http.routers.snpanelold.entrypoints=$(web_entry)" \
        --label "traefik.http.routers.snpanelold.service=snpanel" \
        --label "traefik.http.routers.snpanelold.middlewares=snpanel-to-admin" \
        --label "$tls_old" \
        --label "traefik.http.middlewares.snpanel-to-admin.redirectregex.regex=^https?://panel\\.(.*)" \
        --label "traefik.http.middlewares.snpanel-to-admin.redirectregex.replacement=$(web_scheme)://admin.\${1}" \
        --label "traefik.http.middlewares.snpanel-to-admin.redirectregex.permanent=true" \
        --label "traefik.http.services.snpanel.loadbalancer.server.port=$PANEL_HTTP_PORT" \
        "$UI_IMAGE" >/dev/null \
        || die "failed to start the panel container"
    manifest_record docker-container "$PANEL_CT"
}

# install_panel_data_renderer -- install sn-panel-data.sh + its systemd
# unit + timer. The panel JS at /data/*.json reads snapshots this script
# writes; without it, the panel loads HTML/CSS but stays empty.
#
# Files come from the vendored smart-node-ui snapshot (host-tools/ and
# systemd/ subdirs of $UI_SRC_DIR). Idempotent re-run: install -m 0755
# overwrites if changed.
install_panel_data_renderer() {
    local renderer_src="$UI_SRC_DIR/host-tools/sn-panel-data.sh"
    local svc_src="$UI_SRC_DIR/systemd/sn-panel-data.service"
    local timer_src="$UI_SRC_DIR/systemd/sn-panel-data.timer"

    [[ -f "$renderer_src" ]] \
        || die "panel data renderer missing in vendor ($renderer_src)"
    [[ -f "$svc_src" && -f "$timer_src" ]] \
        || die "panel data renderer systemd units missing in vendor ($svc_src, $timer_src)"

    # The renderer's OUTDIR; pre-create so the panel container's read-only
    # mount has a source and nginx can serve /data/ from first boot.
    install -d -m 0755 -o root -g root "$PANEL_RENDER_DIR"

    install -m 0755 -o root -g root "$renderer_src"  /usr/local/sbin/sn-panel-data.sh
    install -m 0644 -o root -g root "$svc_src"       /etc/systemd/system/sn-panel-data.service
    install -m 0644 -o root -g root "$timer_src"     /etc/systemd/system/sn-panel-data.timer
    manifest_record host-script   /usr/local/sbin/sn-panel-data.sh
    manifest_record systemd-unit  sn-panel-data.service
    manifest_record systemd-unit  sn-panel-data.timer

    systemctl daemon-reload
    systemctl enable --now sn-panel-data.timer >/dev/null 2>&1 \
        || die "systemctl enable --now sn-panel-data.timer failed"

    # Render once synchronously so the panel has data before wait_for_panel
    # starts polling. The oneshot service is short (well under 30s).
    systemctl start sn-panel-data.service >/dev/null 2>&1 || true
    log "panel data renderer + timer installed (sn-panel-data.timer enabled)"
}

wait_for_panel() {
    # The panel is a small static site -- it answers in seconds once
    # Traefik has read its labels. Poll a static file through Traefik on the
    # route without sign-in: 2xx means the route and the container are healthy.
    local deadline=$(( $(date +%s) + 120 )) code
    log "waiting for the panel to answer through Traefik"
    while (( $(date +%s) < deadline )); do
        code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H "Host: $POLL_HOST" "$(web_scheme)://127.0.0.1/assets/shell.css" 2>/dev/null || true)
        if [[ "$code" =~ ^[23] ]]; then
            log "panel is up (HTTP $code)"
            return 0
        fi
        sleep 3
    done
    die "panel did not become ready within 120s (container '$PANEL_CT')"
}

write_report_section() {
    local route_note setup_note
    if [[ -n "${SMART_NODE_DOMAIN:-}" ]]; then
        route_note="ACME cert (${SMART_NODE_TLS_RESOLVER:-})"
    else
        route_note="self-signed bootstrap cert (no domain set)"
    fi
    if (( SETUP_DONE )); then
        setup_note="super-admin '$SUPERADMIN_USER' configured -- log in at $(web_scheme)://$PANEL_HOST"
    else
        setup_note="first run pending -- the installer prints Studio's first-run link (it carries the one-time token)"
    fi
    local body
    body=$(cat <<EOF
**Version:** B06-web-interface@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
panel:      smart-node-ui  ($PANEL_CT, $UI_IMAGE, no published port)
URL:        $(web_scheme)://$PANEL_HOST
gating:     $AUTH_MIDDLEWARE on every route but the static files (/ui, /assets)
tls:        $route_note
super-admin: $SUPERADMIN_USER  (AuthZ rows seeded in the core DB)
\`\`\`

$setup_note

Phase 1 (bootstrap) is complete. The owner is created on Studio's first-run
page once the companions are installed (install-on.sh --all).
EOF
)
    report_section "Stage B06 -- web-interface" "$body"
}

run_intent() {
    require_root
    log "stage B06 (web-interface) -- start"
    require_stage B03
    require_stage B05
    log_init "$STAGE_ID"
    load_contracts
    [[ -n "${SMART_NODE_DOCKER_NETWORK:-}" ]] \
        || die "routing contract incomplete -- no SMART_NODE_DOCKER_NETWORK (run B03 first)"
    [[ -n "${SMART_NODE_PG_CLUSTER:-}" ]] \
        || die "persistence contract incomplete -- no SMART_NODE_PG_CLUSTER (run B02 first)"
    command -v docker >/dev/null 2>&1 \
        || die "docker not available -- B03 should have installed it"
    derive_site
    probe_before
    ensure_dirs
    build_ui_image
    seed_superadmin_authz
    seed_superadmin_authn
    ensure_setup_token
    run_panel
    install_panel_data_renderer
    wait_for_panel
    write_report_section
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=B06-web-interface@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=B06-web-interface@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "stage.done" "stage" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"panel\":\"smart-node-ui\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "stage B06 (web-interface) -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
    # The one piece of operator-facing output: how to reach the panel.
    echo ""
    echo "  ============================================================"
    echo "  Smart Node panel ready -- Phase 1 bootstrap complete."
    echo "    URL:    $(web_scheme)://$PANEL_HOST"
    if (( SETUP_DONE )); then
        echo "    Login:  super-admin '$SUPERADMIN_USER' already configured"
    else
        echo "    First run: Studio's /welcome page with the one-time token in $SETUP_TOKEN_FILE"
    fi
    echo "  ============================================================"
}

run_verify_checks() {
    local cluster="${SMART_NODE_PG_CLUSTER:-}" coredb="${SMART_NODE_PG_CORE_DB:-}"
    # 1-2. preconditions.
    verify_check "stage B03 done" require_stage B03
    verify_check "stage B05 done" require_stage B05
    # 3. routing network exists.
    verify_check "docker network ${SMART_NODE_DOCKER_NETWORK:-smart-node} exists" \
        docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}"
    # 4. panel image present.
    verify_check "panel image $UI_IMAGE present" docker image inspect "$UI_IMAGE"
    # 5-8. container state.
    verify_check "container $PANEL_CT exists" bash -c "
        docker ps -aq -f 'name=^${PANEL_CT}\$' 2>/dev/null | grep -q ."
    verify_check "container $PANEL_CT running" bash -c "
        [[ \$(docker inspect -f '{{.State.Running}}' '$PANEL_CT' 2>/dev/null) == true ]]"
    verify_check "container $PANEL_CT uses image $UI_IMAGE" bash -c "
        [[ \$(docker inspect -f '{{.Config.Image}}' '$PANEL_CT' 2>/dev/null) == '$UI_IMAGE' ]]"
    verify_check "$PANEL_CT publishes no port" bash -c "
        [[ -z \$(docker port '$PANEL_CT' 2>/dev/null) ]]"
    # 9. panel data dir.
    verify_check "panel data dir exists" test -d "$PANEL_DATA_DIR"
    # 10. setup token present XOR super-admin configured.
    verify_check "setup token present or super-admin configured" bash -c "
        [[ -s '$SETUP_TOKEN_FILE' ]] && exit 0
        [[ -f '$AUTH_USERS_FILE' ]] && grep -q '\"$SUPERADMIN_USER\"' '$AUTH_USERS_FILE'"
    # 11-13. super-admin AuthZ rows in the core DB.
    verify_check "identity_users has '$SUPERADMIN_USER'" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$cluster' -d '$coredb' -tAc \
            \"SELECT 1 FROM identity_users WHERE username='$SUPERADMIN_USER'\" 2>/dev/null) == 1 ]]"
    verify_check "'$SUPERADMIN_USER' granted super-admin" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$cluster' -d '$coredb' -tAc \
            \"SELECT 1 FROM user_roles WHERE username='$SUPERADMIN_USER' AND role_id='super-admin'\" 2>/dev/null) == 1 ]]"
    verify_check "actor human:$SUPERADMIN_USER exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$cluster' -d '$coredb' -tAc \
            \"SELECT 1 FROM actors WHERE id='human:$SUPERADMIN_USER'\" 2>/dev/null) == 1 ]]"
    # 14. the panel answers through Traefik on its static files (no sign-in there).
    verify_check "panel answers through Traefik" bash -c "
        code=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H 'Host: ${POLL_HOST:-localhost}' $(web_scheme)://127.0.0.1/assets/shell.css 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    # 15. the panel root IS gated -- an unauthenticated request is bounced
    #     by smart-node-auth (302 to login / 401). A 2xx would mean the
    #     panel is exposed unauthenticated.
    verify_check "panel root gated by $AUTH_MIDDLEWARE" bash -c "
        code=\$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H 'Host: ${POLL_HOST:-localhost}' $(web_scheme)://127.0.0.1/ 2>/dev/null)
        [[ \"\$code\" == 302 || \"\$code\" == 401 ]]"
    # 16. install-report has the B06 section.
    verify_check "install-report has Stage B06 section" \
        bash -c "grep -qFx '## Stage B06 -- web-interface' '$SMART_NODE_REPORT'"
    # 17. manifest records the panel container.
    verify_check "manifest records the panel container" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"docker-container\",\"value\":\"$PANEL_CT\",' \"\$m\""
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
