#!/usr/bin/env bash
#
# An HTTP agent's chat shows its context meter, and a relay into a desk is not
# logged as a refused wake.
#
# WHAT WENT WRONG (sn-4680, 2026-09-18).
#   meter    core-api computed the percent only when automatic handoff runs,
#            which is never for an HTTP agent; the Studio then hid the meter,
#            so HTTP-model agent's chat had no context bar at all (7810 tokens known).
#   desk     the message watcher resolved desk:<id> like an agent and logged
#            message.routing_denied reason=no-agent on every relay into a desk.
#
# WHAT IS CHECKED. The real functions, lifted out of their modules as written
# (the modules need a database and httpx to import):
#   meter    context_status._serialize gives a percent for a non-opaque session
#            with a threshold, and still reports enabled=false for it
#   desk     MessageWatcher._route_one closes a desk:<id> row as processed,
#            asks core nothing and logs no denial; an agent row still resolves
#   Studio   the meter shows the percent when handoff is off, marked as such
#
# USAGE
#   tests/http-agent-desk-context.test.sh                  # working tree, expect PASS
#   tests/http-agent-desk-context.test.sh --baseline REF   # git:REF, expect FAIL
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
src() {
    mkdir -p "$(dirname "$WORK/$1")"
    if [[ "${1:-}" && "${BASELINE:-}" ]]; then git -C "$ROOT" show "$BASELINE:$1" > "$WORK/$1"
    else cp "$ROOT/$1" "$WORK/$1"; fi
}
BASELINE=""; [[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
src companions/core-api/routes/context_status.py
src companions/brin/services/message_watcher.py
src companions/studio/public/next2/js/app.js

cd "$WORK" && python3 - <<'PY'
import ast, asyncio, sys
fails = 0
def ok(m): print("  ok   " + m)
def bad(m):
    global fails; fails += 1; print("  FAIL " + m)

def lift(path, names, cls=None, methods=()):
    tree = ast.parse(open(path).read())
    keep = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names:
            keep.append(n)
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets):
            keep.append(n)
        elif cls and isinstance(n, ast.ClassDef) and n.name == cls:
            n.bases, n.keywords, n.decorator_list = [], [], []
            n.body = [f for f in n.body if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in methods)
                      or (isinstance(f, ast.Assign) and any(getattr(t, "id", "") in methods for t in f.targets))]
            keep.append(n)
    ns = {"log": None, "logger": type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                                "warning": staticmethod(lambda *a, **k: None),
                                                "debug": staticmethod(lambda *a, **k: None)})()}
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), ns)
    return ns

# meter
try:
    ns = lift("companions/core-api/routes/context_status.py",
              {"ContextStatusReadError", "_token", "_context_tokens", "_serialize"})
    row = {"session_id": "cec92f7a", "is_opaque": False, "threshold_tokens": 120000,
           "policy_enabled": True, "ledger_type": "array", "agent": "http-agent",
           "conversation": "desk:3f80", "desk_id": "3f80", "threshold_scope": "global",
           "latest_request": {"input_tokens": 810, "cache_read_tokens": 6000, "cache_write_tokens": 1000}}
    out = ns["_serialize"](row)
    if out.get("percent") == 6 and out.get("enabled") is False:
        ok("meter: an HTTP agent's session has a percent (6% of 120k) and still enabled=false")
    else:
        bad("meter: HTTP agent session gives percent=%r enabled=%r, the Studio hides the meter"
            % (out.get("percent"), out.get("enabled")))
except Exception as e:
    bad("meter: could not run _serialize: %s: %s" % (type(e).__name__, e))

# desk
try:
    ns = lift("companions/brin/services/message_watcher.py", set(), cls="MessageWatcher",
              methods={"_route_one", "_TERMINAL"})
    W = ns["MessageWatcher"]
    class Fake(W):
        def __init__(self, deliver_ok=True):
            self._seen, self._held, self.processed, self.denied, self.asked = set(), set(), [], [], []
            self.delivered, self._ok = [], deliver_ok
        async def _mark_processed(self, i): self.processed.append(i)
        async def _emit_denied(self, *a): self.denied.append(a)
        async def _deliver_desk(self, i):
            self.delivered.append(i)
            return self._ok
        async def _resolve(self, c, r):
            self.asked.append(r)
            return (None, "no-agent") if r.startswith("desk:") else ("foreman", "ok")
    # a desk row is intent; core-api's deliver-desk decides. The watcher
    # must neither close it itself nor resolve it like an agent.
    w = Fake()
    asyncio.run(w._route_one({"id": 69, "recipient": "desk:3f80", "channel_type": "internal"}))
    if w.delivered == [69] and not w.processed and not w.denied and not w.asked and 69 in w._seen:
        ok("desk: a row into desk:<id> goes to the deliver-desk decision, never closed unseen")
    else:
        bad("desk: row into a desk -> delivered=%r processed=%r denied=%r asked=%r seen=%r"
            % (w.delivered, w.processed, w.denied, w.asked, sorted(w._seen)))
    wf = Fake(deliver_ok=False)
    asyncio.run(wf._route_one({"id": 71, "recipient": "desk:3f80", "channel_type": "internal"}))
    if wf.delivered == [71] and 71 not in wf._seen and not wf.processed:
        ok("desk: a failed deliver-desk call leaves the row for the next tick")
    else:
        bad("desk: failed deliver-desk -> seen=%r processed=%r" % (sorted(wf._seen), wf.processed))
    w2 = Fake()
    asyncio.run(w2._route_one({"id": 70, "recipient": "foreman", "channel_type": "internal"}))
    if w2.asked == ["foreman"] and not w2.denied and not w2.processed:
        ok("desk: a message to an agent is still resolved and left for the Runner")
    else:
        bad("desk: a message to an agent -> asked=%r denied=%r processed=%r" % (w2.asked, w2.denied, w2.processed))
except Exception as e:
    bad("desk: could not run _route_one: %s: %s" % (type(e).__name__, e))

# Studio
js = open("companions/studio/public/next2/js/app.js").read()
if "handoff off" in js and "x.status && x.status.percent != null" in js \
        and "if (!s || !s.enabled || s.percent == null) return" not in js:
    ok("Studio: the meter shows the percent when handoff is off, marked 'handoff off'")
else:
    bad("Studio: the meter is hidden whenever handoff is off")
print("ALL PASS" if not fails else "FAILED: %d" % fails)
sys.exit(1 if fails else 0)
PY
rc=$?
node --check "$WORK/companions/studio/public/next2/js/app.js" || rc=1
exit $rc
