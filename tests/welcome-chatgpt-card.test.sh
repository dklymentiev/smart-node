#!/usr/bin/env bash
# The first-run page offers the ChatGPT plan and the Studio server relays its two sign-in steps to core-api.
set -u
cd "$(dirname "$0")/.."
W=companions/studio/public/welcome/welcome.js; S=companions/studio/src/server.js
fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chk "the page lists the ChatGPT subscription, waiting for the Codex runtime" "grep -q \"id: .chatgpt., .*needs: .codex.\" <(tr -d \"\n\" < $W | sed \"s/  */ /g\")"
chk "sign-in start goes to the chatgpt route" "grep -q \"/api/gateway/chatgpt\" $W"
chk "the pasted address is sent as url, a Claude code as code" "grep -q \"gpt ? { url: code } : { code }\" $W"
chk "Studio relays login/start" "grep -q \"app.post(./api/gateway/chatgpt/login/start.\" $S"
chk "Studio relays login/complete" "grep -q \"app.post(./api/gateway/chatgpt/login/complete.\" $S"
node --check $S && node --check $W && echo "  ok   both files parse" || fail=1
[ $fail = 0 ] && echo "PASS: welcome chatgpt card" || { echo "FAIL"; exit 1; }
