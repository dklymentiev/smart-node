"""SessionService -- DB-backed orchestration for AI gateway sessions.

Owns the persistence side of session-first gateway contract:
    * Create rows in sn_core.ai_sessions
    * Append rows in sn_core.ai_session_messages
    * Read history for replay/resume
    * Rollup usage onto session aggregate
    * Lifecycle status transitions: active -> streaming -> idle_released
      -> completed | failed | cancelled

Does NOT own backend runtime (subprocess, http calls). That's SessionBackend.
SessionService is the DB layer; SessionBackend is the wire layer; together
they implement the session-first contract.

Stage 2b-1: full DB CRUD shipped. Backend dispatch is provider-aware but
backends are mostly stubs (only ClaudeCliBackend exists, methods unimplemented
beyond create/close which are no-ops). Smoke tests exercise pure DB path.
"""
import json
import logging
import os
import uuid as _uuid
from typing import Optional, List, Dict, Any

from psycopg2.extras import Json

import db
import backends
from core_api_proof import service_headers

logger = logging.getLogger(__name__)


class SessionService:
    """Persistence + lifecycle for AI gateway sessions.

    Stateless: each call opens/closes its own DB connection via db.with_transaction
    or db.query helpers. Safe to use as a module-level singleton.
    """

    # -------------------------------------------------------------- creation

    def create_from_agent(self, agent_slug: str,
                          metadata: Optional[Dict[str, Any]] = None,
                          parent_session_id: Optional[str] = None) -> str:
        """Mode A creation: resolve model + system_prompt + tools from agent row.

        Returns:
            session_id (uuid string)

        Raises:
            ValueError: if agent_slug not found / archived / disabled.
        """
        # Minimal agent row -- we still need model/tenant/workspace/os_user
        # from DB. system_prompt comes from the resolver below.
        agent_rows = db.query(
            """SELECT a.id, a.model, a.tenant_id, a.mesh_workspace, a.os_user
                 FROM sn_config.agents a
                WHERE a.slug = %s AND a.archived_at IS NULL AND a.enabled = true""",
            (agent_slug,),
            fetch=True,
        )
        if not agent_rows:
            raise ValueError(f"agent slug {agent_slug!r} not found or disabled")

        agent_id, model, tenant_id, workspace, os_user = agent_rows[0]
        model = self._canonical_model(model)
        # v0 single-tenant: agents may have a NULL tenant_id; default to 1
        # to match the Mode B convention (create_adhoc / brin hardcode tenant 1).
        tenant_id = tenant_id if tenant_id is not None else 1

        # Resolver: GET /api/agents/{slug}/system-prompt assembles role
        # (specialist.body_md) + applicable policies + per-agent overrides
        # into a single markdown blob.
        # Fail-soft: if core-api unreachable, fall back to bare specialist body.
        system_prompt = None
        try:
            import httpx
            r = httpx.get(
                os.environ.get("CORE_API_URL", "") + f"/api/agents/{agent_slug}/system-prompt",
                headers=service_headers(),
                timeout=5.0,
            )
            r.raise_for_status()
            system_prompt = r.text
        except Exception as e:
            logger.warning("resolver failed for agent=%s (%s); falling back to specialist.body_md only",
                           agent_slug, e)
            fb_rows = db.query(
                """SELECT s.body_md
                     FROM sn_config.agents a
                     LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id
                    WHERE a.id = %s""",
                (agent_id,),
                fetch=True,
            )
            system_prompt = (fb_rows[0][0] if fb_rows else "") or ""

        provider = self._resolve_provider(model)
        is_opaque = (provider == "flint-cli" or provider.startswith("claude-cli")
                     or provider.startswith("codex-cli")
                     or provider.endswith("-cli"))

        meta = dict(metadata or {})
        meta.setdefault("agent_slug", agent_slug)
        if workspace:
            meta.setdefault("mesh_workspace", workspace)
        if os_user:
            meta.setdefault("os_user", os_user)
        # CLI backends spawn claude in this cwd so it reads the agent's
        # .mcp.json (rendered by core-api from the agent's MCP grants).
        agent_dir = os.path.join(
            os.environ.get("SN_AGENTS_ROOT", "/srv/agents"), agent_slug)
        if os.path.isdir(agent_dir):
            meta.setdefault("cwd", agent_dir)

        return self._insert_session(
            tenant_id=tenant_id,
            session_type=meta.pop("type", "chat"),
            model=model,
            provider=provider,
            is_opaque=is_opaque,
            agent_id=agent_id,
            system_prompt=system_prompt,
            parent_session_id=parent_session_id,
            metadata=meta,
        )

    def create_adhoc(self, *, tenant_id: int, session_type: str, model: str,
                     system_prompt: Optional[str] = None,
                     metadata: Optional[Dict[str, Any]] = None,
                     parent_session_id: Optional[str] = None) -> str:
        """Mode B creation: ad-hoc session not bound to a configured agent.

        For smoke tests, one-shot OpenAI-shape /v1/chat/completions, dev curls.
        """
        model = self._canonical_model(model)
        provider = self._resolve_provider(model)
        is_opaque = (provider == "flint-cli" or provider.startswith("claude-cli")
                     or provider.startswith("codex-cli")
                     or provider.endswith("-cli"))

        return self._insert_session(
            tenant_id=tenant_id,
            session_type=session_type,
            model=model,
            provider=provider,
            is_opaque=is_opaque,
            agent_id=None,
            system_prompt=system_prompt,
            parent_session_id=parent_session_id,
            metadata=metadata or {},
        )

    def _insert_session(self, *, tenant_id, session_type, model, provider,
                        is_opaque, agent_id, system_prompt, parent_session_id,
                        metadata) -> str:
        rows = db.query(
            """INSERT INTO sn_core.ai_sessions
                   (tenant_id, type, model, provider, is_opaque, agent_id,
                    system_prompt, parent_session_id, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (tenant_id, session_type, model, provider, is_opaque, agent_id,
             system_prompt, parent_session_id, Json(metadata)),
            fetch=True,
        )
        session_id = str(rows[0][0])
        logger.info("session.created id=%s model=%s provider=%s opaque=%s",
                    session_id, model, provider, is_opaque)
        return session_id

    @staticmethod
    def _canonical_model(model: str) -> str:
        """Return the backend id '<runtime>/<model>' for a model.

        now an agent's model already IS that id (claude-cli/sonnet,
        openrouter/openai/gpt-5: the catalog row's provider, a slash, its model),
        so a known runtime passes straight through.

        Legacy, one release, then removed: a bare
        catalog name, or an OpenRouter id written without its runtime, is looked
        up in the catalog, and a bare name no row carries is the Claude CLI.
        The migration rewrites every agent it can, so these are the agents it
        could not and callers written before the canonical shape.
        """
        # Only backends actually implemented in backends/__init__.py. Everything
        # else (e.g. "anthropic/..." catalog models) is looked up in the catalog
        # so it maps to its real backend (openrouter) rather than a missing one.
        known = ("claude-cli", "flint-cli", "openrouter", "gemini")
        if model and model.split("/", 1)[0] in known:
            return model  # already a backend id
        rows = db.query(
            """SELECT provider, model FROM sn_config.models
                WHERE model = %s AND enabled = true
                ORDER BY sort_order LIMIT 1""",
            (model,),
            fetch=True,
        )
        if rows:
            prov, mdl = rows[0]
            upstream = "claude-cli" if prov == "cli" else prov
            return f"{upstream}/{mdl}"
        # Fallback: a bare alias not in the catalog -> claude CLI.
        if "/" not in (model or ""):
            return f"claude-cli/{model}"
        return model

    @staticmethod
    def _resolve_provider(model: str) -> str:
        # The runtime is the first segment of the id. _canonical_model has
        # already given every model one; a bare name reaching here is a caller
        # that skipped it, and bare meant the Claude CLI.
        if "/" not in model:
            return "cli"
        return model.split("/", 1)[0]

    # ---------------------------------------------------------------- append

    def append_message(self, session_id: str, role: str, content: str,
                       content_type: str = "text",
                       tool_calls: Optional[List[Dict[str, Any]]] = None,
                       tool_call_id: Optional[str] = None,
                       is_injected: bool = False,
                       usage: Optional[Dict[str, Any]] = None,
                       is_partial: bool = False) -> int:
        """Append one message to ai_session_messages. Returns message_id.

        seq is auto-assigned (next monotonic per session). is_partial marks an
        answer cut short because a person stopped the turn.
        """
        rows = db.query(
            """INSERT INTO sn_core.ai_session_messages
                   (session_id, seq, role, content_type, content,
                    tool_calls, tool_call_id, is_injected, usage, is_partial)
               VALUES (%s,
                       COALESCE((SELECT max(seq)+1 FROM sn_core.ai_session_messages
                                  WHERE session_id = %s), 0),
                       %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (session_id, session_id, role, content_type, content,
             Json(tool_calls) if tool_calls else None,
             tool_call_id, is_injected,
             Json(usage) if usage else None, is_partial),
            fetch=True,
        )
        # Bump last_activity_at on the session
        db.query(
            "UPDATE sn_core.ai_sessions SET last_activity_at = now() WHERE id = %s",
            (session_id,),
        )
        return int(rows[0][0])

    def discard_message(self, session_id: str, message_id: int) -> None:
        """Drop a stored inbound message that was never delivered to a backend.

        One caller: a turn that queued behind a context handoff and found its
        session retired. The text is re-sent on the clean session, so
        leaving this row would show the person's message twice in a conversation
        that spans both sessions.
        """
        db.query(
            "DELETE FROM sn_core.ai_session_messages WHERE id = %s AND session_id = %s",
            (int(message_id), session_id),
        )

    # ---------------------------------------------------------------- usage

    def record_usage(self, session_id: str, input_tokens: int, output_tokens: int,
                     cost_usd: float, estimated: bool = False,
                     cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                     requests: Optional[list] = None) -> None:
        """Rollup per-call usage onto session aggregate.

        usage jsonb shape on session: cumulative fields plus a bounded `requests`
        list containing provider request deltas for the panel.

        The cache pair is provider-neutral and arrives already translated by the
        backend (see BackendResponse.usage). Rolled up like the others so a
        session's total input is input + cache_read + cache_write, which is what
        was billed -- input_tokens alone is only the uncached part, and reporting
        it as the whole is what made a $0.10 turn display as "8 in".

        NOTE for anyone adding a field: jsonb_build_object REBUILDS the object,
        so a key missing from this list is silently dropped on the next write.
        It is not a merge.
        """
        request_rows = requests if isinstance(requests, list) else []
        db.query(
            """UPDATE sn_core.ai_sessions s
                   SET usage = jsonb_build_object(
                       'input_tokens',       COALESCE((s.usage->>'input_tokens')::int, 0) + %s,
                       'output_tokens',      COALESCE((s.usage->>'output_tokens')::int, 0) + %s,
                       'cache_read_tokens',  COALESCE((s.usage->>'cache_read_tokens')::int, 0) + %s,
                       'cache_write_tokens', COALESCE((s.usage->>'cache_write_tokens')::int, 0) + %s,
                       'cost_usd',           COALESCE((s.usage->>'cost_usd')::numeric, 0) + %s,
                       'calls',              COALESCE((s.usage->>'calls')::int, 0) + 1,
                       'estimated',          COALESCE((s.usage->>'estimated')::boolean, false) OR %s,
                       'requests', CASE
                           WHEN jsonb_array_length(COALESCE(s.usage->'requests', '[]'::jsonb) || %s::jsonb) <= 500
                           THEN COALESCE(s.usage->'requests', '[]'::jsonb) || %s::jsonb
                           ELSE (SELECT jsonb_agg(e.value ORDER BY e.ord)
                                   FROM jsonb_array_elements(COALESCE(s.usage->'requests', '[]'::jsonb) || %s::jsonb)
                                        WITH ORDINALITY AS e(value, ord)
                                  WHERE e.ord > jsonb_array_length(COALESCE(s.usage->'requests', '[]'::jsonb) || %s::jsonb) - 500)
                       END
                   )
                 WHERE s.id = %s""",
            (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             cost_usd, estimated, json.dumps(request_rows), json.dumps(request_rows),
             json.dumps(request_rows), json.dumps(request_rows), session_id),
        )

    # ------------------------------------------------------------------ read

    def agent_state(self, agent_id: int) -> Optional[Dict[str, Any]]:
        """The agent an agent_id names: {slug, enabled}, or None when there is
        no such agent. Read at every turn: a disabled agent runs none."""
        rows = db.query("SELECT slug, enabled FROM sn_config.agents WHERE id = %s",
                        (agent_id,), fetch=True)
        if not rows:
            return None
        slug, enabled = rows[0]
        return {"slug": slug, "enabled": bool(enabled)}

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = db.query(
            """SELECT id, tenant_id, type, model, provider, is_opaque, agent_id,
                      status, system_prompt, parent_session_id, metadata, usage,
                      budget_key, created_at, last_activity_at, completed_at, error
                 FROM sn_core.ai_sessions
                WHERE id = %s""",
            (session_id,),
            fetch=True, as_dict=True,
        )
        return rows[0] if rows else None

    def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        return db.query(
            """SELECT id, seq, role, content_type, content, artifact_uri,
                      tool_calls, tool_call_id, is_partial, is_injected, usage,
                      created_at, completed_at
                 FROM sn_core.ai_session_messages
                WHERE session_id = %s
                ORDER BY seq""",
            (session_id,),
            fetch=True, as_dict=True,
        ) or []

    def list_sessions(self, tenant_id: int,
                      status: Optional[str] = None,
                      agent_id: Optional[int] = None,
                      conversation: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        clauses = ["tenant_id = %s"]
        args: list = [tenant_id]
        if status:
            clauses.append("status = %s")
            args.append(status)
        if agent_id:
            clauses.append("agent_id = %s")
            args.append(agent_id)
        if conversation:
            # durable desk-session reuse looks a desk's session up by its
            # metadata.conversation tag ('desk:<id>') so a dispatcher restart
            # reuses it instead of spawning a fresh, context-less session.
            clauses.append("metadata->>'conversation' = %s")
            args.append(conversation)
        args.append(limit)
        return db.query(
            f"""SELECT id, type, model, provider, is_opaque, status,
                       last_activity_at, completed_at, usage
                  FROM sn_core.ai_sessions
                 WHERE {' AND '.join(clauses)}
                 ORDER BY last_activity_at DESC
                 LIMIT %s""",
            tuple(args),
            fetch=True, as_dict=True,
        ) or []

    # ------------------------------------------------------------ lifecycle

    def set_status(self, session_id: str, status: str,
                   error: Optional[str] = None) -> None:
        """Transition session status. Sets completed_at for terminal states."""
        # "reaped" is typically written by the session-reaper directly via psql,
        # but if anything ever calls set_status("reaped"), completed_at should
        # be set. Keep this list in sync with _TERMINAL_STATUSES in
        # sessions_v2_routes.py and executor.py.
        terminal = status in ("completed", "failed", "cancelled", "reaped")
        db.query(
            """UPDATE sn_core.ai_sessions
                  SET status = %s,
                      error = %s,
                      completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE id = %s""",
            (status, error, terminal, session_id),
        )

    def close_session(self, session_id: str) -> None:
        self.set_status(session_id, "completed")

    def mark_idle_released(self, session_id: str) -> None:
        self.set_status(session_id, "idle_released")

    # --------------------------------------------------------------- backend

    def get_backend(self, session_id: str) -> backends.SessionBackend:
        """Resolve the backend instance for an existing session.

        Caches per-session instance not implemented in 2b-1 -- factory call
        per access. Stage 2c will add a session-id -> backend instance map.
        """
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"session {session_id} not found")
        return backends.resolve_backend(session["model"])


# Module-level singleton for convenience
service = SessionService()
