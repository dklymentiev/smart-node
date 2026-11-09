#!/usr/bin/env bash
#
# A grants merge keeps what the agent holds in the database.
#
# WHAT WENT WRONG. seed-tools grants some rows straight into
# sn_config.agent_tools on every core-api start: messages_search, my_wakes and
# secrets_resolve to every agent, node_summary to the configurator. They are not
# in the agent's grants.json. PUT /api/agents/<slug>/grants?merge=true merged the
# request with grants.json only, and _sync_agent_tools then deleted every row not
# in the merged set. onboarding-seed runs exactly such a merge on every update, so
# every node that updated lost those four until core-api next restarted; the
# update's grants gate reported it as a narrowing (1.12.47 candidate stand run).
#
# WHAT IS CHECKED. core-api's agents router, run for real (FastAPI TestClient,
# the core-api venv) against a throwaway postgres built from the tree's B02
# files, with a throwaway agents root. Nothing on the host is touched.
#
#   merge-keeps    a merge that names one new tool keeps a row that exists only in
#                  the database, with its capabilities
#   merge-adds     and adds the new tool and keeps the grants.json ones
#   merge-file     the kept row is written into grants.json, so the file and the
#                  database agree afterwards
#   replace        a PUT without merge still replaces the whole set (unchanged)
#   role-less      a caller with no role is refused the write, and nothing changes
#
# USAGE
#   tests/grants-merge-keeps-db-rows.test.sh                  # working tree, expect PASS
#   tests/grants-merge-keeps-db-rows.test.sh --baseline REF   # git:REF, expect FAIL
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
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/core-api" "$WORK/src/companions/"
fi

echo "grants merge keeps database rows (${BASELINE:+git:}${BASELINE:-working tree})"

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
# One agent, three tools: bash and fetch are what grants.json knows about, and
# my_wakes is granted in the database only, the way seed-tools grants it.
"${PSQL[@]}" -d smartnode -c "
  INSERT INTO sn_config.agents (slug, title, model, capability)
    VALUES ('cfg1', 'cfg1', 'claude-cli/sonnet', 'configurator');
  INSERT INTO sn_core.tools (name, kind, title) VALUES
    ('bash', 'builtin', 'bash'), ('fetch', 'builtin', 'fetch'), ('my_wakes', 'api', 'my wakes')
    ON CONFLICT (name) DO NOTHING;
  INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps)
    SELECT a.id, t.id, CASE t.name WHEN 'bash' THEN '{read,write,delete}'::text[] ELSE '{read}'::text[] END
      FROM sn_config.agents a, sn_core.tools t
     WHERE a.slug = 'cfg1' AND t.name IN ('bash', 'my_wakes');
  -- The grants routes ask for a permission, so the callers below need real
  -- actor rows. The owner rides the seed's super-admin; dev1 is an actor with
  -- NO actor_roles row on purpose: it resolves to 'user', which does not hold
  -- agents.grants.write, and the last check shows it refused.
  INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) VALUES
    ('agent:dev1', 'dev1', 'agent', 'specialist', 'agent', 'internal')
    ON CONFLICT (id) DO NOTHING;" >/dev/null \
    || { echo "cannot run: fixture" >&2; exit 2; }
mkdir -p "$WORK/agents/cfg1" "$WORK/agents/_template"
echo '{"grants": {"bash": "rwd"}, "secret_refs": []}' > "$WORK/agents/cfg1/grants.json"

cat > "$WORK/drive.py" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["CORE_API_DIR"])
from fastapi import FastAPI
from fastapi.testclient import TestClient
import auth
from auth import Principal
from routes import agents as agents_route
from db import cursor

with cursor() as cur:
    cur.execute("SELECT actor_id FROM sn_core.actor_roles WHERE role = 'super-admin'")
    owner_actor = cur.fetchone()["actor_id"]
# The permission check reads the role from the actor's row, not from
# Principal.role, so each caller is anchored to a real actor.
OWNER = Principal(username="admin", user_id=1, tenant_id=1, role="super-admin",
                  actor_id=owner_actor, actor_kind="human", strength="session")
DEV = Principal(username="dev1", user_id=3, tenant_id=1, role="agent",
                actor_id="agent:dev1", actor_kind="agent", strength="proved")
who = {"p": OWNER}

app = FastAPI()
app.include_router(agents_route.router)
app.dependency_overrides[auth.get_principal] = lambda: who["p"]
c = TestClient(app)
gfile = os.path.join(os.environ["SN_AGENTS_ROOT"], "cfg1", "grants.json")

def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))

def db():
    with cursor() as cur:
        cur.execute("SELECT t.name, at.granted_caps FROM sn_config.agent_tools at "
                    "JOIN sn_core.tools t ON t.id = at.tool_id "
                    "JOIN sn_config.agents a ON a.id = at.agent_id WHERE a.slug = 'cfg1'")
        return {r["name"]: sorted(r["granted_caps"]) for r in cur.fetchall()}

r = c.put("/api/agents/cfg1/grants?merge=true", json={"grants": {"fetch": "rw-"}},
          headers={"Remote-User": "admin"})
after = db()
res("merge-keeps: a database-only row survives a merge, with its caps",
    after.get("my_wakes") == ["read"], f"{r.status_code} {r.text[:160]} db={after}")
res("merge-adds: the new tool is added and the grants.json one kept",
    after.get("fetch") == ["read", "write"] and after.get("bash") == ["delete", "read", "write"],
    f"db={after}")
f = json.load(open(gfile)).get("grants", {})
res("merge-file: grants.json now carries the kept row too",
    f.get("my_wakes") == "r--", f"file={f}")

r = c.put("/api/agents/cfg1/grants", json={"grants": {"bash": "rwd"}},
          headers={"Remote-User": "admin"})
after = db()
res("replace: a PUT without merge still replaces the whole set",
    set(after) == {"bash"}, f"{r.status_code} db={after}")

who["p"] = DEV
r = c.put("/api/agents/cfg1/grants", json={"grants": {"fetch": "rw-"}})
after = db()
res("role-less: a caller with no role is refused the write, and nothing changes",
    r.status_code == 403 and "'user'" in r.text and "'agents.grants.write'" in r.text
    and set(after) == {"bash"}, f"{r.status_code} {r.text[:160]} db={after}")
PY

CORE_API_DIR="$WORK/src/companions/core-api" SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/agents/_template" \
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    "$PY" "$WORK/drive.py" >"$WORK/drive.out" 2>"$WORK/drive.err"
[[ -s "$WORK/drive.out" ]] || { echo "cannot run: driver produced nothing" >&2; tail -20 "$WORK/drive.err" >&2; exit 2; }
sed 's/^/  /' "$WORK/drive.out"
FAIL=$(grep -c '^FAIL' "$WORK/drive.out"); TOTAL=$(grep -cE '^(ok|FAIL)' "$WORK/drive.out")
echo
if (( FAIL == 0 )); then echo "PASS: $TOTAL checks"; exit 0; fi
echo "FAIL: $FAIL of $TOTAL"; exit 1
