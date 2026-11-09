#!/usr/bin/env bash
#
# A background turn woken by a colleague's message ends in an answer to that
# colleague.
#
# WHAT WENT WRONG (example-node, 2026-09-25). HTTP-model agent, the one agent on an HTTP model,
# read web-agent's question and answered it in plain text; later she spent
# two whole turns hunting for task comments she had no tool to
# read and hit the round cap. Every time the text went into a background thread
# nobody reads, the messages stayed new, and the asker reported her silent.
#
# This drives the real Executor._maybe_run_tool_loop with a scripted model and a
# fake tool catalogue, so it checks what the loop DOES, not what the source says.
#
#   tests/background-turn-answers.test.sh                  # tree, PASS
#   tests/background-turn-answers.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
PY="${SN_BRIN_PY:-/opt/smart-node/dispatcher/venv/bin/python}"
[[ -x "$PY" ]] || { echo "SKIP: no brin python at $PY (set SN_BRIN_PY)"; exit 0; }

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
cp -r "$REPO/companions/brin" "$D/brin"
if [[ -n "$BASELINE" ]]; then
    echo "background turn answers its asker (git:$BASELINE)"
    git -C "$REPO" show "$BASELINE:companions/brin/core/executor.py" > "$D/brin/core/executor.py" || exit 1
else
    echo "background turn answers its asker (working tree)"
fi

cd "$D/brin" && "$PY" - <<'PY'
import asyncio, json, sys
sys.path.insert(0, ".")
import core.executor as ex

FAILED = []
def ok(m): print("  ok   " + m)
def bad(m): print("  FAIL " + m); FAILED.append(m)

SPECS = [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
         for n in ("messages_inbox", "messages_reply", "messages_send", "messages_dismiss",
                   "mesh__mesh_search", "planner_tasks")]

class FakeCT:
    def __init__(self, inbox_rows):
        self.rows = inbox_rows; self.calls = []
    def openai_specs(self): return SPECS
    def deferred_names(self): return []
    async def dispatch(self, name, params):
        self.calls.append((name, params))
        if name == "messages_inbox":
            return "[HTTP 200] " + json.dumps({"conversation": self.rows})
        if name == "messages_reply":
            for r in self.rows:
                if r["id"] == params.get("to_message_id"): r["status"] = "processed"
            return '[HTTP 200] {"ok": true, "id": 9999}'
        return "[HTTP 200] []"

def row(mid, sender="web-agent"):
    return {"id": mid, "channel_type": "internal", "direction": "inbound",
            "sender": sender, "recipient": "http-agent", "status": "new", "content": "q"}

class Model:
    """Scripted model: a list of replies, each either text or [tool names]."""
    def __init__(self, script):
        self.script = list(script); self.seen = []
    async def __call__(self, model, system, history, specs, **kw):
        self.seen.append(([s["function"]["name"] for s in specs], [h.get("content") for h in history]))
        step = self.script.pop(0) if self.script else self.last
        self.last = step
        if isinstance(step, str):
            return {"content": step}
        return {"content": "", "tool_calls": [
            {"id": "c%d" % i, "type": "function",
             "function": {"name": n, "arguments": json.dumps(
                 {"to_message_id": step_mid[0], "body": "my answer"} if n == "messages_reply" else {})}}
            for i, n in enumerate(step)]}

step_mid = [0]

async def run(conv, channel, rows, script):
    ct = FakeCT(rows); m = Model(script)
    self = object.__new__(ex.Executor)
    async def _rt(slug): return ("gemini/gemini-flash-latest", "http")
    async def _none(*a, **k): return None
    async def _empty(*a, **k): return []
    async def _prompt(*a, **k): return "system"
    self._agent_runtime = _rt; self._assembled_prompt = _prompt
    self._load_persisted_history = _empty; self._persist_tool_truth = _none
    self._persist_answer = _none; self._gateway_chat_stateless = m
    self.memory_service = None
    class TC:
        def get_memory(self):
            class M: enabled = False; mode = "off"
            return M()
    self.tools_config = TC()
    async def _load(slug): return ct
    ex.CatalogTools.load = staticmethod(_load)
    async def _ledger(*a, **k): return ""
    ex.build_ledger_sections = _ledger
    ex.CORE_API_URL = "http://127.0.0.1:9"  # event post fails fast; the loop must not care
    ans = await self_loop(self, conv, channel)
    return ans, ct, m

async def self_loop(self, conv, channel):
    return await ex.Executor._maybe_run_tool_loop(self, "http-agent", "wake", conversation_id=conv, channel=channel)

def replies(ct): return [p for n, p in ct.calls if n == "messages_reply"]

async def main():
    N = ex.MAX_TOOL_ROUNDS
    # 1. answers in text, twice. The asker must still get it.
    step_mid[0] = 7369
    ans, ct, m = await run("runner-http-agent-7369-51", "system", [row(7369)],
                           ["Here is my opinion on the SERP plan.", "Here is my opinion on the SERP plan."])
    r = replies(ct)
    (ok if r and r[0]["to_message_id"] == 7369 else bad)("1. text answer on a runner turn reaches the asker (%d reply)" % len(r))
    (ok if r and "SERP plan" in r[0]["body"] else bad)("1. the reply carries the model's own answer")

    # 2. text answer, then the model replies itself after the check: no double reply.
    ans, ct, m = await run("runner-http-agent-7369-52", "system", [row(7369)],
                           ["Here is my opinion.", ["messages_reply"], "done"])
    (ok if len(replies(ct)) == 1 else bad)("2. model replies after the check: exactly one reply (%d)" % len(replies(ct)))

    # 3. searches until the cap. Budget shown, last round answer-only, asker answered.
    step_mid[0] = 7561
    ans, ct, m = await run("runner-http-agent-7561-119", "system", [row(7561, "integration-agent")],
                           [["mesh__mesh_search"]] * (N + 1))
    notes = [c for _s, hist in m.seen for c in hist if isinstance(c, str) and "tool rounds left" in c]
    (ok if notes else bad)("3. the model is told its remaining tool rounds")
    last_specs = m.seen[-1][0] if m.seen else []
    (ok if last_specs and set(last_specs) <= set(ex.__dict__.get("_REPLY_TOOLS", ())) else bad)(
        "3. the last round offers only answering tools (%s)" % ",".join(last_specs))
    r = replies(ct)
    (ok if r and r[0]["to_message_id"] == 7561 else bad)("3. the original asker gets a reply (%d)" % len(r))

    # 4. last round the model does reply itself: that reply, no automatic note on top.
    ans, ct, m = await run("runner-http-agent-7561-120", "system", [row(7561, "integration-agent")],
                           [["mesh__mesh_search"]] * N + [["messages_reply"]])
    r = replies(ct)
    (ok if len(r) == 1 and r[0]["body"] == "my answer" else bad)("4. a reply in the last round is the only one (%d)" % len(r))

    # 5. a person waits (web-chat): text is the answer, no guard, no reply call.
    ans, ct, m = await run("desk-abc", "web-chat", [row(7000)], ["Hello, owner."])
    (ok if ans == "Hello, owner." and not replies(ct) else bad)("5. person-facing turn untouched")

    # 6. runner turn with nothing waiting (woken for a task): text ends it, no reply.
    ans, ct, m = await run("runner-http-agent-7000-9", "system", [], ["Nothing to do."])
    (ok if not replies(ct) and len(m.seen) == 1 else bad)("6. nothing waiting: no extra round, no reply")

    # 7. a message newer than the wake is not this turn's business.
    ans, ct, m = await run("runner-http-agent-7000-10", "system", [row(7001)], ["Nothing to do."])
    (ok if not replies(ct) else bad)("7. a message newer than the wake is left alone")

asyncio.run(main())
print()
print("FAIL" if FAILED else "PASS")
sys.exit(1 if FAILED else 0)
PY
