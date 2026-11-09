#!/usr/bin/env bash
#
# An agent on an HTTP model can read a planner task, its log, comment on it and
# move it, like a CLI colleague.
#
# WHAT WENT WRONG. brin sends an api row as endpoint + http_path with the
# arguments in the query or the body; it never filled a path parameter. Every
# planner route that names a task in its path (/api/v1/tasks/{task_id}...) was
# therefore out of reach, and the node gave an HTTP agent only `planner` (file)
# and `planner_tasks` (list). HTTP-model agent, woken for a task, could not read it, log on
# it or move it; a CLI agent had all of that through the tracking MCP server.
#
# WHAT IS CHECKED
#   fill            {task_id} is filled from the arguments and removed from them
#   missing         a missing path argument is named, nothing is sent
#   one-segment     a value cannot add path segments (URL-quoted whole)
#   wire-get        brin's real _dispatch_api, against a local server: a GET row
#                   with {task_id} requests /api/v1/tasks/7, task_id not in the query
#   wire-patch      a PATCH row sends /api/v1/tasks/7 with body {status} only
#   seed-rows       seed-tools.py defines planner_task, _comments, _search,
#                   _comment, _advance against integration planner, {task_id}
#                   in the paths that need it
#   pair-rw         a planner rw- grant brings the reads at r-- and both writes
#   pair-ro         a planner r-- grant brings the reads and NO write
#   stale-text      tracking.sh no longer tells the agent that filing is the only call
#   proxy-proved    core-api's planner proxy: a call carried by a verified service for
#                   that same agent reaches the planner as 'proved', so a write
#                   (planner_comment, planner_advance) is not refused as asserted
#   proxy-asserted  without that proof, or for another agent, it stays 'asserted'
#
#   tests/api-tool-path-params.test.sh                  # tree, PASS
#   tests/api-tool-path-params.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
PY="${BRIN_PY:-/opt/smart-node/dispatcher/venv/bin/python}"
"$PY" -c "import httpx" 2>/dev/null || { echo "cannot run: $PY lacks httpx (set BRIN_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/brin companions/core-api companions/tracking.sh \
        | tar -x -C "$WORK" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/companions"
    cp -r "$ROOT/companions/brin" "$ROOT/companions/core-api" "$ROOT/companions/tracking.sh" "$WORK/companions/"
fi
echo "api tools with path parameters (${BASELINE:+git:}${BASELINE:-working tree})"

cat > "$WORK/drive.py" <<'PY'
import ast, asyncio, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
W = sys.argv[1]
sys.path.insert(0, os.path.join(W, "companions/brin"))

def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))

try:
    from core import catalog_tools as ct
except Exception as e:
    print(f"FAIL import  {e}"); sys.exit(0)

fp = getattr(ct, "fill_path", None)
if fp is None:
    for n in ("fill", "missing", "one-segment"):
        res(n, False, "brin has no fill_path")
else:
    p, rest, miss = fp("/api/v1/tasks/{task_id}/comments", {"task_id": 4918, "content": "x"})
    res("fill", p == "/api/v1/tasks/4918/comments" and rest == {"content": "x"} and miss == [], (p, rest, miss))
    p, rest, miss = fp("/api/v1/tasks/{task_id}", {"status": "done"})
    res("missing", miss == ["task_id"], (p, rest, miss))
    p, rest, miss = fp("/api/v1/tasks/{task_id}", {"task_id": "7/../../secrets"})
    res("one-segment", p == "/api/v1/tasks/7%2F..%2F..%2Fsecrets", p)
    p, rest, miss = fp("/api/v1/tasks/{task_id}/children/{task_id}", {"task_id": 7})
    res("fill-twice", p == "/api/v1/tasks/7/children/7" and miss == [] and rest == {},
        (p, rest, miss))

# The real dispatcher against a local server that records what arrives.
seen = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _rec(self):
        n = int(self.headers.get("content-length") or 0)
        seen.append((self.command, self.path, self.rfile.read(n).decode() if n else ""))
        self.send_response(200); self.send_header("content-type", "application/json")
        self.end_headers(); self.wfile.write(b"{}")
    do_GET = do_PATCH = do_POST = _rec
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ep = f"http://127.0.0.1:{srv.server_address[1]}/api/planner"

inst = ct.CatalogTools.__new__(ct.CatalogTools) if hasattr(ct, "CatalogTools") else None
cls = next((getattr(ct, n) for n in dir(ct) if isinstance(getattr(ct, n), type)
            and hasattr(getattr(ct, n), "_dispatch_api")), None)
if cls is None:
    res("wire-get", False, "no class with _dispatch_api"); res("wire-patch", False, "")
else:
    obj = cls.__new__(cls); obj.agent_slug = "http-agent"
    ct._api_headers = lambda r, slug: {}
    row = {"name": "planner_task", "endpoint": ep, "http_method": "GET", "http_path": "/api/v1/tasks/{task_id}"}
    out = asyncio.run(obj._dispatch_api(row, {"task_id": 7}))
    got = seen[-1] if seen else None
    res("wire-get", got is not None and got[0] == "GET" and got[1] == "/api/planner/api/v1/tasks/7", (out[:80], got))
    row = {"name": "planner_advance", "endpoint": ep, "http_method": "PATCH", "http_path": "/api/v1/tasks/{task_id}"}
    n0 = len(seen)
    out = asyncio.run(obj._dispatch_api(row, {"task_id": 7, "status": "in_progress"}))
    got = seen[-1] if len(seen) > n0 else None
    res("wire-patch", got is not None and got[1] == "/api/planner/api/v1/tasks/7"
        and json.loads(got[2] or "{}") == {"status": "in_progress"}, (out[:80], got))

    # planner_advance is a PATCH of the whole task. Its schema is what keeps it to
    # a status transition, so the schema has to be enforced where the call is made.
    closed = {"type": "object", "additionalProperties": False,
              "required": ["task_id", "status"],
              "properties": {"task_id": {"type": "integer"},
                             "status": {"type": "string"},
                             "comment": {"type": "string"}}}
    row = {"name": "planner_advance", "endpoint": ep, "http_method": "PATCH",
           "http_path": "/api/v1/tasks/{task_id}", "input_schema": closed}
    n0 = len(seen)
    out = asyncio.run(obj._dispatch_api(row, {"task_id": 7, "status": "done",
                                              "assignee": "someone-else"}))
    res("closed-extra", len(seen) == n0 and "assignee" in out and "REFUSED" in out,
        (out[:120], len(seen) - n0))
    n0 = len(seen)
    out = asyncio.run(obj._dispatch_api(row, {"task_id": 7, "status": "done",
                                              "comment": "why"}))
    got = seen[-1] if len(seen) > n0 else None
    res("closed-declared", got is not None
        and json.loads(got[2] or "{}") == {"status": "done", "comment": "why"},
        (out[:80], got))
    openrow = {"name": "messages_send", "endpoint": ep, "http_method": "POST",
               "http_path": "/api/v1/send",
               "input_schema": {"type": "object", "properties": {"to": {"type": "string"}}}}
    n0 = len(seen)
    out = asyncio.run(obj._dispatch_api(openrow, {"to": "alex", "subject": "hi"}))
    got = seen[-1] if len(seen) > n0 else None
    res("open-unchanged", got is not None
        and json.loads(got[2] or "{}") == {"to": "alex", "subject": "hi"}, (out[:80], got))

seed = open(os.path.join(W, "companions/core-api/seed-tools.py")).read()
want = {"planner_task": "/api/v1/tasks/{task_id}", "planner_comments": "/api/v1/tasks/{task_id}/comments",
        "planner_search": "/api/v1/tasks/search", "planner_comment": "/api/v1/tasks/{task_id}/comments",
        "planner_advance": "/api/v1/tasks/{task_id}"}
ok = all(f'("{n}"' in seed and f'"{p}"' in seed for n, p in want.items()) and "'planner', %s::jsonb" in seed
res("seed-rows", ok, "planner rows missing from seed-tools.py")

# The pairing, run as written: take _PAIRED and the loop out of put_grants.
src = open(os.path.join(W, "companions/core-api/routes/agents.py")).read()
fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "put_grants")
body, grab = [], False
for st in fn.body:
    if isinstance(st, ast.Assign) and any(getattr(t, "id", "") == "_PAIRED" for t in st.targets):
        grab = True
    if grab:
        body.append(st)
        if isinstance(st, ast.For):
            break
def pair(grants):
    class B: pass
    b = B(); b.grants = dict(grants)
    exec(compile(ast.Module(body=body, type_ignores=[]), "agents.py", "exec"), {"body": b})
    return b.grants
g = pair({"planner": "rw-"})
res("pair-rw", all(g.get(n) == "r--" for n in ("planner_task", "planner_comments", "planner_search"))
    and g.get("planner_comment") == "rw-" and g.get("planner_advance") == "rw-", g)
g = pair({"planner": "r--"})
res("pair-ro", g.get("planner_task") == "r--" and "planner_comment" not in g and "planner_advance" not in g, g)

tr = open(os.path.join(W, "companions/tracking.sh")).read()
res("stale-text", "Creating is the only planner call" not in tr and "the other planner call only files new ones" not in tr,
    "tracking.sh still says filing is the only planner call")

blk = seed[seed.index("_planner_tools = ["):]
blk = blk[:blk.index("\n            ]\n")]
res("seed-closed", blk.count('"additionalProperties": False') == 5,
    f"{blk.count(chr(34) + 'additionalProperties' + chr(34) + ': False')} of 5 planner schemas are closed")
res("seed-advance-text", "and nothing else" in blk and "cannot set an assignee" in blk,
    "planner_advance still describes itself as more than a status change")
grant_sql = seed[seed.index("INSERT INTO sn_config.agent_tools", seed.index("_planner_tools = [")):]
grant_sql = grant_sql[:grant_sql.index("ON CONFLICT")]
res("seed-http-only", "cli_providers" in grant_sql,
    "the planner rows are seeded to every planner holder, whatever its runtime")
res("pair-runtime-filter", "_PLANNER_API_ROWS" in src and "_runtime_kind(slug)" in src,
    "put_grants does not filter the paired planner rows by runtime")
PY

"$PY" "$WORK/drive.py" "$WORK" >"$WORK/out" 2>"$WORK/err"

# The proxy, in the core-api venv. Only the identity plumbing is stubbed (who the
# transport says the caller is, whether a service proved it); _handle is replaced
# by a recorder so nothing reaches a database or a planner. What is measured is
# the strength the proxy hands to _handle, which is what decides whether a write
# is refused as an asserted task mutation.
cat > "$WORK/proxy.py" <<'PY'
import os, sys, types
sys.path.insert(0, os.path.join(sys.argv[1], "companions/core-api"))
# `db.cursor` and `events_emit.emit` are called by `permissions.requires` as
# well as by the planner proxy itself. The test stubs them so no Postgres is
# touched; permissions.cursor must be a working no-op context manager so the
# chokepoint's audit emit path does not raise.
from contextlib import contextmanager
class _StubCur:
    def execute(self, *a, **k): return None
    def fetchone(self): return None
    def fetchall(self): return []
@contextmanager
def _stub_cursor(*a, **k):
    yield _StubCur()
for mod in ("db", "events_emit"):
    m = types.ModuleType(mod); sys.modules[mod] = m
sys.modules["db"].cursor = _stub_cursor
sys.modules["events_emit"].emit = lambda *a, **k: {"id": 0}
def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))
try:
    from routes import planner as pl
    import permissions, auth
except Exception as e:
    print(f"FAIL proxy-import  {e}"); sys.exit(0)
# The role-axis chokepoint runs ahead of _handle. For this test it
# must say YES and not touch the database. The test's point is `_handle`'s
# strength plumbing, not the chokepoint.
permissions.cursor = _stub_cursor
permissions.check_permission = lambda principal, name, object_id=None: permissions.PermissionDecision(
    permission=name, role="admin", axis="none", object_id=object_id,
    allowed=True, reason="stub allow")
from fastapi import FastAPI
from fastapi.testclient import TestClient
seen = {}
def rec(method, subpath, qs, body, actor, ct, strength="asserted", unproved=False, op=False):
    seen.update(strength=strength, unproved=unproved)
    return 200, b"{}", "application/json"
pl._handle = rec
pl.identity_strength = lambda req: "asserted"
pl.peer_operator_identity = lambda req: None
pl.agent_identity_unproved = lambda req, actor, strength: True
pl._operator_token = lambda: ""
app = FastAPI(); app.include_router(pl.router)
app.dependency_overrides[pl.get_actor] = lambda: "http-agent"
app.dependency_overrides[auth.get_principal] = lambda: auth.Principal(
    username="http-agent", user_id=0, tenant_id=0, role="admin",
    actor_id="agent:http-agent", strength="proved")
c = TestClient(app)
pl.acting_for = lambda req: ("brin", "http-agent")
c.patch("/api/planner/api/v1/tasks/7", json={"status": "in_progress"})
res("proxy-proved", seen.get("strength") == "proved" and seen.get("unproved") is False, seen)
pl.acting_for = lambda req: None
c.patch("/api/planner/api/v1/tasks/7", json={"status": "in_progress"})
a1 = dict(seen)
pl.acting_for = lambda req: ("brin", "someone-else")
c.patch("/api/planner/api/v1/tasks/7", json={"status": "in_progress"})
res("proxy-asserted", a1.get("strength") == "asserted" and seen.get("strength") == "asserted", (a1, seen))
PY
CORE_PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
SN_AGENTS_ROOT="$WORK" SN_AGENT_TEMPLATE="$WORK" "$CORE_PY" "$WORK/proxy.py" "$WORK" >>"$WORK/out" 2>>"$WORK/err"
sed 's/^/  /' "$WORK/out"
FAILED=$(grep -c '^FAIL' "$WORK/out"); PASSED=$(grep -c '^ok' "$WORK/out")
EXPECTED=19
if [[ $((FAILED + PASSED)) -lt $EXPECTED ]]; then
    echo "  FAIL only $((FAILED + PASSED)) of $EXPECTED checks ran"; tail -3 "$WORK/err" | sed 's/^/    /'
    FAILED=$((EXPECTED - PASSED))
fi
if [[ "$FAILED" -gt 0 ]]; then echo "FAIL: $FAILED of $EXPECTED"; exit 1; fi
echo "PASS: $PASSED checks"
