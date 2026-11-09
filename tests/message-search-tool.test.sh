#!/usr/bin/env bash
#
# An agent that runs as a CLI session can search what was said.
#
# WHAT WENT WRONG. The message search was a catalogue entry of kind api. A CLI
# agent sees only the tools its MCP servers list, so an api row is a contract
# with no way to execute: across 27 reviewed chats not one call to it appears,
# and an agent that needed an old message could only read the newest few lines
# of one thread, then answer "not found".
#
# THE PROPERTIES, each run on the real code:
#
#   1. the messaging MCP server has a message_search tool: q is required, the
#      person, channel and time bounds are optional
#   2. it posts to the search route with exactly what was given, and leaves out
#      what was not (an empty identity must not narrow the search to nobody)
#   3. the limit is held to 1..100
#   4. the messaging contract names it (the contract test checks every tool)
#
#   tests/message-search-tool.test.sh                  # working tree, PASS
#   tests/message-search-tool.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "message search tool (git:$BASELINE)"
else echo "message search tool (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/mcp_messaging.py > "$D/mcp.py"
read_src companions/dispatcher.sh > "$D/dispatcher.sh"

if python3 - "$D/mcp.py" <<'PY'
import ast, asyncio, inspect, sys
from typing import Optional
src = open(sys.argv[1], encoding="utf-8").read()
fn = next((n for n in ast.parse(src).body
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "message_search"), None)
if fn is None:
    print("   - the messaging server has no message_search tool")
    sys.exit(1)
fn.decorator_list = []
mod = ast.Module(body=[fn], type_ignores=[])
ast.fix_missing_locations(mod)
calls = []
async def fake_call(method, path, body=None):
    calls.append((method, path, body))
    return "{}"
ns = {"Optional": Optional, "_call": fake_call}
exec(compile(mod, "mcp", "exec"), ns)
f = ns["message_search"]
failed = []
sig = inspect.signature(f)
if sig.parameters["q"].default is not inspect.Parameter.empty:
    failed.append("q is optional")
for p in ("identity", "channel", "since", "until"):
    if p not in sig.parameters or sig.parameters[p].default is not None:
        failed.append("%s is not an optional parameter" % p)
asyncio.run(f("contacts list"))
if calls[-1] != ("POST", "/search", {"q": "contacts list", "limit": 20}):
    failed.append("a bare query posts %r" % (calls[-1],))
asyncio.run(f("x", identity="a@b.c", channel="email", since="2026-10-01", until="2026-10-20", limit=5))
if calls[-1][2] != {"q": "x", "limit": 5, "identity": "a@b.c", "channel": "email",
                    "since": "2026-10-01", "until": "2026-10-20"}:
    failed.append("a full query posts %r" % (calls[-1][2],))
asyncio.run(f("x", identity="", channel=""))
if "identity" in calls[-1][2] or "channel" in calls[-1][2]:
    failed.append("an empty identity or channel is sent")
asyncio.run(f("x", limit=0))
asyncio.run(f("x", limit=5000))
if calls[-2][2]["limit"] != 20 or calls[-1][2]["limit"] != 100:
    failed.append("limit is not held to 1..100: %r %r" % (calls[-2][2]["limit"], calls[-1][2]["limit"]))
for x in failed:
    print("   - " + x)
sys.exit(1 if failed else 0)
PY
then ok "the tool exists, sends what was given and nothing else, holds the limit"
else bad "the messaging server cannot search by words"; fi

if grep -q "message_search" "$D/dispatcher.sh"; then
    ok "the messaging contract names the tool"
else bad "the messaging contract does not name message_search"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
