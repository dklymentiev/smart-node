"""SessionBackend ABC and provider-resolution factory.

Each backend implementation owns one provider family (cli_claude.ClaudeCliBackend,
http_openrouter.OpenrouterBackend, http_anthropic.AnthropicBackend, ...).

Backend selection at session creation time is purely by model id prefix:
    "claude-cli/sonnet"                  -> ClaudeCliBackend
    "openrouter/anthropic/claude-3-haiku" -> OpenrouterBackend
    "anthropic/claude-3-haiku-20240307"   -> AnthropicBackend
    ...

Same parse rule as v2 contract: split on first '/', prefix is
upstream, rest is upstream-specific model id.
"""
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import db

logger = logging.getLogger("gateway.backends")


@dataclass
class BackendResponse:
    """Uniform shape returned by any SessionBackend.send()."""
    content: str
    usage: Dict[str, Any] = field(default_factory=dict)
    # usage shape: {input_tokens, output_tokens, cache_read_tokens,
    #               cache_write_tokens, cost_usd, estimated: bool}
    # Optional `requests` holds per-request input/cache/output measurements.
    # `cumulative: True` marks a multi-request turn rollup: its flat token
    # fields are billing totals, never a context size when records are absent.
    #
    # CACHE TOKENS ARE PART OF THE SHAPE, NOT AN ANTHROPIC DETAIL. Every provider
    # that caches prompts bills for those tokens and reports them separately from
    # the uncached input, under its own name: Anthropic sends
    # cache_creation_input_tokens / cache_read_input_tokens, OpenAI-compatible
    # APIs send prompt_tokens_details.cached_tokens. A backend translates its
    # provider's names into this pair; nothing above the backend knows which
    # provider it is talking to.
    #
    # Both default to 0 rather than being absent, so a backend that does not
    # cache (or a provider that does not report it) is indistinguishable from one
    # that cached nothing, which is the truth in both cases.
    #
    # Why it matters: dropping them made the displayed input a small fraction of
    # the billed one -- an agent turn showing "8 in" beside a real $0.10, because
    # the system prompt and tool context were served from cache and counted
    # nowhere. cost_usd was right the whole time, which is what made the numbers
    # look like a pricing mystery rather than a missing field.
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # A person stopped this turn before it finished. content is what the
    # agent had said by then, which is kept rather than thrown away: it is the
    # part of the work the person already watched happen.
    stopped: bool = False
    stopped_by: Optional[str] = None
    # The turn ended without doing its work: the CLI reported an error as its
    # result (an exhausted provider, a context that no longer fits). content
    # keeps the CLI's words; this is the reason, and the turn is not an answer.
    failed: Optional[str] = None
    # Separate customer-facing answer from the full turn kept for the desk.
    # None means a single-response backend; an empty string means no final
    # answer was produced and must never fall back to working text.
    final_answer: Optional[str] = None


class SessionBackend(ABC):
    """Provider-agnostic session lifecycle interface.

    SessionService owns DB persistence; SessionBackend owns runtime resources
    (subprocess, in-memory state, upstream HTTP connections).

    Concurrency: SessionService calls one backend instance method at a time per
    session_id. Subclasses do not need their own locking for single-session ops.
    """

    is_opaque: bool = False
    """True for backends where gateway cannot observe what happens inside (cli/*).
    Mirrored to sn_core.ai_sessions.is_opaque so UI/budget render 'estimated' badges."""

    provider_name: str = ""
    """Resolved provider id, e.g. 'claude-cli', 'openrouter', 'anthropic'.
    Subclasses must override."""

    @abstractmethod
    async def create(self, session_id: str, model: str,
                     system_prompt: Optional[str] = None,
                     metadata: Optional[Dict[str, Any]] = None) -> None:
        """Initialize backend state for a new session.

        Args:
            session_id: uuid from sn_core.ai_sessions.id (caller's truth)
            model: full model id (e.g. 'claude-cli/sonnet'); backend parses suffix
            system_prompt: optional system message text
            metadata: passed through from session metadata (os_user, etc.)
        """

    @abstractmethod
    async def send(self, session_id: str, history: List[Dict[str, Any]],
                   message: str) -> BackendResponse:
        """Send a user message and return assistant response.

        Args:
            session_id: uuid
            history: full conversation history (list of message dicts)
            message: new user message text

        Returns:
            BackendResponse with content + usage
        """

    @abstractmethod
    async def close(self, session_id: str) -> None:
        """Free backend resources (kill subprocess, close http session, etc.)."""

    @abstractmethod
    async def resume(self, session_id: str,
                     history: List[Dict[str, Any]]) -> None:
        """Recreate runtime state from history for an idle_released session."""

    async def interrupt(self, session_id: str,
                        requested_by: Optional[str] = None) -> Dict[str, Any]:
        """Stop the turn in flight without closing the session.

        Not abstract: a backend that cannot stop a turn says so, and the caller
        tells the person, rather than pretending it stopped."""
        return {"interrupted": False, "reason": f"{self.provider_name or 'this backend'} cannot stop a turn"}

    def estimate_tokens(self, text: str) -> int:
        """Default tiktoken-based estimator. Subclasses can override.

        Used by opaque backends where provider doesn't return accurate usage."""
        # Lazy import to avoid hard dep at module load
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            # Rough heuristic: ~4 chars per token
            return max(1, len(text) // 4)


# The upstreams this gateway can run, and how: 'cli' runs a local binary under the
# CLI provider's own credential, 'http' calls an API with the key stored at
# gateway/<id>-key. This is the one statement of that set: resolve_backend
# dispatches on it, GET /api/v1/backends reports it, and core-api validates a new
# sn_config.models row against that report instead of keeping a list of its own.
# Adding a backend means a branch below AND a row here; the test asserts they agree.
UPSTREAMS = {
    "claude-cli": "cli",
    "flint-cli": "cli",
    "openrouter": "http",
    "gemini": "http",
}

# A row of sn_config.cli_providers that runs the Claude Code binary is a backend
# too, under its own slug: the same process handling as claude-cli, with the
# row's own env_vars (the upstream its model calls go to and the credential for
# it, read by anthropic_relay) and its own supported_models. That is how a
# second Anthropic-compatible upstream is set up without code: a row, a catalog
# entry (POST /api/models validates the slug against GET /api/v1/backends), an
# agent on it. A local model server on a test node and a second Anthropic
# account are the two cases this is for. The built-in rows above are the floor.
# Rows are read every ROWS_TTL seconds; the last copy serves while the database
# is away, so a session in flight is not refused by a blink of the database.
ROWS_TTL = 30.0
_rows: dict = {"at": 0.0, "value": {}}


def cli_provider_rows() -> dict:
    """slug -> row (binary_path, supported_models, env_vars, enabled)."""
    now = time.monotonic()
    if now - _rows["at"] > ROWS_TTL:
        try:
            rows = db.query(
                "SELECT slug, binary_path, supported_models, env_vars, enabled "
                "FROM sn_config.cli_providers", (), fetch=True, as_dict=True) or []
            _rows["value"] = {r["slug"]: dict(r) for r in rows}
            _rows["at"] = now
        except Exception as e:  # noqa: BLE001
            logger.error("cli_providers unreadable, keeping the last copy: %s", e)
            _rows["at"] = now - ROWS_TTL + 5.0
    return _rows["value"]


def forget_rows() -> None:
    """Drop the cache (tests, and after a row changes)."""
    _rows.update(at=0.0, value={})


def runs_claude_code(row: Optional[dict]) -> bool:
    """Does this cli_providers row declare the Claude Code binary? The runtime
    runs CLI_PATH for every such row; binary_path is the row's declaration of
    what it is, as it was for claude-cli."""
    return bool(row) and os.path.basename(str(row.get("binary_path") or "")) == "claude"


def claude_cli_upstreams() -> set:
    """claude-cli and every enabled row that runs the Claude Code binary."""
    out = {"claude-cli"}
    for slug, row in cli_provider_rows().items():
        if row.get("enabled") and runs_claude_code(row):
            out.add(slug)
    return out


def upstreams() -> dict:
    """UPSTREAMS plus the enabled Claude Code rows: what resolve_backend
    dispatches on, what GET /api/v1/backends reports."""
    out = dict(UPSTREAMS)
    for slug in claude_cli_upstreams():
        out.setdefault(slug, "cli")
    return out


def cli_model_supported(upstream: str, model: str) -> bool:
    """Is `model` in the row's supported_models? The catalog and the built-in
    list are the other two ways a CLI model is allowed (model_catalog)."""
    row = cli_provider_rows().get(upstream) or {}
    return model in (row.get("supported_models") or [])


def resolve_backend(model_id: str) -> SessionBackend:
    """Parse '<upstream>/<rest>' and return a SessionBackend instance.

    Raises:
        ValueError: if model_id is malformed or upstream not recognized.
    """
    if "/" not in model_id:
        raise ValueError(
            f"model_id missing provider prefix: {model_id!r} "
            "(expected '<upstream>/<model>')"
        )

    upstream = model_id.split("/", 1)[0]

    # Registry. Add new backends here as Stage 2b-2/2b-3 ships them.
    if upstream == "claude-cli":
        from backends.cli_claude import ClaudeCliBackend
        return ClaudeCliBackend()
    if upstream == "flint-cli":
        from backends.cli_claude import FlintCliBackend
        return FlintCliBackend()
    row = cli_provider_rows().get(upstream)
    if row is not None and row.get("enabled") and runs_claude_code(row):
        from backends.cli_claude import ClaudeCliBackend
        return ClaudeCliBackend(provider_name=upstream)
    if upstream == "openrouter":
        from backends.http_openrouter import OpenrouterBackend
        return OpenrouterBackend()
    if upstream == "gemini":
        # Direct Google Gemini API. Note the upstream key is 'gemini', not 'google':
        # ALLOWED_MODELS['gemini'] and sn_config.models.provider='gemini' both use
        # the shorter name. The earlier placeholder comment said 'google' but no
        # code, config, or seed ever committed to that spelling.
        from backends.http_google import GoogleBackend
        return GoogleBackend()

    raise ValueError(
        f"Unknown provider {upstream!r} in model_id {model_id!r}. "
        f"Known: {', '.join(sorted(upstreams()))}."
    )


__all__ = ["SessionBackend", "BackendResponse", "resolve_backend", "UPSTREAMS",
           "upstreams", "claude_cli_upstreams", "cli_provider_rows", "cli_model_supported",
           "runs_claude_code", "forget_rows"]
