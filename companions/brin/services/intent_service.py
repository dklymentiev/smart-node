"""
Intent Service -- classify user intent and parse search queries via Gemini Flash.

Ported from brin-client-tg intent_service.py. Drops parse_model_command,
should_allow_commands, and health_check. Returns Pydantic models.
"""

import json
import logging
import os

import httpx

from models.intent import Intent, ClassifyResult, SearchParseResult, ModelParseResult, SessionParseResult

logger = logging.getLogger(__name__)

# Resolve an OpenRouter key for the direct path: env first, then the
# legacy host layout (brin-client-tg .env). An empty string -- not an
# error -- when neither is present; brin then routes through the
# gateway companion instead (see IntentService.__init__).
def _load_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_file = "/opt/smart-node/brin-client-tg/.env"
    try:
        for line in open(env_file):
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except (FileNotFoundError, OSError):
        pass
    return ""


# The gateway companion is brin's single egress to LLMs. AI_GATEWAY_URL
# is the same env var the live executor path (core/executor.py) reads.
_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:19850").rstrip("/")


class IntentService:
    """Classify user intent and parse search queries using Gemini Flash.

    Environment-agnostic: with an OpenRouter key it calls OpenRouter
    directly; without one it routes through the
    gateway companion (the Smart Node layout -- the gateway owns the
    provider key). The route is chosen once at init."""

    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    GATEWAY_CHAT_PATH = "/api/v1/chat"
    PROVIDER = "openrouter"
    MODEL = "google/gemini-3-flash-preview"
    TIMEOUT = 5

    SYSTEM_PROMPT = """You are an intent classifier. Classify the user's message into exactly ONE category.

Categories:
- diagnostic: user wants system information (cpu, memory, disk, docker, processes, logs, fail2ban, status)
- question: user is asking about capabilities, how something works, or a general question (NOT requesting action)
- file_op: user wants to do something with a file (read, move, delete, analyze)
- save: user wants to save, remember, or store something (save this, remember that, note it down)
- search: user wants to find, search, or show something from saved memory/links (find, search, show links, what do we have on, links in inbox, all links, tiktok, youtube, instagram links)
- model_settings: user wants to change, view, or configure AI models (model, recognition, vision, audio, chat model settings)
- image_generation: user wants to generate/create/draw an image (draw, generate an image, create a picture, generate image, draw)
- video_download: user wants to download video from URL (download video, save video, URL from youtube/tiktok/instagram/twitter)
- video_analyze: user wants to analyze/understand video content (analyze video, what is in the video, describe video)
- session: user wants to switch, list, delete, or deactivate a named conversation session (switch to marketer, sysadmin session, show sessions, delete session, leave session, session marketing)
- check_inbox: user is asking whether there are NEW MESSAGES or REPLIES from people/colleagues -- the internal mailbox, NOT saved links/notes (any new messages, anything from my colleague, did anyone reply, what's the latest, check my mail, anything from the team)
- hint: user is teaching/correcting the bot how to do something (you should search in..., use grep, the right way is, look in the inbox)
- chat: general conversation, greeting, thanks, or unclear intent

Rules:
- If user asks "can you do X?" or "do you know how to X?" -> question (NOT diagnostic)
- If user asks "check docker" or "show cpu" -> diagnostic
- If user sends a file/photo with no clear instruction -> chat
- If user says "save this" or "remember that" -> save
- If user says "find", "search", "show links", "all links", "in inbox" about SAVED links/notes/content -> search
- Examples of search: "show all links", "find tiktok", "show inbox links", "links in inbox"
- If user asks about new messages/replies FROM PEOPLE (not links) -> check_inbox
- Examples of check_inbox: "any new messages?", "anything from my colleague?", "did anyone reply?", "what's the latest", "did they get back to us"
- If user mentions "model", "recognition", "vision", "audio" in context of settings -> model_settings
- If user says "draw", "generate a picture", "create an image", "generate image" -> image_generation
- If message contains URL from youtube.com, youtu.be, tiktok.com, instagram.com/reel, twitter.com, x.com AND has "download", "save video" -> video_download
- Examples of video_download: "download https://youtube.com/...", "download this video https://tiktok.com/...", "save video https://..."
- Examples of image_generation: "draw a cat", "generate a picture of space", "create an image of sunset"
- Examples of model_settings: "use gemini for photos", "show models", "which model", "switch to nemotron"
- If user says "switch to", "session", "show sessions", "delete session", "leave session", "switch to" -> session
- Examples of session: "switch to marketer", "sysadmin session", "my sessions", "which sessions", "delete session test", "leave session"
- Default to "chat" if unclear

Respond with ONLY the category name, nothing else."""

    MODEL_PARSE_PROMPT = """Parse this model settings command and return JSON.

Available services: vision (photos/images), audio (voice/transcription), chat (conversation), intent (intent detection), image_gen (image generation)

Available models by service:
- vision: nemotron-free, nemotron, gemini-flash, gemini-2.5-flash, gemini-2.5-pro
- audio: whisper-local, whisper-medium, gemini-audio
- chat: gemini-3-flash, gemini-2.5-flash, claude-haiku, claude-sonnet
- intent: gemini-3-flash, gemini-2.5-flash
- image_gen: flux-schnell, flux-dev, flux-pro

User message: {message}

Return JSON with these fields:
- action: "list" (show models), "get" (show current), "set" (change model), "stats" (usage statistics)
- service: null (all) or "vision", "audio", "chat", "intent", "image_gen"
- model: null or model alias (only for action=set)

Examples:
"show models" -> {{"action": "list", "service": null, "model": null}}
"use gemini-2.5-pro for photos" -> {{"action": "set", "service": "vision", "model": "gemini-2.5-pro"}}
"which model for images" -> {{"action": "get", "service": "vision", "model": null}}
"usage statistics" -> {{"action": "stats", "service": null, "model": null}}
"set flux-pro for generation" -> {{"action": "set", "service": "image_gen", "model": "flux-pro"}}

Return ONLY valid JSON, no explanation."""

    SEARCH_PARSE_PROMPT = """Parse this search request and return JSON.

User message: {message}

Extract:
- scope: where to search
  - "inbox" = user's incoming messages (inbox, recent)
  - "notes" = saved notes (notes)
  - "worklog" = work logs (worklog)
  - "all" = search everywhere (default)
- kind: what type of content (null if not specified)
  - "url" = links/URLs (links, url)
  - "photo" = images (photos, images)
  - "video" = videos (video)
  - null = any content
- platform: specific platform filter (null if not specified)
  - "tiktok", "youtube", "instagram", "twitter"
- query: search terms for semantic search (cleaned, translated to English if needed)

Examples:
"show links in inbox" -> {{"scope": "inbox", "kind": "url", "platform": null, "query": "links"}}
"find tiktok links" -> {{"scope": "all", "kind": "url", "platform": "tiktok", "query": "tiktok links"}}
"what is in notes about API" -> {{"scope": "notes", "kind": null, "platform": null, "query": "API"}}
"all inbox" -> {{"scope": "inbox", "kind": null, "platform": null, "query": ""}}
"find youtube videos" -> {{"scope": "all", "kind": "video", "platform": "youtube", "query": "youtube video"}}

Return ONLY valid JSON, no explanation."""

    SESSION_PARSE_PROMPT = """Parse this session management command and return JSON.

User message: {message}

Return JSON with these fields:
- action: "switch" (switch to / create session), "list" (show sessions), "delete" (remove session), "deactivate" (exit session), "status" (show current session)
- name: session name (only for action=switch or action=delete, null otherwise)

Examples:
"switch to marketer" -> {{"action": "switch", "name": "marketer"}}
"sysadmin session" -> {{"action": "switch", "name": "sysadmin"}}
"go to the test one" -> {{"action": "switch", "name": "test"}}
"show sessions" -> {{"action": "list", "name": null}}
"my sessions" -> {{"action": "list", "name": null}}
"which sessions" -> {{"action": "list", "name": null}}
"delete session test" -> {{"action": "delete", "name": "test"}}
"leave session" -> {{"action": "deactivate", "name": null}}
"turn off session" -> {{"action": "deactivate", "name": null}}
"which session is active" -> {{"action": "status", "name": null}}

Important: extract the session NAME as-is from the message.

Return ONLY valid JSON, no explanation."""

    def __init__(self):
        self.openrouter_key = _load_openrouter_key()
        self.gateway_url = _GATEWAY_URL
        # Direct OpenRouter when a key is available; otherwise route
        # through the gateway companion (keyless on a Smart Node).
        self.mode = "openrouter" if self.openrouter_key else "gateway"
        logger.info("IntentService initialized",
                    extra={"action": "intent_init", "model": self.MODEL, "mode": self.mode})

    async def _call_openrouter(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 20,
        timeout: float = 5,
    ) -> str:
        """Call Gemini Flash for intent work -- reusable for classify +
        parse. Two routes, picked at init: direct OpenRouter when a
        provider key is available, else through the gateway companion.
        Both reach the openrouter provider; the reply is normalised to
        plain text."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        if self.mode == "openrouter":
            headers = {
                "Authorization": f"Bearer {self.openrouter_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
            }
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(self.OPENROUTER_URL, headers=headers, json=payload)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        # gateway route -- the gateway holds the provider key; on the
        # loopback it needs no auth header.
        payload = {
            "service": "dispatcher-intent",
            "provider": self.PROVIDER,
            "model": self.MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        url = f"{self.gateway_url}{self.GATEWAY_CHAT_PATH}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()["content"].strip()

    async def classify(self, text: str) -> ClassifyResult:
        """Classify user message intent."""
        if not text or len(text.strip()) < 2:
            return ClassifyResult(intent="chat", confidence="high")

        try:
            raw = await self._call_openrouter(self.SYSTEM_PROMPT, text, max_tokens=20, timeout=self.TIMEOUT)
            intent_str = raw.strip().lower()

            valid = {i.value for i in Intent}
            if intent_str not in valid:
                intent_str = "unknown"

            logger.info(
                "Intent classified",
                extra={"action": "intent_classify", "text": text[:80], "intent": intent_str},
            )
            return ClassifyResult(intent=intent_str, confidence="high")

        except httpx.TimeoutException:
            logger.warning("Intent classification timed out", extra={"action": "intent_classify", "text": text[:80]})
            return ClassifyResult(intent="chat", confidence="low", error="timeout")
        except Exception as e:
            logger.error("Intent classification failed", extra={"action": "intent_classify", "error": str(e)})
            return ClassifyResult(intent="unknown", error=str(e))

    async def parse_model_command(self, text: str) -> ModelParseResult:
        """Parse model settings command into structured parameters."""
        try:
            prompt = self.MODEL_PARSE_PROMPT.format(message=text)
            raw = await self._call_openrouter(
                "You are a model command parser. Return only valid JSON.",
                prompt,
                max_tokens=100,
                timeout=self.TIMEOUT,
            )

            content = raw
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            parsed = json.loads(content)
            logger.info(
                "Model command parsed",
                extra={"action": "model_parse", "text": text[:80], "result": parsed},
            )
            return ModelParseResult(
                action=parsed.get("action", ""),
                service=parsed.get("service"),
                model=parsed.get("model"),
            )

        except Exception as e:
            logger.error("Model command parse failed", extra={"action": "model_parse", "error": str(e)})
            return ModelParseResult(error=str(e))

    async def parse_search_query(self, text: str) -> SearchParseResult:
        """Parse natural language search query into structured parameters."""
        try:
            prompt = self.SEARCH_PARSE_PROMPT.format(message=text)
            raw = await self._call_openrouter(
                "You are a search query parser. Return only valid JSON.",
                prompt,
                max_tokens=150,
                timeout=self.TIMEOUT,
            )

            # Strip markdown code fences if present
            content = raw
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            parsed = json.loads(content)
            logger.info(
                "Search query parsed",
                extra={"action": "search_parse", "text": text[:80], "result": parsed},
            )
            return SearchParseResult(
                scope=parsed.get("scope", "all"),
                kind=parsed.get("kind"),
                platform=parsed.get("platform"),
                query=parsed.get("query", ""),
            )

        except Exception as e:
            logger.error("Search parse failed", extra={"action": "search_parse", "error": str(e)})
            return SearchParseResult(scope="all", query=text, error=str(e))

    async def parse_session_command(self, text: str) -> SessionParseResult:
        """Parse session management command into structured parameters."""
        try:
            prompt = self.SESSION_PARSE_PROMPT.format(message=text)
            raw = await self._call_openrouter(
                "You are a session command parser. Return only valid JSON.",
                prompt,
                max_tokens=80,
                timeout=self.TIMEOUT,
            )

            content = raw
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            parsed = json.loads(content)
            logger.info(
                "Session command parsed",
                extra={"action": "session_parse", "text": text[:80], "result": parsed},
            )
            return SessionParseResult(
                action=parsed.get("action", ""),
                name=parsed.get("name"),
            )

        except Exception as e:
            logger.error("Session parse failed", extra={"action": "session_parse", "error": str(e)})
            return SessionParseResult(error=str(e))
