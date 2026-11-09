#!/usr/bin/env bash
#
# An owner name sent over TCP with nothing behind it is refused, and every
# caller the node itself runs still gets through by a path that proves it.
#
# WHAT WENT WRONG. core-api began refusing a non-GET request whose only identity
# is an asserted `Remote-User` naming a person. The refusal has no switch, so it
# takes effect the moment the code is deployed -- and the node's own services
# were still asserting the owner that way: the installer creating the first
# agent, the planner raising a task notification, the dispatcher deciding desk
# delivery and releasing an approved reply. Each would have answered 403 with
# nothing visibly failing: an install with no agents, a task that wakes nobody.
#
# WHAT IS CHECKED, against real code on both sides. core-api's own routers and
# its own identity dependencies are served by uvicorn on a real TCP port and a
# real unix socket (the pair companions/core-api/serve.py binds), in front of a
# throwaway postgres built from the tree's B02 files. The callers are the real
# ones: planner/notify.py, brin's message watcher and runner, and
# bootstrap/onboarding-seed.sh. No identity dependency is overridden and no HTTP
# client is stubbed, so a refusal by core-api fails the test.
#
#   bare        an asserted owner name on a mutation is refused 403, on a route
#               gated by get_principal and on one that calls get_actor, and
#               each refusal writes auth.human_tcp_assertion_denied
#   planner     the planner's notify is admitted and raises the assignee's wake
#   scope       the same proof on any other route is refused; so is a wrong
#               secret, a name that is not the owner, an unlisted service, and
#               a wildcard service clause used to name the owner
#   brin        desk delivery, closing a system message, the owner alert, the
#               approved reply and the calendar event are admitted; an agent
#               delegation by the same service still works
#   socket      a kernel-proved operator peer is the owner: it creates a desk
#               owned by the owner, and the name agent creation hands to
#               admin-runner's owner gate is the owner
#   seed        bootstrap/onboarding-seed.sh, run for real, leaves an agent
#   loud        with the owner or the credential missing, core-api answers 503
#               naming the setting and each consumer logs the setting by name
#   installer   the owner contract is written by a step that runs on a full
#               install AND on a contracts-only update, from the users table,
#               and every unit that needs it loads it
#
# TWO LIMITS, stated rather than hidden.
#   * The app is assembled from core-api's routers, not imported as main:app.
#     main:app refuses every route that has not yet declared a permission, which
#     at this tree is all of the routes below; the identity code under test is
#     the same either way.
#   * The socket peer is this test's own OS user, listed in SN_OPERATOR_USERS in
#     the place root holds on a node. The socket and SO_PEERCRED are real; a
#     root peer cannot be produced without privilege. admin-runner is a stand-in
#     that records the name it was given.
#
# USAGE
#   tests/human-tcp-proof.test.sh                  # working tree, expect PASS
#   tests/human-tcp-proof.test.sh --baseline REF   # git:REF, expect FAIL
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
"$PY" -c "import fastapi, httpx, psycopg2, uvicorn" 2>/dev/null \
    || { echo "cannot run: $PY lacks fastapi/httpx/psycopg2/uvicorn (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    # Pid files, not a shell array: the servers are started inside command
    # substitutions, whose variables never reach this shell.
    local f
    for f in "$WORK"/*.pid; do [[ -r "$f" ]] && kill "$(cat "$f")" 2>/dev/null; done
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# The code under test comes from the ref; the database is always the tree's, so
# a baseline run measures the callers and the identity code, not a schema change.
SRC="$WORK/src"
mkdir -p "$SRC"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api companions/planner/src \
        companions/brin companions/core-api.sh companions/dispatcher.sh \
        companions/tracking.sh bootstrap/onboarding-seed.sh companions/srv-skeleton/agents \
        | tar -x -C "$SRC" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$SRC/companions/planner" "$SRC/companions/srv-skeleton" "$SRC/bootstrap"
    cp -r "$ROOT/companions/core-api" "$ROOT/companions/brin" "$SRC/companions/"
    cp -r "$ROOT/companions/planner/src" "$SRC/companions/planner/"
    cp -r "$ROOT/companions/srv-skeleton/agents" "$SRC/companions/srv-skeleton/"
    cp "$ROOT/companions/core-api.sh" "$ROOT/companions/dispatcher.sh" \
       "$ROOT/companions/tracking.sh" "$SRC/companions/"
    cp "$ROOT/bootstrap/onboarding-seed.sh" "$SRC/bootstrap/"
fi

echo "human identity over TCP: proof required, node callers proved (${BASELINE:+git:}${BASELINE:-working tree})"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
apply_b02() {
    local f
    for f in "$@"; do
        "${PSQL[@]}" -d smartnode -f "$ROOT/bootstrap/$f" >>"$WORK/apply.log" 2>&1 \
            || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
    done
}
apply_b02 B02-schema.sql B02-sn-core-schema.sql B02-sn-core-seed.sql

free_port() {
    "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'
}

# Stand-in admin-runner. It answers the one operation agent creation calls and
# records who core-api said the caller was, which is the fact under test.
RUNNER_PORT=$(free_port)
RUNNER_LOG="$WORK/admin-runner.calls"
: > "$RUNNER_LOG"
cat > "$WORK/runner.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
log = sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        with open(log, "a") as f:
            f.write(json.dumps({"path": self.path,
                                "service": self.headers.get("X-SN-Service"),
                                "on_behalf_of": self.headers.get("X-SN-On-Behalf-Of")}) + "\n")
        agents = (body.get("args") or {}).get("agents") or []
        out = json.dumps({"ok": True, "result": {"ensured": len(agents), "failed": 0,
                                                  "agents": agents}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
"$PY" "$WORK/runner.py" "$RUNNER_PORT" "$RUNNER_LOG" &
echo $! > "$WORK/runner.pid"

# Fixtures. An agent to be woken and delegated for; the admin-runner address;
# a second person who is not the owner. The schema is applied again so the
# agent gets its actor row the way an updated node's agents do.
"${PSQL[@]}" -d smartnode -c "
  INSERT INTO sn_config.agents (tenant_id, slug, title, capability)
    VALUES (1, 'testagent', 'Test Agent', '');
  INSERT INTO sn_config.users (tenant_id, email, role, active)
    VALUES (1, 'testagent', 'agent', true), (1, 'other@example.test', 'admin', true);
  INSERT INTO sn_config.integrations (slug, label, kind, endpoint)
    VALUES ('admin-runner', 'Admin runner', 'system', 'http://127.0.0.1:$RUNNER_PORT');" >/dev/null \
    || { echo "cannot run: fixtures" >&2; exit 2; }
apply_b02 B02-sn-core-schema.sql
# The owner is deliberately NOT called 'admin': a code path that assumes that
# name finds nobody here.
"${PSQL[@]}" -d smartnode -c "
  UPDATE sn_config.users SET email = 'owner@example.test' WHERE email = 'admin';" >/dev/null
# The specialist the onboarding seed creates its agent from.
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    SN_SPECIALISTS_SEED="$SRC/companions/srv-skeleton/agents/_specialists" \
    "$PY" "$SRC/companions/core-api/seed-specialists.py" >"$WORK/specialists.log" 2>&1 || true

# The owner, by the installer's own definition: the two SQL fragments are read
# out of core-api.sh so the test resolves the owner exactly as an update would.
OWNER=""
OWNER_SQL=$(sed -n -E 's/^    local (is_owner|from)="(.*)"$/\1=\2/p' "$SRC/companions/core-api.sh")
if [[ -n "$OWNER_SQL" ]]; then
    is_owner=$(sed -n 's/^is_owner=//p' <<<"$OWNER_SQL")
    from=$(sed -n 's/^from=//p' <<<"$OWNER_SQL")
    OWNER=$("${PSQL[@]}" -d smartnode -At -c "SELECT u.email $from WHERE $is_owner ORDER BY u.id LIMIT 1" 2>/dev/null)
fi
INSTALLER_OWNER="$OWNER"
# A ref whose installer resolves no owner still gets the rest of the checks.
[[ -n "$OWNER" ]] || OWNER="owner@example.test"

SECRET="test-only-service-credential"
PROXY="test-only-session-proof"
cat > "$WORK/app.py" <<'PY'
import os, sys
sys.path.insert(0, os.environ["CORE_API_DIR"])
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
import serve
from db import init_pool, close_pool
from routes import agents, desks, notifications, messages, google

@asynccontextmanager
async def lifespan(app):
    init_pool()
    yield
    close_pool()

app = FastAPI(lifespan=lifespan)
for module in (agents, desks, notifications, messages, google):
    app.include_router(module.router)
uvicorn.Server(uvicorn.Config(app, log_level="warning", http="h11")).run(
    sockets=[serve._tcp_socket(), serve._unix_socket()])
PY

# start_api NAME OWNER_SETTING -> a core-api on its own port and socket.
# SN_SECRETS_FILE points at nothing: the calendar route reads the node's secret
# store, and a test run by a user who can read the real one must not reach a
# real calendar.
start_api() {
    local name="$1" owner="$2" port
    port=$(free_port)
    mkdir -p "$WORK/$name"
    ( cd "$SRC/companions/core-api" && exec env \
        PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
        CORE_API_DIR="$SRC/companions/core-api" \
        CORE_API_HOST=127.0.0.1 CORE_API_PORT="$port" \
        CORE_API_UDS="$WORK/$name/core-api.sock" CORE_API_UDS_GROUP="$(id -gn)" \
        SN_OPERATOR_USERS="$(id -un)" SN_OPERATOR_ACTOR="$owner" \
        SN_SERVICE_AUTH_SECRET="$SECRET" SN_SERVICE_AGENTS="voice:*;brin:*" \
        SN_PROXY_AUTH_SECRET="$PROXY" SN_ADMIN_CLIENT_SECRET="test-only-runner-credential" \
        SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$SRC/companions/srv-skeleton/agents/_template" \
        PLANNER_API_URL="" SN_SECRETS_FILE="$WORK/no-secret-store.yaml" \
        "$PY" "$WORK/app.py" ) >"$WORK/$name.log" 2>&1 &
    echo $! > "$WORK/$name.pid"
    local i
    for i in $(seq 1 100); do
        curl -s -o /dev/null -m 1 "http://127.0.0.1:$port/api/agents" && { echo "$port"; return 0; }
        sleep 0.1
    done
    echo "cannot run: core-api ($name) did not start" >&2; tail -20 "$WORK/$name.log" >&2
    return 1
}
mkdir -p "$WORK/agents"
PORT=$(start_api main "$OWNER") || exit 2
PORT_NOOWNER=$(start_api noowner "") || exit 2

fail=0

# ---- the real onboarding seed ------------------------------------------------
# Run as the installer runs it. A ref that still reaches core-api over TCP is
# given the address, so its refusal is core-api's and not a missing URL.
SN_CORE_API="http://127.0.0.1:$PORT" SN_CORE_API_SOCKET="$WORK/main/core-api.sock" \
    SN_ONBOARD_OWNER="$OWNER" SN_AGENTS_ROOT="$WORK/agents" \
    SN_SPECIALIST_MD="$SRC/companions/srv-skeleton/agents/_specialists/configurator.md" \
    bash "$SRC/bootstrap/onboarding-seed.sh" >"$WORK/seed.log" 2>&1
seeded=$("${PSQL[@]}" -d smartnode -At -c \
    "SELECT count(*) FROM sn_config.agents WHERE capability = 'configurator' AND archived_at IS NULL")
if [[ "$seeded" == "1" ]]; then
    echo "ok   seed: onboarding-seed.sh leaves the node its first agent"
else
    echo "FAIL seed: onboarding-seed.sh left $seeded configurator agent(s)  $(grep -m1 -i 'provision returned\|ERROR' "$WORK/seed.log")"
    fail=1
fi
# The stand-in admits any name; the real owner gate admits only an owner.
if grep -q "\"on_behalf_of\": \"$OWNER\"" "$RUNNER_LOG"; then
    echo "ok   seed: the OS user was requested in the owner's name"
else
    echo "FAIL seed: the OS user was requested as: $(tail -n 1 "$RUNNER_LOG")"
    fail=1
fi

# ---- the calls ---------------------------------------------------------------
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    T_BASE="http://127.0.0.1:$PORT" T_SOCK="$WORK/main/core-api.sock" \
    T_NOOWNER="http://127.0.0.1:$PORT_NOOWNER" T_NOOWNER_SOCK="$WORK/noowner/core-api.sock" \
    T_OWNER="$OWNER" T_SECRET="$SECRET" T_PROXY="$PROXY" \
    T_SRC="$SRC" T_RUNNER_LOG="$RUNNER_LOG" T_PY="$PY" \
    "$PY" "$HERE/human_tcp_proof.py" || fail=1

# ---- the installer -----------------------------------------------------------
note() { if [[ "$2" == 0 ]]; then echo "ok   $1"; else echo "FAIL $1  ${3:-}"; fail=1; fi; }
body_of() { awk -v f="$2" '$0 ~ "^"f"\\(\\) \\{" {on=1} on {print} on && /^}/ {exit}' "$1"; }

[[ "$INSTALLER_OWNER" == "owner@example.test" ]]
note "installer: core-api.sh resolves the owner from the users table" $? "got '${INSTALLER_OWNER}'"
body_of "$SRC/companions/core-api.sh" install_service | grep -q '^ *render_operator_contract$'
note "installer: the owner contract is written before the core-api unit" $?
body_of "$SRC/companions/core-api.sh" run_contracts_only | grep -q '^ *install_service$'
note "installer: a contracts-only update writes it too" $?
! body_of "$SRC/companions/core-api.sh" render_operator_contract | grep -Eq 'SN_OPERATOR_ACTOR=(admin|[a-z])'
note "installer: no owner name is written as a literal" $?
# Three units are written there and each runs as the node's own service: the
# dispatcher, the runner, and the flow sweep, which has to run exactly as the
# runner does.
[[ $(grep -c '^EnvironmentFile=-/etc/smart-node/operator.env$' "$SRC/companions/dispatcher.sh") == 3 ]]
note "installer: the dispatcher, the runner and the flow sweep unit load the owner contract" $?
grep -q '^EnvironmentFile=-/etc/smart-node/operator.env$' "$SRC/companions/tracking.sh" \
    && grep -q '^EnvironmentFile=-/etc/smart-node/core-api-service-auth.env$' "$SRC/companions/tracking.sh"
note "installer: the planner unit loads the owner contract and the credential" $?
grep -q '^EnvironmentFile=-\$OPERATOR_ENV$' "$SRC/companions/core-api.sh"
note "installer: the core-api unit loads the owner contract" $?

echo
if [[ "$fail" == 0 ]]; then
    echo "PASS: an asserted owner is refused over TCP and every node caller is proved"
else
    echo "FAIL: see above"
    [[ -s "$WORK/main.log" ]] && { echo "--- core-api log (tail)"; tail -15 "$WORK/main.log"; }
fi
exit "$fail"
