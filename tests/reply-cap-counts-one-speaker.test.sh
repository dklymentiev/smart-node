#!/usr/bin/env bash
#
# The reply cap counts one speaker, not one room.
#
# WHAT WENT WRONG. mailrules.reply_cap_reached guards against a loop: one
# mailbox answering the same conversation over and over. It counted EVERY
# outbound message on the thread, which is also exactly what a working group
# conversation produces. Measured on the field node: a roll call to seven
# colleagues on one thread, each answering once; four answers went out and the
# rest were refused as a loop. Nothing had looped.
#
# THE PROPERTIES:
#   1. a room of many speakers, each under the ceiling, is not a loop
#   2. one speaker over the ceiling still is
#   3. the count is narrowed to the sender, case-insensitively
#   4. a caller that names no sender keeps the old, stricter count
#   5. a message on no thread is not counted at all
#   6. send_outbound passes the sender, and the refusal names it
#
# The registry query is replaced by a fake: what is asserted is the DECISION.
# The function is extracted from the source with ast, so no database driver is
# needed and --baseline runs the old code the same way.
#
#   tests/reply-cap-counts-one-speaker.test.sh                  # tree, PASS
#   tests/reply-cap-counts-one-speaker.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "reply cap counts one speaker (git:$BASELINE)"
else echo "reply cap counts one speaker (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/communication/common/mailrules.py > "$D/mailrules.py"
read_src companions/communication/send_outbound.py   > "$D/send_outbound.py"
for f in mailrules send_outbound; do
    [[ -s "$D/$f.py" ]] || { bad "sources are readable ($f.py)"; echo; echo "FAIL"; exit 1; }
done

if grep -q 'mailrules.reply_cap_reached("email", thread_id, sender)' "$D/send_outbound.py"; then
    ok "send_outbound counts the sender, not the room"
else
    bad "send_outbound counts the sender, not the room"
fi

while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/mailrules.py" <<'PY'
import ast, inspect, sys
tree = ast.parse(open(sys.argv[1]).read())
keep = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "reply_cap_reached")
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in ("REPLY_CAP", "REPLY_WINDOW_MINUTES") for t in n.targets))]
calls = []

class FakeDb:
    @staticmethod
    def query(sql, args=None, fetch=False):
        # The room said six things on this thread; the speaker asked about, two.
        calls.append((sql, args))
        return [(2 if "sender" in sql else 6,)]

ns = {"db": FakeDb}
exec(compile(ast.Module(body=keep, type_ignores=[]), "mailrules.py", "exec"), ns)
f = ns.get("reply_cap_reached")
if f is None:
    print("FAIL reply_cap_reached exists"); sys.exit(0)
if "sender" not in inspect.signature(f).parameters:
    print("FAIL reply_cap_reached takes the sender -- it still counts the whole room")
    sys.exit(0)

T = "thread-1@example.com"

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

check("a room of seven, each speaking under the ceiling, is not a loop",
      f("email", T, sender="maya.node@example.com") is False)
check("one speaker over the ceiling is still a loop",
      f("email", T, sender="maya.node@example.com", cap=2) is True)
calls.clear(); f("email", T, sender="Maya.Node@example.com")
sql, args = calls[-1]
check("the count is narrowed to the sender, case-insensitively",
      "lower(sender) = lower(%s)" in sql and "Maya.Node@example.com" in args)
check("a caller that names no sender keeps the old, stricter count",
      f("email", T) is True and "sender" not in calls[-1][0])
calls.clear()
check("a message on no thread is not counted at all",
      f("email", None, sender="maya.node@example.com") is False and calls == [])
PY
)

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
