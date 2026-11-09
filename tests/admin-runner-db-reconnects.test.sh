#!/usr/bin/env bash
# admin-runner keeps a pool of database connections. When the cluster restarts
# under it (the documents stage does that during an install) every pooled
# connection is dead; the next request failed with "server closed the
# connection unexpectedly", and on a clean install that request was the one
# creating the first agent. A dead connection is replaced before it is used.
# Supports --baseline REF to show RED on main.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
if [[ -n "$BASELINE" ]]; then echo "admin-runner replaces a dead pooled connection (git:$BASELINE)"
else echo "admin-runner replaces a dead pooled connection (working tree)"; fi
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/admin-runner/db.py > "$D/db.py"
python3 - "$D" <<'PY'
import sys, types
d = sys.argv[1]
class OperationalError(Exception): pass
class InterfaceError(Exception): pass
class Conn:
    def __init__(self, name, dead=False):
        self.name, self.dead, self.closed = name, dead, 0
    def cursor(self, **k):
        conn = self
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a):
                if conn.dead:
                    conn.closed = 2
                    raise OperationalError("server closed the connection unexpectedly")
        return C()
    def rollback(self):
        if self.closed: raise InterfaceError("connection already closed")
    def commit(self): pass
class Pool:
    def __init__(self, *a, **k):
        self.free = [Conn("dead", dead=True), Conn("fresh")]
        self.closed_out, self.back = [], []
    def getconn(self): return self.free.pop(0)
    def putconn(self, c, close=False):
        (self.closed_out if close else self.back).append(c.name)
    def closeall(self): pass
pg = types.ModuleType("psycopg2"); pg.OperationalError, pg.InterfaceError = OperationalError, InterfaceError
pg.extras = types.SimpleNamespace(RealDictCursor=None)
pool_mod = types.ModuleType("psycopg2.pool"); pool_mod.ThreadedConnectionPool = Pool
sys.modules.update({"psycopg2": pg, "psycopg2.extras": pg.extras, "psycopg2.pool": pool_mod,
                    "config": types.SimpleNamespace(DB_NAME="x", DB_USER="y")})
sys.path.insert(0, d)
import db
bad = 0
def check(name, cond):
    global bad
    print("  %s %s" % ("ok  " if cond else "FAIL", name)); bad += not cond
db.init_pool()
used = None
try:
    with db.cursor() as cur:
        used = "ran"
except Exception as e:
    used = "raised %s" % type(e).__name__
p = db._pool
check("a request after a cluster restart runs on a live connection", used == "ran")
check("the dead connection is closed, not handed back to the pool", "dead" in p.closed_out and "dead" not in p.back)
check("the live connection goes back to the pool", p.back == ["fresh"])
p.free = [Conn("again")]
try:
    with db.cursor() as cur:
        raise ValueError("query failed")
except ValueError:
    pass
check("a failing query still returns its connection to the pool", p.back[-1] == "again")
print(); print("PASS" if not bad else "FAIL (%d)" % bad)
sys.exit(1 if bad else 0)
PY
