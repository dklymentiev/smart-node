#!/usr/bin/env bash
#
# An agent's email reply threads against the Message-ID, not the row id.
#
# WHAT WENT WRONG. 38e3440 made an inbound email row's external_id
# "<mailbox>|<Message-ID>", so that one letter delivered to three of our boxes
# is three rows. It moved the fast dispatch path to metadata.message_id for
# In-Reply-To and left core-api's POST /api/messages/reply on external_id. That
# route is the one every agent uses (comm.sh reply), so from 1.12.36 on each
# such reply would carry In-Reply-To "box@x|<id@y>": not a Message-ID, and the
# reader's mail client opens a new conversation for it.
#
# Found while landing an earlier field report, which touches the same route. Not in any PR.
#
# The rule is a pure function, so it is extracted from the source with ast and
# run here; no database and no FastAPI needed, and the same works on --baseline.
#
#   tests/reply-threads-on-the-wire-id.test.sh                  # tree, PASS
#   tests/reply-threads-on-the-wire-id.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "reply threads on the wire id (git:$BASELINE)"
else echo "reply threads on the wire id (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
[[ -s "$D/messages.py" ]] || { bad "messages.py is readable"; echo; echo "FAIL"; exit 1; }

# The route must use the rule, not carry its own copy of the old one.
# The body of the route: from its def to the next top-level def, class or
# decorator. A plain awk range would close on its own first line.
# Read the whole awk stream: grep -q may close early and make awk fail with
# SIGPIPE under pipefail even when the call is present.
if awk 'f && /^(def|class|@)/{exit} /^def reply_to_message\(/{f=1} f' "$D/messages.py" \
     | grep -F 'in_reply_to = _wire_in_reply_to(src, meta)' >/dev/null; then
    ok "the reply route takes In-Reply-To from the wire id rule"
else
    bad "the reply route takes In-Reply-To from the wire id rule"
fi

while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/messages.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_wire_in_reply_to"]
if not fn:
    print("FAIL _wire_in_reply_to exists")
    sys.exit(0)
ns = {}
exec(compile(ast.Module(body=fn, type_ignores=[]), "messages.py", "exec"), ns)
f = ns["_wire_in_reply_to"]

cases = [
    ("a scoped row threads against the Message-ID in metadata",
     {"external_id": "alex@node.example|<abc@mail.example>"},
     {"message_id": "<abc@mail.example>"}, "<abc@mail.example>"),
    ("a row from before the scoping still threads against external_id",
     {"external_id": "<old@mail.example>"}, {}, "<old@mail.example>"),
    ("a scoped row with no Message-ID never leaks the row id into the header",
     {"external_id": "alex@node.example|uid:4411"}, {"message_id": ""}, None),
    ("a bare uid placeholder threads nothing",
     {"external_id": "uid:4411"}, {}, None),
    ("no metadata at all is not an error",
     {"external_id": "<old@mail.example>"}, None, "<old@mail.example>"),
]
for name, src, meta, want in cases:
    got = f(src, meta)
    print(("ok " if got == want else "FAIL ") + name + ("" if got == want else " -- got %r" % (got,)))
PY
)

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
