#!/usr/bin/env bash
#
# Every inbound message is stamped with who it is from, by ONE rule.
#
# WHAT WENT WRONG. The read side shipped without the corresponding write side.
# GET /api/messages/by-contact answers "everything this person said, on every
# channel" from sn_config.contact_channels, and it works. Nothing ever wrote
# sn_core.message_identity, and nothing ever ADDED a handle to the map: it grew
# only when a person typed into the panel. Measured here 2026-09-10: 0 identity
# rows against 1596 inbound messages. On a field node the same gap left one
# person spelled three ways across voice and
# web-chat and recognised as none of them.
#
# THE PROPERTIES:
#
#   1. one rule, in the database -- core-api and the communication adapters are
#      different deployments sharing no Python, and "who is this" must not have
#      two answers
#   2. it is SECURITY DEFINER with a pinned search_path: the adapters hold
#      SELECT/INSERT on message_identity only, and touching the handle map is
#      part of the rule rather than part of what an adapter may do
#   3. the verdict is written once and never revised (ON CONFLICT DO NOTHING),
#      and the function reports what is ON RECORD, not what this call computed
#   4. the map learns a handle ONLY from a session-proved door. A claim in a
#      message body proves nothing
#   5. every inbound writer stamps: the four core-api ingests and BOTH companion
#      recorders -- email goes through record_inbound_with_attachments, which
#      the original PR missed, and email is the channel whose handle is a real
#      portable address
#   6. internal and system traffic is not stamped -- agent-to-agent, no person
#   7. no stamp is ever fatal to an ingest
#
#   tests/message-identity-write-side.test.sh                  # tree, PASS
#   tests/message-identity-write-side.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "message identity write side (git:$BASELINE)"
else echo "message identity write side (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src bootstrap/B02-sn-core-schema.sql             > "$D/schema.sql"
read_src companions/core-api/routes/messages.py       > "$D/routes.py"
read_src companions/core-api/message_identity.py      > "$D/mi.py"
read_src companions/communication/common/db.py        > "$D/db.py"
[[ -s "$D/schema.sql" && -s "$D/routes.py" && -s "$D/db.py" ]] \
    || { bad "sources are readable"; echo; echo "FAIL"; exit 1; }

# ------------------------------------------------------------------------ 1
if grep -q 'CREATE OR REPLACE FUNCTION sn_core.stamp_message_identity' "$D/schema.sql"; then
    ok "the rule exists once, in the database"
else
    bad "the rule exists once, in the database"
fi
for f in mi.py db.py; do
    if grep -q 'sn_core.stamp_message_identity' "$D/$f"; then
        ok "$f calls the same function"
    else
        bad "$f calls the same function -- two answers to 'who is this' is the defect"
    fi
done

# ------------------------------------------------------------------------ 2
if grep -q 'LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public AS \$stamp_message_identity\$' "$D/schema.sql"; then
    ok "SECURITY DEFINER with a pinned search_path"
else
    bad "SECURITY DEFINER with a pinned search_path"
fi

# ------------------------------------------------------------------------ 3
n=$(grep -c 'ON CONFLICT (message_id) DO NOTHING' "$D/schema.sql")
if [[ "$n" -ge 2 ]]; then
    ok "the verdict is written once on both branches ($n inserts)"
else
    bad "the verdict is written once on both branches (found $n, want at least 2)"
fi
if grep -q 'RETURNING trust INTO v_stored' "$D/schema.sql" \
   && grep -q 'SELECT trust INTO v_stored FROM sn_core.message_identity' "$D/schema.sql"; then
    ok "it reports what is on record, not what this call computed"
else
    bad "it reports what is on record, not what this call computed"
fi

# ------------------------------------------------------------------------ 4
if grep -q "IF v_how = 'account' AND p_proof = 'session' THEN" "$D/schema.sql"; then
    ok "the map learns only from a session-proved door"
else
    bad "the map learns only from a session-proved door"
fi
if grep -q "verified_by, verified_at, last_seen_at" "$D/schema.sql" \
   && grep -q "'session', now(), now()" "$D/schema.sql"; then
    ok "a learned handle records HOW it was learned"
else
    bad "a learned handle records HOW it was learned"
fi

# ------------------------------------------------------------------------ 5
n=$(grep -c 'stamp_identity(cur,' "$D/routes.py")
if [[ "$n" -ge 4 ]]; then
    ok "all $n core-api ingests stamp (inbound, voice, voice turns, web-chat)"
else
    bad "all four core-api ingests stamp (found $n)"
fi
n=$(grep -c '^        stamp_identity(channel_type, msg_id, sender)' "$D/db.py")
if [[ "$n" -ge 1 ]]; then
    ok "record_inbound stamps (telegram, slack)"
else
    bad "record_inbound stamps (telegram, slack)"
fi
# The email path is a DIFFERENT recorder, and it is the one that matters most.
if awk '/^def record_inbound_with_attachments/,/^def record_outbound/' "$D/db.py" \
   | grep -q 'stamp_identity(channel_type, msg_id, sender)'; then
    ok "record_inbound_with_attachments stamps -- the email path"
else
    bad "record_inbound_with_attachments stamps -- the email path is unstamped"
fi

# ------------------------------------------------------------------------ 6
if grep -q 'if body.channel_type not in ("internal", "system"):' "$D/routes.py"; then
    ok "internal and system traffic is not stamped"
else
    bad "internal and system traffic is not stamped"
fi

# ------------------------------------------------------------------------ 7
# Every call site in the routes is inside a try, and the companion helper
# swallows its own failure.
opens=$(grep -c 'stamp_identity(cur,' "$D/routes.py")
guards=$(grep -B2 'stamp_identity(cur,' "$D/routes.py" | grep -c 'try:')
if [[ "$guards" -ge "$opens" ]]; then
    ok "every route stamp is wrapped ($guards guards for $opens calls)"
else
    bad "every route stamp is wrapped ($guards guards for $opens calls)"
fi
if awk '/^def stamp_identity/,/^def _emit/' "$D/db.py" | grep -q 'except Exception'; then
    ok "the companion helper never raises"
else
    bad "the companion helper never raises"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
