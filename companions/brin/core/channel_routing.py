"""Channel-aware routing: maps (intent, channel) -> (specialist, capabilities)."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RouteResult:
    specialist_id: str
    allow_commands: bool = False


# Intents that clients handle locally (never reach Brin routing)
CLIENT_LOCAL_INTENTS = {
    "image_generation", "video_download", "video_analyze",
    "model_settings",
}

# (intent, channel) -> RouteResult
# channel=None means "any channel"
ROUTING_TABLE: dict[tuple[str, Optional[str]], RouteResult] = {
    # Admin intents -- only via telegram (has shell access)
    ("diagnostic", "telegram"): RouteResult("admin-specialist", allow_commands=True),
    ("file_op", "telegram"):    RouteResult("admin-specialist", allow_commands=True),

    # Research intents
    ("search", "telegram"):  RouteResult("research-specialist", allow_commands=True),
    ("search", "email"):     RouteResult("research-specialist", allow_commands=False),
    ("search", "web"):       RouteResult("research-specialist", allow_commands=False),
    ("save", "telegram"):    RouteResult("research-specialist", allow_commands=True),
    ("save", None):          RouteResult("research-specialist", allow_commands=False),

    # Chat/question -- channel-specific assistant
    ("chat", "telegram"):    RouteResult("assistant-tg-specialist"),
    ("chat", "email"):       RouteResult("assistant-email-specialist"),
    ("chat", "web"):         RouteResult("assistant-web-specialist"),
    ("question", "telegram"): RouteResult("assistant-tg-specialist"),
    ("question", "email"):    RouteResult("assistant-email-specialist"),
    ("question", "web"):      RouteResult("assistant-web-specialist"),
}

# Fallback per channel
CHANNEL_DEFAULTS: dict[str, RouteResult] = {
    "telegram": RouteResult("assistant-tg-specialist"),
    "email":    RouteResult("assistant-email-specialist"),
    "web":      RouteResult("assistant-web-specialist"),
}
DEFAULT_ROUTE = RouteResult("assistant-tg-specialist")


# Specialists that are allowed to execute commands (via @mention or channel routing)
COMMAND_ENABLED_SPECIALISTS = {
    "admin-specialist", "research-specialist",
    # Legacy names kept for backward compat
    "tg-admin-specialist", "tg-research-specialist",
}


def resolve_route(intent: str, channel: Optional[str] = None) -> RouteResult:
    """Look up routing table. Tries (intent, channel) then (intent, None) then channel default."""
    if result := ROUTING_TABLE.get((intent, channel)):
        return result
    if result := ROUTING_TABLE.get((intent, None)):
        return result
    if channel and (result := CHANNEL_DEFAULTS.get(channel)):
        return result
    return DEFAULT_ROUTE
