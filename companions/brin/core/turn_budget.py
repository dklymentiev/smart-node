"""Outer waits follow the largest turn limit in the durable registry.

The gateway enforces the effective per-agent profile. Brin and the Runner wait
above the largest configured value so an outer timeout cannot silently end a
longer turn first. A registry edit takes effect at the next turn start.
"""

import os
import httpx
import logging

from core.service_proof import ServiceProofMissing, service_headers

# brin waits this much longer than the gateway, and the runner this much longer
# than brin. Big enough that a slow answer is not mistaken for a dead one, small
# enough that a caller does not hold a connection long after the turn is over.
GATEWAY_MARGIN = 30.0
RUNNER_MARGIN = 60.0
_last_ceiling: float | None = None
_log = logging.getLogger(__name__)


async def refresh_turn_ceiling() -> float:
    """Read the registry without blocking the event loop, once at turn start.

    A core-api restart must not make a running node forget the last bound.
    With no successful read yet, fail rather than inventing a shorter bound.
    """
    global _last_ceiling
    base = (os.environ.get("SN_CORE_API_URL") or os.environ.get("CORE_API_URL") or "").rstrip("/")
    if not base:
        if _last_ceiling is None:
            raise RuntimeError("core-api URL missing: cannot read turn limit")
        return _last_ceiling
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(base + "/api/limits/turn-bound",
                                        headers=service_headers())
            response.raise_for_status()
            value = float(response.json()["seconds"])
        if value <= 0:
            raise ValueError("invalid turn limit in registry")
        _last_ceiling = value
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ServiceProofMissing) as exc:
        if _last_ceiling is None:
            raise RuntimeError("cannot read turn limit from registry") from exc
        _log.warning("turn limit refresh failed; retaining last registered bound: %s", exc)
    return _last_ceiling


def turn_ceiling() -> float:
    """Last registered ceiling, with no I/O on the caller's event loop."""
    if _last_ceiling is None:
        raise RuntimeError("turn limit has not been loaded")
    return _last_ceiling


def gateway_call_timeout() -> float:
    """How long brin waits on a gateway call that runs a turn."""
    return turn_ceiling() + GATEWAY_MARGIN


def runner_call_timeout() -> float:
    """How long the runner waits on the wake it sends to brin."""
    return turn_ceiling() + RUNNER_MARGIN
