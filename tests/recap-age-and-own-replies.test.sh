#!/usr/bin/env bash
#
# The person block of a wake prompt is recent, and keeps what the agent last said.
#
# WHAT WENT WRONG. brin folds "[conversation with <person>]" into every turn with a
# known person. The block is the newest twelve lines of EACH channel, with no age
# limit, and every line is cut to 160 characters. Measured on this node:
#
#   * the owner's voice channel contributed 12 of 12 lines older than three days
#     (the oldest 22 days) next to lines from this morning, so a new session read
#     a month-old call as part of the current conversation;
#   * 510 of 594 replies an agent had sent in three days are longer than 160
#     characters. The sentence that matters ("tests passed, 38 of 38") sits past
#     the cut, so the next session did not know it had said it and accepted a
#     reproach that was wrong.
#
# THE PROPERTIES:
#
#   1. by-contact accepts max_age_days, applies it before the per-channel cap, and
#      leaves every caller that omits it unchanged
#   2. the person block is fetched with a three day bound
#   3. when the bound leaves fewer than four lines the window is widened ONCE, to
#      fourteen days, and never removed: a person returning after a long gap gets
#      the last two weeks, not a month-old call beside this morning. The header of
#      the block names the window (the last 3 or 14 days)
#   4. the agent's own last three replies are rendered up to 1500 characters, every
#      other line stays at 160, and the block stays small
#
#   tests/recap-age-and-own-replies.test.sh                  # working tree, PASS
#   tests/recap-age-and-own-replies.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "recap age and own replies (git:$BASELINE)"
else echo "recap age and own replies (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/services/ledger_service.py > "$D/ledger_service.py"
read_src companions/core-api/routes/messages.py     > "$D/messages.py"

# 1. the route
if python3 - "$D/messages.py" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
start = src.index("def messages_by_contact(")
end = src.index("class _MessageSearch")
fn = src[start:end]
assert "max_age_days: int = None" in fn, "no max_age_days parameter"
assert "make_interval(days => %s)" in fn, "no age predicate"
# the age predicate is part of the inner select, before the per-channel cap
assert fn.index("age_sql") < fn.index("WHERE rn <= %s"), "age predicate after the cap"
# its parameter is bound before the limit parameter
assert fn.index("*age_params") < fn.index("max(4, min(limit, 12))"), "age param after limit"
# off by default
assert "if max_age_days and max_age_days > 0" in fn, "not off by default"
PY
then ok "by-contact takes max_age_days, applies it before the channel cap, off by default"
else bad "by-contact does not bound the age of its rows"; fi

# 2-4. brin: run the real functions with a stubbed HTTP client
if python3 - "$D/ledger_service.py" <<'PY'
import ast, asyncio, sys, types

src = open(sys.argv[1], encoding="utf-8").read()
tree = ast.parse(src)
consts = {"_PERSON_LIMIT", "_PERSON_MAX_AGE_DAYS", "_PERSON_WIDE_AGE_DAYS", "_PERSON_MIN_LINES", "_PERSON_LINE_CAP",
          "_OWN_REPLY_CAP", "_OWN_REPLIES_FULL", "_RESULT_SNIPPET"}
funcs = {"_ago", "_snippet", "render_person_history", "fetch_person_history"}
keep = []
for n in tree.body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) in consts for t in n.targets):
        keep.append(n)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in funcs:
        keep.append(n)
mod = ast.Module(body=keep, type_ignores=[])
ast.fix_missing_locations(mod)

calls = []
script = []


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._b = body

    def json(self):
        return self._b


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        calls.append(dict(params or {}))
        return _Resp(script.pop(0))


httpx = types.SimpleNamespace(AsyncClient=_Client)
ns = {"httpx": httpx, "CORE_API_URL": "http://core", "_agent_headers": lambda s: {},
      "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
      "datetime": __import__("datetime").datetime,
      "timezone": __import__("datetime").timezone}
exec(compile(mod, "ledger_service", "exec"), ns)

def msg(direction, text, ch="web-chat"):
    return {"direction": direction, "content": text, "channel_type": ch,
            "received_at": "2026-10-20T12:00:00+00:00"}

failed = []

# 4. own latest replies in full, the rest short, block small
msgs = []
for i in range(6):
    msgs.append(msg("inbound", ("question %d " % i) + "x" * 700 + " THEIRTAIL"))
    msgs.append(msg("outbound", ("reply %d " % i) + "y" * 700 + " OWNTAIL%d" % i))
out = ns["render_person_history"]({"display_name": "P", "messages": msgs})
lines = [l for l in out.splitlines() if l.startswith("- ")]
own_lines = [l for l in lines if "] you:" in l]
their_lines = [l for l in lines if "] them:" in l]
if not all(("OWNTAIL%d" % k) in l for k, l in zip((3, 4, 5), own_lines[-3:])):
    failed.append("the last three own replies are cut")
if any("OWNTAIL" in l for l in own_lines[:-3]):
    failed.append("older own replies are not cut")
if any("THEIRTAIL" in l for l in their_lines):
    failed.append("their lines are not cut")
if len(out) > 9000:
    failed.append("block too large: %d chars" % len(out))

# 2. asks with a three day bound
async def run(first, second=None):
    calls.clear()
    script.clear()
    script.append(first)
    if second is not None:
        script.append(second)
    return await ns["fetch_person_history"]("someone", "agent-a")

recent = {"messages": [msg("inbound", "a")] * 6}
try:
    got = asyncio.run(run(recent))
    if calls[0].get("max_age_days") != 3:
        failed.append("first fetch carries no three day bound: %r" % calls[0])
    if len(calls) != 1:
        failed.append("a recent block was fetched twice")
    if (got or {}).get("window_days") != 3:
        failed.append("a recent block does not say its window is 3 days: %r" % (got or {}).get("window_days"))
    if "the last 3 days" not in ns["render_person_history"]({**got, "display_name": "P"}):
        failed.append("the header of a recent block does not name its window")
    # 3. almost nothing recent: widen once to fourteen days, never to no bound
    sparse = {"messages": [msg("inbound", "a")] * 2}
    wider = {"messages": [msg("inbound", "wider")] * 9}
    got = asyncio.run(run(sparse, wider))
    if len(calls) != 2 or calls[1].get("max_age_days") != 14:
        failed.append("sparse block was not refetched with a 14 day bound: %r" % calls)
    if len((got or {}).get("messages") or []) != 9:
        failed.append("sparse block did not use the wider window")
    if (got or {}).get("window_days") != 14:
        failed.append("a widened block does not say its window is 14 days")
    if "the last 14 days" not in ns["render_person_history"]({**got, "display_name": "P"}):
        failed.append("the header of a widened block does not name its window")
    # still sparse after widening: it stays sparse and is not fetched a third time
    got = asyncio.run(run(sparse, {"messages": [msg("inbound", "b")] * 1}))
    if len(calls) != 2:
        failed.append("the window was widened more than once: %r" % calls)
    if any("max_age_days" not in c for c in calls):
        failed.append("a fetch went out with no age bound at all: %r" % calls)
except Exception as ex:
    failed.append("fetch_person_history raised %r" % ex)

for f in failed:
    print("   - " + f)
sys.exit(1 if failed else 0)
PY
then ok "person block: bounded fetch, one widening when sparse, window named, own last three replies in full"
else bad "person block does not keep recent lines and the agent's own latest replies"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
