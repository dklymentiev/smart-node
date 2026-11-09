"""Health check with dependency probing for Brin."""

import asyncio
import os
import time

import httpx

GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:19850")
# Set by dispatcher.sh only when the memory module is installed. Without it
# there is nothing to ask: probing a default name waited out the 5 s timeout on
# every /health, longer than the installer's check waits (2026-10-30).
MESH_URL = os.environ.get("MESH_URL", "")
PROBE_TIMEOUT = 5.0


async def _probe(url: str) -> dict:
    """GET url, return {ok, latency_ms, error?}."""
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, verify=False) as client:
            r = await client.get(url)
            latency = round((time.time() - start) * 1000, 1)
            # Consider any non-5xx response as "reachable"
            return {"ok": r.status_code < 500, "latency_ms": latency}
    except Exception as exc:
        latency = round((time.time() - start) * 1000, 1)
        return {"ok": False, "latency_ms": latency, "error": str(exc)}


async def check_gateway() -> dict:
    return await _probe(f"{GATEWAY_URL}/health")


async def check_mesh() -> dict:
    if not MESH_URL:
        return {"ok": True, "installed": False}
    return await _probe(f"{MESH_URL}/health")


async def get_health(specialist_count: int, workflow_count: int) -> dict:
    """Run all dependency checks and return overall health."""
    gw, mesh = await asyncio.gather(check_gateway(), check_mesh())

    if gw["ok"]:
        overall = "ok" if mesh["ok"] else "degraded"
    else:
        overall = "unhealthy"

    return {
        "status": overall,
        "specialists": specialist_count,
        "workflows": workflow_count,
        "dependencies": {
            "ai_gateway": gw,
            "mesh": mesh,
        },
    }
