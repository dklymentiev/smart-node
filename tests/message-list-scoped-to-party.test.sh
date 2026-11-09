#!/usr/bin/env bash
#
# An agent reads the messages it is a party to, and only those.
#
# WHAT WENT WRONG. GET /api/messages filtered by tenant and nothing else. An
# agent calling it without `mine` received every colleague's correspondence,
# both directions, bodies included: more than the contact gate lets the same
# agent WRITE to. And `mine` matched the recipient alone, so an agent could not
# see what it had sent, nor that a send had been refused. On the field node a
# front desk queued a roll call to seven colleagues, reported seven letters
# sent, and all seven had been blocked a second later.
#
# TAKEN AS A PROBLEM, NOT AS THE DIFF. The field PR scoped role == "agent" and
# left every other role reading the whole node, including any role added later.
# Here the whole view belongs to two named seats, super-admin (the owner in the
# panel, and brin's message watcher which calls as admin) and operator, and
# every other principal is scoped. Unknown means scoped.
#
# THE PROPERTIES:
#   1. an agent is scoped even without `mine`
#   2. super-admin and operator keep the whole view; asking for `mine` scopes them
#   3. a role nobody has thought of yet is scoped (fail closed)
#   4. the clause covers both ends of a message, by slug and by every mailbox
#      the agent holds, and is parameterised rather than interpolated
#   5. the route uses the rule, and rows carry ms.error beside ms.status so
#      `blocked` arrives with the sentence that explains it
#   6. state changes use the same visibility guard, so an agent cannot clear a
#      blocked stranger row by PATCHing its state
#
#   tests/message-list-scoped-to-party.test.sh                  # tree, PASS
#   tests/message-list-scoped-to-party.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "message list scoped to its party (git:$BASELINE)"
else echo "message list scoped to its party (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
[[ -s "$D/messages.py" ]] || { bad "messages.py is readable"; echo; echo "FAIL"; exit 1; }

# The body of the route: from its def to the next top-level def, class or
# decorator.
route=$(awk 'f && /^(def|class|@)/{exit} /^def list_messages\(/{f=1} f' "$D/messages.py")
if grep -q 'if _must_scope(principal, mine):' <<<"$route"; then
    ok "the list route asks the scoping rule"
else
    bad "the list route asks the scoping rule -- it filters by tenant only"
fi
# `mine` stays "addressed to me". comm.sh inbox marks what it lists as routed,
# and an internal letter an agent sends is an inbound row with that agent as
# sender: widening `mine` to both ends, as the field PR did, has the author
# take delivery of its own letter and the recipient never woken.
if grep -q '(_inbox_clause if mine else _party_clause)(principal.username)' <<<"$route"; then
    ok "mine keeps meaning the inbox; the wider view is for everything else"
else
    bad "mine keeps meaning the inbox; the wider view is for everything else"
fi
if grep -q 'ms.status, ms.error' <<<"$route"; then
    ok "a row carries the reason beside its status"
else
    bad "a row carries the reason beside its status"
fi

state_route=$(awk 'f && /^(def|class|@)/{exit} /^def mark_message_state\(/{f=1} f' "$D/messages.py")
if grep -q '_visible_message(cur, principal, message_id)' <<<"$state_route"; then
    ok "state changes use the party/blocked visibility guard"
else
    bad "state changes use the party/blocked visibility guard"
fi

while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/messages.py" <<'PY'
import ast, sys
from types import SimpleNamespace as P
tree = ast.parse(open(sys.argv[1]).read())
keep = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in ("_must_scope", "_party_clause", "_inbox_clause"))
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_READS_WHOLE_NODE" for t in n.targets))]
ns = {}
exec(compile(ast.Module(body=keep, type_ignores=[]), "messages.py", "exec"), ns)
if "_must_scope" not in ns or "_party_clause" not in ns:
    print("FAIL _must_scope and _party_clause exist"); sys.exit(0)
scope, clause_of = ns["_must_scope"], ns["_party_clause"]

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

check("an agent is scoped even without mine", scope(P(username="alex", role="agent"), False) is True)
check("the owner in the panel keeps the whole view", scope(P(username="admin", role="super-admin"), False) is False)
check("the operator seat keeps the whole view", scope(P(username="claude", role="operator"), False) is False)
check("asking for mine scopes anyone", scope(P(username="admin", role="super-admin"), True) is True)
check("a role nobody has thought of yet is scoped", scope(P(username="x", role="viewer"), False) is True)
check("an empty role is scoped", scope(P(username="x", role=""), False) is True)

clause, params = clause_of("alex")
check("the clause matches what the agent sent", "m.sender = %s" in clause and "m.sender IN (" in clause)
check("the clause matches what the agent received", "m.recipient = %s" in clause and "m.recipient IN (" in clause)
check("both ends match the agent's mailboxes too", clause.count("agent_channels") == 2)
check("one parameter per placeholder", clause.count("%s") == len(params) and set(params) == {"alex"})
evil, _ = clause_of("alex'; DROP TABLE sn_core.messages; --")
check("the name is a parameter, never part of the SQL", "DROP TABLE" not in evil)
PY
)

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
