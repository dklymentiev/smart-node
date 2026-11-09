#!/usr/bin/env bash
# install-verify.sh -- verifies a clean docker-compose deployment.
#
# Goal (per the v0.1.0 acceptance gate): from `git clone` to "first task
# created via REST" in under five minutes on a host with Docker installed.
#
# Usage:
#   ./scripts/install-verify.sh
#
# Exits 0 on success, non-zero on any failure.

set -euo pipefail

PROJECT_NAME="planner-install-verify"
COMPOSE_FILE="$(cd "$(dirname "$0")/.." && pwd)/docker-compose.yml"
# Allow caller to override (e.g. on a host where 8080 is taken).
PORT="${PLANNER_VERIFY_PORT:-8080}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32m[OK]\033[0m %s\n' "$*"; }

cleanup() {
  bold "[5/5] Tearing down..."
  docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

bold "[1/5] Cleaning prior state for project '$PROJECT_NAME'..."
docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true

bold "[2/5] Building + starting the stack on port ${PORT}..."
START="$(date +%s)"
# Override the host-side port via PLANNER_VERIFY_PORT (compose reads $PORT_OVERRIDE).
PORT_OVERRIDE="$PORT" docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" up -d --build

bold "[3/5] Waiting for /health to report ok..."
for _ in $(seq 1 60); do
  body="$(curl -fsS "$HEALTH_URL" 2>/dev/null || true)"
  if echo "$body" | grep -q '"status":"ok"'; then
    ok "/health is ok: $body"
    break
  fi
  sleep 2
done
if ! echo "$body" | grep -q '"status":"ok"'; then
  err "service did not become healthy in 120s"
  err "container logs:"
  docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" logs --tail 80 app
  exit 1
fi

bold "[4/5] Extracting root API key from container logs..."
KEY="$(docker compose -f "$COMPOSE_FILE" -p "$PROJECT_NAME" logs app 2>&1 \
        | awk '/X-API-Key:/ {print $NF}' | tail -1)"
if [ -z "${KEY:-}" ]; then
  err "could not find X-API-Key in container logs"
  exit 1
fi
ok "root key obtained: ${KEY:0:8}..."

bold "[4a/5] Creating a project + task via REST..."
PROJECT_RESP="$(curl -fsS -X POST "http://127.0.0.1:${PORT}/api/v1/projects" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -d '{"slug":"smoke","title":"Smoke"}')"
PROJECT_ID="$(echo "$PROJECT_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
ok "project id: $PROJECT_ID"

TASK_RESP="$(curl -fsS -X POST "http://127.0.0.1:${PORT}/api/v1/tasks" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -d "{\"title\":\"first task\",\"project_id\":${PROJECT_ID}}")"
TASK_ID="$(echo "$TASK_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
ok "task id: $TASK_ID"

ELAPSED="$(( $(date +%s) - START ))"
ok "End-to-end first-task time: ${ELAPSED}s"
if [ "$ELAPSED" -gt 300 ]; then
  err "exceeded 5-minute target (took ${ELAPSED}s)"
  exit 1
fi

bold "Install-verify PASSED in ${ELAPSED}s."
