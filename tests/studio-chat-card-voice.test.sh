#!/usr/bin/env bash
#
# The Studio knows where the node's voice line is, from the routing contract.
#
# WHAT WENT WRONG. There was no way to reach the voice interface from the Studio
# at all, and an early fix wrote a literal host into the source.
#
# THE PROPERTIES:
#   1. /api/me carries voiceUrl, resolved from SMART_NODE_VOICE_HOSTNAME /
#      routing.env / voice.<domain>, never a literal host
#   2. app.js and server.js still parse
#
# NOT YET IN THE STUDIO: a button that starts a call. The page loads voiceUrl
# (meIdentity.voiceUrl) but nothing on it uses the value yet. The shell that
# had the button in its chat card has been removed.
#
#   tests/studio-chat-card-voice.test.sh                  # tree, PASS
#   tests/studio-chat-card-voice.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio: voice address (git:$BASELINE)"
else echo "Studio: voice address (working tree)"; fi

APP="$(read_src companions/studio/public/next2/js/app.js)"
SRV="$(read_src companions/studio/src/server.js)"

if grep -qE 'voiceUrl: *VOICE_URL' <<<"$SRV" && grep -qF 'SMART_NODE_VOICE_HOSTNAME' <<<"$SRV" \
   && grep -qF 'routing.env' <<<"$SRV"; then ok "/api/me carries voiceUrl from the routing contract"
else bad "/api/me has no voiceUrl resolved from routing.env"; fi

if grep -qE "(voice|line)\.[a-z0-9-]+\.(com|app|net|org)" <<<"$SRV$APP"; then bad "a literal voice host is written into the source"
else ok "no literal voice host"; fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '%s\n' "$APP" >"$tmp/app.js"; printf '%s\n' "$SRV" >"$tmp/server.mjs"
if node --check "$tmp/app.js" 2>/dev/null && node --check "$tmp/server.mjs" 2>/dev/null; then ok "app.js and server.js parse"
else bad "app.js or server.js does not parse"; fi

[[ $FAILED -eq 0 ]] && { echo "PASS"; exit 0; } || { echo "FAIL ($FAILED)"; exit 1; }
