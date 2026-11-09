#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${GATEWAY_PY:-/opt/smart-node/gateway/venv/bin/python}"
[[ -x "$PY" ]] || { echo "cannot run: $PY not found (set GATEWAY_PY)" >&2; exit 2; }
SMARTNODE_DB_DSN='user=smartnode dbname=smartnode' \
CORE_API_URL='http://127.0.0.1:8030' \
BUDGET_NOTIFY_ACTOR=admin \
BUDGET_NOTIFY_CHANNEL=email \
SN_GATEWAY_CORE_API_SECRET=test-only-gateway-proof \
PYTHONPATH="$ROOT/companions/gateway" "$PY" - <<'PY'
import asyncio
import httpx
import sessions_v2_routes as gateway

calls = []

class Response:
    def raise_for_status(self):
        pass
    def json(self):
        return {"status": "queued", "message_id": 1}

class Client:
    def __init__(self, **kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return Response()

httpx.AsyncClient = Client

async def check():
    await gateway._send_budget_message("Budget pre-warning: spend is near the cap.")
    assert await gateway._send_budget_exhaustion_notification(7)
    assert await gateway._notify_turn_cutoff({"agent": "test"})

asyncio.run(check())
assert len(calls) == 3, calls
assert [url.rsplit("/api/", 1)[1] for url, _ in calls] == [
    "messages/send", "budgets/7/notify-exhaustion", "limits/notify-turn-cutoff"
]
for _, kwargs in calls:
    headers = kwargs["headers"]
    assert headers["X-SN-Service"] == "gateway"
    assert headers["X-SN-Service-Auth"] == "test-only-gateway-proof"
    assert headers["X-SN-On-Behalf-Of"] == "admin"
print("PASS: all three gateway notices carry owner delegation proof")
PY
