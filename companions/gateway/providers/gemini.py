"""
Google Gemini API Provider
"""
import json
from typing import List, AsyncIterator
from models import Message
from providers.base import BaseProvider
from config import GEMINI_API_KEY, GEMINI_DEFAULT_MODEL
from secrets_store import get_provider_key
from sse_parser import SSEParser


def _text_of(content) -> str:
    """A message's text, whether content is a string or a list of blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return str(content)


def _function_declarations(tools) -> list:
    """OpenAI function specs -> Gemini functionDeclarations.

    parametersJsonSchema takes the JSON Schema as written; the older
    `parameters` field accepts only an OpenAPI subset and refuses keys such as
    additionalProperties that MCP servers put in their schemas.
    """
    decls = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        d = {"name": fn["name"], "description": fn.get("description") or fn["name"]}
        params = fn.get("parameters")
        if isinstance(params, dict) and params:
            d["parametersJsonSchema"] = params
        decls.append(d)
    return decls


class GeminiTally:
    """The door's reader of a Gemini answer (model_door.Format). Gemini sends
    JSON, whole or (streamGenerateContent) as an array of the same objects;
    the last usageMetadata is the call's."""

    def __init__(self):
        self.usage: dict = {}
        self.stop_reason = None
        self.tools: list = []
        self.progress = 0
        self._whole = b""

    def feed_sse(self, chunk: bytes) -> None:
        self.feed_whole(chunk)

    def feed_whole(self, chunk: bytes) -> None:
        self.progress += 1
        if len(self._whole) < 4_000_000:
            self._whole += chunk

    def finish_whole(self) -> None:
        try:
            data = json.loads(self._whole or b"null")
        except ValueError:
            return
        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            self.usage.update(msg.get("usageMetadata") or {})
            for cand in msg.get("candidates") or []:
                self.stop_reason = cand.get("finishReason") or self.stop_reason
                for part in (cand.get("content") or {}).get("parts") or []:
                    name = (part.get("functionCall") or {}).get("name")
                    if name:
                        self.tools.append(name)

    def summary(self) -> dict:
        u = self.usage
        return {"input_tokens": u.get("promptTokenCount"), "output_tokens": u.get("candidatesTokenCount"),
                "cache_read": u.get("cachedContentTokenCount"), "cache_write": None,
                "stop_reason": self.stop_reason, "tool_use": self.tools}


def _gemini_model(request) -> str:
    # .../models/<model>:generateContent
    tail = request.url.path.rsplit("/models/", 1)[-1]
    return tail.split(":", 1)[0] or None


class GeminiProvider(BaseProvider):
    """Google Gemini API provider"""

    door_name = "gemini"

    def door_format(self):
        import model_door
        return model_door.Format(
            "gemini",
            is_model_call=lambda r: r.method == "POST" and "Content" in r.url.path.rsplit(":", 1)[-1],
            model_of=_gemini_model,
            tally=GeminiTally,
        )

    def __init__(self, api_key: str = None, model: str = None):
        self._explicit_key = api_key
        self.model = model or GEMINI_DEFAULT_MODEL
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    @property
    def api_key(self):
        # Key from the node secret store (sn_config.secrets, ref gateway/gemini-key),
        # resolved lazily so a key set in AI Gateway settings applies without a
        # restart. Explicit ctor arg wins (tests); otherwise store is SoT.
        if self._explicit_key:
            return self._explicit_key
        return get_provider_key("gemini")

    def _headers(self):
        key = self.api_key
        if not key:
            raise RuntimeError("gemini: no API key configured (set it in AI Gateway settings)")
        return {"x-goog-api-key": key}

    def _convert_messages(self, messages: List[Message]) -> list:
        """OpenAI-shaped history -> Gemini `contents`, tool turns included.

        An assistant turn's tool_calls become functionCall parts (with the
        thoughtSignature Gemini gave them, which it requires back); a `tool`
        message becomes a functionResponse part. OpenAI results carry only the
        call id, Gemini wants the function name, so names are remembered from
        the calls as they pass. Consecutive results share one user turn, the way
        Gemini expects the answers to one model turn's parallel calls.
        """
        contents = []
        call_names = {}
        for msg in messages:
            text = _text_of(msg.content)
            if msg.role == "system":
                contents.append({"role": "user", "parts": [{"text": f"System: {text}"}]})
            elif msg.role == "assistant":
                parts = [{"text": text}] if text else []
                for tc in msg.tool_calls or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or ""
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        args = {}
                    call_names[tc.get("id")] = name
                    part = {"functionCall": {"name": name, "args": args if isinstance(args, dict) else {}}}
                    sig = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif msg.role == "tool":
                name = msg.name or call_names.get(msg.tool_call_id) or "tool"
                part = {"functionResponse": {"name": name, "response": {"content": text}}}
                last = contents[-1] if contents else None
                if last and last["role"] == "user" and all("functionResponse" in p for p in last["parts"]):
                    last["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            else:
                contents.append({"role": "user", "parts": [{"text": text}]})
        return contents

    async def chat(self, messages: List[Message], model: str = None,
                   max_tokens: int = None, tools: list = None,
                   tool_choice: str = None, system: str = None,
                   timeout: float = None) -> dict:
        """Send messages to Gemini API.

        Same keyword surface as OpenRouterProvider.chat: /api/v1/chat passes
        max_tokens/tools/tool_choice/system to every provider.

        tools are OpenAI function specs, as brin's tool loop sends them to every
        provider; they become Gemini functionDeclarations, and the model's
        functionCall parts come back as OpenAI-shaped tool_calls. Until
        then tools were accepted and dropped, so an agent on Gemini could not
        read the letter that woke it or answer it.
        """
        use_model = model or self.model
        contents = self._convert_messages(messages)

        body = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if max_tokens:
            body["generationConfig"] = {"maxOutputTokens": int(max_tokens)}
        decls = _function_declarations(tools)
        if decls:
            body["tools"] = [{"functionDeclarations": decls}]
            mode = {"none": "NONE", "required": "ANY"}.get(tool_choice or "auto", "AUTO")
            body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}

        client = await self.get_client()
        response = await client.post(
            f"{self.base_url}/models/{use_model}:generateContent",
            headers=self._headers(),
            json=body,
            timeout=self.call_timeout(timeout),
        )

        response.raise_for_status()
        data = response.json()

        # A blocked or empty candidate has no parts; name it instead of a KeyError.
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") if candidates else None
        if not parts:
            reason = (candidates[0].get("finishReason") if candidates
                      else (data.get("promptFeedback") or {}).get("blockReason"))
            raise RuntimeError(f"gemini: no text in response (reason: {reason})")
        # Thought summaries are the model's reasoning, not its answer.
        content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        tool_calls = []
        for i, p in enumerate(parts):
            fc = p.get("functionCall")
            if not fc:
                continue
            call = {
                "id": fc.get("id") or f"gemini-call-{i}",
                "type": "function",
                "function": {"name": fc.get("name", ""),
                             "arguments": json.dumps(fc.get("args") or {})},
            }
            if p.get("thoughtSignature"):
                call["extra_content"] = {"google": {"thought_signature": p["thoughtSignature"]}}
            tool_calls.append(call)

        out = {
            "id": "",
            "content": content,
            "usage": data.get("usageMetadata", {})
        }
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out

    async def chat_stream(self, messages: List[Message], model: str = None) -> AsyncIterator[dict]:
        """Stream response from Gemini API"""
        use_model = model or self.model
        contents = self._convert_messages(messages)

        client = await self.get_client()
        parser = SSEParser()

        async with client.stream(
            "POST",
            f"{self.base_url}/models/{use_model}:streamGenerateContent",
            headers=self._headers(),
            json={"contents": contents},
        ) as response:
            async for line in parser.parse(response):
                if line:
                    try:
                        data = json.loads(line)
                        if "candidates" in data:
                            text = data["candidates"][0]["content"]["parts"][0].get("text", "")
                            yield {"chunk": text, "done": False}
                    except json.JSONDecodeError:
                        continue
            yield {"chunk": "", "done": True}

    async def health_check(self) -> bool:
        """Check if Gemini API is accessible"""
        if not self.api_key:
            return False
        try:
            client = await self.get_client()
            response = await client.post(
                f"{self.base_url}/models/{self.model}:generateContent",
                headers=self._headers(),
                json={"contents": [{"parts": [{"text": "hi"}]}]},
                timeout=10,
            )
            return response.status_code == 200
        except Exception:
            return False
