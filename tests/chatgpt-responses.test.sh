#!/usr/bin/env bash
#
# The Chat Completions <-> Responses translation behind the ChatGPT plan upstream
# (companions/gateway/chatgpt_responses.py). No network, nothing on the host touched.
#
# WHAT IS CHECKED
#   request   system and developer text becomes instructions; user text and images, assistant text,
#             assistant tool calls and tool results become input items; tools and tool_choice are
#             reshaped; store is false and stream is true whatever the caller asked; the parameters
#             the plan's route refuses are dropped and named
#   stream    text deltas, a tool call with its argument pieces, the finish reason (stop, tool_calls,
#             length), the usage chunk, a failed response, and a stream that ends without
#             response.completed (which is an error, never an answer)
#   whole     a request that did not ask to stream is answered with one completion
#   bytes     the same answer whatever way the upstream's bytes are cut; a cut stream, an upstream error
#             (JSON or not) and the model list come out in the shape OpenAI clients read
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../companions/gateway"

python3 -I - <<'PY'
import json
import sys

sys.path.insert(0, ".")
import chatgpt_responses as r

failed = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failed.append(name)


# ---- request ----
chat = {
    "model": "gpt-x", "stream": False, "temperature": 0.2, "max_tokens": 50, "user": "u1", "metadata": {"a": 1},
    "messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "developer", "content": [{"type": "text", "text": "Use tools."}]},
        {"role": "user", "content": [{"type": "text", "text": "Look at this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]},
        {"role": "assistant", "content": "Checking.", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":1}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "found it"},
        {"role": "user", "content": "Thanks"},
    ],
    "tools": [{"type": "function", "function": {"name": "lookup", "description": "find", "parameters": {"type": "object", "properties": {"q": {"type": "integer"}}}}}],
    "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    "reasoning_effort": "low",
}
body, dropped = r.to_responses(chat)
check("request: store false and stream true whatever was asked", body["store"] is False and body["stream"] is True)
check("request: system and developer text become instructions", body["instructions"] == "Be brief.\n\nUse tools.")
items = body["input"]
check("request: user text and image", items[0] == {"role": "user", "content": [{"type": "input_text", "text": "Look at this"}, {"type": "input_image", "image_url": "data:image/png;base64,AAA"}]})
check("request: assistant text then its tool call", items[1] == {"role": "assistant", "content": "Checking."}
      and items[2] == {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{\"q\":1}"})
check("request: tool result", items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "found it"})
check("request: the last user turn", items[4] == {"role": "user", "content": "Thanks"})
check("request: tools reshaped", body["tools"] == [{"type": "function", "name": "lookup", "description": "find",
                                                      "parameters": {"type": "object", "properties": {"q": {"type": "integer"}}}}])
check("request: a named tool choice", body["tool_choice"] == {"type": "function", "name": "lookup"})
check("request: reasoning effort", body["reasoning"] == {"effort": "low"})
check("request: refused parameters are not sent", all(k not in body for k in ("temperature", "max_tokens", "user", "metadata", "messages")))
check("request: and are named", set(dropped) == {"temperature", "max_tokens", "user", "metadata"})
b2, _ = r.to_responses({"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": [
    {"type": "function", "function": {"name": "t"}}], "tool_choice": "required", "parallel_tool_calls": False})
check("request: a string tool choice and parallel calls pass", b2["tool_choice"] == "required" and b2["parallel_tool_calls"] is False
      and "instructions" not in b2 and b2["tools"][0]["parameters"] == {"type": "object", "properties": {}})

# ---- stream ----
def run(events, model="gpt-x"):
    s = r.ChatStream(model)
    out = []
    for name, data in events:
        out += s.feed(name, data)
    return s, out


text_events = [
    ("response.created", {"response": {}}),
    ("response.output_text.delta", {"delta": "Hel"}),
    ("response.output_text.delta", {"delta": "lo"}),
    ("response.completed", {"response": {"usage": {"input_tokens": 11, "output_tokens": 2, "total_tokens": 13,
                                                   "input_tokens_details": {"cached_tokens": 4}}}}),
]
s, out = run(text_events)
deltas = [c["choices"][0]["delta"] for c in out if c.get("choices")]
check("stream: role first, then the text pieces", deltas[0].get("role") == "assistant" and "".join(d.get("content", "") for d in deltas) == "Hello")
fin = [c for c in out if c.get("choices") and c["choices"][0]["finish_reason"]]
check("stream: stop at the end", len(fin) == 1 and fin[0]["choices"][0]["finish_reason"] == "stop")
usage = [c for c in out if c.get("usage")]
check("stream: usage chunk with cached tokens", usage and usage[0]["usage"] == {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13,
                                                                                   "prompt_tokens_details": {"cached_tokens": 4}})
check("stream: finished and nothing after it", s.finished and s.feed("response.output_text.delta", {"delta": "x"}) == [] and s.end_of_stream() == [])
check("stream: ids and model stay the same", len({c["id"] for c in out if "id" in c}) == 1 and all(c["model"] == "gpt-x" for c in out if "model" in c))

tool_events = [
    ("response.output_item.added", {"output_index": 0, "item": {"type": "message"}}),
    ("response.output_item.added", {"output_index": 1, "item": {"type": "function_call", "call_id": "call_9", "name": "lookup"}}),
    ("response.function_call_arguments.delta", {"output_index": 1, "delta": "{\"q\":"}),
    ("response.function_call_arguments.delta", {"output_index": 1, "delta": "2}"}),
    ("response.completed", {"response": {"usage": {"input_tokens": 5, "output_tokens": 3}}}),
]
s, out = run(tool_events)
calls = [t for c in out if c.get("choices") for t in (c["choices"][0]["delta"].get("tool_calls") or [])]
check("stream: a tool call starts with its id and name", calls[0]["id"] == "call_9" and calls[0]["function"]["name"] == "lookup" and calls[0]["index"] == 0)
check("stream: its arguments arrive in pieces", "".join(t["function"].get("arguments", "") for t in calls) == "{\"q\":2}")
check("stream: finish reason is tool_calls", [c["choices"][0]["finish_reason"] for c in out if c.get("choices") and c["choices"][0]["finish_reason"]] == ["tool_calls"])
whole = r.collect(out)
check("whole: one completion with the tool call", whole["choices"][0]["message"]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": "{\"q\":2}"}
      and whole["choices"][0]["finish_reason"] == "tool_calls" and whole["usage"]["prompt_tokens"] == 5)
check("whole: text answer", r.collect(run(text_events)[1])["choices"][0]["message"]["content"] == "Hello")

s, out = run([("response.output_text.delta", {"delta": "par"}), ("response.incomplete", {"response": {}})])
check("stream: incomplete finishes as length", [c["choices"][0]["finish_reason"] for c in out if c.get("choices") and c["choices"][0]["finish_reason"]] == ["length"])
s, out = run([("response.output_text.delta", {"delta": "x"}), ("response.failed", {"response": {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}})])
check("stream: a failed response is an error with its code", out[-1]["error"]["code"] == "subscription_sharing_usage_limit_exceeded" and s.finished)
check("whole: an error stays an error", "error" in r.collect(out))
s, out = run([("response.output_text.delta", {"delta": "x"})])
end = s.end_of_stream()
check("stream: ending without response.completed is an error", end and end[0]["error"]["code"] == "incomplete_stream" and s.finished)
s, out = run([("error", {"code": "rate_limit", "message": "slow down"})])
check("stream: an error event", out[0]["error"]["code"] == "rate_limit")
check("stream: events nobody maps are ignored", run([("response.reasoning_summary_text.delta", {"delta": "…"}), ("response.in_progress", {})])[1] == [])

# ---- sse parsing ----
raw = ["event: response.output_text.delta", "data: {\"delta\": \"a\"}", "", ": keepalive", "event: response.completed",
       "data: {\"response\": {}}", ""]
pairs = list(r.sse_events(raw))
check("sse: events in order", [p[0] for p in pairs] == ["response.output_text.delta", "response.completed"] and pairs[0][1] == {"delta": "a"})
check("sse: bytes lines and a last event with no blank line", list(r.sse_events([b"event: x", b"data: {\"k\": 1}"])) == [("x", {"k": 1})])
check("sse: a broken data line is skipped", list(r.sse_events(["event: x", "data: {nope", ""])) == [])
check("lines: a chunk and the end marker", r.chunk_line({"a": 1}) == b"data: {\"a\":1}\n\n" and r.DONE_LINE == b"data: [DONE]\n\n")

# ---- Translator: bytes in, bytes out ----
def sse(events):
    return b"".join(b"event: " + n.encode() + b"\ndata: " + json.dumps(d).encode() + b"\n\n" for n, d in events)


def parse_out(raw):
    chunks, done = [], False
    for line in raw.decode().split("\n\n"):
        if line.startswith("data: "):
            if line[6:] == "[DONE]":
                done = True
            else:
                chunks.append(json.loads(line[6:]))
    return chunks, done


wire = sse(text_events)
for size in (1, 7, len(wire)):
    t = r.Translator(200, "gpt-x", True)
    raw = b"".join(t.feed(wire[i:i + size]) for i in range(0, len(wire), size)) + t.close()
    chunks, done = parse_out(raw)
    said = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    check("translator: SSE in pieces of %d bytes gives the same answer" % size, said == "Hello" and done and t.media_type == "text/event-stream" and t.streaming)
t = r.Translator(200, "gpt-x", True)
raw = t.feed(sse([("response.output_text.delta", {"delta": "cut"})])) + t.close()
chunks, done = parse_out(raw)
check("translator: a stream cut short ends in an error and the end marker", chunks[-1].get("error", {}).get("code") == "incomplete_stream" and done)
t = r.Translator(200, "gpt-x", False)
t.feed(wire)
one = json.loads(t.close())
check("translator: not streaming gives one completion as JSON", one["object"] == "chat.completion" and one["choices"][0]["message"]["content"] == "Hello"
      and t.media_type == "application/json" and not t.streaming)
t = r.Translator(429, "gpt-x", True)
t.feed(json.dumps({"error": {"message": "slow down", "type": "rate", "code": "rate_limit_exceeded"}}).encode())
err = json.loads(t.close())
check("translator: an upstream error keeps its message, type and code, as JSON", err == {"error": {"message": "slow down", "type": "rate", "code": "rate_limit_exceeded"}}
      and t.media_type == "application/json" and not t.streaming)
t = r.Translator(502, "gpt-x", True)
t.feed(b"<html>bad gateway</html>")
err = json.loads(t.close())
check("translator: an upstream error that is not JSON still gets the error shape", err["error"]["code"] == 502 and "502" in err["error"]["message"])
t = r.Translator(200, "", False, kind="models")
t.feed(json.dumps({"models": [{"slug": "gpt-a", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"}, {"slug": "gpt-b", "visibility": "list"}]}).encode())
lst = json.loads(t.close())
check("translator: the model list becomes an OpenAI list of the listed ones", lst == {"object": "list", "data": [
    {"id": "gpt-a", "object": "model", "owned_by": "openai"}, {"id": "gpt-b", "object": "model", "owned_by": "openai"}]})
sys.exit(1 if failed else 0)
PY
echo "PASS: chatgpt responses translation"
