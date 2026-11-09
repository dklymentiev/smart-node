#!/usr/bin/env bash
# A live socket that died quietly is noticed and replaced: the server sends a tick the page can see, the page
# opens a new socket after 70 s of silence, and again when it comes back into view with a stale one.
set -u
cd "$(dirname "$0")/.."
A=companions/studio/public/next2/js/app.js; S=companions/studio/src/server.js
fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chk "server sends a tick message beside the protocol ping" "grep -q \"send({ kind: .tick. })\" $S"
chk "page remembers when it last heard the socket" "grep -q \"_liveHeardAt = Date.now()\" $A"
chk "a watchdog reopens after 70 s of silence while in view" "grep -q \"Date.now() - _liveHeardAt > 70000\" $A"
chk "coming back into view reopens a socket that is not healthy" "grep -q \"if (_liveReopen && !_liveHealthy()) _liveReopen();\" $A"
chk "pageshow and online are handled" "grep -q \"addEventListener(.pageshow.\" $A && grep -q \"addEventListener(.online.\" $A"
chk "a reopen clears the frozen reconnect timer" "grep -q \"if (waitT) { clearTimeout(waitT); waitT = null; }\" $A"
node tests/live-socket.behavior.mjs $A >/tmp/_live_beh.out 2>&1 && echo "  ok   behaviour: quiet socket replaced, ticking one kept" || { echo "  FAIL behaviour"; tail -5 /tmp/_live_beh.out; fail=1; }
cp $A /tmp/_live_check.mjs && node --check /tmp/_live_check.mjs && node --check $S && echo "  ok   both files parse" || fail=1
[ $fail = 0 ] && echo "PASS: live socket silence" || { echo "FAIL"; exit 1; }
