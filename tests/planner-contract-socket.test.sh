#!/usr/bin/env bash
#
# The planner contract handed to an agent names the socket, never a TCP door.
#
# WHAT WENT WRONG. companions/tracking.sh writes the usage text every agent reads
# for the planner integration, and it said:
#
#     Over HTTP as yourself: every call carries `Remote-User: <your-slug>`, and
#     that same slug is your `assignee`. Base: `__CORE_API_URL__/api/planner/api/v1`.
#
# core-api refuses an agent identity asserted over TCP by design (the identity boundary;
# routes/planner.py: "agent identity asserted, not proved"), so an agent that
# followed its own contract got 403 on every planner call. Reported from the
# a node in the field: every planner call an agent made there
# failed, and a roll call answered "zero open tasks" from nine agents, none of
# whom could see the planner at all. The answer looked like data and was the
# artefact of a door that was never open.
#
# The text is written TWICE in tracking.sh -- run_intent and run_contracts_only --
# and only one copy carried the placeholder, so a reader fixing the obvious one
# would leave the other. Both are checked here.
#
# THE PROPERTIES:
#
#   1. every planner usage block names the core-api unix socket
#   2. no planner usage block offers a TCP base for /api/planner (that includes
#      the __CORE_API_URL__ placeholder, which renders to one)
#   3. any address line in a planner usage block carries --unix-socket
#   4. no planner usage block tells the agent to send Remote-User. It is not
#      merely useless there, it is the sentence that contradicted the fix and
#      would put the next reader back on the TCP door
#   5. both copies of the block exist and are checked (2 blocks, not 1)
#   6. the block says writes go through the planner tools, before it shows any
#      raw POST/PATCH form: an unattended run refuses a curl with a body
#
# Property 4 is scoped to the planner blocks on purpose: node-admin and contacts
# are NOT socket-only, and their usage text says Remote-User correctly.
#
#   tests/planner-contract-socket.test.sh                  # working tree, PASS
#   tests/planner-contract-socket.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() { # PATH -> stdout
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then
    echo "planner contract names the socket (git:$BASELINE)"
else
    echo "planner contract names the socket (working tree)"
fi

SRC=$(mktemp); read_src companions/tracking.sh > "$SRC"
if [[ ! -s "$SRC" ]]; then
    bad "companions/tracking.sh is readable"
    rm -f "$SRC"; echo; echo "FAIL"; exit 1
fi

# Each planner usage block runs from its heading to the USAGE heredoc terminator.
STARTS=$(grep -n '^## planner -- your node task queue' "$SRC" | cut -d: -f1)
COUNT=$(printf '%s\n' "$STARTS" | grep -c '[0-9]')

if [[ "$COUNT" -eq 2 ]]; then
    ok "both copies of the planner usage block are present (found $COUNT)"
else
    bad "both copies of the planner usage block are present (found $COUNT, want 2)"
fi

n=0
for start in $STARTS; do
    n=$((n + 1))
    end=$(awk -v s="$start" 'NR > s && /^USAGE$/ { print NR; exit }' "$SRC")
    if [[ -z "$end" ]]; then
        bad "block $n: found its USAGE terminator"
        continue
    fi
    BLOCK=$(mktemp)
    sed -n "${start},${end}p" "$SRC" > "$BLOCK"

    if grep -q -- '--unix-socket /run/smart-node/api/core-api.sock' "$BLOCK"; then
        ok "block $n: names the core-api unix socket"
    else
        bad "block $n: names the core-api unix socket"
    fi

    # Property 2 -- a TCP base for the planner, in any of the three shapes that
    # have appeared in this file: the placeholder, loopback, and a bare host.
    if grep -qE '(__CORE_API_URL__|https?://(127\.0\.0\.1|localhost)(:[0-9]+)?)/api/planner' "$BLOCK" \
       && ! grep -qE -- '--unix-socket.*(__CORE_API_URL__|https?://)' "$BLOCK"; then
        bad "block $n: offers a TCP base for /api/planner"
    else
        ok "block $n: offers no TCP base for /api/planner"
    fi

    # Property 3 -- every line that carries an address carries the socket flag.
    stray=$(grep -nE 'https?://[^ ]*/api/planner' "$BLOCK" | grep -v -- '--unix-socket' || true)
    if [[ -z "$stray" ]]; then
        ok "block $n: every planner address line goes through the socket"
    else
        bad "block $n: address line without --unix-socket: ${stray%%$'\n'*}"
    fi

    # Property 4 -- and it must not ask for the header that the TCP door reads.
    if grep -q 'Remote-User' "$BLOCK"; then
        if grep -qE 'NO Remote-User|no Remote-User' "$BLOCK"; then
            ok "block $n: mentions Remote-User only to forbid it"
        else
            bad "block $n: still tells the agent to send Remote-User"
        fi
    else
        ok "block $n: does not ask for Remote-User"
    fi
    # Property 6 -- writes go through the planner tools. A curl that sends a body
    # is a shell command that changes something; a run with no operator attached
    # refuses it, and the comment the agent meant to write is lost. The rule has
    # to come before the first raw write form, or the reader copies the form.
    rule=$(grep -n 'WRITE WITH THE PLANNER TOOLS' "$BLOCK" | head -1 | cut -d: -f1)
    form=$(grep -nE '^    (POST|PATCH) /tasks' "$BLOCK" | head -1 | cut -d: -f1)
    if [[ -n "$rule" && -n "$form" && "$rule" -lt "$form" ]]; then
        ok "block $n: tells the agent to write with the planner tools before any curl write form"
    else
        bad "block $n: tells the agent to write with the planner tools before any curl write form (rule line ${rule:-none}, first form line ${form:-none})"
    fi
    rm -f "$BLOCK"
done

rm -f "$SRC"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
