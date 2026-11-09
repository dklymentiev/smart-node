#!/usr/bin/env bash
#
# A service may act for an agent, and only for the agents it was listed for.
#
# WHAT WENT WRONG. core-api makes an agent's name unforgeable by refusing it over TCP:
# "an agent that types another agent's name is measured against the other's
# permissions, and a capability any process on the trusted network can type is not
# one." Correct, and it left the voice line unable to do its job -- a person
# speaks, an agent must delegate, and voice is a container that can never be the
# agent's own process. From 2026-08-14 every voice delegation answered 403 and the
# caller was told, in prose, that something had gone wrong.
#
# It went unnoticed for SEVEN WEEKS. Not because the tests were missing: reference-node
# carries e2e/reorder_delegation.sh, which catches exactly this and exits 1
# correctly. Nothing ran it. That is why this file exists as well as that one --
# and why the acceptance below is behaviour, not source.
#
# THE FOUR PROPERTIES, and all four must hold together. Three of them are
# refusals: a fix that opens the door is not a fix, it is the identity spoofing hole.
#
#   1. a listed service acting for a listed agent  -> 200, sender IS the agent
#   2. a listed service acting for an UNLISTED agent -> 403
#   3. the right service name with a WRONG secret    -> 403
#   4. a bare agent name over TCP, as before         -> 403
#
#   source-mode (default): the properties that can be read off the tree
#   --node IP: the four calls above, against a running node. This is the real one.
#
#     tests/service-on-behalf.test.sh                       # tree, expect PASS
#     tests/service-on-behalf.test.sh --baseline REF        # git:REF, expect FAIL
#     tests/service-on-behalf.test.sh --node <node-ip>    # live, needs the service credential
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""; NODE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --baseline) BASELINE="${2:-}"; shift 2 ;;
        --node)     NODE="${2:-}"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

src() {
    local rel="$1"
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:$rel" 2>/dev/null
    else
        cat "$ROOT/$rel" 2>/dev/null
    fi
}

echo "service acting for an agent (${BASELINE:-working tree})"

AUTH="$(src companions/core-api/auth.py)"
MSGS="$(src companions/core-api/routes/messages.py)"
VOICE="$(src companions/voice/backend/main.py)"

# --- the door exists at all ------------------------------------------------
if grep -q "def acting_for" <<<"$AUTH"; then
    ok "core-api can recognise a service acting for an agent"
else
    bad "no way for a service to act for an agent -- voice cannot delegate at all"
fi

# --- and it is not a way to bypass TCP identity refusal -----------------------------------
# The list must default to refusal. A default of 'allow' would hand every agent
# to anything holding the secret, which is the capability the whole design
# withholds.
if grep -q "SN_SERVICE_AGENTS" <<<"$AUTH" && grep -q "return False" <<<"$AUTH"; then
    ok "an unlisted service is refused by default"
else
    bad "no authorisation list, or no default refusal -- the credential alone would open every agent"
fi

if grep -q "compare_digest" <<<"$AUTH"; then
    ok "the credential is compared in constant time"
else
    bad "the service secret is compared with == somewhere -- that leaks its prefix by timing"
fi

# --- the voice line stops typing somebody else's name ----------------------
if grep -q 'Remote-User": brin_agent' <<<"$VOICE"; then
    bad "voice still asserts a bare agent name over TCP -- refused now, so every delegation 403s"
else
    ok "voice presents a credential instead of typing the agent's name"
fi

# --- and who acted is written down, not inferred ---------------------------
if grep -q "provenance_chain" <<<"$MSGS" && grep -q "acting_service" <<<"$MSGS"; then
    ok "the acting service is recorded on the message"
else
    bad "nothing records that a service acted -- 'alex sent this' and 'the line sent this as alex' become one row"
fi

# --- the real acceptance: the four calls, against a node -------------------
if [[ -n "$NODE" ]]; then
    echo
    echo "live, against $NODE"
    KEY="${SN_SSH_KEY:-}"
    [[ -n "$KEY" ]] || { echo "SN_SSH_KEY is required with --node" >&2; exit 2; }
    rex() { ssh -n -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "admin@$NODE" "$1" 2>/dev/null; }

    SECRET="$(rex "sudo sed -n 's/^SN_SERVICE_AUTH_SECRET=//p' /etc/smart-node/core-api-service-auth.env | tail -1")"
    if [[ -z "$SECRET" ]]; then
        bad "no service credential on the node -- core-api has not minted one"
    else
        api="${SN_CORE_API:-}/api/messages/send"
        hdr_common="-H 'Content-Type: application/json'"
        payload='{"to":"theo","body":"service-on-behalf test probe","channel":"internal"}'

        code() { rex "curl -sS --max-time 20 -o /dev/null -w '%{http_code}' $1 -d '$payload' $api"; }

        c1=$(code "-X POST -H 'Remote-User: alex' -H 'X-SN-Service-Auth: $SECRET' -H 'X-SN-Service: voice' -H 'X-SN-On-Behalf-Of: alex' $hdr_common")
        [[ "$c1" == 200 ]] && ok "listed service, listed agent -> 200" \
                           || bad "listed service, listed agent -> $c1 (delegation is still dead)"

        c2=$(code "-X POST -H 'Remote-User: theo' -H 'X-SN-Service-Auth: $SECRET' -H 'X-SN-Service: voice' -H 'X-SN-On-Behalf-Of: theo' $hdr_common")
        [[ "$c2" == 403 ]] && ok "listed service, UNLISTED agent -> 403" \
                           || bad "listed service acting for an unlisted agent -> $c2 (the list is not enforced)"

        c3=$(code "-X POST -H 'Remote-User: alex' -H 'X-SN-Service-Auth: wrong-secret' -H 'X-SN-Service: voice' -H 'X-SN-On-Behalf-Of: alex' $hdr_common")
        [[ "$c3" == 403 ]] && ok "wrong credential -> 403" \
                           || bad "a wrong credential got $c3 -- the secret is not being checked"

        c4=$(code "-X POST -H 'Remote-User: alex' $hdr_common")
        [[ "$c4" == 403 ]] && ok "bare agent name over TCP -> 403" \
                           || bad "a bare agent name got $c4 -- TCP identity refusal has been undone"
    fi
fi

echo
if (( fails )); then echo "FAILED ($fails)"; exit 1; fi
echo "PASS"
