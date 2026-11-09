#!/usr/bin/env bash
#
# The Studio chat does not block, a running turn can be stopped, and the tabs
# show which chats are being worked on.
#
# WHY THIS EXISTS. While an agent worked, the person could only
# watch. The page had one busy flag, so a turn in any chat disabled the composer
# in every chat. The Stop button asked core-api for the desk's session by a key
# brin no longer writes, got nothing, and printed "Stopped." over a turn that
# carried on (measured on sn-4671: abort answered "no active session", the turn
# completed 14 s later). Had it found the session, killing the CLI made the
# gateway read a crash and send the same message again. And a turn longer than
# the gateway's sync window released its agent at once and never wrote its end.
#
# The behaviour was shown in a browser on the stand; this pins the shape of the
# source so it does not quietly come back. Each property names the file it reads.
#
#   tests/studio-async-desk-chat.test.sh [--baseline REF]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:?--baseline needs a ref}"

SRC="$REPO"
if [[ -n "$BASELINE" ]]; then
  SRC="$(mktemp -d)"; trap 'rm -rf "$SRC"' EXIT
  for f in companions/studio/public/next2/js/app.js companions/studio/public/next2/index.html \
           companions/studio/public/next2/css/app.css companions/studio/src/server.js \
           companions/gateway/backends/cli_claude.py companions/gateway/sessions_v2_routes.py \
           companions/gateway/session_service.py companions/brin/main.py \
           companions/brin/core/executor.py companions/core-api/routes/desks.py; do
    mkdir -p "$SRC/$(dirname "$f")"
    git -C "$REPO" show "$BASELINE:$f" > "$SRC/$f" 2>/dev/null || : > "$SRC/$f"
  done
fi
APP="$SRC/companions/studio/public/next2/js/app.js"
HTML="$SRC/companions/studio/public/next2/index.html"
CSS="$SRC/companions/studio/public/next2/css/app.css"
SERVER="$SRC/companions/studio/src/server.js"
CLI="$SRC/companions/gateway/backends/cli_claude.py"
ROUTES="$SRC/companions/gateway/sessions_v2_routes.py"
SESS="$SRC/companions/gateway/session_service.py"
BRIN="$SRC/companions/brin/main.py"
EXEC="$SRC/companions/brin/core/executor.py"
DESKS="$SRC/companions/core-api/routes/desks.py"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
has() { grep -qF -- "$1" "$2"; }
expect() { if eval "$1"; then ok "$2"; else bad "$2"; fi; }

echo "Studio: the chat does not block, a turn can be stopped, the tabs say who works${BASELINE:+ [@ $BASELINE]}"

# ---- Studio page -------------------------------------------------------------
expect '! grep -q "_chatBusy" "$APP"' \
  "there is no page-wide busy flag (app.js)"
expect '! grep -qE "inp\.disabled = true|#orch.\)\.disabled = true" "$APP"' \
  "nothing disables the composer while a turn runs (app.js)"
expect 'has "const deskTurns = (id) => _turns[id] || [];" "$APP"' \
  "turns are kept per chat (app.js)"
expect 'has "state: hasTurns(deskId) ? '"'"'queued'"'"' : '"'"'running'"'"'" "$APP" && has "Queued, answered after the current turn" "$APP"' \
  "a message sent while its chat is working is queued and says so (app.js)"
# Stop sits with the running reply (the head of the agent's run, or the reply's
# live line), not by the composer.
stop_block="$(awk '/^function paintTurnStop\(turn\) \{/,/^\}/' "$APP")"
expect '! has "orchStop" "$HTML" && ! has "orchStop" "$APP" && grep -qF "|| turn.el;" <<< "$stop_block" && grep -qF "host.appendChild(b)" <<< "$stop_block" && grep -qF "stopTurn(turn.deskId)" <<< "$stop_block" && has "paintTurnStop(turn);" "$APP"' \
  "Stop is on the running reply itself, not a button by the composer (index.html, app.js)"
tab_block="$(awk '/^function renderTabs\(\) \{/,/^\}/' "$APP")"
expect '! grep -qF "contextBadge(" <<< "$tab_block" && grep -qF "tabContextLine(" <<< "$tab_block" && has "background-size: calc(100% * 100 / var(--pct))" "$CSS"' \
  "a tab shows its context as a coloured line along its bottom, not a number in its title (app.js, app.css)"
expect 'has "for (const turn of deskTurns(id))" "$APP"' \
  "a chat reopened mid-turn draws its running and queued turns again (app.js)"
expect '! grep -q "Date.now() + 120000" "$APP" && has "async function awaitReply(deskId, turn)" "$APP"' \
  "a turn past the sync window is waited for while the gateway says it runs, not for two minutes (app.js)"
expect 'has "fetch('"'"'/api/desks/in-progress'"'"'" "$APP" && has "el.classList.toggle('"'"'is-working'"'"', on);" "$APP" && has ".tab.is-working" "$CSS"' \
  "chat tabs and chat rows are marked from the gateway's turns in flight (app.js, app.css)"
expect 'has "m.stopped ? stoppedNote(null)" "$APP" && has ".sn-stopped" "$CSS"' \
  "a stopped reply keeps its text and its mark after a reload (app.js, app.css)"
expect 'has "let _histGen = 0;" "$APP"' \
  "two loads of one thread cannot both draw it (app.js)"
# The relay poll resyncs a chat after every answer. Clearing the thread before the
# request left it empty until the answer came back: the owner saw the chat flash
# after each reply. Before the fetch, only a switch to another chat clears.
prefetch_block="$(awk "/^async function loadHistory\(id\) \{/,/await fetch/" "$APP")"
expect '! grep -qxF "  t.innerHTML = '"'"';" <<< "$prefetch_block" && grep -qF "_threadFor !== id" <<< "$prefetch_block"'   "reloading the chat on screen does not empty it while the history loads (app.js)"
setactive_block="$(awk '/^function setActive\(id\) \{/,/^\}/' "$APP")"
expect 'grep -qF "draftStash(prev)" <<< "$setactive_block" && grep -qF "draftRestore(id)" <<< "$setactive_block"' \
  "each chat keeps its own draft, text and tray: switching chats stashes one and restores the other (app.js)"
expect 'has "localStorage.setItem(DRAFT_KEY" "$APP" && has "draftSaveText(activeId, _cmpTa.value)" "$APP"' \
  "a chat's draft text survives a reload (app.js)"
send_block="$(awk '/^async function sendChat\(\) \{/,/^\}/' "$APP")"
reset_line="$(grep -nF "cmpReset();" <<< "$send_block" | head -1 | cut -d: -f1)"
# The wait for the chat list is in deliverChat, which sendChat calls after the reset.
deliver_line="$(grep -nF "return deliverChat(deskId, text);" <<< "$send_block" | head -1 | cut -d: -f1)"
deliver_block="$(awk '/^async function deliverChat\(deskId, text\) \{/,/^\}/' "$APP")"
expect '[[ -n "$reset_line" && -n "$deliver_line" && "$reset_line" -lt "$deliver_line" ]] && grep -qF "await _desksReady" <<< "$deliver_block" && grep -qF "draftForget(deskId)" <<< "$send_block"' \
  "sending clears its own chat's draft before any await, never the chat switched to meanwhile (app.js)"

# ---- Studio server -----------------------------------------------------------
abort_block="$(awk "/app.post\('\/api\/desks\/:id\/abort'/,/^}\);/" "$SERVER")"
expect 'grep -qF "/stop\`" <<< "$abort_block" && ! grep -qF "/v2\`" <<< "$abort_block"' \
  "the Studio Stop asks brin to stop the turn, not to delete the session (server.js)"
expect 'grep -qF "no such chat" <<< "$abort_block"' \
  "only the chat's owner can stop it (server.js)"
expect 'has "app.get('"'"'/api/desks/in-progress'"'"'" "$SERVER" && has "data.desks.filter((d) => own.has(d.desk_id))" "$SERVER"' \
  "a person sees only their own chats in progress (server.js)"

# ---- gateway ------------------------------------------------------------------
expect 'has "\"request\": {\"subtype\": \"interrupt\"}" "$CLI" && has "async def interrupt(self, session_id, requested_by=None):" "$CLI"' \
  "the gateway stops a turn with the CLI's own interrupt (cli_claude.py)"
stop_line=$(grep -n "if session_id in _INTERRUPTS and (_got_eof or _result_subtype != \"success\"):" "$CLI" | head -1 | cut -d: -f1)
retry_line=$(grep -n "died without result on attempt 1" "$CLI" | head -1 | cut -d: -f1)
expect '[[ -n "$stop_line" && -n "$retry_line" && "$stop_line" -lt "$retry_line" ]]' \
  "a stopped turn is not retried (cli_claude.py)"
expect 'has "is_partial=response.stopped," "$ROUTES" && has "is_partial: bool = False" "$SESS"' \
  "the stopped reply is stored, marked partial (sessions_v2_routes.py, session_service.py)"
expect 'has "@router.post(\"/desk-interrupt\")" "$ROUTES" && has "@router.get(\"/desks-in-progress\")" "$ROUTES"' \
  "the gateway serves the stop and the turns in flight (sessions_v2_routes.py)"

# ---- brin ---------------------------------------------------------------------
expect 'has "class TurnStoppedError(Exception):" "$EXEC" && has "raise TurnStoppedError(" "$EXEC" && has "\"agent.turn_stopped\"" "$BRIN"' \
  "a stopped turn ends as agent.turn_stopped, not completed or failed (executor.py, main.py)"
expect 'has "\"agent.turn_stop_requested\"" "$BRIN" && has "actor=f\"user:{req.requested_by}\"" "$BRIN"' \
  "the stop is recorded with who asked (main.py)"
working_block="$(awk "/except GatewayWorkingError as e:/,/status_code=202\)/" "$BRIN")"
expect 'grep -qF "_watch_working_turn(" <<< "$working_block" && ! grep -qF "\"release\"" <<< "$working_block"' \
  "a turn past the sync window keeps its agent until it ends (main.py)"
expect 'has "lease_seconds=_desk_lease_seconds()" "$BRIN" && has "return int(turn_ceiling()) + 60" "$BRIN"' \
  "a desk turn holds its agent for the gateway's turn cap, not 300 s (main.py)"

# ---- core-api -----------------------------------------------------------------
sess_block="$(awk "/def desk_sessions/,/return \{\"desk_id\"/" "$DESKS")"
expect 'grep -qF "LIKE %s" <<< "$sess_block"' \
  "a desk's sessions are found under the agent-prefixed key brin writes (desks.py)"

if (( FAILED )); then echo "FAIL ($FAILED)"; exit 1; fi
echo "PASS"
