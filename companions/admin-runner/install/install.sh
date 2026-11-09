#!/usr/bin/env bash
# Idempotent installer for smart-node-admin-runner. Parameterised; no host-specifics.
set -euo pipefail

SN_ADMIN_USER="${SN_ADMIN_USER:-sn-admin}"
APP_DIR="${APP_DIR:-/opt/smart-node/admin-runner}"
ENV_FILE="${ENV_FILE:-/etc/smart-node/admin-runner.env}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[admin-runner] user=${SN_ADMIN_USER} app=${APP_DIR}"

# 1. Privileged identity (system user, no login shell).
if ! id "${SN_ADMIN_USER}" >/dev/null 2>&1; then
  sudo useradd --system --create-home --shell /usr/sbin/nologin "${SN_ADMIN_USER}"
fi

# 2. Postgres peer auth: map the OS user to a pg role via pg_ident, then reload.
#    (The pg role + its grants on sn_config/sn_core are provisioned separately.)
echo "[admin-runner] ensure pg_ident map ${SN_ADMIN_USER} -> \$SN_ADMIN_DB_USER and reload pg"

# 3. Code + venv.
sudo mkdir -p "${APP_DIR}"
sudo rsync -a --delete --exclude venv --exclude tests "${SRC_DIR}/" "${APP_DIR}/"
sudo python3 -m venv "${APP_DIR}/venv"
sudo "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
sudo chown -R "${SN_ADMIN_USER}:${SN_ADMIN_USER}" "${APP_DIR}"

# 4. Env file (operator fills it; we never write secrets here).
if [ ! -f "${ENV_FILE}" ]; then
  sudo install -m 0640 "${SRC_DIR}/install/admin-runner.env.example" "${ENV_FILE}"
  echo "[admin-runner] wrote ${ENV_FILE} from example -- review it"
fi

# 5. systemd unit.
sudo install -m 0644 "${SRC_DIR}/install/smart-node-admin-runner.service" \
  /etc/systemd/system/smart-node-admin-runner.service
sudo systemctl daemon-reload
sudo systemctl enable --now smart-node-admin-runner
echo "[admin-runner] installed; check: curl -s http://127.0.0.1:8041/healthz"
