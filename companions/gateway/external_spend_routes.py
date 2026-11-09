"""Budgets for the callers that do not hold a gateway session.

The limits story rests on one sentence: the gateway is the only door to a model,
so a budget at the gateway is a budget on everything. That sentence was false.
Voice talks to Gemini directly and keeps its own arithmetic, so on 2026-09-19 a
node whose budgets were all exhausted still answered by voice and still spent,
and the day's figure was wrong by however much the owner had talked.

This is the smaller of the two ways to close that. The larger one is to make
voice hold a real gateway session; it is the right end state and it means the
gateway carrying a realtime audio stream. Until then a caller that spends
outside may not stay invisible: it asks here before it starts and reports here
when it ends, and the same budget rows see it.

Two endpoints, deliberately narrow:

    GET  /api/v1/spend/allowance?agent=<slug>
    POST /api/v1/spend/external   {agent, cost_usd, source, estimated, detail}

What this does NOT do is make an outside caller's arithmetic trustworthy. Voice
prices its own audio from byte counts, so what it reports is an estimate; it
says so, the event says so, and nothing here pretends otherwise. An estimate in
the budget is worth more than a silence, and less than a measurement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import budget
import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/spend", tags=["external-spend"])

_DEFAULT_TENANT = 1


def _agent_id(slug: str) -> Optional[int]:
    """The numeric id budgets are scoped by, or None for an unknown agent.

    Unknown is not an error here: an outside caller may be speaking for nobody
    in particular, and its spend still belongs to the node's total.
    """
    rows = db.query(
        "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
        (slug,), fetch=True, as_dict=True,
    ) or []
    return rows[0]["id"] if rows else None


class ExternalSpend(BaseModel):
    agent: Optional[str] = Field(default=None, max_length=64)
    cost_usd: float = Field(gt=0)
    source: str = Field(min_length=2, max_length=32)
    estimated: bool = True
    detail: Optional[Dict[str, Any]] = None


@router.get("/allowance")
def spend_allowance(agent: Optional[str] = Query(default=None, max_length=64)):
    """May this caller start something that will spend?

    The same question the gateway asks itself before a turn, answered for a
    caller that is about to spend elsewhere. It returns the budget status too,
    so the caller can say WHY it is refusing rather than going quiet.
    """
    agent_id = _agent_id(agent) if agent else None
    allowed, reason, status = budget.check_budget(
        tenant_id=_DEFAULT_TENANT, agent_id=agent_id, metadata={},
    )
    return {"allowed": allowed, "reason": reason, "agent": agent,
            "agent_id": agent_id, "budgets": status}


@router.post("/external")
def record_external_spend(body: ExternalSpend):
    """Put spend that happened outside the gateway onto the same budget rows."""
    agent_id = _agent_id(body.agent) if body.agent else None
    if body.agent and agent_id is None:
        raise HTTPException(404, f"no such agent: {body.agent}")

    metadata = {"external_source": body.source, "estimated": body.estimated}
    if body.detail:
        metadata["detail"] = body.detail
    try:
        warnings = budget.record_spend(
            tenant_id=_DEFAULT_TENANT, cost_usd=body.cost_usd,
            agent_id=agent_id, metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 -- the caller has already spent the
        # money; losing the report is bad, but it must not look like a refusal.
        logger.exception("recording external spend from %s failed", body.source)
        raise HTTPException(503, f"could not record spend: {exc}") from exc

    _, _, status = budget.check_budget(
        tenant_id=_DEFAULT_TENANT, agent_id=agent_id, metadata={},
    )
    return {"recorded": True, "agent": body.agent, "agent_id": agent_id,
            "cost_usd": body.cost_usd, "estimated": body.estimated,
            "warnings": warnings or [], "budgets": status}
