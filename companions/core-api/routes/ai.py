"""AI Gateway status -- live read-side projection of the session-first gateway.

Replaces the static /data/ai.json snapshot. Everything here is read straight
from the database on request, scoped to the caller's tenant:

  * sessions / messages  -> sn_core.ai_sessions + sn_core.ai_session_messages
  * budgets              -> sn_config.budgets (pct/over computed)
  * providers            -> sn_config.model_pricing + sn_config.cli_providers

The gateway EGRESS itself is a separate service. Its health comes from the
gateway's own GET /api/v1/health, asked on every request with a short timeout:
summary.status is 'healthy', 'degraded' (a configured provider is not ok) or
'unreachable' (the gateway did not answer), and each provider's runtime_status is
what the gateway reports for it. The prompt-cache figures are the last 24 hours of
cache_read / cache_write / input tokens recorded in ai_sessions.usage.

Tenant scoping: a cross-tenant principal (super-admin) sees every tenant;
everyone else sees only their own tenant_id.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import Principal, get_principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"])

# How many recent messages to embed per session, and content truncation -- keeps
# the payload bounded for the 30s panel poll (matches the old ai.json contract).
MSG_PREVIEW_LIMIT = 30
MSG_CONTENT_TRUNC = 800

# Cap the embedded session list: the panel table shows only the most recent
# sessions, and embedding a 30-message preview for every row turned the 30s poll
# into a multi-MB / multi-second response once thousands of sessions piled up.
# Summary counts are computed by a separate aggregate so they stay truthful.
SESSION_LIST_LIMIT = 100

# The panel polls every 30 s; a stuck gateway must not stall that poll, so the
# health probe gets two seconds and a miss reads as 'unreachable', not an error.
GATEWAY_HEALTH_TIMEOUT = 2.0


def _gateway_health() -> dict | None:
    """The gateway's own health report, or None when it does not answer."""
    gw = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        with urllib.request.urlopen(gw + "/api/v1/health", timeout=GATEWAY_HEALTH_TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except Exception as exc:  # unreachable, timeout, bad JSON: all mean "no answer"
        log.warning("gateway health probe failed: %s", exc)
        return None


@router.get("/model-calls", response_model=dict,
            dependencies=[Depends(requires("ai.status.read"))])
def ai_model_calls(session_id: str | None = None, agent: str | None = None, limit: int = 200,
                   principal: Principal = Depends(get_principal)):
    """The model calls the gateway door passed, newest first: whose, which model,
    the tokens sent (new, read from the cache, written to it) and returned, how
    long the call took and how it ended. What a turn costs is here call by call:
    every step of an agent sends its whole context again. Tenant-scoped through
    the call's session; a call with no session is shown to a cross-tenant caller
    only."""
    limit = max(1, min(int(limit), 1000))
    where: list[str] = []
    params: list = []
    if not principal.cross_tenant:
        where.append("s.tenant_id = %s")
        params.append(principal.tenant_id)
    if session_id:
        where.append("mc.session_id = %s")
        params.append(session_id)
    if agent:
        where.append("mc.agent = %s")
        params.append(agent)
    sql = ("SELECT mc.ts, mc.agent, mc.model, mc.provider, mc.outcome, mc.status, mc.session_id, "
           "       mc.input_tokens, mc.cache_read, mc.cache_write, mc.output_tokens, "
           "       mc.duration_ms, mc.stop_reason, mc.tool_use "
           "FROM sn_core.model_calls mc "
           "LEFT JOIN sn_core.ai_sessions s ON s.id::text = mc.session_id "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY mc.ts DESC LIMIT %s")
    params.append(limit)
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    calls = []
    for r in rows:
        calls.append({
            "ts": r["ts"].isoformat() if r["ts"] else None,
            "agent": r["agent"], "model": r["model"], "provider": r["provider"],
            "outcome": r["outcome"], "status": r["status"], "session_id": r["session_id"],
            "input_tokens": r["input_tokens"], "cache_read": r["cache_read"],
            "cache_write": r["cache_write"], "output_tokens": r["output_tokens"],
            "duration_ms": r["duration_ms"], "stop_reason": r["stop_reason"],
            "tools": [t for t in (r["tool_use"] or []) if isinstance(t, str)],
        })
    return {"calls": calls, "limit": limit,
            "generated_at": datetime.now(timezone.utc).isoformat()}


@router.get("/status", response_model=dict,
            dependencies=[Depends(requires("ai.status.read"))])
def ai_status(principal: Principal = Depends(get_principal)):
    cross = principal.cross_tenant
    tid = principal.tenant_id

    sessions: list[dict] = []
    active_count = 0
    per_provider_active: dict[str, int] = {}

    with cursor() as cur:
        # ---- active-session counts (aggregate over ALL sessions) ----
        # Done as a single GROUP BY so the summary stays accurate no matter how
        # the embedded list below is capped -- never derived from the limited rows.
        active_sql = (
            "SELECT provider, count(*) AS n FROM sn_core.ai_sessions "
            "WHERE status IN ('active', 'streaming') "
        )
        if cross:
            cur.execute(active_sql + "GROUP BY provider")
        else:
            cur.execute(active_sql + "AND tenant_id = %s GROUP BY provider", (tid,))
        for r in cur.fetchall():
            per_provider_active[r["provider"]] = r["n"]
            active_count += r["n"]

        # ---- all sessions in scope: the table shows the newest SESSION_LIST_LIMIT
        # and says "100 of N", so the reader knows the list is cut ----
        if cross:
            cur.execute("SELECT count(*) AS n FROM sn_core.ai_sessions")
        else:
            cur.execute("SELECT count(*) AS n FROM sn_core.ai_sessions WHERE tenant_id = %s", (tid,))
        sessions_total = int(cur.fetchone()["n"])

        # ---- prompt cache, last 24 h ----
        # Share of input tokens served from the provider's prompt cache: hits are
        # cache_read tokens, the whole is read + write + uncached input.
        cache_sql = (
            "SELECT count(*) AS n, "
            "  COALESCE(sum(COALESCE((usage->>'cache_read_tokens')::numeric, 0)), 0) AS hit, "
            "  COALESCE(sum(COALESCE((usage->>'cache_read_tokens')::numeric, 0) "
            "             + COALESCE((usage->>'cache_write_tokens')::numeric, 0) "
            "             + COALESCE((usage->>'input_tokens')::numeric, 0)), 0) AS total "
            "FROM sn_core.ai_sessions "
            "WHERE created_at > now() - interval '24 hours' AND usage ? 'cache_read_tokens' "
        )
        if cross:
            cur.execute(cache_sql)
        else:
            cur.execute(cache_sql + "AND tenant_id = %s", (tid,))
        cache_row = cur.fetchone() or {}

        # ---- sessions (+ message count), most-recent first, capped ----
        session_sql = (
            "SELECT s.id, s.tenant_id, s.type, s.model, s.provider, s.is_opaque, "
            "       s.status, s.metadata, s.usage, s.created_at, s.last_activity_at, "
            "       (SELECT count(*) FROM sn_core.ai_session_messages m "
            "        WHERE m.session_id = s.id) AS message_count "
            "FROM sn_core.ai_sessions s "
        )
        if cross:
            cur.execute(session_sql + "ORDER BY s.last_activity_at DESC LIMIT %s",
                        (SESSION_LIST_LIMIT,))
        else:
            cur.execute(
                session_sql + "WHERE s.tenant_id = %s ORDER BY s.last_activity_at DESC LIMIT %s",
                (tid, SESSION_LIST_LIMIT),
            )
        session_rows = cur.fetchall()

        # Context resets are first-class timeline markers.  They are kept
        # separate from provider requests (a reset is lifecycle, not token
        # usage) so the panel chart can draw a discontinuity without pretending
        # it was another model call.  Fetched for the whole page in one
        # statement: per-session queries here meant 100 extra round-trips on a
        # panel that is polled.
        resets_by_session: dict[str, list] = {}
        if session_rows:
            cur.execute(
                "SELECT entity_id, ts, payload FROM sn_core.events "
                "WHERE event_type = 'session.context_reset' "
                "AND entity_type = 'ai_session' AND entity_id = ANY(%s) "
                "ORDER BY ts",
                ([str(r["id"]) for r in session_rows],),
            )
            for e in (cur.fetchall() or []):
                resets_by_session.setdefault(e["entity_id"], []).append(
                    {"ts": e["ts"], **(e["payload"] or {})}
                )

        for r in session_rows:
            cur.execute(
                "SELECT seq, role, content, created_at, usage "
                "FROM sn_core.ai_session_messages WHERE session_id = %s "
                "ORDER BY seq DESC LIMIT %s",
                (r["id"], MSG_PREVIEW_LIMIT),
            )
            mrows = cur.fetchall()
            mrows.reverse()  # back to chronological seq order for display

            messages = []
            for m in mrows:
                content = m["content"] or ""
                if len(content) > MSG_CONTENT_TRUNC:
                    content = content[:MSG_CONTENT_TRUNC] + "…"
                messages.append({
                    "seq": m["seq"],
                    "role": m["role"],
                    "content": content,
                    "created_at": m["created_at"],
                    "usage": m["usage"] or {},
                })

            context_resets = resets_by_session.get(str(r["id"]), [])

            meta = r["metadata"] or {}
            # The gateway keeps cumulative totals for budgets and a bounded
            # request-level ledger for the /ai chart. Expose the latter as a
            # separate field so consumers never have to guess which numbers
            # are deltas versus session totals.
            usage = r["usage"] or {}
            requests = usage.get("requests", []) if isinstance(usage, dict) else []
            if not isinstance(requests, list):
                requests = []
            sessions.append({
                "session_id": str(r["id"]),
                "service": meta.get("caller") or meta.get("specialist") or r["type"],
                "model": r["model"],
                "created_at": r["created_at"],
                "last_activity": r["last_activity_at"],
                "message_count": r["message_count"],
                "status": r["status"],
                "is_opaque": r["is_opaque"],
                "usage": usage,
                "requests": requests[-500:],
                "context_resets": context_resets,
                "messages": messages,
            })

        # ---- budgets ----
        if cross:
            cur.execute(
                "SELECT scope_type, scope_id, period, limit_usd, current_spend_usd, "
                "       period_reset_at, hard_limit FROM sn_config.budget_status "
                "WHERE enabled = true ORDER BY scope_type, scope_id"
            )
        else:
            # Non-cross callers see only their own tenant budget. Agent/meta-scoped
            # budgets aren't tenant-tagged in this table, so they stay hidden until
            # a tenant link exists (tracked as follow-up).
            cur.execute(
                "SELECT scope_type, scope_id, period, limit_usd, current_spend_usd, "
                "       period_reset_at, hard_limit FROM sn_config.budget_status "
                "WHERE enabled = true AND scope_type = 'tenant' AND scope_id = %s "
                "ORDER BY scope_id",
                (str(tid),),
            )
        budgets = []
        for b in cur.fetchall():
            limit = float(b["limit_usd"] or 0)
            spend = float(b["current_spend_usd"] or 0)
            pct = round(spend / limit * 100, 2) if limit > 0 else 0.0
            budgets.append({
                "scope_type": b["scope_type"],
                "scope_id": b["scope_id"],
                "period": b["period"],
                "limit_usd": limit,
                "current_spend_usd": spend,
                "pct": pct,
                "over": limit > 0 and spend > limit,
                "hard": b["hard_limit"],
                "period_reset_at": b["period_reset_at"],
            })

        # ---- providers (configured set; runtime health needs a live gateway) ----
        cur.execute(
            "SELECT provider, array_agg(DISTINCT model ORDER BY model) AS models "
            "FROM sn_config.model_pricing GROUP BY provider ORDER BY provider"
        )
        price_rows = cur.fetchall()
        cur.execute("SELECT slug, enabled, env_vars FROM sn_config.cli_providers")
        cli = {c["slug"]: c for c in cur.fetchall()}
        cur.execute("SELECT ref FROM sn_config.secrets")
        have_secrets = {r["ref"] for r in cur.fetchall()}
        # Human-readable name per provider slug. The slug stays the routing key
        # everywhere; the panel's card shows this on top with the slug as a
        # subtitle. A missing row keeps the slug as the name, so a newly plugged
        # backend is still named before this table knows about it.
        cur.execute("SELECT slug, display_name FROM sn_config.providers")
        display_name = {r["slug"]: r["display_name"] for r in cur.fetchall()}

    health = _gateway_health()
    gw_providers = (health or {}).get("providers") or {}

    providers = []
    for p in price_rows:
        pid = p["provider"]
        is_cli = pid in cli
        # CLI providers are 'configured' when every secret: ref in their env_vars is
        # present in the secret store (presence only -- no decryption, and not faked as
        # live 'ok' since there is no runtime health signal yet).
        runtime_status = "not_configured"
        if is_cli:
            refs = [v[len("secret:"):] for v in (cli[pid].get("env_vars") or {}).values()
                    if isinstance(v, str) and v.startswith("secret:")]
            if refs and all(r in have_secrets for r in refs):
                runtime_status = "configured"
        elif f"gateway/{pid}-key" in have_secrets:
            # API-key provider: a stored key (gateway/<id>-key) means configured.
            runtime_status = "configured"
        # The gateway's own word wins where it has one. It names every CLI
        # provider 'cli'; API providers by the same id as here.
        live = (gw_providers.get("cli" if is_cli else pid) or {}).get("status")
        if live:
            runtime_status = live
        providers.append({
            "id": pid,
            "name": display_name.get(pid, pid),
            "type": "cli" if is_cli else "api",
            "runtime_status": runtime_status,
            "models": p["models"] or [],
            "active_sessions": per_provider_active.get(pid, 0),
            "error": None,
        })

    # Only providers that are set up count: an unconfigured one is a choice, not
    # a fault, and "2 of 5" would read as three broken providers.
    configured = [p for p in providers if p["runtime_status"] != "not_configured"]
    providers_ok = sum(1 for p in configured if p["runtime_status"] == "ok")
    if health is None:
        status = "unreachable"
    elif health.get("status") == "healthy" and providers_ok == len(configured):
        status = "healthy"
    else:
        status = "degraded"

    hit = int(cache_row.get("hit") or 0)
    total = int(cache_row.get("total") or 0)
    summary = {
        "status": status,
        "active_sessions": active_count,
        "providers_ok": providers_ok,
        "providers_total": len(configured),
        # Prompt cache over the last 24 h, in input tokens; entries = sessions counted.
        "cache_hit_pct": round(100.0 * hit / total, 1) if total else None,
        "cache_entries": int(cache_row.get("n") or 0),
        "cache_hits": hit,
        "cache_lookups": total,
        "cache_window": "24h",
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "providers": providers,
        "budgets": budgets,
        "sessions": sessions,
        "sessions_total": sessions_total,
    }
