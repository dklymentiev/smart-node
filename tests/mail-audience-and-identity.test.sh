#!/usr/bin/env bash
#
# A letter keeps its room, and a copy knows which box it landed in.
#
# THREE DEFECTS, one test, because they are one confusion: the node treated the
# PROTOCOL's view of a message as the ROW's view of it.
#
# 1. THE ROW ID WAS THE WIRE ID. external_id was the RFC822 Message-ID, and the
#    unique index is (channel_type, external_id). One letter addressed to three
#    of this node's mailboxes is delivered three times with the SAME
#    Message-ID, so copies two and three were dedup hits: one row survived, two
#    agents never saw a letter sitting in their own mailbox, and the poller
#    logged nothing new. Proven on the live DB inside a rolled-back
#    transaction: three copies, old scheme keeps 1 row, new scheme keeps 3, and
#    a repeat poll of the same box still adds 0.
#
#    The Message-ID is now kept in metadata, because the REPLY path needs the
#    real one for In-Reply-To. Scoping the header instead of the row would
#    break threading in the reader's mail client -- the same conflation, cost
#    paid at the other end.
#
# 2. THE REPLY LOST EVERYONE IN COPY. The adapter never recorded To and Cc, so
#    a reply went to the sender alone. Whoever set the copy list was dropped
#    silently and was the last to know. send_outbound has read a `recipients`
#    column now and puts everything past the primary into Cc; nothing
#    was ever writing it.
#
# 3. THE GATE TREATED A COLLEAGUE AS THE OUTSIDE WORLD. A reply to a group was
#    refused with "agent 'architect' has no contact marketing.node@<domain>",
#    where both ends are this node. Checked in BOTH directions here: the field
#    PR fixed only the outbound half, which would leave a colleague's mail
#    arriving and never waking anyone -- the reply permitted and the question
#    still ignored.
#
#   tests/mail-audience-and-identity.test.sh                  # tree, PASS
#   tests/mail-audience-and-identity.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "mail audience and identity (git:$BASELINE)"
else echo "mail audience and identity (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/communication/adapters/email/adapter.py   > "$D/adapter.py"
read_src companions/communication/adapters/email/transport.py > "$D/transport.py"
read_src companions/communication/dispatch.py                 > "$D/dispatch.py"
read_src companions/communication/common/db.py                > "$D/db.py"
read_src companions/communication/common/contacts.py          > "$D/contacts.py"
for f in adapter transport dispatch db contacts; do
    [[ -s "$D/$f.py" ]] || { bad "sources are readable ($f.py)"; echo; echo "FAIL"; exit 1; }
done

# ------------------------------------------------ 1. row identity vs wire id
if grep -qE '^\s*external_id = "%s\|%s" % \(mailbox, wire_id\)' "$D/adapter.py"; then
    ok "the row id is scoped to the receiving mailbox"
else
    bad "the row id is scoped to the receiving mailbox -- one letter to N boxes is N rows"
fi

if grep -qE '"message_id": msg\["message_id"\]' "$D/adapter.py"; then
    ok "the wire Message-ID is kept in metadata"
else
    bad "the wire Message-ID is kept in metadata -- the reply path needs the real one"
fi

if grep -qE 'in_reply_to = \(metadata\.get\("message_id"\) or ""\)' "$D/dispatch.py"; then
    ok "In-Reply-To is built from the wire id, not the row id"
else
    bad "In-Reply-To is built from the wire id, not the row id -- threading breaks"
fi

# The fallback must survive: rows written before the scoping carry the bare
# Message-ID in external_id and must still thread.
if grep -q 'if not in_reply_to and external_id and not external_id.startswith("uid:")' "$D/dispatch.py"; then
    ok "rows written before the scoping still thread (external_id fallback kept)"
else
    bad "rows written before the scoping still thread (external_id fallback kept)"
fi

# ------------------------------------------------------- the row names its box
if grep -qE '^\s*recipient=mailbox,' "$D/adapter.py"; then
    ok "the inbound row names the box that received it"
else
    bad "the inbound row names the box that received it, not the To header"
fi

# --------------------------------------------------------------- 2. audience
for hdr in to_addrs cc_addrs; do
    if grep -q "\"$hdr\":" "$D/transport.py"; then
        ok "transport captures $hdr"
    else
        bad "transport captures $hdr -- a reply cannot rebuild a room it never saw"
    fi
done

if grep -q 'recipients=\[{"addr": a, "role": "cc"} for a in _cc\]' "$D/dispatch.py"; then
    ok "the reply carries the rest of the room as cc"
else
    bad "the reply carries the rest of the room as cc"
fi

# Our own box and the sender must not appear in the copies: the first is the
# replier, the second is already the To.
if grep -q 'if not _al or _al in (_mine, _from) or _al in _seen:' "$D/dispatch.py"; then
    ok "the copies exclude our own box, the sender, and duplicates"
else
    bad "the copies exclude our own box, the sender, and duplicates"
fi

# And the column has to be written, not merely accepted.
if grep -q 'metadata, recipients=recipients)' "$D/db.py" \
   && grep -q 'Json(recipients) if recipients else None' "$D/db.py"; then
    ok "record_outbound writes the recipients column"
else
    bad "record_outbound writes the recipients column -- send_outbound reads it"
fi

# ------------------------------------------------------------------ 3. the gate
if grep -q 'def own_mailboxes(channel_type):' "$D/contacts.py"; then
    ok "contacts knows this node's own addresses"
else
    bad "contacts knows this node's own addresses"
fi

out_hit=$(awk '/^def decide\(/,/^def may_wake\(/' "$D/contacts.py" | grep -c 'own_mailboxes(channel_type)')
in_hit=$(awk '/^def may_wake\(/,0' "$D/contacts.py" | grep -c 'own_mailboxes(channel_type)')
if [[ "$out_hit" -ge 1 ]]; then
    ok "the outbound gate lets a colleague's mailbox through"
else
    bad "the outbound gate lets a colleague's mailbox through"
fi
if [[ "$in_hit" -ge 1 ]]; then
    ok "the inbound gate does too -- a reply allowed and the question ignored is worse"
else
    bad "the inbound gate does too -- a reply allowed and the question ignored is worse"
fi

# Fail closed. An unknown answer must never widen a gate.
if awk '/^def own_mailboxes/,/^def decide/' "$D/contacts.py" | grep -q 'return set()'; then
    ok "an unreadable catalog fails closed"
else
    bad "an unreadable catalog fails closed"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
