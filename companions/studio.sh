#!/bin/bash
#
# studio.sh -- Smart Node installer, Layer C Phase 2 companion: Studio.
#
# Runs INSIDE the target node as root. Idempotent. Depends on core-api,
# doci, planner, dispatcher (brin) -- it proxies all of them server-side on
# behalf of the logged-in operator, so it installs LAST.
#
# Installs Smart Node Studio: the unified end-user panel (the node's public
# "user" face, e.g. demo.example.com). Unlike the containerised
# companions, Studio is a NATIVE node service -- a small Node/Express server
# that owns all /api/* itself and proxies core-api/brin/doci/planner using
# the forwarded Authum identity. It binds the Docker bridge gateway so
# Traefik reaches it across the bridge, and sits behind the same
# smart-node-auth forwardAuth as the panel.
#
# Component: smart-node-studio (vendored snapshot; see studio/VENDORED).
#
# Creates:
#   - node 20 runtime (nodesource) if absent
#   - user smart-node-studio (system, group smart-node)
#   - /opt/smart-node/studio -- runtime tree (vendored snapshot; npm ci)
#   - /var/log/smart-node/studio
#   - /var/lib/smart-node/agents/avatars/custom -- durable uploaded avatars;
#     preserved by update and uninstall
#   - smart-node-studio.service + .service.d/ drop-ins (node-specific URLs)
#   - ufw allow Docker-network-CIDR -> :8050 (Traefik container reaches the bind)
#   - /etc/smart-node/traefik/dynamic/70-studio.yml (studio.<domain> route)
#   - "Companion studio" section in the install report
#   - /var/lib/smart-node/stages/studio.state
#
# NOTE: the PUBLIC root face (<node>.<domain> -> Studio) is wired by a
# separate publish step, NOT here -- this companion keeps to the node-internal
# studio.<domain> route so the base install stays node-scoped.
#
# Usage:
#   studio.sh             full run: node -> user -> snapshot -> service -> route
#   studio.sh --verify    idempotent post-check; exit 0 / N failures
#   studio.sh --status    read /var/lib/smart-node/stages/studio.state
#   studio.sh --uninstall reverse the install
#   studio.sh --help      this message
#
# Exits 0 on success.

set -euo pipefail

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="studio"
STAGE_NAME="studio"
STAGE_VERSION="1.1.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/studio"

INSTALL_DIR="/opt/smart-node/studio"
# Small root-run helpers this companion installs alongside the app.
OPS_DIR="/opt/smart-node/ops"
LOG_DIR="/var/log/smart-node/studio"
SERVICE_USER="smart-node-studio"
SMART_NODE_GROUP="smart-node"
AVATAR_WRITER_USER="smart-node-core-api"
AVATAR_STATE_DIR="/var/lib/smart-node/agents/avatars/custom"
AVATAR_MIGRATOR="$SRC_SNAPSHOT/migrate-custom-avatars.sh"

SYSTEMD_UNIT="smart-node-studio.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"
DROPIN_DIR="/etc/systemd/system/${SYSTEMD_UNIT}.d"

# brin's API key, kept OUT of the drop-ins. Everything in $DROPIN_DIR is 0644
# and readable by every agent on the node, so a credential written there is
# handed to exactly the population it exists to exclude -- BRIN_API_KEY was in
# one from the day Studio shipped. Same shape as the
# proxy proof: a 0600 root:root file that systemd reads as root before dropping
# privilege. A dedicated file rather than brin's own .env, which also carries
# PORT and HOST and would overwrite Studio's.
BRIN_KEY_ENV="/etc/smart-node/studio-brin.env"

TRAEFIK_DYNAMIC="/etc/smart-node/traefik/dynamic"
TRAEFIK_FILE="$TRAEFIK_DYNAMIC/70-studio.yml"

# Studio binds 0.0.0.0 (all interfaces). Access boundary is ufw alone.
# Traefik (container) reaches Studio via host.docker.internal; native callers use 127.0.0.1.
STUDIO_HOST="0.0.0.0"
STUDIO_PORT="8050"

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
        log "[studio] SMART_NODE_DOMAIN empty in routing.env -- public route will be skipped"
        return 0
    fi
}

# node major from `node -v` (e.g. v20.11.1 -> 20); empty if node absent.
node_major() { node -v 2>/dev/null | sed -e 's/^v//' -e 's/\..*//'; }

ensure_node() {
    # The base image may already carry a distro node -- often 18, and WITHOUT
    # npm. Accept an existing runtime ONLY if it is really node 20; otherwise
    # install the official binary and PROVE the result. A silently-wrong node is
    # exactly what broke a clean install: distro node 18 slipped in and
    # npm ci exploded three steps later.
    if command -v node >/dev/null 2>&1 && [[ "$(node_major)" == "20" ]]; then
        log "node 20 present ($(node -v))"
        return 0
    fi
    # Install the official Node binary from nodejs.org -- NOT the nodesource
    # setup script. That script fails to import its signing key (the CDN 403s on
    # the key) yet exits 0, so the repo is never added and apt falls back to
    # distro node 18 without npm -- the silent break behind The nodejs.org
    # tarball is self-contained (node + npm), checksum-verified, no repo/key.
    local ver="v20.18.1" arch tarball tmp
    case "$(uname -m)" in
        x86_64)  arch=x64 ;;
        aarch64) arch=arm64 ;;
        *) die "unsupported arch $(uname -m) -- no prebuilt node binary" ;;
    esac
    tarball="node-${ver}-linux-${arch}.tar.gz"
    log "installing node ${ver} (nodejs.org binary, ${arch})"
    tmp=$(mktemp -d)
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "https://nodejs.org/dist/${ver}/${tarball}" -o "$tmp/$tarball" \
        || die "node download failed (nodejs.org unreachable?)"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "https://nodejs.org/dist/${ver}/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt" \
        || die "node checksum list download failed"
    ( cd "$tmp" && grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c - >/dev/null 2>&1 ) \
        || die "node tarball checksum mismatch -- refusing to install"
    # strip the versioned top dir so bin/ include/ lib/ share/ land in /usr/local
    tar -xzf "$tmp/$tarball" -C /usr/local --strip-components=1 \
        --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE' \
        || die "node extract failed"
    rm -rf "$tmp"
    hash -r
    # Prove the install: version AND npm. Loud failure beats npm ci exploding
    # later with a useless error.
    [[ "$(node_major)" == "20" ]] \
        || die "node 20 install did not take -- got $(node -v 2>/dev/null || echo none)"
    command -v npm >/dev/null 2>&1 || die "npm missing after node install"
    log "node installed ($(node -v), npm $(npm -v))"
}

ensure_user() {
    id "$SERVICE_USER" >/dev/null 2>&1 && return 0
    useradd --system --no-create-home --gid "$SMART_NODE_GROUP" \
        --shell /usr/sbin/nologin "$SERVICE_USER" \
        || die "useradd $SERVICE_USER failed"
    log "created system user $SERVICE_USER"
}

install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/package.json" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no package.json"

    install -d -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$LOG_DIR"
    id "$AVATAR_WRITER_USER" >/dev/null 2>&1 \
        || die "avatar writer account missing ($AVATAR_WRITER_USER) -- install core-api first"
    [[ -x "$AVATAR_MIGRATOR" ]] \
        || die "avatar migration helper missing or not executable ($AVATAR_MIGRATOR)"
    # Freeze the old writer before taking its files. Without this, an upload can
    # land after the migration and before rsync, recreating the data-loss race.
    if systemctl is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null; then
        systemctl stop "$SYSTEMD_UNIT" \
            || die "could not stop $SYSTEMD_UNIT before avatar migration"
    fi
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" \
        /var/lib/smart-node/agents /var/lib/smart-node/agents/avatars
    "$AVATAR_MIGRATOR" "$INSTALL_DIR/public/avatars/custom" \
        "$AVATAR_STATE_DIR" "$AVATAR_WRITER_USER" "$SMART_NODE_GROUP" \
        || die "legacy custom-avatar migration failed"
    log "syncing Studio snapshot -> $INSTALL_DIR"
    install -d "$INSTALL_DIR"
    # node_modules is rebuilt by npm ci; never carry a vendored copy. The
    # legacy custom directory is user data even after migration: never let a
    # package --delete own it again.
    rsync -a --delete --exclude='.env' --exclude='node_modules' \
        --exclude='public/avatars/custom/' "$SRC_SNAPSHOT/" "$INSTALL_DIR/"
    log "npm ci (production deps)"
    ( cd "$INSTALL_DIR" && npm ci --omit=dev >/dev/null 2>&1 || npm install --omit=dev >/dev/null 2>&1 ) \
        || die "npm ci failed in $INSTALL_DIR"
    # Commit hash marker for sn-version (vendored snapshot has no .git).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$INSTALL_DIR/.sn-commit"
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
}

install_service() {
    local unit_src="$SRC_SNAPSHOT/systemd/$SYSTEMD_UNIT"
    [[ -f "$unit_src" ]] || die "systemd unit missing in vendor ($unit_src)"
    # The vendored unit hardcodes ExecStart=/usr/bin/node, but node is installed
    # as the nodejs.org binary in /usr/local/bin -- a stale /usr/bin/node path is
    # a 203/EXEC and the service never binds. Point ExecStart at the node
    # we actually installed.
    local node_bin; node_bin=$(command -v node) || die "node not on PATH at unit install"
    # The vendored unit also hardcodes Environment=HOST=<a bridge gateway>, whichever
    # one this component happened to get on the machine the unit was written on.
    # `docker network create` is called without --subnet, so docker hands out whatever
    # /16 is free and the gateway moves whenever the network is recreated. On this node
    # it moved to 172.19.0.1, and the unit kept binding the old address while THIS script
    # computed the live one for the traefik route and for its own verify. The result was
    # a Studio that ran, a route that pointed nowhere, and a check that waited its full
    # thirty seconds before saying so. Substitute the address the same way ExecStart is
    # substituted, from STUDIO_HOST, which is already read live at the top of this file
    # This substitution fixes both the stale bind and its misleading verify result.
    sed -E -e "s#^(ExecStart=)[^ ]*/node #\1${node_bin} #" \
           -e "s#^Environment=HOST=.*#Environment=HOST=${STUDIO_HOST}#" \
           "$unit_src" > "$SYSTEMD_PATH"
    grep -q "^Environment=HOST=${STUDIO_HOST}$" "$SYSTEMD_PATH" \
        || die "unit install did not set HOST to ${STUDIO_HOST}"
    chown root:root "$SYSTEMD_PATH"; chmod 0644 "$SYSTEMD_PATH"

    # node-specific URLs as drop-ins (container IPs differ per node -- read live).
    # All four may legitimately find nothing -- a node without the doci
    # container, an older .env written before the key existed -- and each is
    # followed by a fallback that says exactly that. Under `set -euo pipefail`
    # a bare assignment from a failing substitution ends the script on the spot
    # with no message, so the `|| true` is what lets those fallbacks run at all
    # (the affected node had no HOST= line, which is the normal state of
    # any node old enough to need an update).
    local brin_key="" brin_port=""
    brin_key=$(grep -E '^BRIN_API_KEY=' /opt/smart-node/dispatcher/.env 2>/dev/null | cut -d= -f2- || true)
    # brin binds 0.0.0.0; native callers always use 127.0.0.1. HOST in
    # dispatcher .env is the bind address, not a caller address -- read the port
    # only. Studio is a native service, so 127.0.0.1:PORT is always correct here.
    brin_port=$(grep -E '^PORT=' /opt/smart-node/dispatcher/.env 2>/dev/null | cut -d= -f2- || true)
    [[ -n "$brin_port" ]] || brin_port=8101

    install -d "$DROPIN_DIR"
    # AUTHUM_AUTH_URL is required by Studio unconditionally -- a domainless node
    # still needs Authum to authenticate the operator. Read the value from
    # /etc/smart-node/identity.env where B05 writes SMART_NODE_AUTH_URL for every
    # install regardless of whether a public domain exists. Fall back to composing
    # from SMART_NODE_DOMAIN only for pre-identity-contract nodes. Never make the
    # drop-in conditional on a public domain.
    local auth_url
    auth_url=$(grep -E '^SMART_NODE_AUTH_URL=' /etc/smart-node/identity.env 2>/dev/null \
        | cut -d= -f2- || true)
    if [[ -z "$auth_url" && -n "${SMART_NODE_DOMAIN:-}" ]]; then
        auth_url="$(web_scheme)://auth.${SMART_NODE_DOMAIN}"
    fi
    if [[ -n "$auth_url" ]]; then
        printf '[Service]\nEnvironment=AUTHUM_AUTH_URL=%s\n' "$auth_url" > "$DROPIN_DIR/auth.conf"
    fi
    # core-api binds 0.0.0.0; Studio is a native caller -> 127.0.0.1.
    printf '[Service]\nEnvironment=CORE_API_URL=http://127.0.0.1:8030\n' > "$DROPIN_DIR/core-api.conf"
    # The agent documents page reads mesh. No step set MESH_API_URL, so
    # server.js fell back to https://mesh.local, which resolves nowhere, and the
    # Documents page never loaded. Read the address from the contract memory.sh
    # writes (the one core-api resolves), rather than baking one here.
    printf '[Service]\nEnvironmentFile=-/etc/smart-node/mesh.env\n' > "$DROPIN_DIR/mesh.conf"
    # Derive DOCI_URL at service start, not at install time. doci now runs
    # natively and writes /etc/smart-node/doci.env, which the script reads
    # first; the container lookup below stays for a node not yet migrated.
    # Container IPs
    # are assigned by start order and change on each container restart; baking
    # one at install time produces the same defect as a baked gateway address.
    # ExecStartPre=+ runs as root so docker inspect is available regardless of
    # the service User=. The leading '-' on EnvironmentFile makes an absent or
    # empty result a no-op: a Studio installed before doci still starts.
    #
    # The derivation lives in a FILE, not inline in the unit. It used to be one
    # ExecStartPre=+/bin/bash -c '... $(docker inspect ...) ...' line, and that
    # cannot work: systemd performs no command substitution, and its own quote
    # handling takes the nested script apart. What it actually wrote was
    #
    #     DOCI_URL=http:///bin/bash
    #
    # on our node and DOCI_URL=http:///usr/bin/bash on another -- the %s filled
    # with $0. Nothing complained: EnvironmentFile=- swallows a bad file, and
    # Studio only refuses to start on an EMPTY value, so a meaningless non-empty
    # one sailed through and the documents page was silently empty. In a file
    # the shell owns its own quoting and the unit line has nothing to mangle.
    install -d -m 0755 "$OPS_DIR"
    cat > "$OPS_DIR/doci-url.sh" <<'DOCIURL'
#!/usr/bin/env bash
# Written by studio.sh. Derives DOCI_URL for smart-node-studio at service start.
# Container IPs are assigned by start order, so this cannot be baked at install
# time. Writes an empty file rather than a wrong one when doci is not up: an
# empty DOCI_URL means "no documents on this node" (documents is optional, see
# studio.manifest.json) and /api/docs answers 404 saying so, while a malformed
# one looks healthy and serves an empty page.
set -euo pipefail
mkdir -p /run/smart-node
# doci runs natively and publishes its address in a contract file. Prefer it.
url=$(sed -n 's/^DOCI_URL=//p' /etc/smart-node/doci.env 2>/dev/null | head -n1 || true)
if [[ -n "$url" ]]; then
    printf 'DOCI_URL=%s\n' "$url" > /run/smart-node/doci-url.env
    exit 0
fi
# A node not yet migrated still runs doci as a container.
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' smart-node-doci 2>/dev/null || true)
if [[ -n "$ip" ]]; then
    printf 'DOCI_URL=http://%s\n' "$ip" > /run/smart-node/doci-url.env
else
    : > /run/smart-node/doci-url.env
    echo "doci-url: smart-node-doci has no address yet -- wrote an empty file" >&2
fi
DOCIURL
    chmod 0755 "$OPS_DIR/doci-url.sh"
    # ExecStartPre= on its own line first: a re-run must replace the old inline
    # command, not append to it.
    printf '[Service]\nExecStartPre=\nExecStartPre=+%s/doci-url.sh\n' "$OPS_DIR" \
        > "$DROPIN_DIR/doci-pre.conf"
    printf '[Service]\nEnvironmentFile=-/run/smart-node/doci-url.env\n' \
        > "$DROPIN_DIR/doci.conf"
    # No PLANNER_URL any more. Studio reaches the planner through
    # core-api, so it needs neither the address nor a credential of its own --
    # and a node that cannot address the planner directly cannot accidentally
    # grow a second path to it. Remove a drop-in left by an earlier install.
    rm -f "$DROPIN_DIR/planner.conf"
    # brin binds 0.0.0.0; Studio is a native caller -> 127.0.0.1.
    printf '[Service]\nEnvironment=BRIN_URL=http://127.0.0.1:%s\n' "$brin_port" > "$DROPIN_DIR/brin.conf"
    # The key moves out of that 0644 drop-in into a 0600 root:root file. Writing
    # it here (rather than leaving it where it was) is what actually closes it:
    # the line above rewrites brin.conf without the key, so an upgraded node
    # stops exposing it as well as a fresh one.
    local btmp; btmp=$(mktemp)
    {
        echo "# brin's API key for Studio -- written by studio.sh@$STAGE_VERSION."
        echo "# 0600 root:root. It lived in a world-readable drop-in previously."
        echo "BRIN_API_KEY=$brin_key"
    } > "$btmp"
    install -m 0600 -o root -g root "$btmp" "$BRIN_KEY_ENV"
    rm -f "$btmp"
    manifest_record file "$BRIN_KEY_ENV"
    printf '[Service]\nEnvironmentFile=-%s\n' "$BRIN_KEY_ENV" > "$DROPIN_DIR/brin-key.conf"
    # The proxy proof. Studio is a second door onto core-api: it sits
    # behind smart-node-auth, so Authum has already authenticated the session,
    # but it then relays over the docker0 gateway and so misses the header
    # Traefik would have stamped. Without this the owner approving a task in
    # Studio looks exactly like an agent on the node asserting Remote-User:
    # admin, and core-api refuses both.
    #
    # EnvironmentFile pointing at core-api's own file -- NOT a drop-in with the
    # value in it. The drop-ins beside this one are world-readable 0644 (which
    # is how BRIN_API_KEY is already exposed to every agent on the node, raised
    # separately); the referenced file is 0600 root:root and systemd reads it
    # as root before dropping privilege. Leading '-' so a Studio installed
    # before core-api on a fresh node still starts; it simply cannot approve
    # until core-api.sh has run, and the refusal says so.
    printf '[Service]\nEnvironmentFile=-/etc/smart-node/core-api-proxy-auth.env\n' \
        > "$DROPIN_DIR/proxy-auth.conf"

    # The node's own mail domain, so the Studio can tell its agents' mailboxes from
    # a stranger's without carrying anyone's domain in its code. Same value the
    # dispatcher writes as SN_MSGID_DOMAIN. Optional: unset simply means the Studio
    # labels fewer addresses as agents.
    if [[ -n "${SMART_NODE_MSGID_DOMAIN:-}" ]]; then
        printf '[Service]\nEnvironment=SN_MAIL_DOMAIN=%s\n' "$SMART_NODE_MSGID_DOMAIN" > "$DROPIN_DIR/mail-domain.conf"
    fi

    systemctl daemon-reload
    manifest_record systemd-unit "$SYSTEMD_UNIT"
    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 || die "systemctl enable $SYSTEMD_UNIT failed"
    systemctl restart "$SYSTEMD_UNIT" || die "systemctl start $SYSTEMD_UNIT failed"
}

open_firewall() {
    # Studio binds 0.0.0.0; Traefik (container) reaches it via host.docker.internal.
    # Traffic crosses the INPUT chain; ufw default-deny means it must be explicitly allowed.
    # Open only the bridge subnet ->:8050; never Anywhere .
    local sn_cidr
    sn_cidr=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    if command -v ufw >/dev/null 2>&1 && [[ -n "$sn_cidr" ]]; then
        ufw allow from "$sn_cidr" to any port "$STUDIO_PORT" proto tcp >/dev/null 2>&1 || true
        log "ufw: allowed $sn_cidr -> :$STUDIO_PORT"
    fi
}

install_route() {
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
        if [[ -f "$TRAEFIK_FILE" ]]; then
            rm -f "$TRAEFIK_FILE"
            manifest_retire_artifact file "$TRAEFIK_FILE" \
                "no public domain -- Studio route retired"
            log "[studio] no public domain -- Traefik route retired ($TRAEFIK_FILE)"
        else
            log "[studio] no public domain -- no Traefik route to write"
        fi
        return 0
    fi
    # A derived name with no DNS record is not a door.
    if ! public_route_eligible "studio.$SMART_NODE_DOMAIN"; then
        retire_route "studio" "$TRAEFIK_FILE" "$PUBLIC_ROUTE_SKIP_REASON"
        return 0
    fi
    install -d "$TRAEFIK_DYNAMIC"
    # Native service on all interfaces; Traefik (container) reaches it
    # via host.docker.internal. Studio owns all /api itself and proxies core-api/
    # brin server-side, so NO /api split. Behind smart-node-auth, except the
    # installable-app files: the browser fetches the manifest and its icons
    # without the session, and they carry no data.
    cat > "$TRAEFIK_FILE" <<EOF
# Smart Node Studio -- unified end-user client. Native node service bound to
# 0.0.0.0:$STUDIO_PORT.
# Traefik reaches it via host.docker.internal. Behind smart-node-auth forwardAuth.
# Node-internal door studio.<domain>;
# the public root face is wired by a separate publish step.
# Written by studio.sh@$STAGE_VERSION.
http:
  routers:
    smart-node-studio:
      rule: "Host(\`studio.$SMART_NODE_DOMAIN\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-studio"
      middlewares: ["smart-node-auth@docker"]
      $(web_tls_yaml)
    # The first-run page: the one door open before anyone has an account. Step 1
    # is guarded by the one-time setup token; later steps call the session /api.
    smart-node-studio-welcome:
      rule: "Host(\`studio.$SMART_NODE_DOMAIN\`) && PathPrefix(\`/welcome\`)"
      priority: 110
      entryPoints: ["$(web_entry)"]
      service: "smart-node-studio"
      $(web_tls_yaml)
    # The installable-app files only (static, no data): no sign-in.
    smart-node-studio-app:
      rule: "Host(\`studio.$SMART_NODE_DOMAIN\`) && (Path(\`/manifest.webmanifest\`) || Path(\`/sw.js\`) || Path(\`/offline.html\`) || PathPrefix(\`/icons/\`))"
      priority: 100
      entryPoints: ["$(web_entry)"]
      service: "smart-node-studio"
      $(web_tls_yaml)
  services:
    smart-node-studio:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:$STUDIO_PORT"
        passHostHeader: true
EOF
    manifest_record file "$TRAEFIK_FILE"
    log "traefik route studio.$SMART_NODE_DOMAIN -> host.docker.internal:$STUDIO_PORT"
}

write_report_section() {
    report_section "Companion studio" "$(cat <<EOF
- Service:      $SYSTEMD_UNIT ($(systemctl is-active "$SYSTEMD_UNIT" 2>/dev/null))
- Bind:         0.0.0.0:$STUDIO_PORT (all interfaces, ufw restricts to docker bridge subnets)
- Route:        $(web_scheme)://studio.$SMART_NODE_DOMAIN (behind smart-node-auth)
- Install dir:  $INSTALL_DIR
- Version:      studio@$STAGE_VERSION
EOF
)"
}

# ---------- modes ----------

run_intent() {
    require_root
    log "companion studio -- start"
    derive_domain
    ensure_node
    ensure_user
    install_snapshot
    install_service
    open_firewall
    install_route
    # Studio never registered itself, so "what is installed here" had no answer
    # for it at all -- the panel's own companion list did not contain the client
    # the operator is looking at. Eight companions registered; Studio and
    # voice did not.
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B06,core-api"
    write_report_section
    stage_mark "$STAGE_ID" done "version=studio@$STAGE_VERSION"
    log "companion studio -- done"
}

run_verify_mode() {
    VERIFY_TOTAL=0 VERIFY_ERRORS=0
    # --verify is its own invocation of this script: nothing the install run
    # sourced survives into it. Resolve the node identity here, or every check
    # below that asks about the domain reads an empty variable and answers the
    # wrong question -- which is exactly how a node WITH a domain failed on the
    # route it had just written correctly.
    derive_domain
    verify_check "install dir present"        test -d "$INSTALL_DIR"
    verify_check "server.js present"          test -f "$INSTALL_DIR/src/server.js"
    verify_check "avatar state dir present"   test -d "$AVATAR_STATE_DIR"
    verify_check "node runtime is v20"        bash -c '[ "$(node -v 2>/dev/null | cut -d. -f1)" = v20 ]'
    verify_check "npm present"                command -v npm
    verify_check "service user present"       id "$SERVICE_USER"
    verify_check "systemd unit installed"     systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1
    verify_check "systemd unit enabled"       systemctl is-enabled "$SYSTEMD_UNIT" >/dev/null 2>&1
    _studio_ok=0
    for _i in $(seq 1 30); do
        if bash -c "ss -ltn 2>/dev/null | grep -q ':$STUDIO_PORT'" >/dev/null 2>&1; then
            _studio_ok=1; break
        fi
        sleep 1
    done
    VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
    if (( ! _studio_ok )); then
        echo "[VERIFY-FAIL] Studio listening $STUDIO_PORT (not up after 30s)" >&2
        VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
    fi
    # Same shape as core-api above: the route is only written when a public
    # domain is configured. On a no-domain node assert its
    # absence rather than skipping the question entirely.
    if [[ -n "${SMART_NODE_DOMAIN:-}" ]] && public_route_eligible "studio.${SMART_NODE_DOMAIN:-}"; then
        verify_check "traefik route present"      test -f "$TRAEFIK_FILE"
    else
        verify_check "no Studio route this node cannot serve" \
            bash -c "[[ ! -f \"$TRAEFIK_FILE\" ]]"
    fi
    verify_check "ufw allows $STUDIO_PORT"     bash -c "iptables -S ufw-user-input 2>/dev/null | grep -q 'dport $STUDIO_PORT' || ! command -v ufw >/dev/null 2>&1"

    if (( VERIFY_ERRORS > 0 )); then
        echo "[FAIL] $VERIFY_ERRORS check(s) failed" >&2
        exit "$VERIFY_ERRORS"
    fi
    echo "[OK] $VERIFY_TOTAL/$VERIFY_TOTAL checks passed"
}

run_uninstall_mode() {
    require_root
    log "Studio companion -- uninstall"
    systemctl disable --now "$SYSTEMD_UNIT" 2>/dev/null || true
    rm -f "$SYSTEMD_PATH"; rm -rf "$DROPIN_DIR"
    systemctl daemon-reload
    rm -f "$TRAEFIK_FILE"
    rm -rf "$INSTALL_DIR"
    # It registers now, so it must deregister: a manifest that keeps claiming a
    # removed component is the same defect as one that reports the wrong version
    # for a present one.
    unregister_companion "$STAGE_ID" 2>/dev/null || true
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "Studio companion uninstalled"
}

run_status_mode() {
    stage_status "$STAGE_ID"
    exit $?
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_node (Node.js runtime check, no state written),
#   ensure_user (OS account creation, one-time), install_snapshot (extract
#   application files, version-gated).
#
# CONTRACT (run): derive_domain (resolves $SMART_NODE_DOMAIN for use in
#   install_service and install_route), install_service (systemd unit file
#   -- must be current), open_firewall (ufw rule for the Studio port --
#   if removed the panel is unreachable; idempotent), install_route (Traefik
#   route file -- must be current), register_companion (companions.d record
#   for version tracking), stage_mark done.
run_contracts_only() {
    require_root
    log "companion studio -- contracts only (v$STAGE_VERSION)"
    derive_domain
    install_service
    open_firewall
    install_route
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B06,core-api"
    stage_mark "$STAGE_ID" done "version=studio@$STAGE_VERSION" "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
