#!/usr/bin/env bash
#
# An agent's own ledger is readable, and the allow list reaches an existing node.
#
# WHAT WENT WRONG. brin's ledger_service asks core-api for an agent's inbox, tool
# calls and last activity with `Remote-User: <slug>` over TCP. now
# core-api refuses an agent identity asserted that way -- correctly, a header is
# forgeable -- and every caller here reads a non-200 as "no rows", because a
# ledger failure must never break a turn.
#
# Measured on THIS node before the change:
#     GET /api/events?actor=agent:alex  (Remote-User: alex)     -> 403
#     the same call as admin                                    -> 200, rows
#     the same call with voice's service headers                -> 200
#     the same call with brin's service headers                 -> 403
#
# So [internal mail] and [tool calls] were empty on every turn of every agent and
# nothing showed it from outside: the agent behaved as though nothing had
# happened, because as far as its context went, nothing had.
#
# THE SECOND HALF IS OURS. Their fix adds `brin:*` to SERVICE_AGENTS_DEFAULT.
# That default is written into /etc/smart-node/core-api-service-auth.env ONCE --
# the file says so in its own header -- so on every node that already exists the
# list stays at whatever the installing release shipped, and brin would have
# remained refused everywhere except on a brand new node. The allow list is not
# the secret and must not be frozen with it.
#
# THE PROPERTIES:
#
#   1. brin is in the shipped default
#   2. an existing node's list is reconciled, not left frozen
#   3. the merge only ADDS: a service already named keeps its own clause, so an
#      operator who narrowed voice:* to voice:alex is not re-widened by a release
#   4. every core-api read in ledger_service goes through _agent_headers -- one
#      left behind is one section silently empty forever
#   5. the wake answers in the language of the conversation, not of the prompt
#
#   tests/agent-ledger-identity.test.sh                  # working tree, PASS
#   tests/agent-ledger-identity.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent ledger identity (git:$BASELINE)"
else echo "agent ledger identity (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api.sh                     > "$D/core-api.sh"
read_src companions/brin/services/ledger_service.py > "$D/ledger.py"
read_src companions/brin/runner.py                  > "$D/runner.py"
read_src companions/dispatcher.sh                   > "$D/dispatcher.sh"
for f in core-api.sh ledger.py runner.py dispatcher.sh; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# ------------------------------------------------------------------------ 1
if grep -q 'SERVICE_AGENTS_DEFAULT=.*brin:\*' "$D/core-api.sh"; then
    ok "brin is in the shipped service allow list"
else
    bad "brin is in the shipped service allow list"
fi

# and the dispatcher must actually be handed the credential
if grep -q 'SN_SERVICE_AUTH_SECRET=\$svc_secret' "$D/dispatcher.sh"; then
    ok "the dispatcher writes brin's service credential"
else
    bad "the dispatcher writes brin's service credential"
fi

# ------------------------------------------------------------------------ 2
if grep -q '_merge_service_agents' "$D/core-api.sh" \
   && grep -q 'SN_SERVICE_AGENTS=\$merged' "$D/core-api.sh"; then
    ok "an existing node's allow list is reconciled, not frozen with the secret"
else
    bad "an existing node's allow list is reconciled, not frozen with the secret"
fi

# ------------------------------------------------------------------------ 3
FN=$(sed -n '/^_merge_service_agents()/,/^}$/p' "$D/core-api.sh")
if [[ -z "$FN" ]]; then
    bad "_merge_service_agents is defined"
else
    (
        eval "$FN"
        t() {
            local got; got=$(_merge_service_agents "$1" "$2")
            if [[ "$got" == "$3" ]]; then printf '  ok   merge [%s] -> %s\n' "$1" "$got"
            else printf '  FAIL merge [%s] -> %s (want %s)\n' "$1" "$got" "$3"; exit 1; fi
        }
        DFT="voice:*;brin:*"
        t "voice:*"        "$DFT" "voice:*;brin:*"
        t "voice:alex"     "$DFT" "voice:alex;brin:*"
        t "voice:*;brin:*" "$DFT" "voice:*;brin:*"
        t ""               "$DFT" "voice:*;brin:*"
        t "runner:*"       "$DFT" "runner:*;voice:*;brin:*"
    ) || FAILED=$((FAILED + 1))
fi

# ------------------------------------------------------------------------ 4
stray=$(grep -n 'headers={"Remote-User"' "$D/ledger.py" || true)
if [[ -z "$stray" ]]; then
    ok "every core-api read in the ledger goes through _agent_headers"
else
    bad "a bare Remote-User read is left in the ledger: ${stray%%$'\n'*}"
fi
n=$(grep -c 'headers=_agent_headers(agent_slug)' "$D/ledger.py")
if [[ "$n" -ge 4 ]]; then
    ok "all $n ledger reads carry the service identity"
else
    bad "all ledger reads carry the service identity (found $n, want at least 4)"
fi

# ------------------------------------------------------------------------ 5
if grep -q 'language of the conversation you are answering' "$D/runner.py"; then
    ok "the wake answers in the language of the conversation"
else
    bad "the wake answers in the language of the conversation"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
