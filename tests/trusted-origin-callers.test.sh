#!/usr/bin/env bash
# Every node component that calls one of core-api's internal callbacks presents
# a trusted origin: the service credential, or the unix socket.
#
# core-api admits these callbacks by where they come from. A caller that sends
# them bare, or with only a name typed in a header, is refused, and because the
# callers are best-effort the refusal is silent: events stop being written and
# call transcripts stop being stored with nothing failing loudly. This reads the
# callers and fails on the first one that would be refused.
#
# SN_TEST_ROOT points the same checks at another copy of the tree.
set -euo pipefail

ROOT="${SN_TEST_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${PYTHON:-python3}"
fail=0
pass() { echo "PASS: $*"; }
bad()  { echo "FAIL: $*"; fail=1; }

# ---- 1. every call site of an internal callback carries the proof ---------
if "$PY" - "$ROOT" <<'PYEOF'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
# Routes admitted by trusted origin, as they appear in a caller's URL.
ROUTES = re.compile(
    r"/api/events[\"' ]|/api/events$|Request\(CORE,|/api/messages/inbound|/api/messages/record-voice"
    r"|/api/messages/voice-call-ended|/api/messages/record-webchat"
    r"|/api/messages/resolve-recipient|/api/limits/turn-bound"
    r"|/api/desks/\{desk_id\}/ensure|\"/api/workflows\"")
PROOF = re.compile(r"service_headers\(\)|_origin_headers\(\)")
FILES = [
    "companions/brin/runner.py",
    "companions/brin/main.py",
    "companions/brin/core/executor.py",
    "companions/brin/core/fast_tier.py",
    "companions/brin/services/ledger_service.py",
    "companions/brin/services/message_watcher.py",
    "companions/brin/services/task_watcher.py",
    "companions/voice/backend/main.py",
    "companions/flows/run-workflow.py",
    "companions/brin/core/turn_budget.py",
]
bare = []
seen = 0
for rel in FILES:
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not ROUTES.search(line):
            continue
        # The call that owns this URL: from the nearest '.post(' / '.get('
        # above it to the end of the statement a few lines below.
        start = i
        call = r"\.(post|get|put)\(|Request\("
        while start > 0 and not re.search(call, lines[start]):
            start -= 1
            if i - start > 3:
                break
        if not re.search(call, lines[start]):
            continue                      # a comment or a docstring, not a call
        if re.search(r"\.get\(", lines[start]) and "/api/events" in line:
            continue                      # reading the event log is not a callback
        seen += 1
        window = "\n".join(lines[start:i + 4])
        if not PROOF.search(window):
            bare.append(f"{rel}:{i + 1}: {line.strip()[:90]}")
if seen < 19:
    print(f"only {seen} call sites found; the scan no longer matches the callers")
    sys.exit(1)
for b in bare:
    print("bare call:", b)
sys.exit(1 if bare else 0)
PYEOF
then pass "brin, voice and the workflow launcher prove every internal callback"
else bad "an internal callback is sent without the service proof"
fi

# ---- 1b. no node service types a name instead of proving itself -----------
# A service's own name in Remote-User is no account: identity refuses it on
# every declared route, and the refusal is silent because the callers are
# best-effort. The dispatcher stops seeing pending work, tools vanish from a
# turn, the gateway loses the prompt.
typed=$("$PY" - "$ROOT" <<'PYEOF'
import glob
import re
import sys

root = sys.argv[1]
files = []
for pat in ("companions/brin/*.py", "companions/brin/core/*.py", "companions/brin/services/*.py",
            "companions/gateway/*.py", "companions/gateway/backends/*.py"):
    files += glob.glob(root + "/" + pat)
typed = re.compile(r'"Remote-User": *("(brin|runner|gateway|voice)"|ACTOR)')
for path in sorted(files):
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines):
        if not typed.search(line):
            continue
        # The same header also goes to the planner and to the document store,
        # which keep their own rules; only calls to core-api are judged here.
        near = " ".join(lines[max(0, i - 3):i + 1])
        if re.search(r"DOCI|PLANNER|planner|X-API-Key|def _headers", near + " ".join(lines[i:i + 3])):
            continue
        print("%s:%d" % (path[len(root) + 1:], i + 1))
PYEOF
)
if [[ -z "$typed" ]]; then
    pass "brin and the gateway name themselves to core-api only with a proof"
else
    bad "a service types its own name to core-api: $(head -3 <<<"$typed" | tr '\n' ' ')"
fi

# ---- 2. the proof has no bare form -----------------------------------------
if "$PY" - "$ROOT" <<'PYEOF'
import os
import sys

sys.path.insert(0, sys.argv[1] + "/companions/brin")
try:
    from core.service_proof import ServiceProofMissing, service_headers
except ImportError as exc:
    print("no service proof module:", exc)
    sys.exit(1)

os.environ.pop("SN_SERVICE_AUTH_SECRET", None)
try:
    service_headers()
except ServiceProofMissing as exc:
    assert "SN_SERVICE_AUTH_SECRET" in str(exc), exc
else:
    print("a missing credential produced headers")
    sys.exit(1)

os.environ["SN_SERVICE_AUTH_SECRET"] = " abc \n"
h = service_headers()
assert h == {"X-SN-Service": "brin", "X-SN-Service-Auth": "abc"}, h
# Its own callbacks act for nobody: naming an agent would turn the call into a
# delegation and measure it against that agent.
assert "X-SN-On-Behalf-Of" not in h and "Remote-User" not in h
PYEOF
then pass "brin's proof names the service, acts for nobody, and refuses to go bare"
else bad "brin's service proof"
fi

# ---- 3. the cron wrapper reports over the socket ---------------------------
RUNNER="$ROOT/bootstrap/cron-runner.sh"
calls=$(grep -c 'api/crons/' "$RUNNER" || true)
on_socket=$(grep -B2 'api/crons/' "$RUNNER" | grep -c -- '--unix-socket' || true)
if [[ "$calls" -ge 2 && "$on_socket" == "$calls" ]] \
   && ! grep -q 'Remote-User' "$RUNNER" \
   && ! grep -qE 'https?://(127\.0\.0\.1|\$CORE_API)' "$RUNNER"; then
    pass "cron wrapper reports its runs over the core-api socket ($calls calls)"
else
    bad "cron wrapper: $calls run calls, $on_socket on the socket, or a typed identity remains"
fi

# ---- 3b. the installer's own calls go over the socket ----------------------
INTG="$ROOT/companions/_shared/integration.sh"
if [[ "$(grep -c -- '--unix-socket "\$SN_CORE_API_SOCKET"' "$INTG")" == 2 ]] \
   && ! grep -q 'Remote-User' "$INTG" \
   && grep -A3 "core-api /api/system/status reachable" "$ROOT/companions/core-api.sh" \
        | grep -q -- "--unix-socket '\$CORE_API_UDS' 'http://core-api/api/system/status'"; then
    pass "installer registers and checks status over the core-api socket"
else
    bad "an installer call to core-api still goes over TCP or types an identity"
fi

# No installer script names the owner as a literal: the owner is whoever this
# node was configured with, and root proves itself on the socket.
literal=$(grep -nE "Remote-User: (admin|installer)" "$ROOT"/companions/*.sh "$ROOT"/companions/_shared/*.sh \
    "$ROOT"/bootstrap/*.sh 2>/dev/null | grep -vE '^[^:]+:[0-9]+: *#' || true)
if [[ -z "$literal" ]]; then
    pass "no installer script types the owner's or an installer's name to core-api"
else
    bad "an installer script types a name: $(head -2 <<<"$literal" | cut -c1-160 | tr '\n' ' ')"
fi

# ---- 3c. the version recorder writes its event over the socket -------------
if "$PY" - "$ROOT/node-versioning/sn-version" <<'PYEOF'
import http.server
import importlib.machinery
import importlib.util
import json
import os
import socketserver
import sys
import tempfile
import threading

seen = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        seen.update(path=self.path, headers={k.lower(): v for k, v in self.headers.items()},
                    body=json.loads(self.rfile.read(length)))
        self.send_response(201)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def address_string(self):
        return "socket"

    def log_message(self, *args):
        pass


work = tempfile.mkdtemp(prefix="sn-ver-")
path = os.path.join(work, "api.sock")
server = socketserver.UnixStreamServer(path, Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["SN_CORE_API_SOCKET"] = path
os.environ.pop("SN_CORE_API", None)
try:
    loader = importlib.machinery.SourceFileLoader("sn_version_under_test", sys.argv[1])
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    ok = module.emit("9.9.9", "fingerprint", [])
finally:
    server.shutdown()
    server.server_close()
    os.unlink(path)
    os.rmdir(work)
if ok is not True or seen.get("path") != "/api/events":
    print("the event did not arrive on the socket:", ok, seen.get("path"))
    sys.exit(1)
if "remote-user" in seen["headers"]:
    print("a typed identity was sent:", seen["headers"]["remote-user"])
    sys.exit(1)
if seen["body"].get("event_type") != "node.composition.changed":
    print("unexpected body:", seen["body"])
    sys.exit(1)
PYEOF
then pass "version recorder writes its event over the core-api socket, with no typed identity"
else bad "version recorder does not reach core-api over the socket"
fi

# ---- 3d. the prompt recomposer proves itself as core-api's own script ------
# The installer runs it as core-api's service account after an update. It
# named itself in a header, which is no account, so every recomposition was
# refused once the routes declared a permission.
if "$PY" - "$ROOT/companions/core-api/companions/compose-prompt.py" <<'PYEOF'
import importlib.util
import os
import sys
import urllib.request

sent = []


class Answer:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"composed"


def fake(req, timeout=None):
    sent.append((req.get_method(), req.full_url, {k.lower(): v for k, v in req.header_items()}))
    return Answer()


urllib.request.urlopen = fake
os.environ["SN_CORE_API_URL"] = "http://core-api.test"


def run(secret):
    sent.clear()
    if secret is None:
        os.environ.pop("SN_SERVICE_AUTH_SECRET", None)
    else:
        os.environ["SN_SERVICE_AUTH_SECRET"] = secret
    spec = importlib.util.spec_from_file_location("compose_under_test", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.argv[1:] = [sys.argv[1], "someagent"]
    old = sys.argv
    sys.argv = ["compose-prompt.py", "someagent"]
    try:
        mod.main()
    finally:
        sys.argv = old


run("the-credential")
if [m for m, _u, _h in sent] != ["POST", "GET"]:
    print("unexpected calls:", sent); sys.exit(1)
for method, url, headers in sent:
    if headers.get("x-sn-service") != "core-api" or headers.get("x-sn-service-auth") != "the-credential":
        print("call without the service proof:", method, url, headers); sys.exit(1)
    if "remote-user" in headers or "x-sn-on-behalf-of" in headers:
        print("a typed or borrowed identity was sent:", headers); sys.exit(1)
try:
    run(None)
except SystemExit as exc:
    if "SN_SERVICE_AUTH_SECRET" not in str(exc):
        print("missing credential not named:", exc); sys.exit(1)
else:
    print("ran without the credential"); sys.exit(1)
if sent:
    print("a call was sent without the credential:", sent); sys.exit(1)
PYEOF
then pass "prompt recomposer proves itself as core-api and has no bare form"
else bad "prompt recomposer types a name or goes bare"
fi
if grep -A12 '^recompose_agent_prompts()' "$ROOT/companions/core-api.sh" | grep -q 'export SN_SERVICE_AUTH_SECRET' \
   && ! grep -A12 '^recompose_agent_prompts()' "$ROOT/companions/core-api.sh" | grep -qE 'env .*SN_SERVICE_AUTH_SECRET='; then
    pass "installer hands the recomposer the credential in its environment, not on a command line"
else
    bad "installer does not hand the recomposer the credential, or shows it on a command line"
fi

# ---- 3e. the integration provisioner reads over the socket -----------------
if "$PY" - "$ROOT/companions/tools/integration" <<'PYEOF'
import http.server
import importlib.machinery
import importlib.util
import os
import socketserver
import sys
import tempfile
import threading

seen = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen.update(path=self.path, headers={k.lower(): v for k, v in self.headers.items()})
        body = b'{"servers": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "socket"

    def log_message(self, *args):
        pass


work = tempfile.mkdtemp(prefix="sn-intg-")
path = os.path.join(work, "api.sock")
server = socketserver.UnixStreamServer(path, Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["SN_CORE_API_SOCKET"] = path
os.environ["SN_CORE_API_URL"] = "http://127.0.0.1:9"      # a dead TCP address: must not be used
try:
    loader = importlib.machinery.SourceFileLoader("integration_under_test", sys.argv[1])
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    answer = module._api_get("/api/mcp/servers")
finally:
    server.shutdown()
    server.server_close()
    os.unlink(path)
    os.rmdir(work)
if answer != {"servers": []} or seen.get("path") != "/api/mcp/servers":
    print("the read did not go over the socket:", answer, seen.get("path")); sys.exit(1)
if "remote-user" in seen["headers"]:
    print("a typed identity was sent:", seen["headers"]["remote-user"]); sys.exit(1)
PYEOF
then pass "integration provisioner reads core-api over the socket, with no typed identity"
else bad "integration provisioner does not reach core-api over the socket"
fi

# ---- 3f. the voice line reads as the configured owner, not as a literal ----
VOICE="$ROOT/companions/voice/backend/main.py"
if ! grep -q '"Remote-User": "admin"' "$VOICE" \
   && grep -q 'SN_OPERATOR_ACTOR' "$VOICE" \
   && grep -q 'SN_OPERATOR_ACTOR=' "$ROOT/companions/voice.sh" \
   && "$PY" - "$VOICE" <<'PYEOF'
import ast
import os
import sys

tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == "_owner_read_headers"), None)
if fn is None:
    print("no _owner_read_headers in the voice backend"); sys.exit(1)
ns = {"os": os}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "voice", "exec"), ns)
os.environ["SN_OPERATOR_ACTOR"] = " owner@example.test "
assert ns["_owner_read_headers"]() == {"Remote-User": "owner@example.test"}
os.environ.pop("SN_OPERATOR_ACTOR")
try:
    ns["_owner_read_headers"]()
except RuntimeError as exc:
    assert "SN_OPERATOR_ACTOR" in str(exc), exc
else:
    print("a read was built with no owner configured"); sys.exit(1)
PYEOF
then pass "voice reads as the owner this node was configured with, and refuses to guess one"
else bad "voice names the owner as a literal, or guesses one"
fi

# ---- 4. the installer names what core-api reads ----------------------------
CORE_SH="$ROOT/companions/core-api.sh"
PERMS="$ROOT/companions/core-api/permissions.py"
SEED="$ROOT/bootstrap/B02-sn-core-schema.sql"
if grep -q '^SN_SERVICE_PERMISSIONS=\$SERVICE_PERMISSIONS_DEFAULT' "$CORE_SH" \
   && grep -q '^Environment=SN_COMPONENTS_GROUP=\$SMART_NODE_GROUP' "$CORE_SH" \
   && grep -q '"SN_SERVICE_PERMISSIONS"' "$PERMS" \
   && grep -q '"SN_COMPONENTS_GROUP"' "$PERMS"; then
    pass "installer writes the two settings the permission check reads"
else
    bad "installer and permission check disagree on SN_SERVICE_PERMISSIONS / SN_COMPONENTS_GROUP"
fi

# Every name the installer lists for a service is a catalogue row a service can
# be admitted to: it exists in the seed and has no object axis.
if "$PY" - "$CORE_SH" "$SEED" <<'PYEOF'
import re
import sys

sh = open(sys.argv[1], encoding="utf-8").read()
seed = open(sys.argv[2], encoding="utf-8").read()
m = re.search(r'^SERVICE_PERMISSIONS_DEFAULT="\$\{SMART_NODE_SERVICE_PERMISSIONS:-([^}]*)\}"', sh, re.M)
if not m:
    print("no SERVICE_PERMISSIONS_DEFAULT in the installer")
    sys.exit(1)
admitted = set(re.findall(r"^    \('([a-z_.]+)', '(?:[^']|'')*', 'none'\),?$", seed, re.M))
listed = {}
for clause in m.group(1).split(";"):
    svc, _, names = clause.partition(":")
    listed[svc] = [n for n in names.split(",") if n]
unknown = [(s, n) for s, ns in listed.items() for n in ns if n not in admitted]
if unknown or not all(listed.get(s) for s in ("brin", "voice", "flows")):
    print("installer list:", listed, "unknown:", unknown)
    sys.exit(1)
PYEOF
then pass "every permission the installer lists is a seeded permission with no object axis"
else bad "installer lists a permission a service cannot be admitted to"
fi

[[ "$fail" == 0 ]] && echo "ALL PASS" || { echo "FAILED"; exit 1; }
