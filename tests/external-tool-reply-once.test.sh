#!/usr/bin/env bash
#
# A fast-channel turn that already replied through messaging has one outbound
# row, not that reply followed by the model's internal closing summary.
# Explicit tool metadata + a committed comm-send row are the proof; prose is
# never classified heuristically.
#
#   tests/external-tool-reply-once.test.sh [--baseline REF]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:?--baseline needs a ref}"

D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT
if [[ -n "$BASELINE" ]]; then
  git -C "$REPO" show "$BASELINE:companions/communication/dispatch.py" > "$D/dispatch.py"
else
  cp "$REPO/companions/communication/dispatch.py" "$D/dispatch.py"
fi

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "external tool reply is delivered once${BASELINE:+ [@ $BASELINE]}"

EXPECTED=9
SEEN=0
while IFS= read -r line; do
  case "$line" in
    ok*)   ok "${line#ok }"; SEEN=$((SEEN + 1));;
    FAIL*) bad "${line#FAIL }"; SEEN=$((SEEN + 1));;
  esac
done < <(python3 - "$D/dispatch.py" <<'PY'
import importlib.util
import sys
import types

path = sys.argv[1]
state = {"confirmed": False, "row": "comm-send", "states": [], "events": []}

class FakeDB:
    def query(self, sql, args=None, fetch=False):
        compact = " ".join(sql.split())
        if "COALESCE(MAX(id), 0)" in compact:
            return [(100,)]
        if "metadata->>'source' = 'comm-send'" in compact:
            if "reply_to_message_id" in compact:
                return [(101,)] if state["confirmed"] else []
            return [(101,)] if state["confirmed"] and state["row"] == "comm-send" else []
        return [] if fetch else None
    def emit_turn(self, *args):
        state["events"].append(args)

common = types.ModuleType("common")
common.contacts = types.SimpleNamespace(
    may_wake=lambda *a: (True, "allowed"),
    inbound_enabled=lambda: True,
    enabled=lambda: True,
)
common.db = FakeDB()
common.mailrules = types.SimpleNamespace(is_auto_generated=lambda *a: False)
config = types.ModuleType("common.config")
config.LOG = lambda *a, **k: None
config.get = lambda _key, default="": default
config.require = lambda _key: "test-key"
sys.modules["common"] = common
sys.modules["common.config"] = config

spec = importlib.util.spec_from_file_location("dispatch_under_test", path)
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)

dispatch._resolve_mailbox = lambda recipient, channel_id, channel=None: ("john_bot", "foreman")
dispatch._claim_agent = lambda *a: True
dispatch._release_agent = lambda *a: None
dispatch._set_state = lambda mid, status, error=None: state["states"].append(status)
deliveries = []
dispatch._DELIVERERS["telegram"] = (
    lambda reply, *a, **k: deliveries.append(reply)
)

def run(answer, confirmed):
    # The transport now distinguishes the terminal answer from desk content.
    answer = dict(answer, final_answer=answer["content"])
    state["confirmed"] = confirmed
    state["states"].clear()
    deliveries.clear()
    dispatch._ask_brin = lambda *a, **k: answer
    dispatch._handle(
        7, "telegram", "1646539396", "john_bot", "question",
        {"chat_id": 1646539396}, "1646539396", "1646539396:1", "john_bot",
    )
    return list(deliveries), list(state["states"])

def check(good, text):
    print(("ok " if good else "FAIL ") + text)

tool_answer = {
    "content": "Answered on telegram: confirmed the operation.",
    "actions": [{
        "name": "mcp__messaging__send_message",
        "input": {"to": "1646539396", "channel": "telegram",
                  "body": "The actual answer"},
    }],
}
out, statuses = run(tool_answer, confirmed=True)
check(out == [] and statuses[-1:] == ["processed"],
      "a confirmed explicit Telegram tool reply suppresses the closing summary")

out, _ = run({"content": "The answer", "actions": []}, confirmed=False)
check(out == ["The answer"],
      "an ordinary final answer is still queued by fast-dispatch")

colleague = {
    "content": "I asked the analyst and will follow up.",
    "actions": [{
        "name": "mcp__messaging__send_message",
        "input": {"to": "analyst", "body": "Please check this"},
    }],
}
out, _ = run(colleague, confirmed=True)
check(out == [colleague["content"]],
      "an internal colleague send does not suppress the person's reply")

out, _ = run(tool_answer, confirmed=False)
check(out == [tool_answer["content"]],
      "a tool invocation without a committed outbound row does not swallow the reply")

reply_answer = {
    "content": "Replied by email, see the thread.",
    "actions": [{
        "name": "mcp__messaging__inbox_reply",
        "input": {"to_message_id": 7, "body": "The actual answer"},
    }],
}
state["row"] = "reply"
out, statuses = run(reply_answer, confirmed=True)
check(out == [] and statuses[-1:] == ["processed"],
      "a committed inbox_reply row suppresses the closing summary")

out, _ = run(reply_answer, confirmed=False)
check(out == [reply_answer["content"]],
      "an inbox_reply without a committed outbound row does not swallow the reply")

http_reply = dict(reply_answer, actions=[{
    "name": "messages_reply",
    "input": {"to_message_id": 7, "body": "The actual answer"},
}])
out, _ = run(http_reply, confirmed=True)
check(out == [], "the HTTP tool name messages_reply is recognised the same way")
state["row"] = "comm-send"

source = open(path).read()
check("metadata->>'source' = 'comm-send'" in source,
      "confirmation is a committed comm-send row, not a prose heuristic")
check("Answered on telegram" not in source,
      "production code does not classify closing-summary wording")
PY
)

(( SEEN == EXPECTED )) || bad "expected $EXPECTED checks, ran $SEEN (the python block died)"
if (( FAILED )); then echo "FAIL ($FAILED)"; exit 1; fi
echo "PASS"
