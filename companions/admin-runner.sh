#!/bin/bash
#
# admin-runner.sh -- Smart Node installer, Layer C Phase 2 companion: admin-runner.
#
# Runs INSIDE the target node as root. Idempotent. Installs the privileged
# operations service the node's control plane delegates system changes to.
#
# core-api stays unprivileged; every /etc/cron.d write, systemctl action and
# other host mutation goes through admin-runner over a host-local socket. In
# particular /api/crons/render (the cron-reconcile timer, installed by the
# core-api companion) delegates the /etc/cron.d materialisation here -- so
# WITHOUT this companion cron-reconcile 502s and scheduled agent wakes never
# materialise. It therefore installs EARLY, before core-api.
#
# Component: smart-node-admin-runner (vendored snapshot; see admin-runner/VENDORED).
# FastAPI/uvicorn app, host-local on 127.0.0.1:8041, owner-gated + fully audited.
#
# Security model (why this service is deliberately privileged):
#   - runs as its own OS user `sn-admin` with NOPASSWD sudo (broad by design --
#     the boundary is the APPLICATION: host-local bind + owner-gate + audit +
#     confirm-on-destructive, not a narrowed sudoers). See the vendored
#     install/sudoers.d/smart-node-admin-runner.
#   - its Postgres access is the OPPOSITE -- a dedicated least-privilege role
#     `sn_admin_runner` (peer auth via the smartnode ident map): SELECT on
#     sn_config.users (owner gate), SELECT on the role tables the permission
#     gate reads (sn_config.permissions, sn_config.role_permissions,
#     sn_core.actor_roles), SELECT+INSERT on sn_core.actors and INSERT
#     on sn_core.events (append-only audit). It can neither read the audit log
#     back nor touch anything else. Mirrors the canon node (reference-node).
#
# Creates:
#   - OS user sn-admin (system, home /home/sn-admin, nologin) + sudoers.d
#   - pg role sn_admin_runner + the `smartnode  sn-admin  sn_admin_runner`
#     ident map line (peer auth) + the least-privilege grants above
#   - python3 venv at /opt/smart-node/admin-runner/venv (pinned requirements)
#   - /opt/smart-node/admin-runner -- runtime tree (vendored snapshot)
#   - /etc/smart-node/admin-runner.env (from the vendored example; the real
#     DB role / owner role names, which the example leaves as placeholders)
#   - smart-node-admin-runner.service (the vendored unit) -- enabled + started
#   - "Companion admin-runner" section in the install report
#   - /var/lib/smart-node/stages/admin-runner.state
#
# NOTE: the sn_config.integrations row that points core-api at this service
# (slug=admin-runner, endpoint=http://127.0.0.1:8041) is seeded by B02, NOT
# here -- it must exist before core-api's resolve_endpoint() runs, and B02
# bakes the whole sn_config seed. This companion only makes the endpoint real.
#
# Usage:
#   admin-runner.sh             full run
#   admin-runner.sh --verify    idempotent post-check; exit 0 / N failures
#   admin-runner.sh --status    read /var/lib/smart-node/stages/admin-runner.state
#   admin-runner.sh --uninstall reverse the install
#   admin-runner.sh --help      this message
#
# Exits 0 on success.

set -euo pipefail

case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="admin-runner"
STAGE_NAME="admin-runner"
STAGE_VERSION="1.0.2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/admin-runner"

INSTALL_DIR="/opt/smart-node/admin-runner"
VENV_DIR="$INSTALL_DIR/venv"
ENV_FILE="/etc/smart-node/admin-runner.env"

SERVICE_USER="sn-admin"
SERVICE_HOME="/home/sn-admin"
# Canonical CLAUDE_HOME where `claude setup-token` (run as sn-admin) writes
# credentials. Must match admin-runner/config.py SN_ADMIN_CLAUDE_HOME default.
CLAUDE_HOME_DIR="/etc/smart-node/claude"
DB_ROLE="sn_admin_runner"
DB_NAME="smartnode"
OWNER_ROLES="super-admin"

SYSTEMD_UNIT="smart-node-admin-runner.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"
SUDOERS_PATH="/etc/sudoers.d/smart-node-admin-runner"

BIND_HOST="127.0.0.1"
BIND_PORT="8041"
ENDPOINT="http://$BIND_HOST:$BIND_PORT"

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
    --uninstall)      MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)                die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------

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

# sn-admin is a bespoke identity (NOT the smart-node-<short> convention): the
# vendored systemd unit and sudoers file both hardcode `sn-admin`, and its
# home carries the canonical CLAUDE_HOME staging. So we create it directly
# rather than via create_service_user.
ensure_user() {
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --create-home --home-dir "$SERVICE_HOME" \
            --shell /usr/sbin/nologin "$SERVICE_USER" \
            || die "useradd $SERVICE_USER failed"
        log "created system user $SERVICE_USER"
    else
        log "system user $SERVICE_USER already present"
    fi

    # FIX: sn-admin must exec /usr/local/bin/claude, whose target lives
    # under the agent home /var/lib/smart-node/agent (0750 smart-node-agent:smart-node).
    # sn-admin is not the owner, so it needs the umbrella group to TRAVERSE (the
    # dir is already 0750 -- group-traversable -- so membership alone suffices; we
    # do NOT chmod any agent dir). Idempotent: usermod -aG is additive.
    if getent group "$SMART_NODE_GROUP" >/dev/null 2>&1; then
        usermod -aG "$SMART_NODE_GROUP" "$SERVICE_USER" || die "usermod -aG $SMART_NODE_GROUP $SERVICE_USER failed"
        log "ensured $SERVICE_USER in group $SMART_NODE_GROUP (traverse agent home)"
    else
        log "WARN: umbrella group $SMART_NODE_GROUP missing -- claude exec may fail"
    fi

    # FIX: create the writable CLAUDE_HOME. config.py points CLAUDE_HOME at
    # /etc/smart-node/claude but nothing else creates it, so `claude setup-token`
    # (run as sn-admin) dies with EACCES. 0700, owned by sn-admin primary group.
    local sn_grp; sn_grp="$(id -gn "$SERVICE_USER")"
    install -d -m 0700 -o "$SERVICE_USER" -g "$sn_grp" "$CLAUDE_HOME_DIR"
    install -d -m 0700 -o "$SERVICE_USER" -g "$sn_grp" "$CLAUDE_HOME_DIR/.claude"
    manifest_record dir "$CLAUDE_HOME_DIR"

    manifest_record user "$SERVICE_USER"
}

# setup_db -- the least-privilege Postgres role + peer-auth ident map. Reads
# the pg_ident path + cluster from the B02 persistence contract (like the
# core-api companion's setup_db_ident), creates the role idempotently, grants
# EXACTLY what the runner needs (owner gate + append-only audit), maps the OS
# user onto it, and reloads the cluster so peer auth resolves before the
# service starts. SQL runs as the postgres superuser over the local socket.
setup_db() {
    local pg_ident pg_cluster
    pg_ident=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_IDENT)
    pg_cluster=$(_envfile_get "$SMART_NODE_PERSISTENCE_ENV" SMART_NODE_PG_CLUSTER)
    [[ -n "$pg_ident" && -n "$pg_cluster" ]] \
        || die "incomplete persistence contract ($SMART_NODE_PERSISTENCE_ENV) -- run B02 first"
    [[ -f "$pg_ident" ]] \
        || die "pg_ident.conf missing ($pg_ident) -- cluster not created?"

    # Role (NOLOGIN would break peer auth -- the mapped role must be able to
    # log in; no password, peer auth only). Grants are additive + idempotent.
    log "provisioning least-privilege role $DB_ROLE in $DB_NAME"
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "$DB_NAME" <<SQL || die "role/grant provisioning failed"
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_ROLE') THEN
        CREATE ROLE $DB_ROLE LOGIN;
    END IF;
END
\$\$;
GRANT USAGE ON SCHEMA sn_config TO $DB_ROLE;
GRANT USAGE ON SCHEMA sn_core   TO $DB_ROLE;
-- owner gate: read the users table only.
GRANT SELECT ON sn_config.users TO $DB_ROLE;
-- permission gate: the service checks its operation catalogue against the
-- seeded permissions at startup and reads the caller's role on every call.
-- Without these three it dies in that startup check and restarts forever,
-- which is what an update did before they were here. Read-only: roles and
-- the matrix are changed through core-api, never through this role.
GRANT SELECT ON sn_config.permissions TO $DB_ROLE;
GRANT SELECT ON sn_config.role_permissions TO $DB_ROLE;
GRANT SELECT ON sn_core.actor_roles TO $DB_ROLE;
-- append-only audit: register the actor (SELECT for the idempotent ON CONFLICT,
-- INSERT to create it) and append events (INSERT only -- no SELECT, so the
-- audit log cannot be read back through this role).
GRANT SELECT, INSERT ON sn_core.actors TO $DB_ROLE;
GRANT INSERT         ON sn_core.events TO $DB_ROLE;
SQL
    log "role $DB_ROLE ready (SELECT sn_config.users and the role tables; SELECT+INSERT sn_core.actors; INSERT sn_core.events)"

    # Peer-auth ident map: OS user sn-admin -> pg role sn_admin_runner.
    local ident_line="smartnode  $SERVICE_USER  $DB_ROLE"
    if ! grep -qF "$ident_line" "$pg_ident"; then
        echo "$ident_line" >> "$pg_ident"
        log "added the $SERVICE_USER -> $DB_ROLE line to the smartnode ident map"
    else
        log "smartnode ident map already carries the $SERVICE_USER -> $DB_ROLE line"
    fi
    manifest_record pg-ident "$SERVICE_USER"

    # Activate the map edit (peer auth reads pg_ident at reload).
    local pg_unit="postgresql@${pg_cluster/\//-}"
    if systemctl is-active "$pg_unit" >/dev/null 2>&1; then
        systemctl reload "$pg_unit" >/dev/null 2>&1 \
            || systemctl restart "$pg_unit" >/dev/null 2>&1 \
            || die "failed to reload $pg_unit after the ident-map change"
    else
        pg_ctlcluster "${pg_cluster%%/*}" "${pg_cluster##*/}" reload >/dev/null 2>&1 || true
    fi
}

install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/app.py" && -f "$SRC_SNAPSHOT/requirements.txt" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no app.py / requirements.txt"
    [[ ! -e "$SRC_SNAPSHOT/.git" ]] \
        || die "vendored snapshot carries a .git/ tree -- the admin-runner repo remote holds a plaintext GitLab token; re-vendor without .git/"

    install -d -m 0755 "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    log "syncing admin-runner snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude 'venv/' --exclude '.env' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .env \
            -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$INSTALL_DIR"/
    fi
    # Write the installer commit hash so sn-version can detect drift in this
    # vendored snapshot. The directory has no .git, so git-describe cannot
    # tell which commit is running; .sn-commit is the reliable alternative.
    echo "${SN_BUILD_COMMIT:-unknown}" > "$INSTALL_DIR/.sn-commit"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
}

build_venv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "creating venv -> $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" || die "venv creation failed ($VENV_DIR)"
    else
        log "venv already present -- reusing"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$VENV_DIR"
    log "installing requirements (pip)"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip install of requirements failed"
}

# write_env -- the runtime config. The vendored .example ships placeholder
# values (SN_ADMIN_DB_USER=sn-admin, SN_ADMIN_OWNER_ROLES=owner) that do NOT
# match a real Smart Node node; we write the node-true ones: the mapped pg
# role and the seeded owner role (super-admin, per B02). The core-api client
# credential is generated once and carried forward when this file is rewritten.
write_env() {
    install -d -m 0755 /etc/smart-node
    local tmp secret
    tmp=$(mktemp)
    secret=$(_envfile_get "$ENV_FILE" SN_ADMIN_CLIENT_SECRET 2>/dev/null || true)
    if [[ -z "$secret" ]]; then
        secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
        [[ ${#secret} -eq 64 ]] || die "could not generate admin-runner client credential"
    fi
    {
        echo "# Smart Node admin-runner companion runtime config -- admin-runner.sh@$STAGE_VERSION."
        echo "# Loaded by systemd via EnvironmentFile=. All values fail loud if unset."
        echo "SN_ADMIN_OWNER_ROLES=$OWNER_ROLES"
        echo "SN_ADMIN_DB_USER=$DB_ROLE"
        echo "SN_ADMIN_DB_NAME=$DB_NAME"
        echo "SN_ADMIN_BIND_HOST=$BIND_HOST"
        echo "SN_ADMIN_BIND_PORT=$BIND_PORT"
        echo "SN_ADMIN_CLIENT_SECRET=$secret"
    } > "$tmp"
    install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_USER" "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    manifest_record file "$ENV_FILE"
}

install_service() {
    local unit_src="$SRC_SNAPSHOT/install/$SYSTEMD_UNIT"
    [[ -f "$unit_src" ]] || die "systemd unit missing in vendor ($unit_src)"
    # The vendored unit is DELIBERATELY unhardened (the service escalates via
    # sudo); install it verbatim rather than substituting a sandboxed one.
    install -m 0644 -o root -g root "$unit_src" "$SYSTEMD_PATH"
    systemctl daemon-reload
    manifest_record systemd-unit "$SYSTEMD_UNIT"
    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 || die "systemctl enable $SYSTEMD_UNIT failed"
    systemctl restart "$SYSTEMD_UNIT" || die "systemctl start $SYSTEMD_UNIT failed"
}

install_sudoers() {
    local src="$SRC_SNAPSHOT/install/sudoers.d/smart-node-admin-runner"
    [[ -f "$src" ]] || die "sudoers file missing in vendor ($src)"
    install -m 0440 -o root -g root "$src" "$SUDOERS_PATH"
    # Never leave an unparseable sudoers fragment on the node.
    if ! visudo -cf "$SUDOERS_PATH" >/dev/null 2>&1; then
        rm -f "$SUDOERS_PATH"
        die "vendored sudoers fragment failed visudo -c -- removed to keep sudo sane"
    fi
    manifest_record file "$SUDOERS_PATH"
    log "installed + validated $SUDOERS_PATH"
}

# register_integration -- make core-api's admin_runner.resolve_endpoint() find
# this service. It reads `endpoint` from sn_config.integrations WHERE
# slug='admin-runner' AND archived_at IS NULL; a missing/empty endpoint is the
# 502 that breaks cron-reconcile. The shared sn_integration_register
# helper POSTs to core-api and canNOT set `endpoint` (not in its payload), and
# core-api is not up yet (this companion installs first), so we upsert the row
# directly over the local socket as the postgres superuser -- the companion
# owns its own integration row, endpoint included. Idempotent. Mirrors the
# canon node's row (reference-node id 4355).
register_integration() {
    log "registering the admin-runner integration row (endpoint=$ENDPOINT)"
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "$DB_NAME" \
        -v ep="$ENDPOINT" <<'SQL' || die "integration row upsert failed"
INSERT INTO sn_config.integrations (slug, label, description, kind, status, endpoint)
VALUES ('admin-runner', 'Admin Runner',
        'Privileged node operations service (owner-gated, audited).',
        'app', 'available', :'ep')
ON CONFLICT (slug) DO UPDATE
    SET endpoint = EXCLUDED.endpoint,
        status   = EXCLUDED.status,
        archived_at = NULL,
        updated_at  = now();
SQL
}

wait_healthy() {
    local i
    for i in $(seq 1 30); do
        if curl -fsS -m 2 "http://$BIND_HOST:$BIND_PORT/healthz" >/dev/null 2>&1; then
            log "admin-runner healthy on $BIND_HOST:$BIND_PORT"
            return 0
        fi
        sleep 1
    done
    log "WARN: admin-runner did not answer /healthz within 30s (check: journalctl -u $SYSTEMD_UNIT)"
    return 1
}

write_report_section() {
    report_section "Companion admin-runner" "$(cat <<EOF
- Service:      $SYSTEMD_UNIT ($(systemctl is-active "$SYSTEMD_UNIT" 2>/dev/null))
- Bind:         $BIND_HOST:$BIND_PORT (host-local, owner-gated, audited)
- OS user:      $SERVICE_USER (NOPASSWD sudo -- app-layer boundary)
- DB role:      $DB_ROLE (least-privilege; peer auth)
- Install dir:  $INSTALL_DIR
- Version:      admin-runner@$STAGE_VERSION
EOF
)"
}

# ---------- modes ----------

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, one-time), ensure_user (OS
#   account + ident map, one-time), setup_db (pg role + ident map, one-time),
#   install_snapshot (extract application files, only when version changes),
#   build_venv (pip install, only when version changes),
#   wait_healthy (health probe, no system state written).
#
# CONTRACT (run): write_env (env file read by the unit on every restart --
#   must be current), install_sudoers (sudoers rule grants admin-runner its
#   NOPASSWD scope -- must be current), install_service (systemd unit file --
#   must be current), register_integration (sn_config.integrations row --
#   must point to the live endpoint), register_companion (companions.d record
#   -- install-on.sh reads stage_version from here to decide full vs contracts
#   on future updates; without this call the record is never written and every
#   update re-runs admin-runner fully), stage_mark done.
run_contracts_only() {
    require_root
    log "companion admin-runner -- contracts only (v$STAGE_VERSION)"
    write_env
    install_sudoers
    install_service
    register_integration
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    stage_mark "$STAGE_ID" done "version=admin-runner@$STAGE_VERSION"
    log "companion admin-runner -- contracts only done"
}

run_intent() {
    require_root
    log "companion admin-runner -- start"
    ensure_python
    ensure_user
    setup_db
    install_snapshot
    build_venv
    write_env
    install_sudoers
    install_service
    wait_healthy || true
    register_integration
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02"
    write_report_section
    stage_mark "$STAGE_ID" done "version=admin-runner@$STAGE_VERSION"
    log "companion admin-runner -- done"
}

run_verify_mode() {
    VERIFY_TOTAL=0 VERIFY_ERRORS=0
    verify_check "install dir present"        test -d "$INSTALL_DIR"
    verify_check "app.py present"             test -f "$INSTALL_DIR/app.py"
    verify_check "venv interpreter present"   test -x "$VENV_DIR/bin/python"
    verify_check "venv has uvicorn + fastapi" \
        bash -c "'$VENV_DIR/bin/python' -c 'import uvicorn, fastapi, psycopg2'"
    verify_check "service user present"       id "$SERVICE_USER"
    verify_check "pg role present"            bash -c "runuser -u postgres -- psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$DB_ROLE'\" | grep -qx 1"
    verify_check "env file present"           test -f "$ENV_FILE"
    verify_check "sudoers present + valid"    bash -c "test -f '$SUDOERS_PATH' && visudo -cf '$SUDOERS_PATH'"
    verify_check "systemd unit installed"     systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1
    verify_check "systemd unit enabled"       systemctl is-enabled "$SYSTEMD_UNIT" >/dev/null 2>&1
    verify_check "systemd unit active"        systemctl is-active "$SYSTEMD_UNIT" >/dev/null 2>&1
    verify_check "listening $BIND_PORT"       bash -c "ss -ltn 2>/dev/null | grep -q '$BIND_HOST:$BIND_PORT'"
    verify_check "healthz answers"            curl -fsS -m 3 "http://$BIND_HOST:$BIND_PORT/healthz"
    verify_check "integration endpoint seeded" bash -c "runuser -u postgres -- psql -tAc \"SELECT 1 FROM sn_config.integrations WHERE slug='admin-runner' AND archived_at IS NULL AND endpoint <> ''\" '$DB_NAME' | grep -qx 1"

    if (( VERIFY_ERRORS > 0 )); then
        echo "[FAIL] $VERIFY_ERRORS check(s) failed" >&2
        exit "$VERIFY_ERRORS"
    fi
    echo "[OK] $VERIFY_TOTAL/$VERIFY_TOTAL checks passed"
}

run_uninstall_mode() {
    require_root
    log "admin-runner companion -- uninstall"
    systemctl disable --now "$SYSTEMD_UNIT" 2>/dev/null || true
    rm -f "$SYSTEMD_PATH"
    systemctl daemon-reload
    rm -f "$SUDOERS_PATH"
    rm -f "$ENV_FILE"
    rm -rf "$INSTALL_DIR"
    # Soft-delete the integration row so core-api's resolve_endpoint() stops
    # advertising a dead endpoint (it filters archived_at IS NULL).
    runuser -u postgres -- psql -q -d "$DB_NAME" \
        -c "UPDATE sn_config.integrations SET archived_at = now(), updated_at = now() WHERE slug='admin-runner' AND archived_at IS NULL" \
        >/dev/null 2>&1 || true
    # Leave the pg role + ident line + OS user in place: removing the role
    # could orphan audit rows in sn_core.events (actor FK), and the ident
    # line is harmless. State only. (No stage_clear helper exists in lib.sh --
    # studio.sh/voice.sh call one that is undefined; drop the state file directly.)
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "admin-runner companion uninstalled (pg role + os user retained by design)"
}

run_status_mode() {
    stage_status "$STAGE_ID"
    exit $?
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
