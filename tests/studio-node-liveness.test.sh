#!/usr/bin/env bash
#
# A dead node is not a working agent.
#
# The browser has two independent facts: a turn may be slow while the liveness
# poll still succeeds, or the node may have stopped answering altogether.  Stop
# is also a browser action first: it ends the visible wait even when the request
# asking the server to interrupt cannot get there.
#
#   tests/studio-node-liveness.test.sh [--baseline REF]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:?--baseline needs a ref}"

read_src() {
  if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
  else cat "$REPO/$1" 2>/dev/null; fi
}

D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js > "$D/app.js"
read_src companions/studio/public/ui/components.css > "$D/components.css"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
expect() { if eval "$1"; then ok "$2"; else bad "$2"; fi; }

echo "Studio: a dead node is not a working agent${BASELINE:+ [@ $BASELINE]}"

APP="$D/app.js"
CSS="$D/components.css"
paint_block="$(awk '/^function paintTurn\(turn\) \{/,/^}/' "$APP")"
turn_bubble_block="$(awk '/^function turnBubble\(turn, arrive\) \{/,/^}/' "$APP")"
await_block="$(awk '/^async function awaitReply\(deskId, turn\) \{/,/^}/' "$APP")"
run_block="$(awk '/^async function runTurn\(deskId, turn, text, ag\) \{/,/^}/' "$APP")"
stop_block="$(awk '/^async function stopTurn\(deskId\) \{/,/^}/' "$APP")"
work_block="$(awk '/^async function loadDeskWork\(\) \{/,/^}/' "$APP")"

expect 'grep -qF "const NODE_UNAVAILABLE_MS = 5000;" "$APP" && grep -qF "turn.sawNodeUnavailable = true" <<< "$work_block"' \
  "failed liveness polls become a node-unavailable state within five seconds"
expect 'grep -qF "turn.nodeUnavailableSince" <<< "$paint_block" && grep -qF "node-unavailable" "$APP" && grep -qF "No sign of life since" "$APP"' \
  "the pending reply says the node is not answering, then names the last sign of life"
expect 'grep -qF "_nodeUnavailableSince ? false" "$APP" && grep -qF ".sn-think__label.is-offline" "$CSS"' \
  "an unavailable node is neither marked working nor painted as live activity"
expect 'grep -qF "turn.sawNodeUnavailable" <<< "$await_block" && grep -qF "lost: true" <<< "$await_block" && grep -qF "lost when the node restarted" "$APP"' \
  "the first successful check after recovery settles an orphaned turn as lost"
expect '[[ "$(grep -cF "postChat(deskId, text, ag, turn)" <<< "$run_block")" -eq 1 ]]' \
  "a failed request is never posted a second time blindly"

state_line="$(grep -nF "running.state = 'waiting-stopped';" <<< "$stop_block" | cut -d: -f1)"
fetch_line="$(grep -nF 'await fetch(' <<< "$stop_block" | cut -d: -f1)"
expect '[[ -n "$state_line" && -n "$fetch_line" && "$state_line" -lt "$fetch_line" ]] && grep -qF "Stopped waiting; if the turn is still running, its answer will appear here." "$APP" && ! grep -qF "back()" <<< "$stop_block"' \
  "Stop ends the browser wait before attempting the server and never resumes it on failure"
expect 'grep -qF "turn.state === '"'"'waiting-stopped'"'"'" <<< "$turn_bubble_block" && grep -qF "'"'"'waiting-stopped'"'"'].includes(turn.state)" <<< "$work_block"' \
  "the local Stop notice survives a tab switch and still participates in recovery"

if ! command -v node >/dev/null 2>&1; then
  bad "node is available for the liveness state-machine checks"
elif ! grep -qF "turn.sawNodeUnavailable" "$APP"; then
  bad "the liveness state-machine checks can run"
else
  while IFS= read -r line; do
    case "$line" in
      ok*)   ok "${line#ok }";;
      FAIL*) bad "${line#FAIL }";;
    esac
  done < <(node - "$APP" <<'JS'
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const at = src.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
  if (at < 0) return null;
  let i = src.indexOf('{', at), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(at, i + 1);
  }
  return null;
}
const say = (good, name) => console.log((good ? 'ok ' : 'FAIL ') + name);

(async () => {
  let now = 1000;
  let reply = { desks: null };
  const fakeDate = { now: () => now };
  const turn = { state: 'running', paints: 0 };
  const loadDeskWork = grab('loadDeskWork');
  const deskIsWorking = grab('deskIsWorking');
  const makeLiveness = new Function('fetch', 'Date', 'turn', `
    var _deskWork = { d: { desk_id: 'd' } };
    const activeId = 'd';
    var _deskWorkKnown = null;
    const NODE_UNAVAILABLE_MS = 5000;
    const NO_SIGN_OF_LIFE_MS = 60000;
    var _deskWorkLastOkAt = Date.now();
    var _deskWorkFailedSince = null;
    var _nodeUnavailableSince = null;
    var _turns = { d: [turn] };
    const deskTurns = (id) => _turns[id] || [];
    const hasTurns = (id) => deskTurns(id).length > 0;
    function paintTurn(t) { t.paints++; }
    function paintDeskWork() {}
    function paintQueuedHistoryControls() {}
    function syncDetachedTurn() {}  // the detached-turn bubble; its own behaviour is not what this test is about
    function nextChatWord() { return Promise.resolve(); }  // waits for the next word of a chat; not what this test is about
    function syncDeskLive() {}  // the live-socket subscription; not what this test is about
    ${loadDeskWork}
    ${deskIsWorking}
    return {
      loadDeskWork, deskIsWorking,
      state: () => ({ known: _deskWorkKnown, down: _nodeUnavailableSince })
    };
  `);
  const live = makeLiveness(
    async () => ({ ok: true, json: async () => reply }),
    fakeDate,
    turn,
  );
  await live.loadDeskWork();
  now = 4000; await live.loadDeskWork();
  say(!live.state().down && turn.paints === 0,
    'one transient failed poll does not turn a slow agent into a dead node');
  now = 6100; await live.loadDeskWork();
  say(!!live.state().down && turn.sawNodeUnavailable === true && live.deskIsWorking('d') === false,
    'five seconds of failed polls mark the node unavailable and clear working');
  reply = { desks: [] };
  now = 7000; await live.loadDeskWork();
  say(live.state().known === true && !live.state().down && turn.nodeUnavailableSince === null,
    'a successful liveness poll clears the unavailable state');

  const awaitReply = grab('awaitReply');
  let historyReads = 0;
  const makeAwait = new Function('turn', `
    const _sleep = async () => {};
    const nextChatWord = async () => {};  // the page waits on the event stream, not a timer
    const fetchHistory = async () => { historyReads++; return []; };
    const replyFor = () => null;
    const _deskWork = {};
    const _deskWorkKnown = true;
    let historyReads = 0;
    ${awaitReply}
    return { run: () => awaitReply('d', turn), reads: () => historyReads };
  `);
  const recovered = makeAwait({ state: 'running', sawNodeUnavailable: true });
  const outcome = await recovered.run();
  say(outcome && outcome.lost === true && recovered.reads() === 1,
    'recovery checks stored history once before declaring the turn lost');

  const stopTurn = grab('stopTurn');
  const order = [];
  const stoppedTurn = {
    state: 'running',
    think: { stop: () => order.push('think') },
    el: { isConnected: true },
  };
  const makeStop = new Function(
    'deskTurns', 'paintChatControls', 'renderOutcome', 'fetch', 'deskAgent', 'desks',
    stopTurn + '; return stopTurn;',
  );
  const stop = makeStop(
    () => [stoppedTurn],
    () => order.push('controls'),
    (_el, result) => order.push('render:' + result.kind),
    async () => { order.push('fetch'); throw new Error('node down'); },
    () => 'agent',
    [],
  );
  await stop('d');
  say(stoppedTurn.state === 'waiting-stopped'
      && order.indexOf('render:waiting-stopped') < order.indexOf('fetch'),
    'Stop paints the local result before a failing server request');
})().catch((e) => {
  console.log('FAIL liveness state-machine harness: ' + e.stack);
});
JS
  )
fi

if (( FAILED )); then echo "FAIL ($FAILED)"; exit 1; fi
echo "PASS"
