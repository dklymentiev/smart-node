#!/usr/bin/env python3
"""The node secrets store as an MCP server (stdio) -- resolve for everyone
granted, set and grant for the node's own sysadmin and foreman.

Gives agents with the `secrets: r--` grant AND an exact/namespace entry in
`secret_refs` a way to resolve a secret ref at runtime, without exposing
delete. Identity is NOT a parameter: the
caller is taken from SN_AGENT_SLUG (set by the gateway from the agent folder
name, falling back to the working directory name). The model can therefore
neither forget nor spoof who it is resolving as.

The two write tools (set_secret, grant_secret_ref) exist so the agents whose
job it is to administer the node (capability sysadmin / configurator, role
admin) can put a credential into the store and hand a ref to a colleague
without a human operator in the loop. They carry no policy of their own:
core-api admits a POST /api/secrets only from an approved writer capability
or an operator on the socket (routes/secrets.py), and a PUT on another
agent's grants only from a role holding agents.grants.write. Every other
caller gets 403 and the refusal is audited. The tools are offered to every
agent with the `secrets` grant; the API decides who may use which.

WHY THE SOCKET AND NOT THE ADDRESS. core-api answers on both doors. On the
TCP door an agent identity over docker0 is now refused outright for /resolve
when that agent has no secrets grant. On the socket SO_PEERCRED names the
caller, which the caller never sends and therefore cannot forge. This server
is spawned per agent session as that agent's OS user, so every call
arrives on the socket under the correct uid and peer_identity() returns the
right slug.

The grant check lives in routes/secrets.py: mcp_secrets.py makes the call
and the API enforces the policy. A grant check in both places would leave
the route open to any docker0 caller and add a second, duplicable guard in
the wrong layer. One check, in the right layer.

Transport: stdio, spawned per agent session by the CLI runtime. Registered
once per node by companions/core-api.sh under MCP-server slug `secrets`;
an agent gets it by being granted `secrets: r--`, which renders it into that
agent's .mcp.json.

Usage:
    python mcp_secrets.py        # stdio (the only transport)
"""

import json
import os
from pathlib import Path
from typing import Optional

import httpx

# The MCP SDK renamed its high-level server between majors and nodes are not
# all on the same one. Bind whichever this node has installed.
try:  # SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

CORE_API_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
# A test-bed fallback only. Deliberately NOT a default: over the TCP door an
# agent identity with no secrets grant is now refused, so a fallback would
# produce a 403 the model reads as "secret not found", which is the exact
# failure mode that made the old gaps invisible.
CORE_API_URL = os.environ.get("SN_CORE_API_URL", "")
TIMEOUT = float(os.environ.get("SN_SECRETS_TIMEOUT", "15"))

BASE_PATH = "/api/secrets"
AGENTS_PATH = "/api/agents"

mcp = _McpServer("smart-node-secrets")


def _me() -> str:
    """The calling agent's slug, from the runtime -- never from the model."""
    slug = (os.environ.get("SN_AGENT_SLUG") or "").strip()
    if not slug:
        slug = Path.cwd().name
    return slug


async def _request(method: str, path: str, params: Optional[dict] = None,
                   body: Optional[dict] = None):
    """One call to core-api. Returns (status, parsed-or-text); status None
    when there is no door at all."""
    # Production commonly exports both addresses. The socket must win: TCP can
    # carry only a caller-asserted agent name and /resolve correctly refuses it.
    if os.path.exists(CORE_API_SOCKET):
        transport, base = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET), "http://localhost"
    elif CORE_API_URL:
        transport, base = None, CORE_API_URL.rstrip("/")
    else:
        return None, (f"{CORE_API_SOCKET} does not exist. Secrets are reached "
                      f"over the unix socket only; SN_CORE_API_URL is for test "
                      f"beds without a socket.")
    kwargs = {"headers": {"Remote-User": _me(), "Content-Type": "application/json"},
              "params": params}
    if body is not None:
        kwargs["json"] = body
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.request(method, f"{base}{path}", **kwargs)
    if r.status_code >= 400:
        return r.status_code, r.text[:400]
    return r.status_code, (r.json() if r.content else {})


def _render(status, payload) -> str:
    if status is None:
        return json.dumps({"error": "no core-api socket", "detail": payload}, ensure_ascii=False)
    if status >= 400:
        return json.dumps({"error": f"HTTP {status}", "detail": payload}, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


async def _call(method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None) -> str:
    return _render(*await _request(method, f"{BASE_PATH}{path}", params=params, body=body))


@mcp.tool()
async def resolve_secret(ref: str) -> str:
    """Return the plaintext value of a secret by its dotted-path ref.

    The caller must hold `secrets: r--` and this ref (or its `namespace.*`) in
    grants.json `secret_refs`. Both are checked server-side; a 403 means the
    agent is not permitted to read it and the refusal is recorded.

    This node passes secrets by reference. Agents should resolve a value at
    the moment they need it, use it inline, and not store it. Never log or
    include a resolved value in a message or memory entry.

    Args:
        ref: the dotted path, e.g. 'gitlab.token' or 'replicate.api_key'.
    """
    return await _call("GET", "/resolve", params={"ref": ref})


@mcp.tool()
async def set_secret(ref: str, value: str) -> str:
    """Store or replace one secret in the node store. Sysadmin and foreman only.

    core-api admits the write from an agent whose capability is an approved
    secrets writer (sysadmin, configurator) or from an operator on the socket;
    any other caller gets 403 and the refusal is audited. A replaced value is
    snapshotted by the store for 30 days.

    Storing does not grant. An agent that needs the value must also hold the
    ref in its allowlist: call grant_secret_ref next. Never echo the value
    into a message, a log, memory, or a document.

    Args:
        ref: dotted path, e.g. 'google.gemini_api_key' (letters, digits, _ -,
             up to six segments).
        value: the plaintext credential.
    """
    return await _call("POST", "", body={"ref": ref, "value": value})


@mcp.tool()
async def grant_secret_ref(agent: str, ref: str) -> str:
    """Let another agent resolve one secret ref (or a `namespace.*`).

    Merges `ref` into that agent's secret_refs allowlist and gives it the
    read-only `secrets` tool if it has none; nothing else in its grants
    changes. core-api gates this on agents.grants.write (role admin: the
    node's sysadmin and foreman); others get 403 and the refusal is audited.
    The ref must already be in the store (set_secret), or the grant points at
    nothing. The target agent picks the grant up on its next session.

    Args:
        agent: the colleague's slug, e.g. 'dev'.
        ref: exact ref 'google.gemini_api_key' or namespace 'google.*'.
    """
    slug = agent.strip().lower()
    status, current = await _request("GET", f"{AGENTS_PATH}/{slug}/grants")
    if status is None or status >= 400:
        return _render(status, current)
    grants = (current or {}).get("grants") or {}
    body = {"grants": {"secrets": grants.get("secrets") or "r--"},
            "secret_refs": [ref]}
    status, out = await _request("PUT", f"{AGENTS_PATH}/{slug}/grants",
                                 params={"merge": "true"}, body=body)
    if status is None or status >= 400:
        return _render(status, out)
    return json.dumps({"ok": True, "agent": slug, "ref": ref,
                       "secret_refs": (out or {}).get("secret_refs")}, ensure_ascii=False)


if __name__ == "__main__":
    # SDK 1.x FastMCP.run() blocks and returns None; SDK 2.x returns a
    # coroutine. asyncio.run(None) used to raise at shutdown on 1.x nodes.
    import asyncio
    import inspect
    _r = mcp.run()
    if inspect.isawaitable(_r):
        asyncio.run(_r)
