#!/usr/bin/env bash
#
# The owner's ChatGPT plan as Flint's model: core-api's routes (routes/gateway_chatgpt.py).
#
# WHAT IS CHECKED. The routes run for real (FastAPI TestClient, the core-api venv) against a
# throwaway postgres built from the tree's B02 files, with the secret store on a throwaway key.
# OpenAI is replaced by fixtures: the token trade, the ID token check and the model list.
#
#   start       gives the sign-in address of a first registration, keeps one host id across attempts,
#               keeps the attempt server-side
#   complete    a wrong address is refused and the attempt survives; an account without plan use is
#               refused; no attempt is a conflict; a good one stores the sign-in, drops the attempt,
#               points the Flint row at the plan (the row's earlier settings are saved), lists the
#               models, and no response carries a token
#   status      says who is signed in and that Flint uses it
#   token       a fresh token is returned as is; a stale one is refreshed once and the replacement
#               refresh token is saved; a refused refresh is a 401 and changes nothing
#   disconnect  puts the Flint row back exactly as it was, removes the models and the sign-in
#   guard       a caller who is not the owner is refused
#
# USAGE
#   tests/gateway-chatgpt-routes.test.sh
# Needs postgres server binaries (PG_BIN) and the core-api venv (CORE_API_PY).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import fastapi, httpx, psycopg2" 2>/dev/null \
    || { echo "cannot run: $PY lacks fastapi/httpx/psycopg2 (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
"${PSQL[@]}" -d smartnode -c "CREATE EXTENSION IF NOT EXISTS pgcrypto" >/dev/null || { echo "cannot run: pgcrypto" >&2; exit 2; }
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/$f" >>"$WORK/apply.log" 2>&1 \
        || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
done
# The Flint runtime row as an install leaves it, on OpenRouter with a key kept in the store.
"${PSQL[@]}" -d smartnode -c "
  INSERT INTO sn_config.cli_providers (slug, binary_path, invocation_args, supported_models, env_vars, enabled)
    VALUES ('flint-cli', '/usr/local/bin/flint', '[]'::jsonb, '{}',
            '{\"FLINT_PROVIDER\": \"openrouter\", \"OPENROUTER_API_KEY\": \"secret:gateway.flint-cli.api_key\"}'::jsonb, true)
    ON CONFLICT (slug) DO UPDATE SET env_vars = EXCLUDED.env_vars, enabled = true;
  INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation)
    VALUES ('agent:dev1', 'dev1', 'agent', 'specialist', 'agent', 'internal') ON CONFLICT (id) DO NOTHING;" >/dev/null \
    || { echo "cannot run: fixture" >&2; exit 2; }
printf 'a-throwaway-node-key' > "$WORK/node.key"

cat > "$WORK/drive.py" <<'PY'
import json, os, sys, urllib.parse
sys.path.insert(0, os.environ["CORE_API_DIR"])
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi import HTTPException
import auth
from auth import Principal
from db import cursor
from lib import secret_store
import chatgpt_oauth as oauth
from routes import gateway_chatgpt as gw

with cursor() as cur:
    cur.execute("SELECT actor_id FROM sn_core.actor_roles WHERE role = 'super-admin'")
    owner_actor = cur.fetchone()["actor_id"]
OWNER = Principal(username="admin", user_id=1, tenant_id=1, role="super-admin",
                  actor_id=owner_actor, actor_kind="human", strength="session")
DEV = Principal(username="dev1", user_id=3, tenant_id=1, role="agent",
                actor_id="agent:dev1", actor_kind="agent", strength="proved")
who = {"p": OWNER}
# The owner gate (routes/gateway_secrets._require_session_owner) is shared and has its own checks; here the
# caller is whoever the test says, and the route's permission check still runs for real.
def _owner_only(request):
    if who["p"].role != "super-admin":
        raise HTTPException(status_code=403, detail="owner role required")
    return who["p"]
gw._require_session_owner = _owner_only
app = FastAPI()
app.include_router(gw.router)
app.dependency_overrides[auth.get_principal] = lambda: who["p"]
c = TestClient(app)
H = {"Remote-User": "admin"}
SECRET_WORDS = ("AT-1", "RT-1", "AT-2", "RT-2", "ID-TOKEN")


def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))


def secret(ref):
    with cursor() as cur:
        return secret_store.get_value(cur, ref)


def flint_env():
    with cursor() as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = 'flint-cli'")
        return cur.fetchone()["env_vars"]


def models():
    with cursor() as cur:
        cur.execute("SELECT model, label FROM sn_config.models WHERE provider = 'flint-cli' AND vendor = 'chatgpt' ORDER BY model")
        return [(r["model"], r["label"]) for r in cur.fetchall()]


def leaked(*responses):
    return any(w in r.text for r in responses for w in SECRET_WORDS)


BEFORE = flint_env()

# ---- guard ----
who["p"] = DEV
r = c.post("/api/gateway/chatgpt/login/start", headers=H)
res("guard: a caller who is not the owner is refused", r.status_code == 403 and secret(gw.HOST) is None, f"{r.status_code} {r.text[:120]}")
who["p"] = OWNER

# ---- start ----
r1 = c.post("/api/gateway/chatgpt/login/start", headers=H)
q1 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r1.json().get("auth_url", "")).query))
host = secret(gw.HOST)
pending = json.loads(secret(gw.PENDING) or "{}")
res("start: the address of a first registration", r1.status_code == 200 and q1.get("client_id") == "dynamic_agent_client"
    and q1.get("ext_agent_host_id") == host and q1.get("redirect_uri") == oauth.REDIRECT, f"{r1.status_code} {r1.text[:160]}")
res("start: the attempt is kept server-side and not in the response", pending.get("state") == q1.get("state") and pending["verifier"] not in r1.text)
r2 = c.post("/api/gateway/chatgpt/login/start", headers=H)
res("start: one host id across attempts, a new state each time", secret(gw.HOST) == host
    and json.loads(secret(gw.PENDING))["state"] != pending["state"])
pending = json.loads(secret(gw.PENDING))


def landing(**kw):
    q = {"code": "CODE", "state": pending["state"], "client_id": "oaiapp_test", "scope": oauth.SCOPE}
    q.update(kw)
    return {"url": oauth.REDIRECT + "?" + urllib.parse.urlencode(q)}


# ---- complete ----
r = c.post("/api/gateway/chatgpt/login/complete", json=landing(state="nope"), headers=H)
res("complete: a wrong address is refused and the attempt survives", r.status_code == 400 and "another sign-in" in r.text
    and secret(gw.PENDING) is not None, f"{r.status_code} {r.text[:160]}")

trades = []
TOK = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600, "id_token": "ID-TOKEN", "scope": oauth.SCOPE}
def exchange(code, client_id, verifier):
    trades.append((code, client_id, verifier))
    return dict(TOK)
oauth.exchange = exchange
seen = {}
def verify(id_token, client_id, nonce, **kw):
    seen.update(client_id=client_id, nonce=nonce)
    return {"sub": "user-1", "email": "me@example.com", "iss": oauth.ISSUER}
oauth.verify_id_token = verify
gw._models = lambda token: ["gpt-a", "gpt-b"]

TOK_NO_PLAN = dict(TOK, scope="openid profile")
oauth.exchange = lambda code, client_id, verifier: dict(TOK_NO_PLAN)
r = c.post("/api/gateway/chatgpt/login/complete", json=landing(scope="openid profile"), headers=H)
res("complete: an account without plan use is refused", r.status_code == 400 and "plan use" in r.text and secret(gw.CRED) is None
    and flint_env() == BEFORE, f"{r.status_code} {r.text[:160]}")
res("complete: and the used attempt is gone", secret(gw.PENDING) is None)
oauth.exchange = exchange
r = c.post("/api/gateway/chatgpt/login/complete", json=landing(), headers=H)
res("complete: no attempt is a conflict", r.status_code == 409, f"{r.status_code} {r.text[:120]}")

c.post("/api/gateway/chatgpt/login/start", headers=H)
pending = json.loads(secret(gw.PENDING))
r = c.post("/api/gateway/chatgpt/login/complete", json=landing(), headers=H)
body = r.json() if r.status_code == 200 else {}
res("complete: a good address signs in", r.status_code == 200 and body.get("email") == "me@example.com"
    and body.get("models") == ["gpt-a", "gpt-b"], f"{r.status_code} {r.text[:200]}")
res("complete: the code was traded with the attempt's verifier", trades and trades[-1] == ("CODE", "oaiapp_test", pending["verifier"]))
res("complete: the ID token was checked for this registration and attempt", seen == {"client_id": "oaiapp_test", "nonce": pending["nonce"]})
cred = json.loads(secret(gw.CRED) or "{}")
res("complete: the sign-in is stored with the host id", cred.get("access_token") == "AT-1" and cred.get("client_id") == "oaiapp_test"
    and cred.get("ext_agent_host_id") == host and cred.get("email") == "me@example.com")
res("complete: the attempt is dropped", secret(gw.PENDING) is None)
env = flint_env()
res("complete: Flint is pointed at the plan", env.get("SN_FLINT_UPSTREAM") == "chatgpt" and env.get("FLINT_PROVIDER") == "openai"
    and env.get("OPENAI_API_KEY") == "none" and "OPENROUTER_API_KEY" not in env, f"{env}")
res("complete: the row's earlier settings are saved", json.loads(secret(gw.PREVIOUS) or "{}") == BEFORE)
res("complete: the models are in the catalog under Flint", models() == [("gpt-a", "gpt-a (ChatGPT plan)"), ("gpt-b", "gpt-b (ChatGPT plan)")], f"{models()}")
res("complete: no response carries a token", not leaked(r))

# ---- status ----
r = c.get("/api/gateway/chatgpt", headers=H)
st = r.json()
res("status: who is signed in, and that Flint uses it", st == {"signed_in": True, "email": "me@example.com", "models": ["gpt-a", "gpt-b"],
                                                               "plan_use": True, "flint_uses_it": True}, f"{st}")
res("status: no token in it", not leaked(r))

# ---- token ----
refreshes = []
def fake_refresh(client_id, refresh_token):
    refreshes.append((client_id, refresh_token))
    return {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600}
oauth.refresh = fake_refresh
t = gw.token()
res("token: a fresh one is returned as saved, no refresh", t["access_token"] == "AT-1" and not refreshes, f"{t} {refreshes}")
stale = dict(json.loads(secret(gw.CRED)), saved_at=1)
with cursor(commit=True) as cur:
    secret_store.set_value(cur, gw.CRED, json.dumps(stale), "test", provider="chatgpt")
t = gw.token()
saved = json.loads(secret(gw.CRED))
res("token: a stale one is refreshed once with the saved refresh token", t["access_token"] == "AT-2" and refreshes == [("oaiapp_test", "RT-1")], f"{t} {refreshes}")
res("token: the replacement refresh token is saved with it", saved["refresh_token"] == "RT-2" and saved["access_token"] == "AT-2"
    and saved["email"] == "me@example.com")
gw.token()
res("token: and the next call does not refresh again", len(refreshes) == 1)
with cursor(commit=True) as cur:
    secret_store.set_value(cur, gw.CRED, json.dumps(dict(saved, saved_at=1)), "test", provider="chatgpt")
def refused(client_id, refresh_token):
    raise oauth.AuthError("OpenAI refused the request: invalid_grant (sign in again)")
oauth.refresh = refused
try:
    gw.token()
    code = None
except HTTPException as e:
    code = e.status_code
res("token: a refused refresh is a 401 and changes nothing", code == 401 and json.loads(secret(gw.CRED))["refresh_token"] == "RT-2")

# ---- disconnect ----
r = c.delete("/api/gateway/chatgpt", headers=H)
res("disconnect: the Flint row is exactly as before", r.status_code == 200 and flint_env() == BEFORE, f"{r.status_code} {flint_env()}")
res("disconnect: the sign-in, the saved settings and the models are gone", secret(gw.CRED) is None and secret(gw.PREVIOUS) is None and models() == [])
r = c.delete("/api/gateway/chatgpt", headers=H)
res("disconnect: twice is harmless", r.status_code == 200 and r.json().get("was_signed_in") is False and flint_env() == BEFORE)
try:
    gw.token()
    code = None
except HTTPException as e:
    code = e.status_code
res("token: after disconnect there is none to give", code == 404)
PY

CORE_API_DIR="$ROOT/companions/core-api" SN_GATEWAY_SECRET_KEY_FILE="$WORK/node.key" \
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    "$PY" "$WORK/drive.py" >"$WORK/drive.out" 2>"$WORK/drive.err"
[[ -s "$WORK/drive.out" ]] || { echo "cannot run: driver produced nothing" >&2; tail -20 "$WORK/drive.err" >&2; exit 2; }
sed 's/^/  /' "$WORK/drive.out"
FAIL=$(grep -c '^FAIL' "$WORK/drive.out"); TOTAL=$(grep -cE '^(ok|FAIL)' "$WORK/drive.out")
echo
if (( FAIL == 0 )); then echo "PASS: $TOTAL checks"; exit 0; fi
echo "FAIL: $FAIL of $TOTAL"; tail -15 "$WORK/drive.err" >&2; exit 1
