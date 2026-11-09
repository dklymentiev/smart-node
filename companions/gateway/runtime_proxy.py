"""The door hands the runtime what is the runtime's.

Agent processes are owned by the runtime (runtime_main.py, its own unit).
Callers keep the one address they had, the door's: everything under
`/api/v1/sessions` and a chat on the local CLI is forwarded to the runtime
as it arrived, the response streamed back. The door spawns nothing and holds
no process, so it can be restarted while agents work.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import RUNTIME_URL

logger = logging.getLogger("gateway.runtime_proxy")
router = APIRouter(tags=["runtime"])

_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
        "upgrade", "content-length"}
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # No read timeout: a turn on the CLI runs for as long as its limit
        # profile allows, and the runtime, not this hop, is what ends it.
        _client = httpx.AsyncClient(
            base_url=RUNTIME_URL,
            timeout=httpx.Timeout(None, connect=5.0),
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=16),
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _unavailable(e: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "detail": f"runtime unavailable at {RUNTIME_URL}: {type(e).__name__}"})


async def forward(request: Request, path: Optional[str] = None):
    """Pass the request to the runtime and stream its answer back. `path`
    overrides the request's own path (the door's /api/v1/chat sends a CLI chat
    to the runtime's /api/v1/chat)."""
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    client_ip = getattr(request.state, "client_ip", None)
    if client_ip:
        headers["x-forwarded-for"] = client_ip
    url = path if path is not None else request.url.path
    if request.url.query:
        url += "?" + request.url.query
    client = _get_client()
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=headers, content=body or None),
            stream=True)
    except httpx.HTTPError as e:
        logger.error("runtime unreachable (%s %s): %s", request.method, url, e)
        return _unavailable(e)
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}

    async def body_out():
        try:
            if upstream.is_stream_consumed:
                yield upstream.content
                return
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body_out(), status_code=upstream.status_code, headers=out_headers,
                             media_type=upstream.headers.get("content-type"))


@router.api_route("/api/v1/sessions", methods=["GET", "POST"])
@router.api_route("/api/v1/sessions/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def sessions(request: Request, path: str = ""):
    return await forward(request)


async def get_json(path: str, timeout: float = 3.0) -> Optional[dict]:
    """A small read of the runtime (its health, its session list). None when
    it does not answer, so the caller says so instead of failing."""
    try:
        r = await _get_client().get(path, timeout=timeout)
        return r.json() if r.status_code < 500 else None
    except Exception:  # noqa: BLE001
        return None
