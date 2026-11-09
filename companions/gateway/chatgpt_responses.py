"""OpenAI Chat Completions <-> the Responses API, for the ChatGPT plan upstream.

Flint speaks Chat Completions; a sign-in with a ChatGPT plan is accepted only on the Responses
API (streaming, nothing stored). The door (openai_relay) puts this module between the two: a chat
request becomes a Responses request, and the Responses event stream comes back as chat chunks, so
Flint sees an ordinary OpenAI answer.

Pure functions and one small state machine; no network, no database. What the plan's route does
not accept is dropped here rather than sent and refused (developers.openai.com/siwc/token-sharing-
open-source/preview-limitations): sampling and length parameters, metadata, user, conversation
state. History is sent in full every time because previous_response_id does not work over HTTP.
"""
from __future__ import annotations

import json
import time
import uuid

# Chat parameters the plan's Responses route has no use for or rejects; named so the door can say
# what it dropped.
DROPPED = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "stop", "presence_penalty",
           "frequency_penalty", "logit_bias", "seed", "user", "metadata", "stream_options", "logprobs",
           "top_logprobs", "response_format", "service_tier", "store", "modalities", "audio", "prediction")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def _user_content(content):
    if isinstance(content, str):
        return content
    parts = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("text", "input_text"):
            parts.append({"type": "input_text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            image = p.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts or ""


def to_responses(body: dict) -> tuple[dict, list]:
    """A chat request body as a Responses request body, and the names of what was left out."""
    instructions, items = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            text = _text_of(m.get("content"))
            if text:
                instructions.append(text)
        elif role == "user":
            items.append({"role": "user", "content": _user_content(m.get("content"))})
        elif role == "assistant":
            text = _text_of(m.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({"type": "function_call", "call_id": call.get("id"), "name": fn.get("name"),
                              "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                          "output": _text_of(m.get("content"))})
    out = {"model": body.get("model"), "input": items, "store": False, "stream": True}
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    tools = []
    for t in body.get("tools") or []:
        fn = t.get("function") or {}
        if t.get("type") == "function" and fn.get("name"):
            tools.append({"type": "function", "name": fn["name"], "description": fn.get("description", ""),
                          "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
    if tools:
        out["tools"] = tools
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            if name:
                out["tool_choice"] = {"type": "function", "name": name}
        elif choice in ("auto", "none", "required"):
            out["tool_choice"] = choice
        if "parallel_tool_calls" in body:
            out["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
    if body.get("reasoning_effort"):
        out["reasoning"] = {"effort": body["reasoning_effort"]}
    return out, [k for k in DROPPED if k in body]


def _chunk(state, delta, finish=None, **extra) -> dict:
    out = {"id": state.id, "object": "chat.completion.chunk", "created": state.created, "model": state.model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    out.update(extra)
    return out


class ChatStream:
    """Feed it the Responses events in order; it returns the chat chunks each one produces.
    `finished` is true once the answer is over, normally or not."""

    def __init__(self, model: str):
        self.id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.model = model
        self.finished = False
        self._started = False
        self._tools = {}          # output_index -> chat tool_calls index
        self._saw_tool = False

    def _start(self) -> list:
        if self._started:
            return []
        self._started = True
        return [_chunk(self, {"role": "assistant", "content": ""})]

    def _error(self, code, message) -> list:
        self.finished = True
        return [{"error": {"message": str(message)[:300], "type": "upstream_error", "code": code}}]

    def feed(self, event: str, data: dict) -> list:
        if self.finished:
            return []
        if event == "response.created":
            return self._start()
        if event == "response.output_text.delta":
            return self._start() + [_chunk(self, {"content": data.get("delta", "")})]
        if event == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                i = len(self._tools)
                self._tools[data.get("output_index", i)] = i
                self._saw_tool = True
                return self._start() + [_chunk(self, {"tool_calls": [
                    {"index": i, "id": item.get("call_id") or item.get("id"), "type": "function",
                     "function": {"name": item.get("name", ""), "arguments": ""}}]})]
            return []
        if event == "response.function_call_arguments.delta":
            i = self._tools.get(data.get("output_index"))
            if i is None:
                return []
            return [_chunk(self, {"tool_calls": [{"index": i, "function": {"arguments": data.get("delta", "")}}]})]
        if event in ("response.completed", "response.incomplete"):
            self.finished = True
            resp = data.get("response") or {}
            u = resp.get("usage") or {}
            usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                     "total_tokens": u.get("total_tokens", (u.get("input_tokens", 0) + u.get("output_tokens", 0))),
                     "prompt_tokens_details": {"cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens", 0)}}
            reason = "length" if event == "response.incomplete" else ("tool_calls" if self._saw_tool else "stop")
            return self._start() + [_chunk(self, {}, reason), {"id": self.id, "object": "chat.completion.chunk",
                                                               "created": self.created, "model": self.model,
                                                               "choices": [], "usage": usage}]
        if event == "response.failed":
            err = (data.get("response") or {}).get("error") or {}
            return self._error(err.get("code") or "unknown_error", err.get("message") or "the upstream request failed")
        if event == "error":
            return self._error(data.get("code") or "unknown_error", data.get("message") or "the upstream request failed")
        return []

    def end_of_stream(self) -> list:
        """The upstream closed the stream. Without response.completed that is a failure, not an answer."""
        if self.finished:
            return []
        return self._error("incomplete_stream", "the stream ended without response.completed")


def sse_events(lines):
    """(event, data) pairs from an iterable of SSE lines (str or bytes), blank line ends one event."""
    event, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield event or "message", json.loads("\n".join(data))
                except ValueError:
                    pass
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            yield event or "message", json.loads("\n".join(data))
        except ValueError:
            pass


def chunk_line(chunk: dict) -> bytes:
    return b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"


DONE_LINE = b"data: [DONE]\n\n"


def collect(chunks) -> dict:
    """One chat.completion from a finished run of chunks (for a request that did not ask to stream)."""
    text, calls, finish, usage, ident, model, error = [], {}, None, None, None, None, None
    for c in chunks:
        if c.get("error"):
            error = c["error"]
            continue
        ident, model = c.get("id", ident), c.get("model", model)
        if c.get("usage"):
            usage = c["usage"]
        for ch in c.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            for t in d.get("tool_calls") or []:
                slot = calls.setdefault(t["index"], {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if t.get("id"):
                    slot["id"] = t["id"]
                fn = t.get("function") or {}
                slot["function"]["name"] += fn.get("name", "")
                slot["function"]["arguments"] += fn.get("arguments", "")
            finish = ch.get("finish_reason") or finish
    if error:
        return {"error": error}
    msg = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return {"id": ident, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}], "usage": usage or {}}


class Translator:
    """One upstream answer turned into the chat format, as bytes in and bytes out.

    Built by the door after the upstream has answered, so it knows the status. `media_type` and
    `streaming` say what comes out: chat chunks as SSE, one completion as JSON, or an error body as
    JSON. An upstream error keeps its status and gets the shape OpenAI clients read."""

    def __init__(self, status: int, model: str, stream_out: bool, kind: str = "chat"):
        self._buf = b""
        self._model = model
        self._kind = kind
        self._status = status
        self._chunks = []
        if kind == "models" or status >= 400:
            self.mode = "json"
        else:
            self.mode = "sse" if stream_out else "whole"
        self.streaming = self.mode == "sse"
        self.media_type = "text/event-stream" if self.streaming else "application/json"
        self._state = ChatStream(model)

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        if self.mode != "sse":
            return b""
        out = b""
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            for name, payload in sse_events(raw.decode("utf-8", "replace").split(chr(10)) + [""]):
                for c in self._state.feed(name, payload):
                    out += chunk_line(c)
        return out

    def close(self) -> bytes:
        if self.mode == "sse":
            out = b""
            if self._buf.strip():
                self._buf += b"\n\n"
                out += self.feed(b"")
            for c in self._state.end_of_stream():
                out += chunk_line(c)
            return out + DONE_LINE
        if self.mode == "whole":
            chunks = []
            for name, payload in sse_events((self._buf.decode("utf-8", "replace") + "\n\n").split("\n")):
                chunks += self._state.feed(name, payload)
            chunks += self._state.end_of_stream()
            return json.dumps(collect(chunks)).encode()
        return json.dumps(self._json_body()).encode()

    def _json_body(self) -> dict:
        try:
            data = json.loads(self._buf or b"{}")
        except ValueError:
            data = {}
        if self._kind == "models":
            listed = [m for m in (data.get("models") or []) if m.get("visibility") == "list" and m.get("slug")]
            return {"object": "list", "data": [{"id": m["slug"], "object": "model", "owned_by": "openai"} for m in listed]}
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return {"error": {"message": str(err.get("message") or "the upstream refused the request")[:300],
                              "type": err.get("type") or "upstream_error", "code": err.get("code") or self._status}}
        return {"error": {"message": "the upstream answered %d" % self._status, "type": "upstream_error", "code": self._status}}
