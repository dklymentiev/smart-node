#!/usr/bin/env bash
#
# A reply through core-api keeps the room the letter was addressed to (the room routing rule;
# an earlier field report).
#
# WHAT WENT WRONG. 38e3440 started recording To and Cc on every inbound
# email and taught the FAST dispatch path to answer the room. The other writer
# of replies, POST /api/messages/reply, which is what comm.sh reply and every
# agent use, still built its copies from body.cc alone. The caller is an agent,
# and its only source for those addresses is the quoted text of the letter it
# is answering, so in practice it copied nobody. Seen on the field node: the
# owner copied himself on a question to an agent, the agent answered, and the
# answer reached only the agent that had asked.
#
# The field PR also re-added the adapter hunk; that half is already in canon
# (adapter.py writes to_addrs and cc_addrs) and was not taken.
#
# THE PROPERTIES:
#   1. the room is the To and Cc of the letter, minus our own box (we are the
#      one replying) and minus the sender (they are the To)
#   2. our box is matched case-insensitively: someone else's client wrote it
#   3. one address on both headers is copied once
#   4. a row with no headers (older mail, other channels) copies nobody
#   5. the route defaults to the room; an explicit cc wins; only_sender is the
#      deliberate private answer
#   6. comm.sh reply exposes --only-sender
#
#   tests/reply-keeps-the-room.test.sh                  # tree, PASS
#   tests/reply-keeps-the-room.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "reply keeps the room (git:$BASELINE)"
else echo "reply keeps the room (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
read_src companions/communication/tools/comm.sh > "$D/comm.sh"
for f in messages.py comm.sh; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# The body of the route: from its def to the next top-level def, class or
# decorator. A plain awk range would close on its own first line.
route=$(awk 'f && /^(def|class|@)/{exit} /^def reply_to_message\(/{f=1} f' "$D/messages.py")
if grep -q '_cc = _audience_of(src, meta)' <<<"$route"; then
    ok "the route defaults to the room"
else
    bad "the route defaults to the room -- it copies only what the caller retyped"
fi
if grep -q 'if body.cc is not None:' <<<"$route" && grep -q 'elif body.only_sender:' <<<"$route"; then
    ok "an explicit cc wins, and only_sender answers the sender alone"
else
    bad "an explicit cc wins, and only_sender answers the sender alone"
fi
if grep -q 'Json(_recipient_rows(src\["sender"\], _cc)) if _cc else None' <<<"$route"; then
    ok "the stored recipient set is the resolved room"
else
    bad "the stored recipient set is the resolved room"
fi
if grep -q -- '--only-sender) only_sender="1"' "$D/comm.sh"; then
    ok "comm.sh reply has --only-sender"
else
    bad "comm.sh reply has --only-sender"
fi

while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/messages.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_audience_of"]
if not fn:
    print("FAIL _audience_of exists"); sys.exit(0)
ns = {}
exec(compile(ast.Module(body=fn, type_ignores=[]), "messages.py", "exec"), ns)
f = ns["_audience_of"]

def check(name, got, want):
    print(("ok " if got == want else "FAIL ") + name + ("" if got == want else " -- got %r" % (got,)))

src = {"recipient": "alex.node@example.com", "sender": "owner@example.com"}
check("the room minus us and the sender",
      f(src, {"to_addrs": ["alex.node@example.com", "maya.node@example.com"],
              "cc_addrs": ["owner@example.com", "sam.node@example.com"]}),
      ["maya.node@example.com", "sam.node@example.com"])
check("our box is matched whatever the sender's client capitalised",
      f({"recipient": "Alex.Node@example.com", "sender": "owner@example.com"},
        {"to_addrs": ["ALEX.NODE@example.com", "maya.node@example.com"]}),
      ["maya.node@example.com"])
check("one address on both headers is copied once",
      f(src, {"to_addrs": ["maya.node@example.com"], "cc_addrs": ["maya.node@example.com"]}),
      ["maya.node@example.com"])
check("a row with no headers copies nobody", (f(src, {}), f(src, None)), ([], []))
check("a letter addressed only to us copies nobody",
      f(src, {"to_addrs": ["alex.node@example.com"], "cc_addrs": []}), [])
PY
)

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
