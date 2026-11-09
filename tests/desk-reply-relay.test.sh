#!/usr/bin/env bash
#
# A question delegated from a Studio desk keeps the desk as its origin all the
# way through the opaque Claude CLI path, so the answer becomes a relay duty in
# the asking agent's inbox.
#
# WHAT WENT WRONG. Brin adds metadata.origin around send-message calls it can
# see, but a Claude CLI calls the messaging MCP inside its own process. That
# bypasses Brin's tool loop completely. The MCP accepted no origin and core-api
# therefore stored none. An answer could sit in the asking agent's inbox for
# hours while the desk was truthfully told that no answer had arrived.
#
# This is deliberately a producer-to-consumer check. Merely asserting that some
# source writes a field let the relay regression stay green for six weeks while the writer used
# `caller` and the consumer required `conversation`.
#
#   tests/desk-reply-relay.test.sh                  # tree, PASS
#   tests/desk-reply-relay.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "desk answer reaches its origin (git:$BASELINE)"
else echo "desk answer reaches its origin (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/gateway/backends/cli_claude.py > "$D/cli_claude.py"
read_src companions/brin/mcp_messaging.py         > "$D/mcp_messaging.py"
read_src companions/core-api/routes/messages.py  > "$D/messages.py"
read_src companions/voice/backend/main.py        > "$D/voice.py"
for f in cli_claude mcp_messaging messages voice; do
    [[ -s "$D/$f.py" ]] || { bad "sources are readable ($f.py)"; echo; echo FAIL; exit 1; }
done

if python3 - "$D/cli_claude.py" "$D/mcp_messaging.py" "$D/messages.py" "$D/voice.py" \
    > "$D/results" <<'PY'
import ast
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional


def functions(path, names, namespace=None):
    tree = ast.parse(open(path).read(), path)
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in names]
    for node in keep:
        node.decorator_list = []
    ns = {"Any": Any, "Dict": Dict, "Optional": Optional}
    ns.update(namespace or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), ns)
    return ns


def check(name, condition):
    print(("ok " if condition else "FAIL ") + name)


gateway = functions(sys.argv[1], {"_message_origin_env"})
origin_env_fn = gateway.get("_message_origin_env")
if origin_env_fn is None:
    check("a desk CLI process inherits its message origin", False)
    origin_env = {}
else:
    origin_env = origin_env_fn({"conversation": "sam:desk:9f1f"})
    check("a desk CLI process inherits its message origin",
          origin_env.get("SN_MESSAGE_ORIGIN_CHANNEL") == "internal"
          and origin_env.get("SN_MESSAGE_ORIGIN_CONVERSATION") == "desk:9f1f")

old_env = os.environ.copy()
os.environ.update(origin_env)
mcp = functions(sys.argv[2], {"_send_payload"}, {"os": os})
payload_fn = mcp.get("_send_payload")
if payload_fn is None:
    check("the opaque messaging MCP stamps a colleague request", False)
    request = {"to": "max", "body": "What did the customer decide?"}
else:
    request = payload_fn("max", "What did the customer decide?")
    check("the opaque messaging MCP stamps a colleague request",
          request.get("origin") == {
              "channel": "internal", "conversation": "desk:9f1f"
          })
    relay = payload_fn("desk:9f1f", "Max answered: approved", "internal")
    check("an explicit relay does not start another relay chain",
          "origin" not in relay and relay.get("channel") == "internal")
os.environ.clear()
os.environ.update(old_env)


class HTTPException(Exception):
    pass


class Log:
    def warning(self, *args, **kwargs):
        pass


answer = {
    "id": 4815,
    "channel_type": "internal",
    "direction": "inbound",
    "sender": "max",
    "recipient": "sam",
    "content": "The customer approved the final version.",
    "status": "new",
    "thread_id": "<desk-question@node>",
    "thread_key": "<desk-question@node>",
    "metadata": {"origin": request.get("origin"), "reply_to_message_id": 4800},
    "received_at": "2026-09-22T00:00:00Z",
}


class FakeCursor:
    def __init__(self):
        self.result = []

    def execute(self, query, params=()):
        self.result = [] if "FROM sn_config.agent_channels" in query else [answer]

    def fetchall(self):
        return self.result


class CursorContext:
    def __enter__(self):
        self.cursor = FakeCursor()
        return self.cursor

    def __exit__(self, *args):
        return False


messages = functions(
    sys.argv[3],
    {"my_inbox", "_lean_row"},
    {
        "Principal": object,
        "Depends": lambda value: None,
        "requires": lambda name, **kw: None,
        "get_principal": None,
        "_INBOX_VIEWS": ("full", "new", "thread"),
        "HTTPException": HTTPException,
        "cursor": lambda: CursorContext(),
        "_refused_hidden": lambda principal, mine: ("TRUE", []),
        "log": Log(),
    },
)
result = messages["my_inbox"](
    limit=20,
    view="new",
    thread_id=None,
    principal=SimpleNamespace(username="sam", cross_tenant=False, tenant_id=1),
)
check("a desk question produces a nonempty to_relay for its answer",
      len(result.get("to_relay") or []) == 1
      and result["to_relay"][0]["origin"].get("conversation") == "desk:9f1f")
check("the pending answer is visibly named as a relay duty",
      "YOU HAVE ANSWERS TO RELAY" in result.get("guidance", ""))
# A colleague's question carries the colleague's origin too, but is not an answer:
# relayed, the agent would send the question itself into that chat as the answer.
answer["metadata"] = {"origin": request.get("origin")}
question = messages["my_inbox"](
    limit=20,
    view="new",
    thread_id=None,
    principal=SimpleNamespace(username="sam", cross_tenant=False, tenant_id=1),
)
check("a colleague's question with an origin is not listed for relay",
      not question.get("to_relay"))

voice = open(sys.argv[4]).read()
check("voice uses the same origin key as its consumer",
      '"origin": {"channel": "voice", "conversation": "voice:%s" % uid,' in voice)
PY
then
    while IFS= read -r line; do
        case "$line" in
            ok*)   ok "${line#ok }";;
            FAIL*) bad "${line#FAIL }";;
        esac
    done < "$D/results"
else
    bad "the producer-to-consumer behavior harness runs"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo PASS; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
