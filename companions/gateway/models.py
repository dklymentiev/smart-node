"""
AI Gateway Pydantic Models
"""
from pydantic import BaseModel
from typing import Optional, List, Union
from datetime import datetime
from enum import Enum


class Provider(str, Enum):
    CLI = "cli"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


class Message(BaseModel):
    role: str  # system, user, assistant, tool
    content: Optional[Union[str, list]] = None  # null is valid when assistant only emits tool_calls
    tool_calls: Optional[List[dict]] = None     # assistant-side OpenAI tool_calls (preserved through gateway)
    tool_call_id: Optional[str] = None          # tool-result message: which call this answers
    name: Optional[str] = None                  # optional tool name on tool messages


# ============== Session Models (CLI) ==============

class CreateSessionRequest(BaseModel):
    service: str  # calling service identifier (e.g. "entis", "top-monitor", "rein") - REQUIRED
    model: str = "haiku"
    system_prompt: Optional[str] = None
    timeout: int = 3600
    cwd: Optional[str] = None  # working directory for CLI process


class CreateSessionResponse(BaseModel):
    session_id: str
    provider: str = "cli"
    model: str
    created_at: datetime
    status: str = "ready"


class SessionChatRequest(BaseModel):
    content: Union[str, list]  # str for text, list for content blocks (vision)


class SessionChatResponse(BaseModel):
    session_id: str
    content: str
    tool_uses: List[str] = []
    usage: dict = {}
    cost_usd: Optional[float] = None
    duration_ms: Optional[float] = None
    content_length: Optional[int] = None


class SessionInfo(BaseModel):
    session_id: str
    model: str
    service: str = ""
    status: str
    created_at: datetime
    last_activity: datetime
    message_count: int
    ttl: int = 3600


class SessionListResponse(BaseModel):
    sessions: List[SessionInfo]


class SessionCloseResponse(BaseModel):
    session_id: str
    status: str
    total_messages: int
    duration_seconds: int


# ============== Cloud Provider Models ==============

class ChatRequest(BaseModel):
    service: str  # calling service identifier (e.g. "entis", "top-monitor", "rein") - REQUIRED
    provider: Optional[Provider] = None  # if omitted, derived from model (bare name -> cli, slug -> openrouter)
    model: str
    messages: List[Message]
    temperature: float = 0.7
    max_tokens: int = 4096
    tools: Optional[List[dict]] = None       # OpenAI tools schema -- passthrough to provider
    tool_choice: Optional[str] = None        # "auto" | "none" | "required" -- passthrough
    system: Optional[str] = None             # optional system prompt (for providers that take it separately)
    # --- conversation attribution (optional; for central chat persistence) ---
    session_id: Optional[str] = None         # conversation id to group turns under one session
    agent: Optional[str] = None              # agent slug owning this conversation
    user: Optional[str] = None               # Remote-User / owner of the conversation
    raw_user_content: Optional[str] = None   # raw user text before enrichment; stored in place of messages[-1].content
    # A later round of a tool loop: the person's message was stored with
    # the first round. Without this every round stored it again, and the chat
    # showed the message once per tool call and fed the copies back to the model.
    continues_turn: bool = False


class ChatResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: str
    usage: dict = {}
    tool_calls: Optional[List[dict]] = None  # OpenAI-format tool_calls when model emits them


# ============== Health Models ==============

class ProviderHealth(BaseModel):
    status: str
    latency_ms: Optional[int] = None
    active_sessions: Optional[int] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    providers: dict
