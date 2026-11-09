#!/usr/bin/env bash
#
# The panel's ai.json renders when the recent sessions are larger than one
# command-line argument may be.
#
# WHAT WENT WRONG. sn-panel-data.sh passed the last 20 sessions (up to 30
# messages each, 800 chars per message) to jq as one --argjson. Linux caps a
# single argument at 128 KB, so on a node with enough conversation jq never
# started: "Argument list too long", exit 126, sn-panel-data.service failed on
# every timer run (example-node from 2026-09-15) and install validate failed on it.
#
# WHAT IS CHECKED. The real ai.json render block of the script (from "# Cache
# hit rate" to the ai.json write), cut out as written and run with a SESSIONS
# payload of about 300 KB and small values for the rest:
#   render    the block exits 0 and writes ai.json
#   content   ai.json carries every session, message text intact
#
# USAGE
#   tests/panel-data-argmax.test.sh                  # working tree, expect PASS
#   tests/panel-data-argmax.test.sh --baseline REF   # git:REF, expect FAIL
#
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SCRIPT=bootstrap/smart-node-ui/host-tools/sn-panel-data.sh
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
if [[ "${1:-}" == "--baseline" ]]; then
    git -C "$ROOT" show "${2}:$SCRIPT" > "$WORK/src.sh"
else
    cp "$ROOT/$SCRIPT" "$WORK/src.sh"
fi
FAIL=0
ok()  { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; FAIL=$((FAIL+1)); }

awk '/^# Cache hit rate/{on=1} on{print} on && /write_atomic "\$OUTDIR\/ai.json"/{exit}' "$WORK/src.sh" > "$WORK/block.sh"
[[ -s "$WORK/block.sh" ]] || { bad "render: the ai.json block was not found in the script"; echo "FAILED: $FAIL"; exit 1; }

# The payload reaches the block the way psql output does in the script: as a
# shell variable, read from a file inside the run (an environment variable has
# the same 128 KB cap and would fail before the block even starts).
python3 -c '
import json
msg = "x" * 800
print(json.dumps([{"session_id": "s%d" % i, "messages": [{"seq": j, "role": "user", "content": msg} for j in range(30)]} for i in range(12)]))' > "$WORK/sessions.json"
echo "  payload: SESSIONS is $(wc -c < "$WORK/sessions.json") bytes"

mkdir -p "$WORK/out"
( cd "$WORK" && OUTDIR="$WORK/out" GEN=2026-09-18T00:00:00Z OVERALL=ok ACTIVE_SESS=0 P_TOTAL=0 P_OK=0 \
    CACHE='{}' PROVS_MERGED='[]' BUDGETS='[]' bash -c '
    write_atomic() { mv "${1}.tmp" "${1}"; chmod 644 "${1}"; }
    SESSIONS="$(cat sessions.json)"
    set -e
    . ./block.sh' ) > "$WORK/run.log" 2>&1
rc=$?
if [[ $rc == 0 && -s "$WORK/out/ai.json" ]]; then
    ok "render: ai.json written, $(wc -c < "$WORK/out/ai.json") bytes"
else
    bad "render: exit $rc: $(tail -1 "$WORK/run.log")"
fi
if [[ -s "$WORK/out/ai.json" ]] && jq -e '(.sessions | length) == 12 and (.sessions[11].messages[29].content | length) == 800' "$WORK/out/ai.json" >/dev/null; then
    ok "content: all 12 sessions and their message text are in ai.json"
else
    bad "content: ai.json does not carry the sessions"
fi
[[ $FAIL == 0 ]] && echo "ALL PASS" || { echo "FAILED: $FAIL"; exit 1; }
