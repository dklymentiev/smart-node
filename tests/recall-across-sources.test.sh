#!/usr/bin/env bash
#
# One call finds what an agent knows about a thing, in every place it keeps it.
#
# WHAT WENT WRONG. Asked where "the list of people who wrote to us" was, an agent
# searched memory, the planner and a few guessed folders, found a plausible list
# (a different one) and answered. The list it was after lived in another place
# and was announced in a mail it never looked in. Each search tool answered for
# its own place only, so nothing ever showed the agent that two lists existed.
#
# THE PROPERTIES, each run on the real code:
#
#   1. a snippet is the text around the first word that occurs, on one line
#   2. the time window is the narrowest of 3, 14, 90 days or all time that holds
#      a few hits, so a narrow window is not trusted when it is empty
#   3. the route searches tasks, conversations (under the caller's party rule) and
#      the caller's OWN memory workspace, without the session handoff notes; memory
#      gives exact word matches as findings and the nearest by meaning only as
#      leads (the embedding cannot tell a real match from nonsense)
#   4. it says which sources it read and which it did not, and never lets
#      "nothing found" stand when one was not read; documents are named as not
#      searched
#   5. the messaging MCP server exposes it and the contract tells the agent to
#      call it first and to ask which when more than one thing could be meant
#
#   tests/recall-across-sources.test.sh                  # working tree, PASS
#   tests/recall-across-sources.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "recall across sources (git:$BASELINE)"
else echo "recall across sources (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
read_src companions/brin/mcp_messaging.py > "$D/mcp.py"
read_src companions/dispatcher.sh > "$D/dispatcher.sh"

# 1, 2. the pure helpers
if python3 - "$D/messages.py" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
want_consts = {"_RECALL_WINDOWS_DAYS", "_RECALL_ENOUGH", "_RECALL_SNIPPET"}
keep = []
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) in want_consts for t in n.targets):
        keep.append(n)
    if isinstance(n, ast.FunctionDef) and n.name in ("_snippet", "_pick_window"):
        keep.append(n)
if not any(isinstance(n, ast.FunctionDef) and n.name == "_pick_window" for n in keep):
    print("   - there are no recall helpers")
    sys.exit(1)
mod = ast.Module(body=keep, type_ignores=[])
ast.fix_missing_locations(mod)
ns = {}
exec(compile(mod, "recall", "exec"), ns)
snip, pick = ns["_snippet"], ns["_pick_window"]
failed = []
text = "alpha " * 60 + "the contacts document lives here " + "omega " * 60
out = snip(text, ["contacts"])
if "contacts document" not in out or "\n" in out or len(out) > 300:
    failed.append("snippet is %r" % out[:80])
if not out.startswith("...") or not out.endswith("..."):
    failed.append("a cut snippet is not marked")
if snip("short text", ["zzz"]) != "short text":
    failed.append("a text with no word is not returned whole")
if pick([1, 2, 3]) != 3:
    failed.append("three recent hits should stay in the 3-day window, got %r" % pick([1, 2, 3]))
if pick([1, 2, 20, 40]) != 90:
    failed.append("two recent and two older hits should widen to 90, got %r" % pick([1, 2, 20, 40]))
if pick([1, 2, 10]) != 14:
    failed.append("hits within 14 days should stop at 14, got %r" % pick([1, 2, 10]))
if pick([]) is not None:
    failed.append("no hits should widen to all time (None)")
for x in failed:
    print("   - " + x)
sys.exit(1 if failed else 0)
PY
then ok "snippet around the word, and the window widens to 3, 14, 90, all time"
else bad "the recall helpers are wrong or missing"; fi

# 3, 4. the route
if python3 - "$D/messages.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
i = src.index('@router.post("/recall"')
fn = src[i:src.index("def _desk_of_message")]
mem = src[src.index("def _recall_memory"):i]
assert 'requires("messages.read")' in fn, "not under the messages permission"
assert "messages_search(_MessageSearch(q=q, limit=100), principal)" in fn, "conversations are not searched under the caller's principal"
assert 'api/v1/tasks/search' in fn, "tasks are not searched"
assert "SELECT mesh_workspace FROM sn_config.agents WHERE slug = %s" in mem, "memory is not read from the caller's own workspace"
assert "/keyword" in mem and "_min_words(len(stems))" in mem, "memory has no exact word matches"
assert '"related"' in mem and "leads, not findings" in mem, "semantic neighbours are not kept apart from findings"
assert '"*"' not in mem, "memory is read across every workspace"
assert "_RECALL_MEMORY_SKIP_TAGS" in mem and '"type:handoff"' in src, "handoff notes are not excluded"
assert '"not_searched"' in fn and "documents" in fn, "documents are not named as not searched"
assert '"not_read"' in fn and "Do not conclude that something does not exist" in fn, "an unread source is not reported"
assert '"window_days"' in fn, "the window used is not named"
PY
then ok "tasks, conversations under the party rule, own memory without handoffs; unread and unsearched sources named"
else bad "the route does not search the sources it should, or hides one it did not read"; fi

# 5. the tool and the contract
if python3 - "$D/mcp.py" "$D/dispatcher.sh" <<'PY'
import ast, sys
mcp = open(sys.argv[1], encoding="utf-8").read()
sh = open(sys.argv[2], encoding="utf-8").read()
fn = next((n for n in ast.parse(mcp).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "recall"), None)
assert fn is not None, "the messaging server has no recall tool"
assert '"/recall"' in mcp, "recall does not post to the route"
assert "recall(q) FIRST" in sh, "the contract does not tell the agent to call it first"
assert "ask which" in sh, "the contract does not say to ask which when more than one could be meant"
PY
then ok "the messaging server exposes recall and the contract says when to call it"
else bad "the agent is not given recall or not told to use it"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
