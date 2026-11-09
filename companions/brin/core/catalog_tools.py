"""Catalog-driven tools for brin -- Phase 2 (tools from the DB, executed in brin).

The DB catalog (sn_core.tools + sn_config.agent_tools grants) is the single
source of an agent's tools. core-api surfaces a runtime-ready view:

    GET {CORE_API_URL}/api/agents/{slug}/tools
      -> {"tools": [{name, kind, title, description, input_schema,
                     http_method, http_path, usage_md, endpoint}, ...]}

CatalogTools turns those grants into OpenAI function specs and dispatches the
calls the model makes. brin owns the loop + execution; the gateway only relays
the model turn (egress). There is no hardcoded tool list -- change a grant in
the DB and the agent's tools change with no code edit. This replaces the dead
Agent(ToolRegistry) path with its compiled-in v2tools.

Dispatch by kind:
  api     -> HTTP {http_method} {endpoint}{http_path}, JSON body = params,
             header Remote-User = <agent slug> (owner-gated + audited downstream).
  builtin -> 'bash' runs a shell command locally; 'fetch' GETs/POSTs a public
             URL. 'fs' is not wired: it is not offered to the model and
             is reported as deferred, rather than offered and then refused.
  mcp     -> a granted mcp row is a SERVER grant (name == server slug). brin
             lists that server's tools once (cached) via mcp_client and offers
             each as a namespaced spec '{slug}__{tool}'; a call is dispatched to
             the server with tools/call. Server URLs come from core-api
             GET /api/mcp/servers. No empty-schema rows are ever offered.
  cli     -> handled by the CLI runtime (.mcp.json), not this transparent loop;
             dispatching one returns a loud unsupported error (never a fake ok).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import urllib.parse
from typing import Optional

import httpx

from core import mcp_client

logger = logging.getLogger("brin.catalog_tools")

CORE_API_URL = os.environ.get("CORE_API_URL", "")

# An api tool calls its endpoint AS the agent. For core-api that name alone is
# refused over TCP: a header anyone on the trusted network can type is
# not an identity. brin proves itself the way ledger_service and voice already
# do: the shared secret says which service is calling, on-behalf-of
# names the agent, and core-api checks the pair against SN_SERVICE_AGENTS.
# Without it every core-api tool that asks who is calling answers 403, and the
# model reads that as "nothing there".
#
# ONLY for rows whose integration is core-api. The secret is a node credential
# and must never reach a third-party endpoint an api tool happens to point at.
from core.service_proof import service_headers as _service_headers

SERVICE_SECRET = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
SERVICE_NAME = "brin"
_CORE_API_INTEGRATION = "core-api"


def _is_core_api(row: dict) -> bool:
    """Is this tool call going to core-api?

    Asked of the ADDRESS, not of the integration's name. Both are facts on the
    row, but only one of them is the thing that matters: core-api refuses an
    agent identity over TCP unless the caller proves it is a permitted service,
    and whether it needs that proof depends on where the request lands.

    Keying it on the slug instead cost a whole class of agent its tools. The
    `planner` integration points at http://127.0.0.1:8030/api/planner, which IS
    core-api behind its planner proxy, but its slug is "planner", so no
    delegation header was attached and every planner call an HTTP-model agent
    made came back 403 -- read by the model as an empty queue.
    The slug is kept as a second way in, for a row whose endpoint is unset.
    """
    if row.get("integration_slug") == _CORE_API_INTEGRATION:
        return True
    endpoint = (row.get("endpoint") or "").strip()
    if not endpoint or not CORE_API_URL:
        return False
    try:
        from urllib.parse import urlsplit
        here, there = urlsplit(CORE_API_URL), urlsplit(endpoint)
        return (there.scheme, there.netloc) == (here.scheme, here.netloc)
    except Exception:  # noqa: BLE001 -- a malformed row must not stop a turn
        return False


def _api_headers(row: dict, agent_slug: str) -> dict:
    """Headers for one api tool call made for `agent_slug`.

    Remote-User is always sent, as before, so a node without the secret fails
    exactly as it did rather than in a new way.
    """
    h = {"Remote-User": agent_slug}
    if SERVICE_SECRET and _is_core_api(row):
        h["X-SN-Service-Auth"] = SERVICE_SECRET
        h["X-SN-Service"] = SERVICE_NAME
        h["X-SN-On-Behalf-Of"] = agent_slug
    return h

# Kinds dispatched directly from the row (api over HTTP, builtin locally).
_DIRECT_KINDS = {"api", "builtin"}
# The builtins brin can actually run. Another granted builtin is not offered to
# the model: on 2026-09-18 an HTTP agent was offered fetch, called it, and got
# "[UNSUPPORTED]" back for nine rounds during a production run.
_WIRED_BUILTINS = {"bash", "fetch"}

# Bash guardrails (mirror executor._execute_command).
_DANGEROUS = ["rm -rf", "mkfs", "dd if=/dev/", "> /etc/", "> /root/"]
_MAX_OUTPUT = 8000

# A path parameter in an api row's http_path: /api/v1/tasks/{task_id}/comments.
_PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def fill_path(path: str, params: dict) -> tuple[str, dict, list[str]]:
    """Put the named parameters into the path and take them out of the rest.

    Without this a row could only name a route with no id in it: the literal
    '{task_id}' went out on the wire, so reading one task, its comments,
    commenting and moving it were out of reach for an agent on an HTTP model
    while a CLI colleague had all four. Each value is URL-quoted whole,
    so a value cannot add a path segment. Returns (path, remaining params,
    names that were required and missing).
    """
    rest = dict(params or {})
    missing: list[str] = []
    used: set[str] = set()

    def _one(m: "re.Match") -> str:
        name = m.group(1)
        # Read, do not remove: a name may appear twice in one path
        # (/tasks/{task_id}/children/{task_id}), and popping on the first
        # occurrence reported the second as missing although it was supplied.
        value = rest.get(name)
        if value is None or value == "":
            if name not in missing:
                missing.append(name)
            return m.group(0)
        used.add(name)
        return urllib.parse.quote(str(value), safe="")

    filled = _PATH_PARAM.sub(_one, path or "")
    for name in used:
        rest.pop(name, None)
    return filled, rest, missing


def refused_params(schema: dict, params: dict) -> list[str]:
    """Argument names this row does not accept, when its schema is closed.

    An api row's input_schema was only ever ADVERTISED to the model (see
    openai_specs); nothing checked what came back. A row can therefore be
    named and described for one narrow job while its endpoint accepts far
    more: planner_advance says "move a task to a new status" and is a PATCH
    of the whole task, so `assignee` or `priority` emitted beside `status`
    reached the planner and were applied (review of the merge review). A row that sets
    "additionalProperties": false now means it: anything undeclared is
    refused here, before the request is built, and the refusal names the
    fields it dropped and the ones the row takes.

    Open schemas are untouched, so no existing row changes behaviour: a row
    opts in by closing its schema.
    """
    if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    return [k for k in (params or {}) if k not in props]


class CatalogTools:
    """An agent's granted tools, loaded from the catalog and ready to dispatch."""

    def __init__(self, agent_slug: str, rows: list[dict], servers: dict[str, str]):
        self.agent_slug = agent_slug
        self.rows = {r["name"]: r for r in rows}
        self.servers = servers  # mcp server slug -> sse url
        # Filled by _expand_mcp(): spec name -> (server_url, real_tool_name)
        self._mcp_dispatch: dict[str, tuple[str, str]] = {}
        self._mcp_specs: list[dict] = []
        self._mcp_failed: list[str] = []

    @classmethod
    async def load(cls, agent_slug: str,
                   core_api: str = CORE_API_URL) -> "CatalogTools":
        """Fetch the agent's grants + MCP server coords from core-api."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{core_api}/api/agents/{agent_slug}/tools",
                                  headers=_service_headers())
            r.raise_for_status()
            rows = r.json().get("tools", []) or []
        servers: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{core_api}/api/mcp/servers",
                                     headers=_service_headers())
                if r.status_code == 200:
                    for s in r.json().get("servers", []) or []:
                        if s.get("enabled") and (s.get("config") or {}).get("url"):
                            servers[s["slug"]] = s["config"]["url"]
        except Exception as e:
            logger.warning("mcp servers fetch failed: %s", e)
        logger.info("Loaded %d catalog tool(s) for agent=%s (%d mcp server(s) known)",
                    len(rows), agent_slug, len(servers))
        ct = cls(agent_slug, rows, servers)
        await ct._expand_mcp()
        return ct

    async def _expand_mcp(self) -> None:
        """Expand each granted mcp server-row into per-operation specs (cached)."""
        for name, r in self.rows.items():
            if r.get("kind") != "mcp":
                continue
            url = self.servers.get(name)
            if not url:
                logger.warning("mcp grant %r has no enabled server url; skipped", name)
                self._mcp_failed.append(name)
                continue
            try:
                tools = await mcp_client.list_tools(url)
            except Exception as e:
                logger.warning("mcp list_tools failed for %s (%s): %s", name, url, e)
                self._mcp_failed.append(name)
                continue
            for t in tools:
                spec_name = f"{name}__{t['name']}"
                schema = t.get("input_schema") or {}
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                self._mcp_specs.append({
                    "type": "function",
                    "function": {
                        "name": spec_name,
                        "description": t.get("description") or spec_name,
                        "parameters": schema,
                    },
                })
                self._mcp_dispatch[spec_name] = (url, t["name"])
            logger.info("mcp server %s expanded to %d tool spec(s)", name, len(tools))

    def openai_specs(self) -> list[dict]:
        """OpenAI function specs for everything brin can dispatch: api/builtin
        rows (with an object schema) + expanded mcp operations."""
        specs = []
        for name, r in self.rows.items():
            if r.get("kind") not in _DIRECT_KINDS:
                continue
            if r.get("kind") == "builtin" and name not in _WIRED_BUILTINS:
                continue
            schema = r.get("input_schema") or {}
            if not isinstance(schema, dict) or schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            specs.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": r.get("description") or r.get("title") or name,
                    "parameters": schema,
                },
            })
        specs.extend(self._mcp_specs)
        return specs

    def deferred_names(self) -> list[str]:
        """Granted tools brin still cannot dispatch (cli, or mcp that failed to list)."""
        out = [n for n, r in self.rows.items()
               if (r.get("kind") not in _DIRECT_KINDS and r.get("kind") != "mcp")
               or (r.get("kind") == "builtin" and n not in _WIRED_BUILTINS)]
        out.extend(self._mcp_failed)
        return out

    async def dispatch(self, name: str, params: dict) -> str:
        """Execute one tool call. Always returns a string (errors included)."""
        if name in self._mcp_dispatch:
            url, real = self._mcp_dispatch[name]
            try:
                return await mcp_client.call_tool(url, real, params or {})
            except Exception as e:
                logger.warning("mcp call %s failed: %s", name, e)
                return f"[ERROR] mcp tool {name} failed: {e}"

        r = self.rows.get(name)
        if not r:
            return f"[ERROR] tool {name!r} is not granted to agent {self.agent_slug!r}"
        kind = r.get("kind")
        if kind == "api":
            return await self._dispatch_api(r, params)
        if kind == "builtin":
            return await self._dispatch_builtin(name, params)
        return (f"[UNSUPPORTED] tool {name!r} is kind={kind!r}; brin dispatches "
                f"api/builtin/mcp. cli tools run in the CLI runtime. Not executed.")

    async def _dispatch_api(self, r: dict, params: dict) -> str:
        endpoint = r.get("endpoint")
        if not endpoint:
            return (f"[ERROR] api tool {r['name']!r} has no integration endpoint "
                    f"(integration row missing or endpoint unset)")
        method = (r.get("http_method") or "POST").upper()
        schema = r.get("input_schema") or {}
        extra = refused_params(schema, params)
        if extra:
            takes = ", ".join(sorted((schema.get("properties") or {}).keys()))
            return (f"[REFUSED] {r['name']} does not take {', '.join(sorted(extra))}. "
                    f"It takes: {takes}. Nothing was sent.")
        path, params, missing = fill_path(r.get("http_path") or "", params)
        if missing:
            return (f"[ERROR] {r['name']} needs {', '.join(missing)}: it is part of "
                    f"the address, pass it as an argument")
        url = endpoint.rstrip("/") + path
        # A GET carries its arguments in the query string: sent as a JSON body
        # they are ignored, and messages_inbox returned the default (full) view.
        query = params if method == "GET" else None
        try:
            async with httpx.AsyncClient(timeout=130.0) as client:
                resp = await client.request(
                    method, url, params=query,
                    json=None if method == "GET" else params,
                    headers=_api_headers(r, self.agent_slug),
                )
        except Exception as e:
            logger.warning("api dispatch %s failed: %s", r["name"], e)
            return f"[ERROR] {r['name']} request failed: {e}"
        body = resp.text or ""
        if len(body) > _MAX_OUTPUT:
            body = body[:_MAX_OUTPUT] + f"\n[...truncated, total {len(body)} chars]"
        return f"[HTTP {resp.status_code}] {body}"

    async def _dispatch_builtin(self, name: str, params: dict) -> str:
        if name == "fetch":
            return await _fetch_public(params)
        if name != "bash":
            return (f"[UNSUPPORTED] builtin {name!r} is not wired in brin yet "
                    f"(bash and fetch are). Not executed.")
        cmd = params.get("command") or params.get("cmd") or ""
        if not cmd:
            return "[ERROR] bash requires a 'command' parameter"
        if any(d in cmd for d in _DANGEROUS):
            return f"[BLOCKED] command appears dangerous: {cmd}"
        try:
            result = await asyncio.to_thread(
                subprocess.run, cmd, shell=True, capture_output=True,
                text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "[TIMEOUT] command took too long"
        except Exception as e:
            return f"[ERROR] bash failed: {e}"
        out = (result.stdout if result.returncode == 0 else result.stderr) or ""
        out = out.strip() or "[no output]"
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + f"\n[...truncated, total {len(out)} chars]"
        return out


_FETCH_TIMEOUT = 20.0


def _public_host(host: str) -> str | None:
    """None when every address `host` resolves to is public, else the reason.

    fetch runs inside brin, on the node, next to core-api, the gateway, the
    database and the LXD bridge. "Public URLs" in the tool's description is only
    true if a model cannot name 127.0.0.1, 10.x or a node hostname and read what
    answers there, so the resolved addresses are checked, not the spelling.
    """
    import ipaddress
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"cannot resolve {host!r}: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global:
            return f"{host!r} resolves to non-public address {ip}"
    return None


async def _fetch_public(params: dict) -> str:
    """The `fetch` builtin: one GET or POST to a public http(s) URL.

    Redirects are not followed: a public URL that redirects to an internal one
    would otherwise walk past the address check. The Location is returned so
    the model can fetch it itself, through the same check.
    """
    from urllib.parse import urlsplit
    url = (params or {}).get("url") or ""
    method = ((params or {}).get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return f"[ERROR] fetch supports GET and POST, not {method}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "[ERROR] fetch requires an absolute http(s) 'url'"
    reason = await asyncio.to_thread(_public_host, parts.hostname)
    if reason:
        return f"[BLOCKED] fetch is for public URLs only: {reason}"
    body = (params or {}).get("body")
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
            resp = await client.request(
                method, url,
                content=body if isinstance(body, str) else None,
                json=body if isinstance(body, (dict, list)) else None,
            )
    except Exception as e:
        return f"[ERROR] fetch {url} failed: {e}"
    if resp.is_redirect:
        return f"[HTTP {resp.status_code}] redirect to {resp.headers.get('location', '?')} (not followed)"
    text = resp.text or ""
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT] + f"\n[...truncated, total {len(text)} chars]"
    return f"[HTTP {resp.status_code}] {text}"


def normalise_tool_calls(raw_calls) -> list[dict]:
    """OpenAI tool_calls -> brin's [{id, name, params}] shape."""
    out = []
    for tc in raw_calls or []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function", {}) or {}
        name = func.get("name") or tc.get("name", "")
        try:
            params = json.loads(func.get("arguments", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            params = tc.get("params", tc.get("input", {})) or {}
        if not isinstance(params, dict):
            params = {}
        out.append({"id": tc.get("id", ""), "name": name, "params": params})
    return out
