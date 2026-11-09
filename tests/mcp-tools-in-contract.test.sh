#!/usr/bin/env bash
#
# Every tool an MCP server exposes to an agent is named in the contract that
# agent reads.
#
# WHAT WENT WRONG. chat_history was added to the messaging MCP server, but the
# messaging contract kept saying "four tools". A colleague mentioned in someone
# else's chat is told to call chat_history; it never did, and answered the
# chat's title instead of the request. The planner contract likewise never
# named planner_next_task, which the wake prompt itself tells every agent to
# call. A tool the contract does not name is a tool the agent does not use.
#
# WHAT IS CHECKED, per server (a tool is a function under @mcp.tool()):
#   messaging  every tool is named in the contract in companions/dispatcher.sh
#   planner    every tool is named in the contract in companions/tracking.sh
#   guard      the check can fail: a tool name that is not in the contract is
#              reported as missing
#
#   tests/mcp-tools-in-contract.test.sh                  # tree, PASS
#   tests/mcp-tools-in-contract.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/brin/mcp_messaging.py companions/tracking/mcp_planner.py \
        companions/dispatcher.sh companions/tracking.sh | tar -x -C "$WORK"
    SRC="$WORK"
else
    SRC="$ROOT"
fi

python3 - "$SRC" <<'PY'
import re, sys
src = sys.argv[1]

def tools(path):
    text = open(f"{src}/{path}").read()
    return re.findall(r"@\w+\.tool\([^)]*\)\s*(?:async\s+)?def\s+(\w+)", text)

def block(path, start, end):
    text = open(f"{src}/{path}").read()
    i = text.index(start)
    return text[i:text.index(end, i)]

cases = [
    ("messaging", "companions/brin/mcp_messaging.py", "companions/dispatcher.sh",
     "## messaging --", "\nUSAGE"),
    ("planner", "companions/tracking/mcp_planner.py", "companions/tracking.sh",
     "## planner -- your node task queue", "\nUSAGE"),
]
bad = 0
for name, mcp, sh, start, end in cases:
    names = tools(mcp)
    contract = block(sh, start, end)
    missing = [t for t in names if t not in contract]
    if not names:
        print(f"FAIL {name}: found no tools in {mcp}"); bad += 1
    elif missing:
        print(f"FAIL {name}: not in the contract: {', '.join(missing)}"); bad += 1
    else:
        print(f"PASS {name}: {len(names)} tools, all named in the contract")

probe = block("companions/dispatcher.sh", "## messaging --", "\nUSAGE")
if "no_such_tool_xyz" in probe:
    print("FAIL guard: a made-up tool name was found in the contract"); bad += 1
else:
    print("PASS guard: a name that is not in the contract is not found")
sys.exit(1 if bad else 0)
PY
