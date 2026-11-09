#!/bin/bash
# Native HITL decision server. An existing Docker deployment is adopted only
# with an explicit installer flag and a verified offline backup.
set -euo pipefail

STAGE_ID="hitl"
STAGE_NAME="hitl"
STAGE_VERSION="0.1.4"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/hitl-upstream"
DEST="/opt/smart-node/hitl"
STATE="/var/lib/smart-node/hitl"
CALLER_STATE="$STATE/caller"
LEGACY_CALLER="/var/lib/smart-node/hitl-caller"
LEGACY_FLOW_CALLER="/var/lib/smart-node/hitl-flow-caller"
ADOPT_MARKER="/etc/smart-node/hitl-adoption.json"
BACKUP_BASE="/var/backups/smart-node/hitl"
LEGACY_CONTAINER="smart-node-hitl"
QUEUE="/var/lib/smart-node/hitl-queue"
ENV_FILE="/etc/smart-node/hitl.env"
BINDINGS_FILE="/etc/smart-node/hitl-recipient-actors.json"
UNIT="smart-node-hitl.service"
UNIT_FILE="/etc/systemd/system/$UNIT"
BRIDGE_UNIT="smart-node-hitl-bridge.service"
BRIDGE_UNIT_FILE="/etc/systemd/system/$BRIDGE_UNIT"
DELIVERY_UNIT="smart-node-hitl-delivery.service"
DELIVERY_UNIT_FILE="/etc/systemd/system/$DELIVERY_UNIT"
ROUTE="/etc/smart-node/traefik/dynamic/75-hitl.yml"
MCP_DST="$DEST/mcp_hitl.py"
PORT=8792
SERVICE_USER="smart-node-hitl"
DELIVERY_USER="smart-node-hitl-delivery"
DELIVERY_GROUP="smart-node-hitl-queue"

source /usr/local/lib/smart-node/lib.sh
[[ ! -r "$SCRIPT_DIR/_shared/integration.sh" ]] || source "$SCRIPT_DIR/_shared/integration.sh"

mode="${1:-run}"
[[ "$mode" =~ ^(run|--verify|--status|--uninstall|--contracts-only|--rollback-legacy)$ ]] \
    || die "unknown HITL mode: $mode"
ADOPTION_STARTED=0
ADOPTION_BACKUP=""
HITL_CIDR=""
LEGACY_RESTART=""
ROLLBACK_STARTED=0

select_caller_state() {
    if [[ -f "$ADOPT_MARKER" ]]; then
        CALLER_STATE="$LEGACY_CALLER"
        ROUTE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["route"])' "$ADOPT_MARKER")
    fi
}

origin() {
    select_caller_state
    local value="${SMART_NODE_HITL_ORIGIN:-}"
    if [[ -z "$value" && -r "$ENV_FILE" ]]; then
        value=$(sed -n 's/^HITL_ORIGIN=//p' "$ENV_FILE" | head -1)
    fi
    [[ "$value" =~ ^https://[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]+)?$ ]] \
        || die "SMART_NODE_HITL_ORIGIN must be an HTTPS origin without path"
    if [[ -r "$ENV_FILE" && -f "$CALLER_STATE/caller.json" ]]; then
        local prior
        prior=$(sed -n 's/^HITL_ORIGIN=//p' "$ENV_FILE" | head -1)
        [[ "$prior" == "$value" ]] || die "HITL origin is bound to existing passkeys; migrate keys before changing it"
    fi
    HITL_ORIGIN="$value"
    HITL_HOST="${value#https://}"
    HITL_HOST="${HITL_HOST%%:*}"
}

legacy_preflight() {
    python3 "$SCRIPT_DIR/hitl/migration_preflight.py" \
        --db "$STATE/hitl.sqlite" \
        --caller-state "$LEGACY_CALLER" \
        --caller-state "$LEGACY_FLOW_CALLER"
}

adoption_failure() {
    local rc=$?
    [[ "$ADOPTION_STARTED" -eq 1 && "$rc" -ne 0 ]] || return "$rc"
    systemctl disable --now "$DELIVERY_UNIT" "$BRIDGE_UNIT" "$UNIT" 2>/dev/null || true
    if [[ -n "$HITL_CIDR" ]]; then
        ufw --force delete allow from "$HITL_CIDR" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
    fi
    if [[ -n "$ADOPTION_BACKUP" ]]; then
        if [[ -f "$ADOPTION_BACKUP/route" ]]; then
            cp -a "$ADOPTION_BACKUP/route" "$ROUTE"
        else
            rm -f "$ROUTE"
        fi
    fi
    if [[ -n "$ADOPTION_BACKUP" && -f "$ADOPTION_BACKUP/ownership.json" ]]; then
        python3 "$SCRIPT_DIR/hitl/migration_snapshot.py" --restore-owners "$ADOPTION_BACKUP" \
            --state "$STATE" --caller "$LEGACY_CALLER" 2>/dev/null || true
    fi
    if [[ -n "$LEGACY_RESTART" ]]; then
        docker update --restart="$LEGACY_RESTART" "$LEGACY_CONTAINER" >/dev/null 2>&1 || true
    fi
    docker start "$LEGACY_CONTAINER" >/dev/null 2>&1 || true
    rm -f "$ADOPT_MARKER"
    stage_mark hitl failed "adoption_rolled_back=1" "backup=$ADOPTION_BACKUP" 2>/dev/null || true
    echo "HITL adoption failed; old container restart attempted; backup: $ADOPTION_BACKUP" >&2
    return "$rc"
}

rollback_failure() {
    local rc=$?
    [[ "$ROLLBACK_STARTED" -eq 1 && "$rc" -ne 0 ]] || return "$rc"
    docker stop "$LEGACY_CONTAINER" >/dev/null 2>&1 || true
    docker update --restart=no "$LEGACY_CONTAINER" >/dev/null 2>&1 || true
    chown -R "$SERVICE_USER":smart-node "$STATE" "$LEGACY_CALLER" 2>/dev/null || true
    python3 "$SCRIPT_DIR/hitl/legacy_route.py" rewrite --path "$ROUTE" \
        --old "http://$LEGACY_CONTAINER:8080" \
        --new "http://host.docker.internal:$PORT" 2>/dev/null || true
    if [[ -n "$HITL_CIDR" ]]; then
        ufw allow from "$HITL_CIDR" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
    fi
    systemctl enable --now "$UNIT" "$BRIDGE_UNIT" "$DELIVERY_UNIT" 2>/dev/null || true
    echo "HITL rollback failed; native service restart attempted" >&2
    return "$rc"
}

install_poller() {
    # These scripts can predate this source tree. Keep an installed copy byte for
    # byte, including local configuration, and seed only a missing file.
    if [[ ! -e "$DEST/flow_poller.py" && ! -L "$DEST/flow_poller.py" ]]; then
        install -m 0644 -o root -g smart-node "$SCRIPT_DIR/hitl/flow_poller.py" "$DEST/flow_poller.py"
    fi
    if [[ ! -e "$DEST/flow_poller.sh" && ! -L "$DEST/flow_poller.sh" ]]; then
        install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl/flow_poller.sh" "$DEST/flow_poller.sh"
    fi
}

adopt_legacy() {
    [[ "${SMART_NODE_HITL_ADOPT_LEGACY:-0}" == 1 ]] \
        || die "existing HITL installation detected; use explicit --hitl-adopt-legacy after preflight"
    [[ -f "$STATE/hitl.sqlite" && -f "$LEGACY_CALLER/caller.json" \
       && -f "$LEGACY_FLOW_CALLER/caller.json" ]] \
        || die "legacy HITL database or one of two caller states is missing"
    docker inspect "$LEGACY_CONTAINER" >/dev/null 2>&1 \
        || die "legacy HITL container missing; refusing adoption"
    local old_origin
    old_origin=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$LEGACY_CONTAINER" \
        | sed -n 's/^HITL_ORIGIN=//p' | head -1)
    [[ -n "$old_origin" && "$old_origin" == "$HITL_ORIGIN" ]] \
        || die "HITL origin differs from the deployed passkey origin; refusing adoption"
    ROUTE=$(python3 "$SCRIPT_DIR/hitl/legacy_route.py" find \
        --directory /etc/smart-node/traefik/dynamic --host "$HITL_HOST") \
        || die "legacy HITL router cannot be identified safely"
    local report
    report=$(legacy_preflight) || die "legacy HITL preflight failed"
    [[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["active_unanswered"])' <<< "$report")" == 0 ]] \
        || die "active HITL requests remain; wait for their answers before adoption"
    install -d -o root -g root -m 0700 "$BACKUP_BASE"
    ADOPTION_BACKUP=$(mktemp -d "$BACKUP_BASE/adopt-XXXXXXXX")
    chmod 0700 "$ADOPTION_BACKUP"
    LEGACY_RESTART=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$LEGACY_CONTAINER")
    if [[ "$LEGACY_RESTART" == on-failure ]]; then
        local retry_count
        retry_count=$(docker inspect -f '{{.HostConfig.RestartPolicy.MaximumRetryCount}}' "$LEGACY_CONTAINER")
        [[ "$retry_count" =~ ^[0-9]+$ ]] || die "invalid legacy HITL retry policy"
        [[ "$retry_count" == 0 ]] || LEGACY_RESTART="on-failure:$retry_count"
    fi
    [[ "$LEGACY_RESTART" =~ ^(no|always|unless-stopped|on-failure(:[0-9]+)?)$ ]] \
        || die "unsupported legacy HITL restart policy"
    ADOPTION_STARTED=1
    trap adoption_failure EXIT
    # Docker's 'always' policy can otherwise restart the old server beside
    # the native systemd unit after a reboot. Restore the policy on rollback.
    docker update --restart=no "$LEGACY_CONTAINER" >/dev/null
    docker stop "$LEGACY_CONTAINER" >/dev/null
    # The server is stopped before the final snapshot: its SQLite WAL and
    # both caller trees are copied at one stable cutover point.
    local after
    after=$(python3 "$SCRIPT_DIR/hitl/migration_snapshot.py" \
        --state "$STATE" --caller "$LEGACY_CALLER" \
        --flow-caller "$LEGACY_FLOW_CALLER" --app "$DEST/app" \
        --route "$ROUTE" --dest "$ADOPTION_BACKUP") \
        || die "HITL backup verification failed"
    [[ "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["active_unanswered"])' <<< "$after")" == 0 ]] \
        || die "active HITL request appeared during cutover; old service will be restored"
    CALLER_STATE="$LEGACY_CALLER"
    chown -R "$SERVICE_USER":smart-node "$STATE" "$LEGACY_CALLER"
}

# install_socket_dir -- declare the bridge socket's directory.
#
# tmpfiles.d rather than an ExecStartPre: systemd builds the unit's mount
# namespace before any ExecStartPre runs, so a ReadWritePaths entry for a
# directory that does not exist yet stops the unit with status 226/NAMESPACE.
# On a node whose /run/smart-node/hitl was never created the bridge never
# started. 2750 and setgid keep the same access as before: the agent group may
# connect, nobody else can reach the path.
install_socket_dir() {
    local conf="/etc/tmpfiles.d/smart-node-hitl.conf"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF2
# Smart Node HITL bridge socket directory. Generated; do not edit.
d /run/smart-node 0755 root root -
d /run/smart-node/hitl 2750 $SERVICE_USER smart-node-agents -
EOF2
    install -m 0644 -o root -g root "$tmp" "$conf"
    rm -f "$tmp"
    manifest_record file "$conf"
    systemd-tmpfiles --create "$conf" >/dev/null 2>&1 \
        || die "HITL socket directory could not be created"
}

install_code() {
    [[ -f "$SRC/server/hitl/app.py" && -f "$SRC/caller/hitl.py" && -f "$SCRIPT_DIR/hitl/bridge.py" ]] \
        || die "HITL source snapshot missing"
    id "$SERVICE_USER" >/dev/null 2>&1 || \
        useradd --system --no-create-home --gid smart-node --shell /usr/sbin/nologin "$SERVICE_USER"
    getent group "$DELIVERY_GROUP" >/dev/null || groupadd --system "$DELIVERY_GROUP"
    id "$DELIVERY_USER" >/dev/null 2>&1 || \
        useradd --system --no-create-home --gid "$DELIVERY_GROUP" \
            --groups smart-node --shell /usr/sbin/nologin "$DELIVERY_USER"
    # The bridge asks whether a request was already delivered. It may walk
    # into delivered/ but never list or write it, so the marker stays honest.
    id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$DELIVERY_GROUP" || \
        usermod -aG "$DELIVERY_GROUP" "$SERVICE_USER"
    # A previous Docker deployment uses this same code and database path.
    # rsync --delete would remove its bind-mounted /app while it is running;
    # creating a new caller would also strand its existing signed decisions.
    # Adoption stops the old container and snapshots both caller states first.
    if [[ -d "$DEST/app" && ! -f "$ADOPT_MARKER" ]]; then
        adopt_legacy
    elif [[ -f "$STATE/hitl.sqlite" && ! -f "$ENV_FILE" && ! -f "$ADOPT_MARKER" ]]; then
        die "existing HITL installation detected; migrate its database and caller states before native install"
    fi
    install -d -m 0700 -o "$SERVICE_USER" -g smart-node "$STATE"
    install -d -m 0700 -o "$SERVICE_USER" -g smart-node "$CALLER_STATE"
    install -d -m 2750 -o "$SERVICE_USER" -g "$DELIVERY_GROUP" \
        "$QUEUE" "$QUEUE/requests" "$QUEUE/delivery"
    # 2710, not 0710: the parent is setgid, a directory made inside it inherits
    # the bit, and chmod with a numeric mode cannot clear it. The group part is
    # traverse only, which is all the bridge needs to stat a delivery marker.
    install -d -m 2710 -o "$DELIVERY_USER" -g "$DELIVERY_GROUP" "$QUEUE/delivered"
    systemctl stop "$UNIT" "$BRIDGE_UNIT" "$DELIVERY_UNIT" 2>/dev/null || true
    install -d -m 0755 -o root -g smart-node "$DEST"
    # The published content-article cron names these installed poller files.
    # Keep them alongside the old /app caller through adoption, updates and
    # rollback; neither script is part of the native companion snapshot.
    rsync -a --delete --exclude 'venv/' --exclude 'app/' --exclude '/flow_poller.sh' --exclude '/flow_poller.py' "$SRC/" "$DEST/"
    install_poller
    install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl/bridge.py" "$DEST/bridge.py"
    install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl/mcp_hitl.py" "$MCP_DST"
    install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl/deliver.py" "$DEST/deliver.py"
    install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl/recipient_bindings.py" "$DEST/recipient_bindings.py"
    install -d -m 0755 -o root -g smart-node "$DEST/hitl"
    install -m 0755 -o root -g smart-node "$SCRIPT_DIR/hitl.sh" "$DEST/install.sh"
    install -m 0644 -o root -g smart-node "$SCRIPT_DIR/hitl/migration_preflight.py" "$DEST/hitl/migration_preflight.py"
    install -m 0644 -o root -g smart-node "$SCRIPT_DIR/hitl/migration_snapshot.py" "$DEST/hitl/migration_snapshot.py"
    install -m 0644 -o root -g smart-node "$SCRIPT_DIR/hitl/legacy_route.py" "$DEST/hitl/legacy_route.py"
    python3 -m venv "$DEST/venv"
    "$DEST/venv/bin/pip" install --disable-pip-version-check -r "$DEST/requirements.txt"
    printf '%s\n' "${SN_BUILD_COMMIT:-unknown}" > "$DEST/.sn-commit"
    chown -R root:smart-node "$DEST"
    chmod -R go-w "$DEST"
    manifest_record dir "$DEST"
    if [[ -L "$BINDINGS_FILE" ]]; then
        die "HITL recipient binding file must not be a symlink"
    fi
    if [[ ! -e "$BINDINGS_FILE" ]]; then
        printf '{}\n' > "$BINDINGS_FILE"
    fi
    chown root:smart-node "$BINDINGS_FILE"
    chmod 0640 "$BINDINGS_FILE"
    # Signed decisions and caller keys are durable evidence. The generic
    # uninstall replays "dir" entries with rm -rf, so never claim this tree.
    if [[ ! -f "$CALLER_STATE/caller.json" ]]; then
        local caller_id
        caller_id="sn-$(cut -c1-12 /etc/machine-id)"
        runuser -u "$SERVICE_USER" -- env HITL_STATE="$CALLER_STATE" \
            "$DEST/venv/bin/python" "$DEST/caller/hitl.py" keygen "$caller_id" \
            || die "HITL caller key generation failed"
    fi
}

write_contracts() {
    origin
    id "$SERVICE_USER" >/dev/null 2>&1 || die "HITL service user missing; run full install"
    id "$DELIVERY_USER" >/dev/null 2>&1 || die "HITL delivery user missing; run full install"
    [[ -d "$QUEUE/delivered" ]] || die "HITL delivery queue missing; run full install"
    [[ -x "$DEST/venv/bin/gunicorn" ]] || die "HITL virtualenv missing; run full install"
    install -d -m 0755 /etc/smart-node/traefik/dynamic
    printf 'HITL_ORIGIN=%s\nHITL_URL=%s\nHITL_DB=%s/hitl.sqlite\nHITL_STATE=%s\nHITL_QUEUE=%s\nHITL_BIND=0.0.0.0\nPORT=%s\n' \
        "$HITL_ORIGIN" "$HITL_ORIGIN" "$STATE" "$CALLER_STATE" "$QUEUE" "$PORT" > "$ENV_FILE"
    chown root:root "$ENV_FILE"
    chmod 0644 "$ENV_FILE"
    manifest_record file "$ENV_FILE"
    local caller_id pubkey
    read -r caller_id pubkey < <(runuser -u "$SERVICE_USER" -- env HITL_STATE="$CALLER_STATE" \
        "$DEST/venv/bin/python" -c '
import base64, json, os
from nacl.bindings import crypto_sign_ed25519_sk_to_pk
with open(os.environ["HITL_STATE"] + "/caller.json", encoding="utf-8") as f:
    caller = json.load(f)
secret = caller["secret_key"]
key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
pub = base64.urlsafe_b64encode(crypto_sign_ed25519_sk_to_pk(key)).decode().rstrip("=")
print(caller["caller_id"], pub)
')
    [[ -n "$caller_id" && -n "$pubkey" ]] || die "HITL caller key missing"
    runuser -u "$SERVICE_USER" -- env HITL_DB="$STATE/hitl.sqlite" HITL_ORIGIN="$HITL_ORIGIN" \
        "$DEST/venv/bin/python" "$DEST/server/bin/admin.py" \
        add-caller "$caller_id" "Smart Node" "$HITL_HOST" "$pubkey" >/dev/null \
        || die "HITL caller registration failed"
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Smart Node HITL decision service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=smart-node
WorkingDirectory=$DEST/server
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$DEST/venv/bin/gunicorn -c $DEST/deploy/gunicorn.conf.py hitl.app:application
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$UNIT_FILE"
    manifest_record systemd-unit "$UNIT"
    cat > "$BRIDGE_UNIT_FILE" <<EOF
[Unit]
Description=Smart Node HITL agent bridge
After=network-online.target $UNIT
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=smart-node
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$DEST/venv/bin/python $DEST/bridge.py
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE $QUEUE /run/smart-node/hitl

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$BRIDGE_UNIT_FILE"
    manifest_record systemd-unit "$BRIDGE_UNIT"
    cat > "$DELIVERY_UNIT_FILE" <<EOF
[Unit]
Description=Smart Node HITL signed answer delivery
After=$BRIDGE_UNIT smart-node-core-api.service smart-node-planner.service
Wants=$BRIDGE_UNIT

[Service]
Type=simple
User=$DELIVERY_USER
Group=$DELIVERY_GROUP
SupplementaryGroups=smart-node
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $DEST/deliver.py
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$QUEUE

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$DELIVERY_UNIT_FILE"
    manifest_record systemd-unit "$DELIVERY_UNIT"
    # The decision page has its own passkey proof. An adopted route keeps its
    # existing VPN middleware and TLS policy; only its upstream changes.
    if [[ "$ADOPTION_STARTED" -eq 1 || -f "$ADOPT_MARKER" ]]; then
        python3 "$SCRIPT_DIR/hitl/legacy_route.py" rewrite \
            --path "$ROUTE" --old "http://$LEGACY_CONTAINER:8080" \
            --new "http://host.docker.internal:$PORT"
    else
        cat > "$ROUTE" <<EOF
http:
  routers:
    smart-node-hitl:
      rule: "Host(\`$HITL_HOST\`)"
      entryPoints: ["$(web_entry)"]
      service: "smart-node-hitl"
      $(web_tls_yaml)
  services:
    smart-node-hitl:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:$PORT"
        passHostHeader: true
EOF
    fi
    chmod 0644 "$ROUTE"
    manifest_record file "$ROUTE"
    local cidr
    cidr=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    [[ -n "$cidr" ]] || die "Smart Node Docker subnet unavailable; refusing public HITL bind"
    HITL_CIDR="$cidr"
    command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active' \
        || die "UFW is not active; refusing HITL bind on all interfaces"
    ufw allow from "$cidr" to any port "$PORT" proto tcp >/dev/null
    manifest_record ufw-rule "from $cidr to any port $PORT proto tcp"
    install_socket_dir
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null
    systemctl restart "$UNIT"
    systemctl enable "$BRIDGE_UNIT" >/dev/null
    systemctl restart "$BRIDGE_UNIT"
    systemctl enable "$DELIVERY_UNIT" >/dev/null
    systemctl restart "$DELIVERY_UNIT"
    if declare -F sn_mcp_server_register >/dev/null; then
        local args_json
        args_json=$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$MCP_DST")
        sn_mcp_server_register --slug hitl --name "HITL" --transport stdio \
            --command /opt/smart-node/dispatcher/venv/bin/python --args "$args_json"
    fi
    register_companion hitl "$STAGE_VERSION" "B06,core-api"
}

verify() {
    select_caller_state
    [[ -f "$DEST/server/hitl/app.py" ]] || die "HITL code absent"
    [[ -d "$STATE" ]] || die "HITL state absent"
    [[ -f "$ROUTE" ]] || die "HITL route absent"
    systemctl is-active --quiet "$UNIT" || die "HITL service inactive"
    systemctl is-active --quiet "$BRIDGE_UNIT" || die "HITL bridge inactive"
    systemctl is-active --quiet "$DELIVERY_UNIT" || die "HITL delivery inactive"
    [[ "$(systemctl show -p User --value "$DELIVERY_UNIT")" == "$DELIVERY_USER" ]] \
        || die "HITL delivery is not running as its dedicated user"
    [[ -S /run/smart-node/hitl/agent.sock ]] || die "HITL agent socket absent"
    origin
    curl --fail --silent --max-time 5 "http://127.0.0.1:$PORT/" >/dev/null \
        || die "HITL health request failed"
}

rollback_legacy() {
    require_root
    [[ -f "$ADOPT_MARKER" ]] || die "no adopted legacy HITL state to roll back"
    select_caller_state
    local backup
    backup=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["backup"])' "$ADOPT_MARKER")
    HITL_CIDR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("cidr", ""))' "$ADOPT_MARKER")
    LEGACY_RESTART=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["legacy_restart"])' "$ADOPT_MARKER")
    [[ "$LEGACY_RESTART" =~ ^(no|always|unless-stopped|on-failure(:[0-9]+)?)$ ]] \
        || die "invalid legacy HITL restart policy in adoption marker"
    [[ "$backup" == "$BACKUP_BASE"/adopt-* && -f "$backup/audit.json" ]] \
        || die "verified HITL backup missing; refusing rollback"
    # Keep the live DB and both live caller trees: answers signed after the
    # cutover must remain available to the old container after rollback.
    [[ "$(docker inspect -f '{{.State.Running}}' "$LEGACY_CONTAINER")" == false ]] \
        || die "legacy HITL container already running; refusing two servers"
    ROLLBACK_STARTED=1
    trap rollback_failure EXIT
    systemctl disable --now "$DELIVERY_UNIT" "$BRIDGE_UNIT" "$UNIT"
    if ! legacy_preflight >/dev/null; then
        die "current HITL state failed audit"
    fi
    if [[ -n "$HITL_CIDR" ]]; then
        ufw --force delete allow from "$HITL_CIDR" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
    fi
    if [[ -f "$backup/route" ]]; then
        cp -a "$backup/route" "$ROUTE"
    else
        rm -f "$ROUTE"
    fi
    python3 "$SCRIPT_DIR/hitl/migration_snapshot.py" --restore-owners "$backup" \
        --state "$STATE" --caller "$LEGACY_CALLER"
    docker update --restart="$LEGACY_RESTART" "$LEGACY_CONTAINER" >/dev/null
    docker start "$LEGACY_CONTAINER" >/dev/null
    [[ "$(docker inspect -f '{{.State.Running}}' "$LEGACY_CONTAINER")" == true ]] \
        || die "legacy HITL container did not restart"
    rm -f "$ADOPT_MARKER"
    ROLLBACK_STARTED=0
    trap - EXIT
    stage_mark hitl failed "rolled_back_to=legacy" "backup=$backup" || true
    echo "Legacy HITL restored with live database; backup remains at $backup"
}

case "$mode" in
    run) require_root; origin; install_code; write_contracts; verify
         # workflows runs earlier in install-on.sh, so this is the second
         # attempt on a clean install with --hitl-origin. Wait until the
         # native service is verified before enabling its optional poller.
         if [[ -f "$SCRIPT_DIR/hitl/install-poller.py" ]] \
             && ! python3 "$SCRIPT_DIR/hitl/install-poller.py"; then
             log "WARN: optional HITL poller setup failed; HITL installation continues"
         fi
         stage_mark hitl done "version=$STAGE_VERSION"
         if [[ "$ADOPTION_STARTED" -eq 1 ]]; then
             marker_tmp=$(mktemp /etc/smart-node/.hitl-adoption-XXXXXXXX)
             printf '{"backup":"%s","caller_state":"%s","route":"%s","cidr":"%s","legacy_restart":"%s"}\n' \
                 "$ADOPTION_BACKUP" "$LEGACY_CALLER" "$ROUTE" "$HITL_CIDR" "$LEGACY_RESTART" > "$marker_tmp"
             chmod 0600 "$marker_tmp"
             mv -f "$marker_tmp" "$ADOPT_MARKER"
             sync -f "$ADOPT_MARKER"
             ADOPTION_STARTED=0
             trap - EXIT
         fi ;;
    --contracts-only) require_root; write_contracts ;;
    --verify) verify ;;
    --status) stage_status hitl ;;
    --rollback-legacy) rollback_legacy ;;
    --uninstall) die "HITL state contains signed decisions; explicit migration required before uninstall" ;;
esac
