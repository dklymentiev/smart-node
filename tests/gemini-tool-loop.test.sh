#!/usr/bin/env bash
#
# An agent on Gemini can use its tools in brin's loop.
#
# WHAT WENT WRONG. brin's tool loop sends OpenAI function specs to the gateway's
# stateless chat, which hands them to the provider of the model's runtime. The
# Gemini provider accepted `tools` and dropped them ("function calling is not
# wired"), so an agent on gemini/... could neither read the letter that woke it
# nor answer it. Separately, the builtins an HTTP model is offered had no
# parameter schema ({}), and fetch was offered but not wired: on the earlier change stand
# run an OpenRouter model called bash with "code" and fetch got "[UNSUPPORTED]"
# for nine rounds.
#
# WHAT IS CHECKED. The real code, with only its imports stubbed (the host has
# no pydantic/httpx; nothing touches the network):
#   gemini provider (companions/gateway/providers/gemini.py)
#     decl        OpenAI tool specs become functionDeclarations with the JSON
#                 schema as written, and toolConfig AUTO
#     call-out    a functionCall part comes back as an OpenAI tool_call, its
#                 thoughtSignature kept, thought text left out of the answer
#     round-trip  the next request carries the call as a functionCall part with
#                 its signature, and the tool result as a functionResponse named
#                 after the call (OpenAI results carry only the id)
#   brin catalog (companions/brin/core/catalog_tools.py)
#     offered     bash and fetch are offered with their schemas; fs (not wired)
#                 is not offered and is reported deferred
#     fetch-guard fetch refuses loopback, RFC1918 and link-local targets and a
#                 non-http scheme before any request is made
#     get-query   a GET api tool (messages_inbox) sends its arguments as query
#                 parameters, not as an ignored JSON body
#   chat log (companions/gateway/chat_logger.py + brin executor)
#     once        a tool turn of three rounds stores the person's message once; each
#                 round's assistant row (its cost) is still stored
#     origin      messages_send from a turn a person waits on carries {channel,
#                 conversation}, so the colleague's answer is relayed back; a
#                 runner (system) or internal turn stamps none
#     context     provider usage (Gemini, OpenAI-shaped) is normalised and each
#                 round lands on the session ledger the context meter reads
#   seed (companions/core-api/seed-tools.py)
#     schemas     bash requires `command`, fetch requires `url`, and the upsert
#                 rewrites input_schema on an existing node
#     grant-pairing  PUT grants adds those rows whenever messaging is granted
#     messaging-api  inbox/reply/send/dismiss exist as api tools, granted to the
#                 agents that hold `messaging` (brin cannot run its stdio server)
#
# USAGE
#   tests/gemini-tool-loop.test.sh                  # working tree, expect PASS
#   tests/gemini-tool-loop.test.sh --baseline REF   # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
src() {  # src <path> <dest>: working tree or git:BASELINE; missing = empty
    mkdir -p "$(dirname "$2")"
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:$1" > "$2" 2>/dev/null || : > "$2"
    elif [[ -f "$ROOT/$1" ]]; then cp "$ROOT/$1" "$2"; else : > "$2"; fi
}
src companions/gateway/providers/gemini.py "$WORK/gw/providers/gemini.py"
src companions/brin/core/catalog_tools.py  "$WORK/brin/core/catalog_tools.py"
src companions/core-api/seed-tools.py      "$WORK/seed/seed_tools.py"
src companions/core-api/routes/agents.py  "$WORK/seed/agents_route.py"
src companions/core-api/lib/default_grants.py "$WORK/seed/lib/default_grants.py"
src companions/gateway/chat_logger.py     "$WORK/gw/chat_logger.py"
src companions/brin/core/executor.py      "$WORK/brin/core/executor.py"

echo "gemini tool loop (${BASELINE:+git:}${BASELINE:-working tree})"
cd "$WORK" && python3 - <<'PY'
import asyncio, importlib.util, json, sys, types

failed = 0
def ok(m): print("  ok   " + m)
def bad(m):
    global failed; failed += 1; print("  FAIL " + m)

def stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ------------------------------------------------------------- gemini provider
print(" gemini provider")
class Message:
    def __init__(self, role, content=None, tool_calls=None, tool_call_id=None, name=None):
        self.role, self.content, self.tool_calls = role, content, tool_calls
        self.tool_call_id, self.name = tool_call_id, name
class BaseProvider:
    async def get_client(self): return self._client
    # The real one turns a caller's limit into an httpx timeout; the stub client
    # takes whatever it is given, so passing the number through is enough.
    @staticmethod
    def call_timeout(seconds): return seconds
stub("models", Message=Message)
stub("providers"); stub("providers.base", BaseProvider=BaseProvider)
stub("config", GEMINI_API_KEY=None, GEMINI_DEFAULT_MODEL="gemini-flash-latest")
stub("secrets_store", get_provider_key=lambda p: "test-key")
stub("sse_parser", SSEParser=object)

class Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d
class Client:
    def __init__(self, replies): self.replies, self.sent = list(replies), []
    async def post(self, url, headers=None, json=None, **kw):
        self.sent.append(json); return Resp(self.replies.pop(0))

TOOLS = [{"type": "function", "function": {
    "name": "messaging__send_message", "description": "Send a message",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                   "required": ["to", "text"]}}}]
REPLY_CALL = {"candidates": [{"content": {"role": "model", "parts": [
    {"text": "planning the reply", "thought": True},
    {"functionCall": {"name": "messaging__send_message", "args": {"to": "agent-b", "text": "hi"}},
     "thoughtSignature": "SIG-1"}]}}], "usageMetadata": {"promptTokenCount": 10}}
REPLY_TEXT = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Sent."}]}}]}

try:
    g = load("gemini_under_test", "gw/providers/gemini.py")
    p = g.GeminiProvider(api_key="k")
    p._client = Client([REPLY_CALL, REPLY_TEXT])
    msgs = [Message("user", "Answer Agent B.")]
    r1 = asyncio.run(p.chat(msgs, model="gemini-flash-latest", tools=TOOLS,
                            tool_choice="auto", system="You are HTTP-model agent."))
    body1 = p._client.sent[0]
    decls = ((body1.get("tools") or [{}])[0]).get("functionDeclarations") or []
    if (decls and decls[0].get("name") == "messaging__send_message"
            and decls[0].get("parametersJsonSchema") == TOOLS[0]["function"]["parameters"]
            and (body1.get("toolConfig") or {}).get("functionCallingConfig", {}).get("mode") == "AUTO"):
        ok("decl: tool specs sent as functionDeclarations (schema as written), mode AUTO")
    else:
        bad("decl: request had no usable functionDeclarations: %s" % json.dumps(body1)[:200])

    tcs = r1.get("tool_calls") or []
    if (len(tcs) == 1 and tcs[0]["function"]["name"] == "messaging__send_message"
            and json.loads(tcs[0]["function"]["arguments"]) == {"to": "agent-b", "text": "hi"}
            and tcs[0].get("extra_content", {}).get("google", {}).get("thought_signature") == "SIG-1"
            and "planning" not in (r1.get("content") or "")):
        ok("call-out: functionCall -> OpenAI tool_call, signature kept, thought text dropped")
    else:
        bad("call-out: got %s" % json.dumps(r1)[:240])

    msgs2 = msgs + [Message("assistant", "", tool_calls=tcs),
                    Message("tool", "delivered", tool_call_id=tcs[0]["id"] if tcs else "x")]
    r2 = asyncio.run(p.chat(msgs2, model="gemini-flash-latest", tools=TOOLS))
    c = p._client.sent[1]["contents"]
    call_part = c[1]["parts"][-1] if len(c) > 1 else {}
    resp_part = c[2]["parts"][0] if len(c) > 2 else {}
    if (call_part.get("functionCall", {}).get("name") == "messaging__send_message"
            and call_part.get("thoughtSignature") == "SIG-1"
            and resp_part.get("functionResponse", {}).get("name") == "messaging__send_message"
            and r2.get("content") == "Sent."):
        ok("round-trip: call + signature and a named functionResponse go back to Gemini")
    else:
        bad("round-trip: contents were %s" % json.dumps(c)[:300])
except Exception as e:
    bad("gemini provider could not run: %s: %s" % (type(e).__name__, e))
    bad("call-out: not reached"); bad("round-trip: not reached")

# ------------------------------------------------------------- brin catalog
print(" brin catalog")
for k in [k for k in sys.modules if k in ("models", "config")]:
    del sys.modules[k]
stub("httpx", AsyncClient=object)
stub("core"); stub("core.mcp_client")
stub("core.service_proof", service_headers=lambda: {"X-SN-Service": "brin"})
try:
    ct_mod = load("catalog_under_test", "brin/core/catalog_tools.py")
    rows = [
        {"name": "bash", "kind": "builtin", "description": "Run", "input_schema":
            {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "fetch", "kind": "builtin", "description": "Fetch", "input_schema":
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
        {"name": "fs", "kind": "builtin", "description": "Files", "input_schema": {}},
    ]
    ct = ct_mod.CatalogTools("http-agent", rows, {})
    names = [s["function"]["name"] for s in ct.openai_specs()]
    if names == ["bash", "fetch"] and "fs" in ct.deferred_names():
        ok("offered: bash + fetch offered, fs (not wired) withheld and reported deferred")
    else:
        bad("offered: specs %s, deferred %s" % (names, ct.deferred_names()))
    outs = [asyncio.run(ct.dispatch("fetch", {"url": u})) for u in
            ("http://127.0.0.1:8030/api/agents", "http://10.0.100.1/", "http://169.254.169.254/",
             "file:///etc/passwd")]
    if all(o.startswith("[BLOCKED]") or o.startswith("[ERROR] fetch requires") for o in outs) \
            and outs[0].startswith("[BLOCKED]") and outs[3].startswith("[ERROR] fetch requires"):
        ok("fetch-guard: loopback, private, link-local and file:// refused before any request")
    else:
        bad("fetch-guard: %s" % [o[:60] for o in outs])
    # A GET api tool must carry its arguments in the query string.
    seen = {}
    class FakeResp:
        status_code, text = 200, "{}"
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, **kw):
            seen.update(method=method, url=url, **kw); return FakeResp()
    ct_mod.httpx.AsyncClient = FakeClient
    inbox = {"name": "messages_inbox", "kind": "api", "endpoint": "http://core",
             "http_method": "GET", "http_path": "/api/messages/inbox",
             "integration_slug": "core-api", "input_schema": {"type": "object"}}
    ct2 = ct_mod.CatalogTools("http-agent", [inbox], {})
    asyncio.run(ct2.dispatch("messages_inbox", {"view": "new"}))
    if seen.get("params") == {"view": "new"} and not seen.get("json"):
        ok("get-query: a GET api tool sends its arguments as query parameters")
    else:
        bad("get-query: GET sent params=%s json=%s" % (seen.get("params"), seen.get("json")))
except Exception as e:
    bad("brin catalog could not run: %s: %s" % (type(e).__name__, e)); bad("fetch-guard: not reached")
    bad("get-query: not reached")

# ------------------------------------------------------------- seed
print(" seed")
stub("psycopg2", connect=None)
sys.path.insert(0, "seed")  # seed-tools runs beside its lib/ on a node
try:
    seed = load("seed_under_test", "seed/seed_tools.py")
    s = getattr(seed, "SYSTEM_TOOL_SCHEMAS", {})
    src_txt = open("seed/seed_tools.py").read()
    if (s.get("bash", {}).get("required") == ["command"] and s.get("fetch", {}).get("required") == ["url"]
            and "input_schema     = CASE" in src_txt):
        ok("schemas: bash requires command, fetch requires url, upsert rewrites input_schema")
    else:
        bad("schemas: bash/fetch schemas missing or not rewritten on an existing node")
    want = {"messages_inbox", "messages_reply", "messages_send", "messages_dismiss"}
    if all("'%s'" % n in src_txt or '"%s"' % n in src_txt for n in want) \
            and "m.name = 'messaging'" in src_txt:
        ok("messaging-api: inbox/reply/send/dismiss seeded as api tools for messaging holders")
    else:
        bad("messaging-api: no api twins of the messaging tools for HTTP agents")
    # The same pairing at grant time, or an agent given messaging in the panel
    # would wait for the next core-api restart to be able to answer.
    import re as _re
    route = open("seed/agents_route.py").read()
    # _PAIRED["messaging"] is a list of (row, mode) pairs now; the
    # older ([rows], mode) shape is accepted too, for --baseline runs.
    m = _re.search(r'"messaging":\s*\(?\[(.*?)\]', route, _re.S)
    if m and all(n in m.group(1) for n in want):
        ok("grant-pairing: PUT grants adds the messaging api rows with the messaging grant")
    else:
        bad("grant-pairing: PUT /api/agents/{slug}/grants does not pair messaging with its api rows")
except Exception as e:
    bad("seed could not load: %s: %s" % (type(e).__name__, e))

# ------------------------------------------------------------- one message per turn
print(" chat log")
try:
    rows = []
    class Cur:
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "INSERT INTO sn_core.ai_session_messages" in s:
                rows.append("user" if "'user'" in s else "assistant")
            self._one = (0,)
        def fetchone(self): return getattr(self, "_one", (0,))
    class Conn:
        def cursor(self): return Cur()
    class Tx:
        def __enter__(self): return Conn()
        def __exit__(self, *a): return False
    stub("db", with_transaction=lambda: Tx(), query=lambda *a, **k: [])
    stub("psycopg2"); stub("psycopg2.extras", Json=lambda x: x)
    cl = load("chat_logger_under_test", "gw/chat_logger.py")
    cl._resolve_agent_id = lambda conn, slug: 1
    hist = [{"role": "user", "content": "ask foreman"}]
    kw = dict(client_session_id="desk:x", agent_slug="http-agent", user="u", service="brin-tools",
              provider="gemini", model="gemini-flash-latest", messages=hist,
              assistant_content="", usage={}, raw_user_content="ask foreman")
    cl.log_chat_turn(**kw)                                   # round 0
    for _ in range(2):                                        # two tool rounds
        try:
            cl.log_chat_turn(**dict(kw, log_user=False))
        except TypeError:
            cl.log_chat_turn(**kw)                            # baseline: no such flag
    if rows == ["user", "assistant", "assistant", "assistant"]:
        ok("once: a three-round tool turn stores the person's message once, each round's reply row kept")
    else:
        bad("once: rows stored for a three-round turn: %s" % rows)
    ex = open("brin/core/executor.py").read()
    if "continues_turn=round_num > 0" in ex:
        ok("once: brin marks every round after the first as continuing the turn")
    else:
        bad("once: brin does not mark later rounds, the gateway cannot tell them apart")
    # The origin decision runs as written: its definitions are lifted out of
    # executor.py (the module itself needs the gateway client to import).
    import ast as _ast
    _tree = _ast.parse(ex)
    _keep = [n for n in _tree.body
             if (isinstance(n, _ast.Assign) and any(getattr(t, "id", "") in ("_SEND_TOOLS", "_BACKGROUND_CHANNELS") for t in n.targets))
             or (isinstance(n, _ast.FunctionDef) and n.name == "_send_origin")]
    _ns = {}
    exec(compile(_ast.Module(body=_keep, type_ignores=[]), "executor.py", "exec"), _ns)
    so = _ns.get("_send_origin")
    if not so:
        bad("origin: executor.py has no _send_origin, the stamp is not decided in one place")
    else:
        # Cases as they reach brin: the Studio posts desk chat with no channel,
        # the Runner wakes with "system", internal mail is "internal".
        desk = so("messages_send", None, "desk:3f80")
        tg = so("send-message", "telegram", "tg:1")
        if desk and desk.get("conversation") == "desk:3f80" and tg and tg.get("channel") == "telegram":
            ok("origin: messages_send from the Studio desk (no channel) and send-message from telegram carry their origin")
        else:
            bad("origin: a person's question to a colleague leaves without origin (desk=%r telegram=%r), the answer cannot come back" % (desk, tg))
        if so("messages_send", "system", "runner-x") is None and so("messages_send", "internal", "m-1") is None                 and so("messages_inbox", None, "desk:3f80") is None:
            ok("origin: runner (system) and internal turns, and tools that start no message, stamp none")
        else:
            bad("origin: a background turn or a non-send tool is stamped")
    # The action-claim guard, run as written: a reply saying "I have sent" after
    # a real messages_send must not be sent back as a lie (sn-4680, messages 65/66).
    _keep = [n for n in _tree.body
             if (isinstance(n, _ast.Assign) and any(getattr(t, "id", "") in ("_SEND_TOOLS", "_SEND_CLAIM_HINTS") for t in n.targets))
             or (isinstance(n, _ast.ClassDef) and n.name == "Executor")]
    _gns = {}
    for n in _keep:
        if isinstance(n, _ast.ClassDef):
            n.bases = []; n.keywords = []; n.decorator_list = []
            n.body = [f for f in n.body if isinstance(f, _ast.FunctionDef) and f.name == "_claims_unbacked_send"]
    exec(compile(_ast.Module(body=_keep, type_ignores=[]), "executor.py", "exec"), _gns)
    _ex = _gns.get("Executor")
    _cl = getattr(_ex, "_claims_unbacked_send", None) if _ex else None
    if not _cl:
        bad("guard: executor.py has no Executor._claims_unbacked_send")
    else:
        sent = [("messages_send", {"to": "foreman"}, "[HTTP 200] {\"ok\":true,\"id\":65}")]
        claim = "I have sent a message to foreman (John)."
        if _cl(None, claim, sent) is False and _cl(None, claim, []) is True:
            ok("guard: \"I have sent\" after messages_send is not called a lie; with no send it still is")
        else:
            bad("guard: after a real messages_send the guard says nothing was sent (%r), the agent sends again"
                % _cl(None, claim, sent))
    # The context meter reads the session's latest provider request.
    nu = getattr(cl, "normalise_usage", None)
    g = nu and nu({"promptTokenCount": 9000, "cachedContentTokenCount": 8000, "candidatesTokenCount": 40})
    o = nu and nu({"prompt_tokens": 9000, "completion_tokens": 40, "cost": 0.001,
                   "prompt_tokens_details": {"cached_tokens": 8960}})
    if (g and g["input_tokens"] + g["cache_read_tokens"] == 9000 and g["cache_read_tokens"] == 8000
            and o and o["input_tokens"] == 40 and o["cache_read_tokens"] == 8960 and o["cost_usd"] == 0.001):
        ok("context: Gemini and OpenAI usage become one request shape whose input + cache is the whole prompt")
    else:
        bad("context: provider usage not normalised: %s / %s" % (g, o))
    src_cl = open("gw/chat_logger.py").read()
    if "metadata[\"conversation\"] = client_raw" in src_cl and "'requests', (SELECT" in src_cl:
        ok("context: each round appends its request to the session ledger under the desk's conversation key")
    else:
        bad("context: an HTTP agent's session has no request ledger or no conversation key, the meter reads 0")
except Exception as e:
    bad("chat log could not run: %s: %s" % (type(e).__name__, e)); bad("once: not reached")

print()
print("FAILED: %d" % failed if failed else "ALL PASS")
sys.exit(1 if failed else 0)
PY
