"""Best-effort persistence of /api/v1/chat turns to sn_core.ai_sessions +
sn_core.ai_session_messages.

Single LLM egress = single source of truth: every cloud chat completion that
flows through the gateway is recorded centrally here. Conversation history thus
lives at the gateway, keyed by a conversation session.

DESIGN: This logging is BEST-EFFORT. A DB-write failure must NEVER break the
chat response. The caller wraps log_chat_turn() in its own try/except as a
second safety net, but every public function here also swallows + logs its own
errors and returns None instead of raising.

Schema reference (inspected 2026-06-11):
  sn_core.ai_sessions
    id uuid PK (we supply a deterministic/derived uuid)
    tenant_id int NOT NULL FK -> sn_config.tenants(id)   [default tenant = 1]
    type text NOT NULL CHECK in (chat,image_gen,...)      [we write 'chat']
    model text NOT NULL
    provider text NOT NULL
    agent_id int NULL FK -> sn_config.agents(id)          [resolved from slug]
    status text NOT NULL default 'active'
    metadata jsonb NOT NULL default '{}'  [we stash client_session_id, user, service]
    usage jsonb NOT NULL default '{}'
    created_at / last_activity_at timestamptz default now()

  sn_core.ai_session_messages
    id bigint PK GENERATED ALWAYS AS IDENTITY  (DB assigns; do NOT supply)
    session_id uuid NOT NULL FK -> ai_sessions(id) ON DELETE CASCADE
    seq int NOT NULL  (per-session monotonic; UNIQUE(session_id, seq))
    role text NOT NULL CHECK in (user,assistant,system,operator,tool)
    content_type text NOT NULL  [we write 'text']
    content text NULL
    usage jsonb NULL
    created_at timestamptz default now()
"""
import logging
import uuid

import db
from psycopg2.extras import Json

logger = logging.getLogger("ai-gateway.chat_logger")

# Stable namespace for deriving a uuid from an arbitrary caller session string,
# so the same client session_id always maps to the same ai_sessions.id row.
_SESSION_NS = uuid.UUID("a3f0c2d4-1b2e-4c6a-9d8f-000000000001")

DEFAULT_TENANT_ID = 1  # sn_config.tenants slug='default'


def _coerce_session_uuid(client_session_id):
    """Map the caller's session_id (any string) to a stable uuid.

    If it already parses as a uuid, use it verbatim. Otherwise derive a
    deterministic uuid5 so repeated calls with the same string land on the
    same session row.
    """
    if not client_session_id:
        return uuid.uuid4(), None
    try:
        return uuid.UUID(str(client_session_id)), None
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(_SESSION_NS, str(client_session_id)), str(client_session_id)


def _resolve_agent_id(conn, agent_slug):
    """Look up sn_config.agents.id by slug. Returns int or None (never raises)."""
    if not agent_slug:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s ORDER BY id LIMIT 1",
            (agent_slug,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("agent slug resolve failed for %r: %s", agent_slug, e)
        return None


def normalise_usage(usage):
    """A stateless chat's provider usage in the canonical request shape.

    {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd}, where input_tokens is the UNCACHED part, so input + cache_read +
    cache_write is the whole prompt -- the sum the context meter shows.
    OpenAI-shaped providers (OpenRouter) and Gemini both count cached tokens
    INSIDE their prompt figure, so the cached part is taken out of it here.
    None when the provider reported nothing usable.
    """
    u = usage or {}
    def n(v):
        try:
            return max(int(v or 0), 0)
        except (TypeError, ValueError):
            return 0
    if "promptTokenCount" in u:                       # Google Gemini usageMetadata
        cached = n(u.get("cachedContentTokenCount"))
        prompt, out = n(u.get("promptTokenCount")), n(u.get("candidatesTokenCount"))
        cw = 0
    elif "prompt_tokens" in u:                        # OpenAI-compatible
        cached = n((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
        prompt, out = n(u.get("prompt_tokens")), n(u.get("completion_tokens"))
        cw = n((u.get("prompt_tokens_details") or {}).get("cache_write_tokens"))
    elif "input_tokens" in u:                         # already canonical
        return {"input_tokens": n(u.get("input_tokens")), "output_tokens": n(u.get("output_tokens")),
                "cache_read_tokens": n(u.get("cache_read_tokens")),
                "cache_write_tokens": n(u.get("cache_write_tokens")),
                "cost_usd": float(u.get("cost_usd") or u.get("cost") or 0)}
    else:
        return None
    cached = min(cached, prompt)
    cw = min(cw, prompt - cached)
    return {"input_tokens": prompt - cached - cw, "output_tokens": out,
            "cache_read_tokens": cached, "cache_write_tokens": cw,
            "cost_usd": float(u.get("cost_usd") or u.get("cost") or 0)}


def _last_user_message(messages):
    """Return (content_str) of the last role=='user' message, or None."""
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if isinstance(content, list):
                # vision / content-block messages: keep a text projection
                parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                return "\n".join(parts) if parts else None
            return content
    return None


def log_chat_turn(
    *,
    client_session_id,
    agent_slug,
    user,
    service,
    provider,
    model,
    messages,
    assistant_content,
    usage,
    raw_user_content=None,
    log_user=True,
):
    """Persist one chat turn (last user msg + assistant reply) atomically.

    raw_user_content, when provided, is stored as the user message content
    instead of the enriched text extracted from messages. The enriched form
    still goes to the model; only the stored row is affected.

    BEST-EFFORT: returns the effective session id (str) on success, or None on
    any failure. Never raises.
    """
    try:
        session_uuid, client_raw = _coerce_session_uuid(client_session_id)
        usage = usage or {}

        with db.with_transaction() as conn:
            agent_id = _resolve_agent_id(conn, agent_slug)
            cur = conn.cursor()

            # 1. Upsert the session row keyed by id.
            metadata = {
                "service": service,
                "user": user,
                "agent_slug": agent_slug,
            }
            if client_raw is not None:
                metadata["client_session_id"] = client_raw
                # The key every reader of a desk's session matches on (context
                # meter, /api/desks/{id}/sessions); a gateway v2 session carries
                # it, this path never did, so an HTTP agent's chat showed no
                # context at all.
                metadata["conversation"] = client_raw
            cur.execute(
                """
                INSERT INTO sn_core.ai_sessions
                    (id, tenant_id, type, model, provider, agent_id, status, metadata)
                VALUES (%s, %s, 'chat', %s, %s, %s, 'active', %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_activity_at = now(),
                    model            = EXCLUDED.model,
                    provider         = EXCLUDED.provider,
                    agent_id         = COALESCE(EXCLUDED.agent_id, sn_core.ai_sessions.agent_id),
                    metadata         = sn_core.ai_sessions.metadata || EXCLUDED.metadata
                """,
                (
                    str(session_uuid),
                    DEFAULT_TENANT_ID,
                    model,
                    provider,
                    agent_id,
                    Json(metadata),
                ),
            )

            # 2. Compute next seq for this session.
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM sn_core.ai_session_messages WHERE session_id = %s",
                (str(session_uuid),),
            )
            next_seq = (cur.fetchone()[0] or 0) + 1

            # 3. Insert the user turn (if present) then the assistant turn.
            # Prefer raw_user_content (the text as typed) over the enriched form
            # extracted from messages. The enriched form reaches the model; only
            # the stored row should reflect what the person actually wrote.
            user_content = raw_user_content if raw_user_content is not None else _last_user_message(messages)
            # A tool loop's later rounds re-send the same history; the person
            # wrote once, so the row is written once. The round's
            # assistant row below is still written: it carries that round's cost.
            if not log_user:
                user_content = None
            if user_content is not None:
                cur.execute(
                    """
                    INSERT INTO sn_core.ai_session_messages
                        (session_id, seq, role, content_type, content)
                    VALUES (%s, %s, 'user', 'text', %s)
                    """,
                    (str(session_uuid), next_seq, user_content),
                )
                next_seq += 1

            cur.execute(
                """
                INSERT INTO sn_core.ai_session_messages
                    (session_id, seq, role, content_type, content, usage)
                VALUES (%s, %s, 'assistant', 'text', %s, %s)
                """,
                (str(session_uuid), next_seq, assistant_content or "", Json(usage)),
            )

            # The same session ledger a gateway v2 session keeps:
            # cumulative tokens and cost plus one row per provider request, the
            # latest of which is what the context meter reads. Without
            # it an HTTP agent's context read 0 and its spend lived only on the
            # message rows.
            req = normalise_usage(usage)
            if req:
                cur.execute(
                    """
                    UPDATE sn_core.ai_sessions s
                       SET usage = COALESCE(s.usage, '{}'::jsonb) || jsonb_build_object(
                           'input_tokens',       COALESCE((s.usage->>'input_tokens')::int, 0) + %(in)s,
                           'output_tokens',      COALESCE((s.usage->>'output_tokens')::int, 0) + %(out)s,
                           'cache_read_tokens',  COALESCE((s.usage->>'cache_read_tokens')::int, 0) + %(cr)s,
                           'cache_write_tokens', COALESCE((s.usage->>'cache_write_tokens')::int, 0) + %(cw)s,
                           'cost_usd',           COALESCE((s.usage->>'cost_usd')::numeric, 0) + %(cost)s,
                           'calls',              COALESCE((s.usage->>'calls')::int, 0) + 1,
                           'requests', (SELECT COALESCE(jsonb_agg(e.value ORDER BY e.ord), '[]'::jsonb)
                                          FROM jsonb_array_elements(
                                               COALESCE(s.usage->'requests', '[]'::jsonb) || jsonb_build_array(%(req)s::jsonb))
                                               WITH ORDINALITY AS e(value, ord)
                                         WHERE e.ord > jsonb_array_length(
                                               COALESCE(s.usage->'requests', '[]'::jsonb)) + 1 - 500))
                     WHERE s.id = %(id)s
                    """,
                    {"in": req["input_tokens"], "out": req["output_tokens"],
                     "cr": req["cache_read_tokens"], "cw": req["cache_write_tokens"],
                     "cost": req["cost_usd"], "req": Json(req), "id": str(session_uuid)},
                )

        return str(session_uuid)
    except Exception as e:  # noqa: BLE001 -- best-effort: never break the chat
        logger.warning("chat persistence skipped (best-effort): %s", e)
        return None



def append_message(client_session_id, role, content):
    """Append ONE message (e.g. a verified tool-activity note) to a conversation.
    Used to record GROUND TRUTH the model cannot fake. Best-effort: id or None."""
    try:
        session_uuid, _ = _coerce_session_uuid(client_session_id)
        with db.with_transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sn_core.ai_sessions (id, tenant_id, type, model, provider, status) "
                "VALUES (%s, %s, 'chat', 'system', 'system', 'active') ON CONFLICT (id) DO NOTHING",
                (str(session_uuid), DEFAULT_TENANT_ID))
            cur.execute("SELECT COALESCE(MAX(seq),0) FROM sn_core.ai_session_messages WHERE session_id=%s",
                        (str(session_uuid),))
            seq = (cur.fetchone()[0] or 0) + 1
            cur.execute(
                "INSERT INTO sn_core.ai_session_messages (session_id, seq, role, content_type, content) "
                "VALUES (%s, %s, %s, 'text', %s)",
                (str(session_uuid), seq, role, content))
        return str(session_uuid)
    except Exception as e:  # noqa: BLE001
        logger.warning("append_message failed for %r: %s", client_session_id, e)
        return None


def load_conversation(client_session_id, limit=40):
    """Clean conversation history oldest-first, rebuilt into turns. Each turn is
    (user -> final assistant -> verified tool-activity note). The tool note is
    GROUND TRUTH written by brin (what it actually ran), surfaced as a system
    line so the model cannot claim actions it did not take. [] on error."""
    try:
        session_uuid, _ = _coerce_session_uuid(client_session_id)
        rows = db.query(
            "SELECT role, content FROM sn_core.ai_session_messages "
            "WHERE session_id = %s AND role IN ('user','assistant','tool') ORDER BY seq",
            (str(session_uuid),), fetch=True, as_dict=True) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("load_conversation failed for %r: %s", client_session_id, e)
        return []
    turns = []
    cur_user = None
    cur_asst = None
    cur_tool = None
    for r in rows:
        role = r["role"]
        content = (r["content"] or "").strip()
        if role == "user":
            if cur_user is not None and cur_user != content:
                turns.append((cur_user, cur_asst, cur_tool))
                cur_asst = None
                cur_tool = None
            cur_user = content
        elif role == "assistant" and content:
            cur_asst = content
        elif role == "tool" and content:
            cur_tool = content
    if cur_user is not None:
        turns.append((cur_user, cur_asst, cur_tool))
    out = []
    for u, a, t in turns[-limit:]:
        out.append({"role": "user", "content": u})
        if a:
            out.append({"role": "assistant", "content": a})
        if t:
            out.append({"role": "system", "content": "[VERIFIED] " + t})
    return out
