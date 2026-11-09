#!/usr/bin/env bash
# Four chat fixes that were first made by hand on a live node, now in the source: a repeated send is answered once,
# an empty sync body is read from history, a silent tool-only turn ends with a service line, and a gateway refusal
# that stored nothing is not shown as "working".
set -u
cd "$(dirname "$0")/.."
fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
S=companions/studio/src/server.js; A=companions/studio/public/next2/js/app.js
G=companions/gateway/backends/cli_claude.py; E=companions/brin/core/executor.py
chk "a repeated chat POST with the same client_turn_id gets the first answer back" "grep -qF '_chatTurnGet(dedupKey)' $S && grep -qF 'X-SN-Dedup' $S"
chk "only a 2xx answer is remembered, so a failed send can be retried" "grep -qF 'upstream.status >= 200 && upstream.status < 300' $S"
chk "the remembered answers are bounded" "grep -qF '_chatTurnMax' $S && grep -qF '_chatTurnTtlMs' $S"
chk "an empty sync body is looked up in the stored history before waiting" "grep -qF 'await fetchHistory(deskId)' $A && grep -qF 'replyFor(_msgs, turn)' $A"
chk "a turn with tool calls and no text ends with a service line" "grep -qF 'silent turn guard fired' $G && grep -qF 'ended without' $G"
chk "the guard does not touch a stopped, failed or finished-early turn" "grep -qF 'not _failed and not _stopped' $G && grep -qF 'not _done_early' $G"
chk "a gateway refusal that stored nothing is an error, not a deferred turn" "grep -qF 'startswith(\"session is \")' $E"
chk "the release record is still read when asked (1.13.1 fix kept)" "grep -qF 'release: releaseNow()' $S"
cp $A /tmp/_hf_check.mjs
node --check $S && node --check /tmp/_hf_check.mjs && python3 -m py_compile $G $E && echo "  ok   all four files parse" || fail=1
[ $fail = 0 ] && echo "PASS: chat hotfixes" || { echo FAIL; exit 1; }
