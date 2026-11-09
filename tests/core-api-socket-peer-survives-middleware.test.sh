#!/usr/bin/env bash
#
# A caller on the unix socket is still recognised behind core-api's middleware.
#
# WHAT WENT WRONG. auth.peer_credentials reads the peer of a unix socket through
# request.receive.__self__, which is uvicorn's own object. The role model added
# a route gate as `@app.middleware("http")`. Starlette runs such a middleware by
# wrapping `receive` in a function of its own, so behind it there is no uvicorn
# object to reach and peer_credentials answers None for every caller. Root, the
# installer and every agent on the socket became "untrusted", and an update
# stopped at the first call the installer makes: 403 "no active user for
# identity 'untrusted'". The unit tests stand in a fake for uvicorn's object, so
# they kept passing. Only a real server on a real socket shows it.
#
# THE PROPERTIES, each on a real uvicorn bound to a unix socket:
#   1. the fault is real: behind an `@app.middleware("http")` gate the peer is lost
#   2. behind core-api's own gate class, taken from main.py, the peer is found
#   3. main.py does not register any middleware of the wrapping kind
#
#   tests/core-api-socket-peer-survives-middleware.test.sh                  # tree, PASS
#   tests/core-api-socket-peer-survives-middleware.test.sh --baseline REF   # REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PY" ]] || { echo "cannot run: needs the core-api venv (CORE_API_PY), not found at $PY" >&2; exit 2; }

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
for f in main.py auth.py; do
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:companions/core-api/$f" > "$D/$f" || exit 1
    else cp "$REPO/companions/core-api/$f" "$D/$f" || exit 1; fi
done
if [[ -n "$BASELINE" ]]; then echo "core-api: the socket peer survives the middleware (git:$BASELINE)"
else echo "core-api: the socket peer survives the middleware (working tree)"; fi

"$PY" - "$D" <<'PY'
import ast, asyncio, http.client, logging, os, re, socket, struct, sys, threading, time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

d = sys.argv[1]
failed = 0


def check(name, cond):
    global failed
    print(("  ok   " if cond else "  FAIL ") + name)
    failed += 0 if cond else 1


def take(path, names):
    """The named top-level functions and classes of a module, without importing it."""
    tree = ast.parse(open(path).read())
    return [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]


ns = {"Request": Request, "socket": socket, "struct": struct, "log": logging.getLogger("t"),
      "JSONResponse": JSONResponse, "undeclared_route": lambda request: None}
exec(compile(ast.Module(body=take(d + "/auth.py", ("peer_credentials",)), type_ignores=[]), "auth.py", "exec"), ns)
peer_credentials = ns["peer_credentials"]


def serve_and_ask(app, tag):
    """Run app on a unix socket under real uvicorn; return what GET /peer says."""
    path = "%s/%s.sock" % (d, tag)
    server = uvicorn.Server(uvicorn.Config(app, uds=path, log_level="error", http="h11", loop="asyncio"))
    t = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(path) and server.started:
            break
        time.sleep(0.05)
    conn = http.client.HTTPConnection("localhost")
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(path)
    conn.request("GET", "/peer")
    body = conn.getresponse().read().decode()
    conn.close()
    server.should_exit = True
    t.join(timeout=5)
    return body


def app_with(gate):
    app = FastAPI()

    @app.get("/peer")
    def peer(request: Request):
        creds = peer_credentials(request)
        return {"uid": None if creds is None else creds[1]}
    gate(app)
    return app


def wrapping_gate(app):
    @app.middleware("http")
    async def gate(request, call_next):
        return await call_next(request)


me = os.getuid()
check("the fault is real: behind an @app.middleware(\"http\") gate the peer is lost",
      serve_and_ask(app_with(wrapping_gate), "wrapped") == '{"uid":null}')

classes = take(d + "/main.py", ("DenyUndeclaredRoutes",))
if not classes:
    check("core-api's gate is a class that does not wrap receive (DenyUndeclaredRoutes in main.py)", False)
else:
    exec(compile(ast.Module(body=classes, type_ignores=[]), "main.py", "exec"), ns)
    got = serve_and_ask(app_with(lambda app: app.add_middleware(ns["DenyUndeclaredRoutes"])), "own")
    check("behind core-api's own gate the socket peer is found (uid %d)" % me, got == '{"uid":%d}' % me)

src = open(d + "/main.py").read()
check("main.py registers no middleware of the wrapping kind",
      not re.search(r"^\s*@app\.middleware\(", src, re.M) and "BaseHTTPMiddleware(" not in src)

print()
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
PY
