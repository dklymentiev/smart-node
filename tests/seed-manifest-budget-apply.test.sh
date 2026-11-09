#!/usr/bin/env bash
#
# A budget declared in the seed manifest must become a limit the node enforces.
#
# WHAT WENT WRONG, TWICE.
# 1. Up to 1.12.48 Phase 3.6 of install-on.sh handed psql a file inside a 0700
#    staging directory that postgres could not enter. The row never applied and
#    the install logged a WARN and exited 0.
# 2. After the limits registry arrived, Phase 3.6 still inserted the row into
#    sn_config.budgets by SQL. That table is only the meter. The limit the
#    gateway enforces is in sn_config.limit_profiles, which core-api fills when
#    a budget is set through its API. B02 copies budgets into the registry at
#    the START of an install; Phase 3.6 ran at the END. A fresh node carried
#    "5 USD a day, hard" in a table nothing enforced.
#
# THE PROPERTIES:
#   1. Phase 3.6 does not write sn_config.budgets by SQL
#   2. Phase 3.6 runs orchestrator/seed-budgets.py on the node and feeds it the
#      manifest rows
#   3. a failed apply dies; nothing logs a WARN and continues
#   4. install-on.sh parses
#   5. the helper, run against a stand-in for core-api's socket: creates a
#      budget that is absent, updates one that exists, and exits non-zero on a
#      refusal, on an unknown period, on a non-positive limit and on a dead
#      socket, without sending a partial set past a bad row
#
#   tests/seed-manifest-budget-apply.test.sh                  # tree, PASS
#   tests/seed-manifest-budget-apply.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "installer: seed-manifest budgets reach the limits registry (git:$BASELINE)"
else echo "installer: seed-manifest budgets reach the limits registry (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src orchestrator/install-on.sh > "$D/install-on.sh"
[[ -s "$D/install-on.sh" ]] || { bad "install-on.sh is readable"; echo; echo "FAIL"; exit 1; }

# Phase 3.6, from its banner to the Identity state block.
awk '/=== Phase 3\.6 \(seed manifest budgets:/{found=1} found{print} /^# Identity state file/{if (found) exit}' \
    "$D/install-on.sh" > "$D/phase.sh"
[[ -s "$D/phase.sh" ]] || { bad "Phase 3.6 block was found in install-on.sh"; echo; echo "FAIL"; exit 1; }

# 1. No SQL write to the meter table from the installer.
if grep -qE 'INSERT INTO sn_config\.budgets|psql ' "$D/phase.sh"; then
    bad "Phase 3.6 still writes budgets by SQL, past core-api and the limits registry"
else
    ok "Phase 3.6 does not write budgets by SQL"
fi

# 2. The helper is sent to the node and fed the manifest rows.
if grep -qF 'seed-budgets.py' "$D/phase.sh" \
        && grep -qE 'MANIFEST_BUDGETS_JSON.*\| *ssh ' "$D/phase.sh"; then
    ok "Phase 3.6 runs seed-budgets.py on the node with the manifest rows on stdin"
else
    bad "Phase 3.6 does not run seed-budgets.py with the manifest rows"
fi

# 3. Failure stops the install.
if grep -qE '^[[:space:]]*die "Phase 3\.6' "$D/phase.sh"; then
    ok "a failed apply calls die -- a declared limit does not fail silently"
else
    bad "a failed apply does not call die"
fi
if grep -qE 'log "WARN: Phase 3\.6' "$D/phase.sh"; then
    bad "a failed apply logs WARN and continues"
else
    ok "a failed apply does not log WARN and continue"
fi

# 4. install-on.sh parses.
if bash -n "$D/install-on.sh" 2>/dev/null; then ok "install-on.sh parses"; else bad "install-on.sh parses"; fi

# 5. The helper against a stand-in for the core-api socket.
read_src orchestrator/seed-budgets.py > "$D/seed-budgets.py"
if [[ ! -s "$D/seed-budgets.py" ]]; then
    bad "orchestrator/seed-budgets.py exists"
else
    if python3 - "$D" <<'PY'
import json, os, pathlib, socketserver, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler

work = pathlib.Path(sys.argv[1])
helper = work / "seed-budgets.py"
sock_path = str(work / "core-api.sock")
state = {"budgets": [], "calls": [], "refuse": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def address_string(self): return "unix"

    def answer(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        state["calls"].append(("GET", self.path, None))
        self.answer(200, {"budgets": state["budgets"]})

    def do_POST(self):
        body = self.body()
        state["calls"].append(("POST", self.path, body))
        if state["refuse"]:
            return self.answer(state["refuse"], {"detail": "refused by the stand-in"})
        row = dict(body, id=len(state["budgets"]) + 1)
        state["budgets"].append(row)
        self.answer(201, row)

    def do_PATCH(self):
        body = self.body()
        state["calls"].append(("PATCH", self.path, body))
        if state["refuse"]:
            return self.answer(state["refuse"], {"detail": "refused by the stand-in"})
        bid = int(self.path.rsplit("/", 1)[1])
        row = next(b for b in state["budgets"] if b["id"] == bid)
        row.update(body)
        self.answer(200, row)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

server = Server(sock_path, Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def run(rows, socket_path=sock_path, raw=None):
    state["calls"].clear()
    env = dict(os.environ, SN_CORE_API_SOCKET=socket_path)
    out = subprocess.run([sys.executable, str(helper)], input=raw if raw is not None else json.dumps(rows),
                         env=env, text=True, capture_output=True, timeout=60)
    writes = [(m, p, b) for m, p, b in state["calls"] if m != "GET"]
    return out.returncode, writes, out.stderr.strip()


daily = {"scope_type": "tenant", "scope_id": "1", "period": "daily", "limit_usd": 5.0, "hard_limit": True}

code, writes, err = run([daily])
assert code == 0, f"an absent budget: exit {code}: {err}"
assert writes == [("POST", "/api/budgets", daily)], writes
print("  ok   an absent budget is created through POST /api/budgets")

code, writes, err = run([dict(daily, limit_usd=7.5)])
assert code == 0, f"an existing budget: exit {code}: {err}"
assert writes == [("PATCH", "/api/budgets/1", {"limit_usd": 7.5, "hard_limit": True, "enabled": True})], writes
print("  ok   an existing budget is updated through PATCH, not created twice")

code, writes, err = run([{"limit_usd": 12}])
assert code == 0 and writes[0][2] == {"scope_type": "tenant", "scope_id": "1", "period": "monthly",
                                      "limit_usd": 12, "hard_limit": False}, (code, writes, err)
print("  ok   a row with only a limit means the node's monthly soft budget")

state["refuse"] = 409
code, writes, err = run([dict(daily, period="weekly")])
assert code != 0 and "409" in err, (code, err)
state["refuse"] = None
print("  ok   a refusal by core-api is a non-zero exit that names the answer")

for label, rows in (("an unknown period", [daily, dict(daily, period="yearly")]),
                    ("a zero limit", [daily, dict(daily, limit_usd=0)]),
                    ("a limit that is not a number", [daily, dict(daily, limit_usd="5")])):
    code, writes, err = run(rows)
    assert code != 0, f"{label}: exit 0"
    assert writes == [], f"{label}: wrote {writes} before refusing the set"
print("  ok   a bad row fails the whole set before anything is written")

code, writes, err = run(None, raw="not json")
assert code != 0, "non-JSON stdin: exit 0"
code, writes, err = run([daily], socket_path=str(work / "absent.sock"))
assert code != 0 and "not answering" in err, (code, err)
print("  ok   unreadable input and a dead socket are non-zero exits")
server.shutdown()
PY
    then :; else bad "seed-budgets.py behaves as the owner's path (see the assertion above)"; fi
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
