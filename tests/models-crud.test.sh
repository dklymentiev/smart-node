#!/usr/bin/env bash
#
# The model catalog can be changed through core-api, by the owner and by the
# configurator, and a model added there runs in the gateway.
#
# WHAT WENT WRONG. The owner asked John to put a new agent on GPT through
# OpenRouter. /api/models answered 405 to anything but GET, so no agent could add
# a model; the operator had to INSERT the row with psql. And a row added that way
# could still be refused by the gateway, which kept its own hardcoded
# ALLOWED_MODELS per provider. John also could not see which providers the node
# can run or which one lacked a key, so he guessed ('openai/gpt-4o', a provider
# the gateway does not have).
#
# WHAT IS CHECKED. core-api's models router, run for real (FastAPI TestClient,
# the core-api venv) against a throwaway postgres built from the tree's B02 files,
# with a stand-in gateway that serves /api/v1/backends from the gateway source's
# own UPSTREAMS table. Nothing on the host is touched.
#
#   providers      GET /api/models/providers lists every gateway upstream with
#                  status and key_ref, and says what is missing in words
#   add-owner      the owner adds openrouter/<vendor>/<model>: 201, listed at once,
#                  event model.added written
#   add-config     an agent whose capability is configurator may add
#   add-other      an agent with another capability is refused 403
#   bad-provider   a provider the gateway cannot run is refused 400, naming the
#                  ones it can (openai -> openrouter hint)
#   duplicate      the same model twice is 409
#   in-use         disabling or deleting a model an agent runs on is 409 and names
#                  the agent; the row stays
#   edit-delete    a label change is saved; an unused model is deleted
#   gw-down        with the gateway unreachable the write is refused 502 and
#                  nothing is written
#   one-table      gateway: UPSTREAMS and resolve_backend's branches name the same set
#   catalog-gate   gateway: a model outside ALLOWED_MODELS but enabled in the catalog
#                  is allowed; one in neither is refused; an unreadable catalog refuses
#
# USAGE
#   tests/models-crud.test.sh                  # working tree, expect PASS
#   tests/models-crud.test.sh --baseline REF   # git:REF, expect FAIL
#
# Needs postgres server binaries (PG_BIN) and the core-api venv (CORE_API_PY).
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import fastapi, httpx, psycopg2" 2>/dev/null \
    || { echo "cannot run: $PY lacks fastapi/httpx/psycopg2 (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
GW_PID=""
cleanup() {
    [[ -n "$GW_PID" ]] && kill "$GW_PID" 2>/dev/null
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# The code under test comes from the ref; the database is always the tree's, so
# a baseline run measures the routes and the gateway, not a schema difference.
mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api companions/gateway \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/core-api" "$ROOT/companions/gateway" "$WORK/src/companions/"
fi

echo "model catalog CRUD (${BASELINE:+git:}${BASELINE:-working tree})"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/$f" >>"$WORK/apply.log" 2>&1 \
        || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
done
"${PSQL[@]}" -d smartnode -c "
  INSERT INTO sn_config.agents (slug, title, model, capability) VALUES
    ('cfg1', 'cfg1', 'claude-cli/sonnet', 'configurator'),
    ('dev1', 'dev1', 'claude-cli/sonnet', 'developer');
  INSERT INTO sn_config.users (email, role, tenant_id, active)
    SELECT s, 'agent', (SELECT min(id) FROM sn_config.tenants), true
    FROM unnest(ARRAY['cfg1','dev1']) s ON CONFLICT DO NOTHING;

  -- The fixture agents are added AFTER B02-sn-core-schema.sql ran, so the
  -- schema's actor_id backfill and class_default actor_roles seed never saw
  -- them. Reconstruct what the schema would have given them, so the role
  -- resolver (companions/core-api/permissions.py _effective_role) sees a real
  -- actor row and the configurator carries the class default. cfg1 gets the
  -- admin class default; dev1 is left WITHOUT an actor_roles row on purpose,
  -- so the add-other case below exercises a role-less caller: _effective_role
  -- resolves to 'user', and the chokepoint has to refuse it on the role
  -- grant (not on capability).
  INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) VALUES
    ('agent:cfg1', 'cfg1', 'agent', 'specialist', 'agent', 'internal'),
    ('agent:dev1', 'dev1', 'agent', 'specialist', 'agent', 'internal')
    ON CONFLICT (id) DO NOTHING;
  UPDATE sn_config.agents SET actor_id = 'agent:' || slug
   WHERE slug IN ('cfg1','dev1') AND actor_id IS NULL;
  INSERT INTO sn_core.actor_roles (actor_id, role, source)
    VALUES ('agent:cfg1', 'admin', 'class_default')
    ON CONFLICT (actor_id) DO NOTHING;" >/dev/null \
    || { echo "cannot run: fixture agents" >&2; exit 2; }

# Stand-in gateway: /api/v1/backends answered from UPSTREAMS of the gateway
# source under test (no key store here, so every http upstream is not_configured).
# A ref without UPSTREAMS has no such endpoint, and the stand-in answers 404.
cat > "$WORK/gw.py" <<'PY'
import ast, json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
src = open(sys.argv[1]).read()
ups = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "UPSTREAMS" for t in node.targets):
        ups = ast.literal_eval(node.value)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path != "/api/v1/backends" or ups is None:
            self.send_response(404); self.end_headers(); return
        body = {"backends": [{"id": k, "kind": v,
                              "status": "available" if v == "cli" else "not_configured",
                              "key_ref": None if v == "cli" else f"gateway/{k}-key"}
                             for k, v in sorted(ups.items())]}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(data)
srv = HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PY
exec 3< <(python3 "$WORK/gw.py" "$WORK/src/companions/gateway/backends/__init__.py")
GW_PID=$!
read -r -t 10 GW_PORT <&3 || { echo "cannot run: stand-in gateway did not start" >&2; exit 2; }

cat > "$WORK/drive.py" <<'PY'
import os, sys, json
sys.path.insert(0, os.environ["CORE_API_DIR"])
from fastapi import FastAPI
from fastapi.testclient import TestClient
import auth
from auth import Principal

try:
    from routes import models as models_route
except Exception as e:
    print(f"FAIL import  routes.models does not import: {e}"); sys.exit(0)

who = {"p": None}
app = FastAPI()
app.include_router(models_route.router)
app.dependency_overrides[auth.get_principal] = lambda: who["p"]
c = TestClient(app)
from db import cursor

# actor_id anchors the principal to its sn_core.actor_roles row; the
# chokepoint in companions/core-api/permissions.py reads it there, not from
# Principal.role. OWNER rides the seed's user:admin / super-admin pair;
# CFG rides the fixture's agent:cfg1 / admin (configurator class default);
# DEV has no actor_id so _effective_role resolves to 'user' (role-less call).
OWNER = Principal(username="admin", user_id=1, tenant_id=1, role="super-admin",
                  actor_id="user:admin", actor_kind="human", strength="session")
CFG = Principal(username="cfg1", user_id=2, tenant_id=1, role="agent",
                actor_id="agent:cfg1", actor_kind="agent", strength="proved")
DEV = Principal(username="dev1", user_id=3, tenant_id=1, role="agent", strength="proved")

def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))

def q(sql, args=()):
    with cursor(commit=True) as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None

def post(p, body):
    who["p"] = p
    return c.post("/api/models", json=body)

GROK = {"provider": "openrouter", "model": "x-ai/grok-9", "vendor": "x-ai", "label": "Grok 9"}

# providers
who["p"] = DEV
r = c.get("/api/models/providers")
ids = {x["id"]: x for x in r.json().get("providers", [])} if r.status_code == 200 else {}
res("providers", r.status_code == 200 and {"claude-cli", "openrouter", "gemini"} <= set(ids)
    and ids["openrouter"].get("key_ref") == "gateway/openrouter-key"
    and "ask the owner" in (ids["openrouter"].get("missing") or "")
    and ids["claude-cli"].get("status") == "available"
    and "cfg1" in ids["claude-cli"].get("agents", []),
    f"{r.status_code} {r.text[:200]}")

# add-owner
r = post(OWNER, GROK)
listed = [m["id"] for m in c.get("/api/models").json().get("models", [])]
ev = q("SELECT count(*) AS n FROM sn_core.events WHERE event_type = 'model.added' "
       "AND entity_id = 'openrouter/x-ai/grok-9'")
res("add-owner", r.status_code == 201 and "openrouter/x-ai/grok-9" in listed and ev and ev[0]["n"] == 1,
    f"{r.status_code} {r.text[:200]} listed={listed}")

# add-config
r = post(CFG, dict(GROK, model="openai/gpt-7", vendor="openai", label="GPT 7"))
res("add-config", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

# add-other
r = post(DEV, dict(GROK, model="x-ai/grok-10", label="Grok 10"))
n = q("SELECT count(*) AS n FROM sn_config.models WHERE model = 'x-ai/grok-10'")[0]["n"]
res("add-other", r.status_code == 403 and n == 0, f"{r.status_code} {r.text[:200]} rows={n}")

# bad-provider
r = post(OWNER, dict(GROK, provider="openai", model="gpt-4o", vendor="openai"))
res("bad-provider", r.status_code == 400 and "openrouter" in r.text, f"{r.status_code} {r.text[:200]}")

# duplicate
r = post(OWNER, GROK)
res("duplicate", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

# in-use
q("INSERT INTO sn_config.agents (slug, title, model) VALUES ('grokker', 'grokker', 'openrouter/x-ai/grok-9')")
who["p"] = OWNER
r1 = c.patch("/api/models/openrouter/x-ai/grok-9", json={"enabled": False})
r2 = c.delete("/api/models/openrouter/x-ai/grok-9")
row = q("SELECT enabled FROM sn_config.models WHERE provider='openrouter' AND model='x-ai/grok-9'")
res("in-use", r1.status_code == 409 and "grokker" in r1.text and r2.status_code == 409
    and row and row[0]["enabled"] is True, f"{r1.status_code} {r1.text[:120]} / {r2.status_code}")

# edit-delete
r1 = c.patch("/api/models/openrouter/openai/gpt-7", json={"label": "GPT 7 (OpenRouter)"})
r2 = c.delete("/api/models/openrouter/openai/gpt-7")
left = q("SELECT count(*) AS n FROM sn_config.models WHERE model = 'openai/gpt-7'")[0]["n"]
res("edit-delete", r1.status_code == 200 and r1.json().get("label") == "GPT 7 (OpenRouter)"
    and r2.status_code == 204 and left == 0, f"{r1.status_code} {r1.text[:120]} / {r2.status_code} left={left}")

# gw-down
os.environ["AI_GATEWAY_URL"] = "http://127.0.0.1:9"
r = post(OWNER, dict(GROK, model="x-ai/grok-11", label="Grok 11"))
n = q("SELECT count(*) AS n FROM sn_config.models WHERE model = 'x-ai/grok-11'")[0]["n"]
res("gw-down", r.status_code == 502 and n == 0, f"{r.status_code} {r.text[:200]} rows={n}")
PY

CORE_API_DIR="$WORK/src/companions/core-api" AI_GATEWAY_URL="http://127.0.0.1:$GW_PORT" \
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    "$PY" "$WORK/drive.py" >"$WORK/drive.out" 2>"$WORK/drive.err"
[[ -s "$WORK/drive.out" ]] || { echo "cannot run: driver produced nothing" >&2; tail -20 "$WORK/drive.err" >&2; exit 2; }

# Gateway side, read from source: the dispatcher and the report name one set, and
# the model check consults the catalog.
python3 - "$WORK/src/companions/gateway" >>"$WORK/drive.out" <<'PY'
import ast, sys, os
gw = sys.argv[1]
init = open(os.path.join(gw, "backends/__init__.py")).read()
tree = ast.parse(init)
ups = None
for n in tree.body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "UPSTREAMS" for t in n.targets):
        ups = set(ast.literal_eval(n.value))
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_backend"), None)
branches = set()
for n in ast.walk(fn) if fn else []:
    if isinstance(n, ast.Compare) and getattr(n.left, "id", "") == "upstream":
        for cmp in n.comparators:
            if isinstance(cmp, ast.Constant):
                branches.add(cmp.value)
ok = ups is not None and ups == branches
print(("ok   " if ok else "FAIL ") + "one-table" + ("" if ok else f"  UPSTREAMS={ups} branches={branches}"))

main = open(os.path.join(gw, "main.py")).read()
# The gate moved to model_catalog.model_allowed when the gateway became a door
# and a runtime: both processes ask it, so it lives in a module of its own.
mtree = ast.parse(open(os.path.join(gw, "model_catalog.py")).read())
f = next((n for n in mtree.body if isinstance(n, ast.FunctionDef) and n.name == "model_allowed"), None)
if f is None or '"/api/v1/backends"' not in main:
    print("FAIL catalog-gate  gateway has no model_allowed / /api/v1/backends")
else:
    calls = {"rows": [], "raise": False}
    class DB:
        @staticmethod
        def query(sql, args=None, fetch=False, **k):
            if calls["raise"]:
                raise RuntimeError("down")
            return calls["rows"]
    import logging
    ns = {"db": DB, "ALLOWED_MODELS": {"gemini": {"gemini-pro"}}, "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=[f], type_ignores=[]), "main.py", "exec"), ns)
    allowed = ns["model_allowed"]
    a = allowed("gemini", "gemini-pro", "gemini")
    calls["rows"] = [(1,)]; b = allowed("gemini", "gemini-9-ultra", "gemini")
    calls["rows"] = [];     c = allowed("gemini", "gemini-9-ultra", "gemini")
    calls["raise"] = True;  d = allowed("gemini", "gemini-9-ultra", "gemini")
    ok = a and b and not c and not d
    print(("ok   " if ok else "FAIL ") + "catalog-gate" + ("" if ok else f"  {a} {b} {c} {d}"))
    # every place that checked ALLOWED_MODELS directly now goes through the helper
    # No caller in the door or the runtime reads the built-in list itself; the
    # one read of it is inside model_allowed.
    runtime = open(os.path.join(gw, "runtime_main.py")).read() if os.path.exists(os.path.join(gw, "runtime_main.py")) else ""
    direct = main.count("ALLOWED_MODELS.get(") + runtime.count("ALLOWED_MODELS.get(")
    print(("ok   " if direct == 0 else "FAIL ") + "catalog-gate-used"
          + ("" if direct == 0 else f"  {direct} direct ALLOWED_MODELS checks remain"))
PY

sed 's/^/  /' "$WORK/drive.out"
FAILED=$(grep -c '^FAIL' "$WORK/drive.out")
PASSED=$(grep -c '^ok' "$WORK/drive.out")
# A driver that dies mid-way prints fewer lines, not a FAIL: count them.
EXPECTED=12
if [[ $((FAILED + PASSED)) -lt $EXPECTED ]]; then
    echo "  FAIL only $((FAILED + PASSED)) of $EXPECTED checks ran; driver stderr:"
    tail -3 "$WORK/drive.err" | sed 's/^/    /'
    FAILED=$((FAILED + EXPECTED - FAILED - PASSED))
fi
if [[ "$FAILED" -gt 0 ]]; then
    echo "FAIL: $FAILED of $((FAILED + PASSED))"
    exit 1
fi
echo "PASS: $PASSED checks"
