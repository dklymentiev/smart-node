"""Agents — sn_config.agents CRUD + legacy grants endpoint.

CRUD (new in B2a):
    GET    /api/agents                       list from DB
    GET    /api/agents/{slug}[?body=true]    one (optionally with CLAUDE.md body)
    POST   /api/agents                       create + provision directory
    PUT    /api/agents/{slug}                update + optionally rewrite CLAUDE.md
    DELETE /api/agents/{slug}                soft-delete (archive directory)

Grants (kept as in B1.5 — filesystem JSON, not yet a DB table):
    GET    /api/agents/{slug}/grants
    PUT    /api/agents/{slug}/grants
"""

import json
import logging
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import psycopg2.errors
from psycopg2.extras import Json
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request
from pydantic import BaseModel, Field, field_validator

from auth import (Principal, get_actor, get_principal, get_provisioning_actor,
                  identity_strength)
from db import cursor
from permissions import requires
import datetime as _dt
from lib import owner_alert
from events_emit import emit
from lib.system_chat import tell_owner
from lib import agent_fs
from lib import default_grants
from lib.channel_routing import block_ambiguous_inbound
from schemas.agent import AgentCreate, AgentRead, AgentUpdate

log = logging.getLogger("core-api.agents")

import datetime as _dt
import os as _os
import ssl as _ssl
import time as _time
import urllib.error as _urlerr
import urllib.request as _urlreq

from lib.admin_runner import runner_op

# Mesh access for the agent-documents listing. Internal CA -> unverified
# context, same opt-out the Studio BFF uses for meshFetch.
MESH_API_URL = _os.environ.get("MESH_API_URL", "https://mesh.local").rstrip("/")
SN_AVATAR_STATE_DIR = Path(_os.environ.get(
    "SN_AVATAR_STATE_DIR", "/var/lib/smart-node/agents/avatars/custom",
))
# The picture packs the Studio serves as /avatars/<pack>/<file>.
AVATARS_DIR = _os.environ.get("SN_AVATARS_DIR", "/opt/smart-node/studio/public/avatars")
_MESH_CTX = _ssl.create_default_context()
_MESH_CTX.check_hostname = False
_MESH_CTX.verify_mode = _ssl.CERT_NONE


def _mesh_get(path: str, workspace: str):
    req = _urlreq.Request(MESH_API_URL + path,
                          headers={"X-Workspace": workspace, "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=8, context=_MESH_CTX) as r:
        return json.loads(r.read() or b"{}")


def _mesh_patch(path: str, workspace: str, payload: dict):
    req = _urlreq.Request(MESH_API_URL + path, data=json.dumps(payload).encode(), method="PATCH",
                          headers={"X-Workspace": workspace, "Content-Type": "application/json",
                                   "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=10, context=_MESH_CTX) as r:
        return json.loads(r.read() or b"{}")


def _mesh_add(workspace, content, tags=None, filename=None):
    """Create a durable mesh doc in `workspace` (PUT /). Returns the new guid or None."""
    body = {"content": content, "source": "api"}
    if tags:
        body["tags"] = tags
    if filename:
        body["filename"] = filename
    req = _urlreq.Request(MESH_API_URL + "/", data=json.dumps(body).encode(), method="PUT",
                          headers={"X-Workspace": workspace, "Content-Type": "application/json",
                                   "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=10, context=_MESH_CTX) as r:
        out = json.loads(r.read() or b"{}")
    return out.get("guid") or out.get("g") or (out.get("doc") or {}).get("guid")


def _mesh_delete(workspace, guid):
    """Delete a mesh doc by guid (DELETE /{guid})."""
    req = _urlreq.Request(MESH_API_URL + "/" + guid, method="DELETE",
                          headers={"X-Workspace": workspace, "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=10, context=_MESH_CTX) as r:
        return json.loads(r.read() or b"{}")


def _gdoc_export(url):
    """Export a Google Doc (by URL) to markdown via the node's connected account.
    No cache (unlike _pinned_content): a fresh durable copy for ingest-at-connect.
    Returns markdown, or None if the URL is not a Google doc. Raises on fetch error."""
    fid = _gdoc_fid(url)
    if not fid:
        return None
    from routes.google import _mint_access_token
    at = _mint_access_token()
    req = _urlreq.Request(
        "https://www.googleapis.com/drive/v3/files/%s/export?mimeType=text/markdown" % fid,
        headers={"Authorization": "Bearer " + at})
    with _urlreq.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


# Pinned-document content. A pinned doc's CONTENT is pre-loaded into
# the prompt (a snapshot), fetched by the node's connected Google account (not the
# agent's grant). Cached per guid so an unrelated recompose does not re-fetch every
# Drive doc; a global char budget caps total pinned size.
_PIN_CACHE = {}            # guid -> (fetched_epoch, markdown, snapshot_label)
_PIN_TTL = 300
MAX_PINNED_CHARS = 12000


def _gdoc_fid(url):
    for pat in (r"/document/d/([A-Za-z0-9_-]+)", r"/d/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"):
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    return None


def _pinned_content(guid, url):
    """A pinned Google doc's content as markdown via the node's connected account
    (core mints the token). Snapshot, cached per guid for _PIN_TTL. Returns
    (markdown, snapshot_label) or (None, None) on any failure (fail-soft)."""
    now = _time.time()
    c = _PIN_CACHE.get(guid)
    if c and now - c[0] < _PIN_TTL:
        return c[1], c[2]
    fid = _gdoc_fid(url)
    if not fid:
        return None, None
    try:
        from routes.google import _mint_access_token
        at = _mint_access_token()
        req = _urlreq.Request(
            "https://www.googleapis.com/drive/v3/files/%s/export?mimeType=text/markdown" % fid,
            headers={"Authorization": "Bearer " + at})
        with _urlreq.urlopen(req, timeout=20) as r:
            md = r.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("pinned content fetch failed for %s: %s", fid, e)
        return None, None
    snap = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(now))
    _PIN_CACHE[guid] = (now, md, snap)
    return md, snap


def _agent_documents(workspace, grants):
    """The agent's attached knowledge documents (its mesh workspace). Each is either
    PINNED (content pre-loaded -> returned with `content`+`snapshot`) or unpinned
    (returned with a GRANT-AWARE `how` read instruction). Fail-soft: returns [] on any
    error so a mesh hiccup never breaks compose. A pin tag is the string tag `pin`."""
    if not workspace:
        return []
    try:
        listing = _mesh_get("/browse?limit=100", workspace).get("docs") or []
    except Exception as e:
        log.warning("mesh browse failed for %s: %s", workspace, e)
        return []
    out = []
    pin_budget = MAX_PINNED_CHARS
    for d in listing:
        tags = d.get("t") or []
        # Only docs the owner attached as knowledge (linked Google docs / uploaded files),
        # not the agent's own memory writes. The Studio tags everything a person
        # attaches with via:ui (plus source:upload / source:google-drive). Any other
        # "source:" is the WRITER, not an attachment: mesh_add puts source:mcp on the
        # agent's own notes and the gateway writes source:gateway-handoff on every
        # context handoff, so a startswith("source:") test listed 24 handoffs as
        # "Documents you can read" in alex's prompt, growing with each handoff.
        if not any(str(x).startswith("via:ui") or str(x) in ("source:google-drive", "source:upload")
                   for x in tags):
            continue
        guid = d.get("g")
        try:
            ptr = (_mesh_get("/" + guid, workspace).get("content") or "") if guid else (d.get("p") or "")
        except Exception:
            ptr = d.get("p") or ""
        lines = [ln for ln in ptr.splitlines() if ln.strip()]
        title = (lines[0].strip() if lines else "Document")[:120]
        m = re.search(r"https?://\S+", ptr)
        url = m.group(0).rstrip(").,") if m else ""
        is_gdoc = ("docs.google.com" in url) or ("source:google-drive" in tags)
        is_pinned = "pin" in tags
        if is_pinned and pin_budget > 0:
            if is_gdoc:
                md, snap = _pinned_content(guid, url)
            else:
                # uploaded file / inline note: the content is already the mesh doc body.
                md, snap = (ptr.strip() or None), None
            if md:
                md = md[:pin_budget]
                pin_budget -= len(md)
                out.append({"title": title, "content": md, "snapshot": snap})
                continue
            # fetch failed -> fall through to the listing (do not break)
        # unpinned (or pin fetch failed / over budget): grant-aware read path
        if is_gdoc:
            how = ("read it live with gdrive_read %s" % url) if "google-drive" in grants \
                else "ask the owner for access (no Google Drive tool is granted to you)"
        elif "/docs/" in url:
            how = ("read it live with /srv/tools/doc/read.sh %s" % url) if "doc-read" in grants \
                else "ask the owner for access (no document tool is granted to you)"
        else:
            how = ('search your memory for "%s"' % title) if "mesh" in grants \
                else "ask the owner for access"
        out.append({"title": title, "how": how})
    return out

_PLANNER_API_URL = _os.environ.get("PLANNER_API_URL", "").rstrip("/")


def _register_planner_actor(slug: str, display_name: str) -> None:
    """Best-effort: register a new agent in the planner actors registry (the who-acts audit ledger;
    FK target for task assignee/requester/author). Without it the agent cannot be a task assignee and
    its actions are not attributable. Idempotent; never blocks agent creation."""
    try:
        body = json.dumps({"id": slug, "display_name": display_name or slug, "type": "agent"}).encode()
        headers = {"Content-Type": "application/json", "Remote-User": "admin"}
        # The planner requires a credential from 0.4.0. Without it this
        # call answers 401 and the agent silently cannot be a task assignee --
        # the failure this function already swallows by design, so it has to
        # carry the key rather than rely on the door being open.
        key = _os.environ.get("PLANNER_API_KEY", "").strip()
        if key:
            headers["X-API-Key"] = key
        req = _urlreq.Request(_PLANNER_API_URL + "/api/v1/actors", data=body, method="POST",
                              headers=headers)
        _urlreq.urlopen(req, timeout=5).read()
    except Exception as e:
        log.warning("planner actor registration failed for %s: %s", slug, e)


def _resolve_create_tenant(cur, tenant_id: int | None) -> int:
    """The tenant a new agent belongs to, never NULL.

    The panel and most callers send no tenant_id. Stored as NULL it reached the
    agent's identity row too, and auth.get_principal refuses a user with no
    tenant (403 "has no tenant assignment") on every call, so the agent could
    not read its own inbox. A node with one tenant has one answer; a node with
    several has no safe guess, so the caller must say which."""
    if tenant_id is not None:
        return tenant_id
    cur.execute("SELECT id FROM sn_config.tenants WHERE archived_at IS NULL ORDER BY id")
    ids = [r["id"] for r in cur.fetchall()]
    if len(ids) != 1:
        raise HTTPException(
            400,
            f"tenant_id is required: this node has {len(ids)} active tenants "
            "and an agent without one cannot be authorised for anything")
    return ids[0]


def _ensure_agent_identity(cur, slug: str, title: str | None, tenant_id: int | None) -> None:
    """Give the agent a node IDENTITY (sn_config.users row), not just an agents row.

    Every principal-gated endpoint resolves the caller's Remote-User against
    sn_config.users.email and rejects an unknown identity with 403 -- that is the
    authorization boundary (auth.get_principal). Internal messaging resolves the
    RECIPIENT the same way. So an agent with no user row can neither write to a
    colleague nor be written to, however its tools are granted. Older nodes had
    these rows seeded by hand; a configurator-built agent got none, which is why
    messaging failed on a fresh node. Idempotent, and in the caller's
    transaction -- identity and agent are created or fail together."""
    cur.execute("SELECT id, active FROM sn_config.users WHERE email = %s", (slug,))
    row = cur.fetchone()
    if row is not None:
        if not row["active"]:
            cur.execute(
                "UPDATE sn_config.users SET active = true, updated_at = now() WHERE id = %s",
                (row["id"],),
            )
    else:
        cur.execute(
            "INSERT INTO sn_config.users (tenant_id, email, name, role, active) "
            "VALUES (%s, %s, %s, 'agent', true)",
            (tenant_id, slug, title or slug),
        )

    # The actor, account projection and capability-derived elevated role must
    # become visible in the same transaction as the new agent. Absence of an
    # elevated capability row means the implicit `user` role.
    actor_id = f"agent:{slug}"
    cur.execute(
        "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
        "VALUES (%s, %s, 'agent', 'specialist', 'agent', 'internal') "
        "ON CONFLICT (id) DO NOTHING",
        (actor_id, title or slug),
    )
    cur.execute(
        "UPDATE sn_config.agents SET actor_id = %s WHERE slug = %s AND actor_id IS NULL",
        (actor_id, slug),
    )
    cur.execute(
        "UPDATE sn_config.users SET actor_id = %s WHERE email = %s AND actor_id IS NULL",
        (actor_id, slug),
    )
    cur.execute(
        "INSERT INTO sn_core.actor_roles (actor_id, role, source) "
        "SELECT %s, d.default_role, 'class_default' "
        "FROM sn_config.agents a JOIN sn_config.capability_default_role d "
        "ON d.capability = a.capability WHERE a.slug = %s "
        "AND d.default_role <> 'user' "
        "ON CONFLICT (actor_id) DO NOTHING",
        (actor_id, slug),
    )

# Single source of truth for the agents root (env-driven in agent_fs).
AGENTS_ROOT = agent_fs.AGENTS_ROOT

# the unattended wake cap. Money stops an agent that spends; this
# stops one that loops. They are different failures: 2026-09-14 cost $300 at
# ordinary wake counts, so the money cap answers that day, while nothing at all
# answered two agents writing to each other cheaply and forever.
#
# Counted per agent over rolling windows from the event journal (agent.woken).
# Chosen from this node's own history rather than from taste: per agent per hour
# average 3.4, p95 9, maximum 20; per day average 21, maximum 86. The defaults
# sit about 1.5x above the observed maximum, so ordinary work never meets them.
from lib.supervisor import resolve_profile, check_limit, record_event, notify_owner
# Same alphabet the tools registry uses (schemas/tool.py). It did NOT include
# the underscore, so a tool could be registered under a name that could never
# be granted to anyone -- it registered, and then silently reached no agent.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
MODE_RE = re.compile(r"^[r-][w-][d-]$")

router = APIRouter(prefix="/api/agents", tags=["agents"])

# The runtime of an agent's model: the first segment of <runtime>/<model>.
# A bare name is the pre-1.12.42 spelling and meant the Claude CLI; it is read that
# way for one release, the same legacy rule the gateway
# keeps, and then goes.
_RUNTIME_SQL = (
    "(CASE WHEN a.model = 'none' THEN NULL "
    "      WHEN position('/' IN a.model) = 0 THEN 'claude-cli' "
    "      ELSE split_part(a.model, '/', 1) END)"
)

# The api rows brin dispatches for an agent on an HTTP model. A CLI agent reaches
# the same planner routes through the tracking MCP server, so these rows are dead
# weight in its grant list (review of the merge review).
_PLANNER_API_ROWS = frozenset({
    "planner_task", "planner_comments", "planner_search",
    "planner_comment", "planner_advance",
})


def _runtime_kind(slug: str) -> str | None:
    """'cli', 'http', or None when the agent has no model or does not exist.

    Same rule as the roster's runtime_kind column: the provider half of the
    model is a cli runtime when an enabled sn_config.cli_providers row carries
    that slug, else the agent is served over HTTP.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT CASE WHEN a.model = 'none' THEN NULL "
            "            WHEN EXISTS (SELECT 1 FROM sn_config.cli_providers cp "
            f"                         WHERE cp.enabled AND cp.slug = {_RUNTIME_SQL}) "
            "            THEN 'cli' ELSE 'http' END AS kind "
            "FROM sn_config.agents a WHERE a.slug = %s AND a.archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
    return (row or {}).get("kind")

AGENT_COLS = (
    "a.id, a.slug, a.title, a.role, a.description, "
    "a.specialist_id, s.slug AS specialist_slug, s.body_md AS specialist_body, "
    "a.model, a.enabled, a.version, "
    f"{_RUNTIME_SQL} AS runtime, "
    "CASE WHEN a.model = 'none' THEN NULL "
    "     WHEN EXISTS (SELECT 1 FROM sn_config.cli_providers cp "
    f"                   WHERE cp.enabled AND cp.slug = {_RUNTIME_SQL}) THEN 'cli' "
    "     ELSE 'http' END AS runtime_kind, "
    "a.os_user, a.schedule_cron, a.os_groups, a.mesh_workspace, "
    # avatar: the column every reader already assumed was here. voice-ui builds
    # its picker from this roster and renders a.avatar, and the Studio crew list
    # reads the same model; the column exists in sn_config.agents and the files
    # are on disk. It was missing from three places at once -- this SELECT,
    # AgentRead and _row_to_read -- so adding it to any one of them changed
    # nothing, which is why it stayed a coloured circle.
    "a.avatar, "
    "a.fs_read, a.fs_write, a.forbidden_fs, a.forbidden_behavior, "
    "a.tenant_id, a.department_id, "
    "a.capability, "
    "a.created_at, a.updated_at, a.created_by, a.updated_by"
)


# ---------------------------------------------------------------------------
# Prompt composition (the DB -> CLAUDE.md projection)
# ---------------------------------------------------------------------------
# An agent's CLAUDE.md is not hand-written: it is the deterministic sum of
# registry blocks -- role (specialists.body_md), per-tool usage contracts
# (sn_core.tools.usage_md for granted tools), and policies. One edit to a shared
# block + recompose propagates to every agent that references it.

def _agent_tool_contracts(cur, agent_id: int) -> list[dict]:
    """Usage contracts for the tools granted to the agent (from agent_tools)."""
    cur.execute(
        "SELECT t.name, t.usage_md FROM sn_config.agent_tools at "
        "JOIN sn_core.tools t ON t.id = at.tool_id "
        "WHERE at.agent_id = %s ORDER BY t.name",
        (agent_id,),
    )
    return [{"name": r["name"], "usage_md": r["usage_md"]} for r in (cur.fetchall() or [])]


def _has_tool_grant(cur, agent_id: int, tool: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sn_config.agent_tools at JOIN sn_core.tools t ON t.id = at.tool_id "
        "WHERE at.agent_id = %s AND t.name = %s",
        (agent_id, tool),
    )
    return cur.fetchone() is not None


def _agent_policies(cur, agent_id: int, policy_categories) -> list[dict]:
    """Enabled policies for the agent: by category + per-agent include/override,
    minus excludes. Same selection as GET /{slug}/system-prompt."""
    cats = list(policy_categories or [])
    cur.execute(
        "SELECT policy_slug, action, override_body_md FROM sn_config.agent_policy_opts "
        "WHERE agent_id = %s",
        (agent_id,),
    )
    opts = {r["policy_slug"]: r for r in (cur.fetchall() or [])}
    forced_in = [s for s, o in opts.items() if o["action"] == "include"]
    forced_out = [s for s, o in opts.items() if o["action"] == "exclude"]
    cur.execute(
        "SELECT slug, title, severity, body_md FROM sn_config.policies "
        "WHERE enabled = true AND archived_at IS NULL "
        "AND (category = 'global' OR category = ANY(%s) OR slug = ANY(%s) OR agent_id = %s) "
        "AND slug <> ALL(%s) "
        "ORDER BY category, slug",
        (cats, forced_in, agent_id, forced_out),
    )
    out = []
    for p in (cur.fetchall() or []):
        o = opts.get(p["slug"])
        body = (o["override_body_md"]
                if o and o["action"] == "override" and o["override_body_md"]
                else p["body_md"])
        out.append({"title": p["title"], "body_md": body})
    return out


def _agent_workflows(cur, agent_id: int) -> list[dict]:
    """Flows granted to the agent (sn_config.agent_workflows -> sn_core.workflows).
    WHY: the grant existed but was never projected into the prompt, so the agent never
    knew it could run a flow. Surfaced into '# Workflows you can run' so the agent picks
    and starts its own flow. Only live ('available') flows are offered."""
    cur.execute(
        "SELECT w.name, w.title, w.description "
        "FROM sn_config.agent_workflows aw "
        "JOIN sn_core.workflows w ON w.id = aw.workflow_id "
        "WHERE aw.agent_id = %s AND w.status = 'available' "
        "ORDER BY w.name",
        (agent_id,),
    )
    return [{"name": r["name"], "title": r["title"], "description": r["description"]}
            for r in (cur.fetchall() or [])]


def _agent_contacts(cur, agent_id: int) -> list[dict]:
    """Contacts granted to the agent (sn_config.agent_contacts -> contacts).
    Only the channels the grant actually allows are listed, so the prompt never
    names a way to reach someone that the gate would then refuse. Deny-by-default
    means an agent with no grants gets no section and knows no one."""
    cur.execute(
        "SELECT c.display_name, c.kind, c.org_name, "
        "       array_agg(ch.slug || ' ' || cc.handle ORDER BY ch.slug, cc.handle) AS reach "
        "  FROM sn_config.agent_contacts g "
        "  JOIN sn_config.contacts c ON c.id = g.contact_id "
        "  JOIN sn_config.contact_channels cc "
        "    ON cc.contact_id = c.id AND cc.channel_id = ANY (g.allowed_channels) AND cc.enabled "
        "  JOIN sn_config.channels ch ON ch.id = cc.channel_id "
        " WHERE g.agent_id = %s AND c.enabled AND c.archived_at IS NULL "
        " GROUP BY c.id, c.display_name, c.kind, c.org_name "
        " ORDER BY c.display_name",
        (agent_id,),
    )
    return [{"display_name": r["display_name"], "kind": r["kind"],
             "org_name": r["org_name"], "reach": list(r["reach"] or [])}
            for r in (cur.fetchall() or [])]


def _agent_channels(cur, agent_id: int) -> list[dict]:
    """The agent's OWN channel bindings -- the addresses it sends and receives as.

    Kept separate from `_agent_contacts` on purpose: that answers "who may I
    write to", this answers "who am I". The prompt had no answer to the second,
    so an agent asked for its own address quoted whatever it had once read in a
    document -- which is how one kept naming a mailbox it had been moved off."""
    cur.execute(
        "SELECT ch.slug AS channel, ac.instance_ref, ac.enabled "
        "  FROM sn_config.agent_channels ac "
        "  JOIN sn_config.channels ch ON ch.id = ac.channel_id "
        " WHERE ac.agent_id = %s AND coalesce(ac.instance_ref, '') <> '' "
        " ORDER BY ch.slug",
        (agent_id,),
    )
    return [{"channel": r["channel"], "instance_ref": r["instance_ref"],
             "enabled": r["enabled"]}
            for r in (cur.fetchall() or [])]


def _agent_knowledge(cur, agent_id: int) -> list[dict]:
    """Enabled knowledge owned by the agent, in the order chosen in the UI.

    Text is stored inline. File and Drive rows point to a mesh snapshot; the
    Documents section supplies the grant-aware read path for those snapshots.
    """
    cur.execute(
        "SELECT kind, title, content FROM sn_config.agent_knowledge "
        "WHERE agent_id = %s AND enabled AND archived_at IS NULL "
        "ORDER BY sort_order, id",
        (agent_id,),
    )
    out = []
    for row in cur.fetchall() or []:
        content = (row.get("content") or "").strip()
        if not content and row.get("kind") in {"link", "doci", "file", "gdrive_file"}:
            content = ("Read the attached document in the 'Documents you can read' "
                       "section. If it is absent, ask the owner to reconnect it.")
        if content:
            out.append({"title": row["title"], "content": content})
    return out


def _compose_body(cur, slug: str) -> str | None:
    """Assemble an agent's prompt from the registries. Returns None if there is no
    such agent.

    THE one composition. It used to have a twin: GET /{slug}/system-prompt, which
    the gateway and brin fetch, assembled its own text from the same registries by
    different code -- and the two had drifted to different section sets, so an
    agent's instruction depended on which path woke it. The endpoint now renders
    this, so the file and the resolved prompt are the same text by construction
    rather than by anyone remembering to add a block in two places."""
    cur.execute(
        "SELECT a.id, a.title, a.policy_categories, a.mesh_workspace, "
        "       s.body_md AS spec_body, s.slug AS spec_slug "
        "FROM sn_config.agents a LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
        "WHERE a.slug = %s AND a.archived_at IS NULL",
        (slug,),
    )
    a = cur.fetchone()
    if a is None:
        return None
    # Grant-aware document listing: read the agent's granted tool slugs so the
    # listing only ever names a read path the agent actually holds.
    grant_set = set()
    try:
        gf = AGENTS_ROOT / slug / "grants.json"
        if gf.is_file():
            grant_set = set((json.loads(gf.read_text()).get("grants") or {}).keys())
    except Exception:
        pass
    body = agent_fs.compose_claude_md(
        a["spec_body"] or "",
        _agent_tool_contracts(cur, a["id"]),
        _agent_policies(cur, a["id"], a["policy_categories"]),
        knowledge=_agent_knowledge(cur, a["id"]),
        # Only an agent granted memory is told its workspace: the others were
        # told to pass workspace= to a memory tool they do not have. The grant
        # is read from sn_config.agent_tools, the table, not from grants.json.
        mesh_workspace=a["mesh_workspace"] if _has_tool_grant(cur, a["id"], "mesh") else None,
        people=agent_fs.people_directory(cur),
        team=_team_directory(cur, slug),
        documents=_agent_documents(a["mesh_workspace"], grant_set),
        workflows=_agent_workflows(cur, a["id"]),
        contacts=_agent_contacts(cur, a["id"]),
        channels=_agent_channels(cur, a["id"]),
        specialist_slug=a["spec_slug"],
        agent_name=a["title"],
    )
    return body


def _write_agent_mcp(cur, slug: str, grant_keys) -> None:
    """Render /srv/agents/<slug>/.mcp.json: the granted MCP servers plus the ones
    every agent runs (sn_config.mcp_servers.default_on). A reminder is as basic
    as answering in the chat, and an agent woken by an internal message needs
    inbox_reply to answer it, so messaging and reminders are marked default by
    the installer; who an agent may write to is still decided by core-api. The
    configurator also runs the `agents` server: building the team is its job, by
    its capability, whatever its name."""
    cur.execute(
        "SELECT slug, transport, config_jsonb AS config "
        "FROM sn_config.mcp_servers "
        "WHERE enabled = true AND (slug = ANY(%s) OR default_on OR (slug = 'agents' AND EXISTS "
        "  (SELECT 1 FROM sn_config.agents a WHERE a.slug = %s AND a.capability = 'configurator'))) "
        "ORDER BY slug",
        (sorted(set(grant_keys)), slug),
    )
    try:
        agent_fs.write_mcp_json(slug, cur.fetchall() or [])
    except FileNotFoundError:
        pass


def _grant_keys(slug: str) -> set:
    try:
        gf = AGENTS_ROOT / slug / "grants.json"
        if gf.is_file():
            return set((json.loads(gf.read_text()).get("grants") or {}).keys())
    except Exception:
        pass
    return set()


def _instructions_file(cur, slug: str) -> str:
    """The file the agent's runtime reads its instructions from. An HTTP agent
    has none of its own (brin reads the prompt from the database); its folder
    keeps CLAUDE.md, for a person to read."""
    cur.execute(
        f"SELECT cp.instructions_file FROM sn_config.agents a "
        f"JOIN sn_config.cli_providers cp ON cp.slug = {_RUNTIME_SQL} AND cp.enabled "
        "WHERE a.slug = %s AND a.archived_at IS NULL", (slug,))
    r = cur.fetchone()
    return (r or {}).get("instructions_file") or "CLAUDE.md"


def _compose_and_write_claude(cur, slug: str) -> bool:
    """Compose the agent's prompt and write it to /srv under the name its runtime
    reads (CLAUDE.md, FLINT.md, AGENTS.md), with its .mcp.json: an agent nobody
    granted anything still gets the base servers. This file is the ONLY way the
    agent's identity reaches a CLI runtime; the gateway passes none by flag.
    Returns False if the agent row or its directory is missing."""
    body = _compose_body(cur, slug)
    if body is None:
        return False
    _write_agent_mcp(cur, slug, _grant_keys(slug))
    name = _instructions_file(cur, slug)
    try:
        agent_fs.rewrite_claude(slug, body, name)
        return True
    except FileNotFoundError:
        log.warning("agent dir missing for %s; %s not composed", slug, name)
        return False


def _recompose_roster(cur, changed_slug: str, actor: str) -> None:
    """Project a roster change into every active agent's Team section once."""
    cur.execute(
        "SELECT slug FROM sn_config.agents "
        "WHERE archived_at IS NULL ORDER BY slug"
    )
    slugs = [r["slug"] for r in cur.fetchall() or []]
    for slug in slugs:
        _compose_and_write_claude(cur, slug)
    emit(cur, actor=actor, event_type="agent.roster_changed",
         entity_type="agent", entity_id=changed_slug,
         payload={"recomposed": len(slugs)})


def _sync_agent_tools(cur, agent_id: int, grants: dict) -> tuple[dict, dict]:
    """Project grants {tool: mode} into agent_tools rows -- the DB grant link that
    makes 'who has tool X' a single query (the source for fan-out).

    Return catalogue rows before and after, so a vanished projection is audited
    even if the grants.json entry did not change.
    """
    cur.execute("SELECT name, id FROM sn_core.tools")
    tool_id = {r["name"]: r["id"] for r in cur.fetchall()}
    unknown = sorted(set(grants) - set(tool_id))
    if unknown:
        raise HTTPException(400, f"unknown tool grant: {', '.join(unknown)}")
    cur.execute(
        "SELECT t.name, at.granted_caps FROM sn_config.agent_tools at "
        "JOIN sn_core.tools t ON t.id = at.tool_id WHERE at.agent_id=%s",
        (agent_id,),
    )
    before = {r["name"]: set(r["granted_caps"] or []) for r in cur.fetchall() or []}
    cur.execute("DELETE FROM sn_config.agent_tools WHERE agent_id = %s", (agent_id,))
    after = {}
    for name, mode in sorted(grants.items()):
        caps = [c for c, ch in zip(("read", "write", "delete"), mode) if ch not in ("-", "")]
        cur.execute(
            "INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps) "
            "VALUES (%s, %s, %s)",
            (agent_id, tool_id[name], caps),
        )
        after[name] = set(caps)
    return before, after


def _row_to_read(row, body_md: str | None = None) -> AgentRead:
    return AgentRead(
        id=row["id"], slug=row["slug"], title=row["title"], role=row["role"],
        description=row["description"],
        specialist_id=row["specialist_id"], specialist_slug=row["specialist_slug"],
        specialist_body=row.get("specialist_body"),
        model=row["model"], enabled=row["enabled"], version=row["version"],
        runtime=row.get("runtime"), runtime_kind=row.get("runtime_kind"),
        os_user=row["os_user"], schedule=row["schedule_cron"],
        os_groups=list(row["os_groups"] or []),
        mesh_workspace=row["mesh_workspace"],
        avatar=row.get("avatar"),
        capability=row.get("capability") or "",
        fs_read=list(row["fs_read"] or []), fs_write=list(row["fs_write"] or []),
        forbidden_fs=list(row["forbidden_fs"] or []),
        forbidden_behavior=list(row["forbidden_behavior"] or []),
        tenant_id=row["tenant_id"], department_id=row["department_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        created_by=row["created_by"], updated_by=row["updated_by"],
        body_md=body_md,
    )


def _perms_from_payload(payload) -> dict:
    """Collect the access/permission fields into the dict agent_fs expects."""
    return {
        "os_user": payload.os_user,
        "schedule": payload.schedule,
        "os_groups": payload.os_groups,
        "mesh_workspace": payload.mesh_workspace,
        "fs_read": payload.fs_read,
        "fs_write": payload.fs_write,
        "forbidden_fs": payload.forbidden_fs,
        "forbidden_behavior": payload.forbidden_behavior,
    }


def _perms_from_row(row) -> dict:
    return {
        "os_user": row["os_user"],
        "schedule": row["schedule_cron"],
        "os_groups": list(row["os_groups"] or []),
        "mesh_workspace": row["mesh_workspace"],
        "fs_read": list(row["fs_read"] or []),
        "fs_write": list(row["fs_write"] or []),
        "forbidden_fs": list(row["forbidden_fs"] or []),
        "forbidden_behavior": list(row["forbidden_behavior"] or []),
    }


# ---------------------------------------------------------- CRUD

@router.get("", dependencies=[Depends(requires("agents.read"))])
def list_agents(tenant_id: int | None = Query(default=None)):
    with cursor() as cur:
        if tenant_id is None:
            cur.execute(
                f"SELECT {AGENT_COLS} FROM sn_config.agents a "
                "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
                "WHERE a.archived_at IS NULL ORDER BY a.slug"
            )
        else:
            cur.execute(
                f"SELECT {AGENT_COLS} FROM sn_config.agents a "
                "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
                "WHERE a.archived_at IS NULL AND a.tenant_id IS NOT DISTINCT FROM %s "
                "ORDER BY a.slug",
                (tenant_id,),
            )
        rows = cur.fetchall()
    return {"agents": [_row_to_read(r).model_dump(mode="json") for r in rows]}


@router.get("/state", dependencies=[Depends(requires("agents.read"))])
def agents_state():
    """Live runtime state of every agent: free | busy | broken (+ current ref / lease / error).
    Source: sn_core.agent_runtime LEFT JOIN sn_config.agents. No runtime row reads 'free'.
    A busy row whose lease_until is in the past is reported with stale=true (crash, recoverable).
    Declared before /{slug} so 'state' is not captured as a slug."""
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT a.slug, a.title, "
            "       COALESCE(r.status,'free') AS status, "
            "       r.current_ref, r.lease_until, r.last_error, r.updated_at, "
            "       (r.status='busy' AND r.lease_until IS NOT NULL AND r.lease_until < now()) AS stale "
            "FROM sn_config.agents a "
            "LEFT JOIN sn_core.agent_runtime r ON r.agent_slug = a.slug "
            "WHERE a.enabled AND a.archived_at IS NULL "
            "ORDER BY a.slug"
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "slug": r["slug"], "title": r["title"], "status": r["status"],
            "stale": bool(r.get("stale")),
            "current_ref": r["current_ref"],
            "lease_until": r["lease_until"].isoformat() if r["lease_until"] else None,
            "last_error": r["last_error"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    counts = {"free": 0, "busy": 0, "broken": 0}
    for a in out:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    return {"agents": out, "count": len(out), "by_status": counts}


@router.get("/{slug}/queue", dependencies=[Depends(requires("agents.read"))])
def agent_queue(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """What is waiting for the agent, oldest first: each inbound message still
    open, what it is (a chat mention, a colleague's message, a reminder, a
    channel message), the chat it came from and whether a wake has handed it to
    the agent yet (seen). For the agent's card: a person sees why an answer is
    taking long, and what is in front of theirs."""
    with cursor() as cur:
        cur.execute(
            "SELECT m.id, m.sender, m.channel_type, m.received_at, ms.handed_at, "
            "       m.metadata->>'kind' AS kind, m.metadata->>'reason' AS reason, "
            "       m.metadata->>'source' AS source, d.id::text AS desk, d.title AS chat "
            "FROM sn_core.messages m "
            "JOIN sn_core.message_state ms ON ms.message_id = m.id "
            # a mention keeps its chat as desk_id, a turn typed in the agent's own
            # chat (web-chat) as desk
            "LEFT JOIN sn_core.desks d ON d.id::text = "
            "     COALESCE(m.metadata->>'desk_id', m.metadata->>'desk') "
            "WHERE m.direction = 'inbound' AND ms.status IN ('new', 'routed', 'awaiting_reply') "
            "  AND (m.recipient = %s OR m.recipient IN ("
            "       SELECT ac.instance_ref FROM sn_config.agent_channels ac "
            "       JOIN sn_config.agents a ON a.id = ac.agent_id "
            "       WHERE a.slug = %s AND ac.enabled AND ac.instance_ref <> '')) "
            "ORDER BY m.id LIMIT 50",
            (slug, slug),
        )
        rows = cur.fetchall()

    def what(r):
        if r["source"] == "reminder":
            return "reminder"
        if r["desk"]:
            return "chat"
        return "message" if r["channel_type"] in ("internal", "system") else r["channel_type"]

    items = [{"id": r["id"], "what": what(r), "from": r["sender"], "desk": r["desk"], "chat": r["chat"],
              "seen": r["handed_at"] is not None,
              "received_at": r["received_at"].isoformat() if r["received_at"] else None}
             for r in rows]
    return {"agent": slug, "waiting": items, "count": len(items)}


@router.get("/health", dependencies=[Depends(requires("agents.read"))])
def agents_health():
    """Per-agent health from sn_core.agent_health_checks (last cron probe run).

    Aggregate per agent:
      fail         any grant has status 'fail'
      ok           no fails and at least one 'ok'
      untestable   all grants are 'untestable' (renders grey in the panel)

    Declared before /{slug} so 'health' is not captured as a slug.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT agent_slug, grant_name, check_type, status, last_checked_at, error_detail "
            "FROM sn_core.agent_health_checks "
            "ORDER BY agent_slug, check_type, grant_name"
        )
        rows = cur.fetchall()

    by_agent: dict = {}
    for r in rows:
        slug = r["agent_slug"]
        if slug not in by_agent:
            by_agent[slug] = {"slug": slug, "grants": [], "aggregate": "untestable"}
        by_agent[slug]["grants"].append({
            "name": r["grant_name"],
            "type": r["check_type"],
            "status": r["status"],
            "last_checked_at": r["last_checked_at"].isoformat() if r["last_checked_at"] else None,
            "error_detail": r["error_detail"],
        })

    for data in by_agent.values():
        statuses = {g["status"] for g in data["grants"]}
        if "fail" in statuses:
            data["aggregate"] = "fail"
        elif "ok" in statuses:
            data["aggregate"] = "ok"
        # else all untestable: stays "untestable"

    return {
        "health": list(by_agent.values()),
        "count": len(by_agent),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


@router.post("/health/refresh")
def agents_health_refresh(principal: Principal = Depends(requires("agents.health.refresh"))):
    """Trigger an immediate health probe run outside the 15-minute cron schedule.

    Delegates to admin-runner run_health_probe (dangerous=False, no confirm needed).
    The probe is idempotent: it upserts sn_core.agent_health_checks and exits.
    Poll GET /api/agents/health after a few seconds to see the updated results.
    """
    result = runner_op(principal.username, "run_health_probe", {})
    return {
        "ok": True,
        "exit_code": result.get("exit_code"),
        "output": (result.get("stdout") or "")[-2000:],
    }


GATEWAY_URL = _os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


def _gateway(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
    """One call to the gateway (the door; the runtime answers behind it).
    Returns (status, json body). Raises urllib.error.URLError when the gateway
    does not answer at all; an HTTP error status is returned, not raised."""
    data = json.dumps(body).encode() if body is not None else None
    req = _urlreq.Request(GATEWAY_URL + path, data=data, method=method,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except _urlerr.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except ValueError:
            return e.code, {"detail": raw.decode(errors="replace")[:300]}


@router.get("/processes", dependencies=[Depends(requires("agents.read"))])
def agents_processes(agent: str | None = Query(None, description="agent slug")):
    """Every live agent process on the node, from the runtime's own process
    table (the gateway's /api/v1/sessions/processes): whose it is, whether a
    turn is running and what it does, how long idle, its last model call.

    This is what "is it working now" means: a process in this list is alive;
    an agent absent from it runs nothing, whatever its lease row says
    (/state). Declared before /{slug} so 'processes' is not captured as a slug.
    """
    path = "/api/v1/sessions/processes" + (f"?agent={agent}" if agent else "")
    try:
        status, body = _gateway("GET", path)
    except _urlerr.URLError as e:
        raise HTTPException(503, f"gateway not reachable at {GATEWAY_URL}: {e.reason}")
    if status != 200:
        raise HTTPException(502, f"gateway answered {status} for the process table")
    return body


class StopRequest(BaseModel):
    reason: str | None = None


@router.post("/{slug}/stop")
def agent_stop(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
               body: StopRequest | None = None,
               principal: Principal = Depends(requires("agents.stop"))):
    """Stop an agent: end its processes now and keep it from waking until it
    is enabled again (PUT /api/agents/{slug} {"enabled": true}).

    Two halves, in this order. First the agent is disabled, in its own
    transaction, so the runner wakes it no more and the gateway refuses every
    further turn on its sessions; this half holds even if the gateway is
    down. Then the runtime ends the agent's live processes: a turn in flight
    is interrupted, every process killed, sessions kept for --resume. The
    result of both halves is journalled as agent.stopped, honestly: a gateway
    that did not answer is reported, not assumed.
    """
    reason = (body.reason if body else None) or "stopped by owner"
    with cursor(commit=True) as cur:
        cur.execute("SELECT enabled FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "no such agent")
        was_enabled = bool(row["enabled"])
        cur.execute("UPDATE sn_config.agents SET enabled=false WHERE slug=%s", (slug,))
    actor = f"user:{principal.username}"
    gateway = "ok"
    out: dict = {}
    try:
        status, out = _gateway("POST", "/api/v1/sessions/agent-stop",
                               {"agent": slug, "requested_by": actor, "reason": reason})
        if status != 200:
            gateway, out = f"http {status}", {}
    except _urlerr.URLError as e:
        gateway, out = f"unreachable ({e.reason})", {}
    result = {
        "agent": slug, "enabled": False, "was_enabled": was_enabled,
        "interrupted": out.get("interrupted", []), "closed": out.get("closed", []),
        "gateway": gateway,
    }
    with cursor(commit=True) as cur:
        emit(cur, actor=actor, event_type="agent.stopped", entity_type="agent", entity_id=slug,
             payload={"reason": reason, **{k: v for k, v in result.items() if k != "agent"}})
    if gateway != "ok":
        log.warning("agent %s disabled, but its processes were not ended: gateway %s", slug, gateway)
    return {"ok": gateway == "ok", **result}


class RuntimeAction(BaseModel):
    action: str                 # claim | unhand | release | broken | budget_suspend
    ref: str | None = None
    error: str | None = None
    lease_seconds: int = 300
    # claim only. handed: the messages this wake is given; they become seen
    # (message_state.handed_at). scope: for a wake for one chat, the messages the
    # agent may answer while the claim stands, and no others.
    handed: list[int] | None = None
    scope: list[int] | None = None


@router.post("/{slug}/runtime", dependencies=[Depends(requires("agents.runtime.control"))])
def agent_runtime_action(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                         body: RuntimeAction):
    """Agent runtime status transitions. Atomic claim via INSERT..ON CONFLICT
    so a free/lease-expired agent is grabbed and a busy+valid-lease agent is not (returns
    claimed=false). release -> free; broken -> broken+error. Lease lets a crashed run recover."""
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        if body.action == "claim":
            # This is the Runner's final gate before launching a wake. Keep it
            # in the same transaction as claim_agent, so a pause races cleanly.
            cur.execute("SELECT node_paused FROM sn_config.node_control WHERE id=true FOR SHARE")
            if cur.fetchone()["node_paused"]:
                return {"claimed": False, "reason": "node_paused"}
            # The rule itself is sn_core.claim_agent(), and the reasoning for
            # every clause in it now lives beside the function in
            # bootstrap/B02-sn-core-schema.sql: the snapshot of the wake flag
            # and the self-healing claim of a 'broken' agent both
            # moved there unchanged.
            #
            # It moved into the database when the communication dispatcher had
            # to take the same lock. That service writes to this database
            # directly and has no URL for this API, so leaving the rule here
            # would have meant a second copy of it -- and two copies of "may
            # these run at the same time" is exactly how one question came to be
            # answered twice.
            cur.execute("SELECT sn_core.claim_agent(%s, %s, %s) AS claimed",
                        (slug, body.ref, body.lease_seconds))
            claimed = bool(cur.fetchone()["claimed"])
            if claimed:
                cur.execute("UPDATE sn_core.agent_runtime SET wake_scope = %s WHERE agent_slug = %s",
                            (Json({"ref": body.ref, "ids": body.scope}) if body.scope else None, slug))
                if body.handed:
                    cur.execute(
                        "UPDATE sn_core.message_state SET handed_at = now(), handed_ref = %s, "
                        "       handed_count = handed_count + 1 "
                        " WHERE message_id = ANY(%s) AND status = 'new'",
                        (body.ref, body.handed))
            return {"claimed": claimed}
        if body.action == "unhand":
            # The wake of this claim failed before the agent saw anything: what it
            # was handed is not "seen and left unread". Counted, four minutes of a
            # provider outage used up every reminder and the message was given up.
            cur.execute(
                "UPDATE sn_core.message_state SET handed_count = handed_count - 1, "
                "       handed_at = CASE WHEN handed_count <= 1 THEN NULL ELSE handed_at END "
                " WHERE handed_ref = %s AND handed_count > 0 AND status = 'new'",
                (body.ref,))
            return {"ok": True}
        if body.action == "release":
            # Same reasoning as claim: one implementation, in the database.
            # The honest report survives -- still_pending
            # says whether work arrived mid-run and the flag was therefore left
            # standing, rather than a flat ok.
            cur.execute("SELECT sn_core.release_agent(%s) AS still_pending", (slug,))
            return {"ok": True, "still_pending": bool(cur.fetchone()["still_pending"])}
        if body.action == "broken":
            cur.execute("INSERT INTO sn_core.agent_runtime (agent_slug,status,last_error,updated_at) "
                        "VALUES (%s,'broken',%s,now()) ON CONFLICT (agent_slug) DO UPDATE SET "
                        "status='broken', last_error=EXCLUDED.last_error, updated_at=now()",
                        (slug, body.error))
            # Nobody is asked for this agent's work any more, and nothing else says so.
            cur.execute("SELECT title, tenant_id FROM sn_config.agents WHERE slug = %s", (slug,))
            a = cur.fetchone()
            if a:
                tell_owner(cur, a["tenant_id"] or 1,
                           f"{a['title'] or slug} stopped working: the node could not wake it. "
                           f"[Open {a['title'] or slug}](/agents/{slug})", f"broken:{slug}")
            return {"ok": True}
        if body.action == "budget_suspend":
            # budget exhaustion is not a failure. Suspend the agent
            # until the budget period resets (body.ref = ISO timestamp of reset_at).
            # Release the claim (free up the slot) and record the suspension time.
            reset_at = body.ref  # ISO timestamp string from the runner
            cur.execute(
                "INSERT INTO sn_core.agent_runtime "
                "  (agent_slug, status, budget_suspended_until, current_ref, last_error, updated_at) "
                "VALUES (%s, 'free', %s::timestamptz, NULL, NULL, now()) "
                "ON CONFLICT (agent_slug) DO UPDATE SET "
                "  status='free', budget_suspended_until=EXCLUDED.budget_suspended_until, "
                "  current_ref=NULL, last_error=NULL, updated_at=now()",
                (slug, reset_at),
            )
            # atomically claim the exhaustion notification for any
            # hard-limit budget that is over its limit and has not yet been notified
            # this period. First runner to call wins; others get notification_claimed=false.
            cur.execute(
                "WITH exhausted AS ("
                " SELECT id, period, limit_usd, current_spend_usd, period_reset_at"
                " FROM sn_config.budget_status"
                " WHERE hard_limit AND current_spend_usd >= limit_usd"
                " AND notified_at IS NULL)"
                " UPDATE sn_config.budgets b SET notified_at=now()"
                " FROM exhausted e WHERE b.id=e.id"
                " RETURNING e.id, e.period, e.limit_usd,"
                " e.current_spend_usd, e.period_reset_at"
            )
            claimed = cur.fetchall() or []
            if claimed:
                first = claimed[0]
                reset_ts = first["period_reset_at"]
                return {
                    "ok": True,
                    "notification_claimed": True,
                    "period": first["period"],
                    "limit": float(first["limit_usd"]),
                    "spent": float(first["current_spend_usd"]),
                    "reset_at": reset_ts.isoformat() if reset_ts else reset_at,
                }
            return {"ok": True, "notification_claimed": False}
        raise HTTPException(400, "action must be claim|release|broken|budget_suspend")


class WakeRequest(BaseModel):
    source: str | None = None       # audit only -- recorded, never branched on
    reason: str | None = None


@router.post("/{slug}/wake", dependencies=[Depends(requires("agents.wake"))])
def agent_wake(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
               body: WakeRequest | None = None):
    """Raise this agent's wake flag: "there is work for you".

    AGNOSTIC by construction. It takes a subject and nothing about the caller --
    the planner, a channel adapter, a workflow, or something built next year all
    make the same call and get the same effect. What varies by source goes to the
    event journal as data; it never becomes a branch here. The bug this replaces
    was exactly such a branch: /api/notify resolved the assignee to a person's
    channels and, finding none for an agent, did nothing at all, so a task
    assigned to an agent woke nobody and only the per-agent cron ever noticed.

    IDEMPOTENT by construction. One mark per agent, not one row per event: ten
    assignments in a second raise one flag and produce one wake, not ten
    launches. This is why the flag is a column and not a doorbell message --
    the latter would make actions unattributable and is refused.

    Reports honestly whether it changed anything (already_pending), so a caller
    can tell "raised" from "was already raised" instead of reading ok=true for
    both. The Runner clears the flag when the run it caused completes.
    """
    src = (body.source if body else None) or "unknown"
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("SELECT slug FROM sn_config.agents "
                    "WHERE slug=%s AND enabled AND archived_at IS NULL", (slug,))
        if cur.fetchone() is None:
            raise HTTPException(404, "no such enabled agent")
        cur.execute(
            "WITH before AS ("
            "  SELECT wake_requested_at FROM sn_core.agent_runtime WHERE agent_slug=%(s)s"
            "), ins AS ("
            "  INSERT INTO sn_core.agent_runtime (agent_slug, status, wake_requested_at, updated_at) "
            "  VALUES (%(s)s, 'free', now(), now()) "
            "  ON CONFLICT (agent_slug) DO UPDATE SET wake_requested_at=now(), updated_at=now() "
            "  RETURNING wake_requested_at"
            ") "
            "SELECT (SELECT wake_requested_at FROM before) AS prev, "
            "       (SELECT wake_requested_at FROM ins) AS now_at",
            {"s": slug})
        row = cur.fetchone()
        already = row["prev"] is not None
        emit(cur, actor=f"component:{src}", event_type="agent.wake_requested",
             entity_type="agent", entity_id=slug,
             payload={"source": src, "reason": (body.reason if body else None),
                      "already_pending": already})
    return {"ok": True, "agent": slug, "already_pending": already,
            "wake_requested_at": row["now_at"].isoformat() if row["now_at"] else None}


@router.get("/with-pending-work", dependencies=[Depends(requires("agents.read"))])
def agents_with_pending_work():
    """Free agents that have pending inbound work on the bus. This is what the
    Runner polls ("work IS the signal"). Pending is either half:

      - inbound messages still 'new', resolved to the agent at query time:
        internal/system -> recipient is the slug; email/telegram -> recipient is
        the mailbox, owner via agent_channels;
      - a raised wake flag (agent_runtime.wake_requested_at), which is how work
        that does NOT live on this node's bus -- a planner task above all --
        reaches the Runner.

    Free = agent_runtime free, or busy with an expired lease. Declared before
    /{slug} so the path is not captured as a slug."""
    fast = [c.strip() for c in _os.environ.get("FAST_CHANNELS", "").split(",") if c.strip()]
    # commit=True because the wake cap writes when it trips: the hold, the event
    # and the owner alert. The read path itself writes nothing on a normal poll,
    # which is every poll but the one that catches a runaway.
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("SELECT node_paused FROM sn_config.node_control WHERE id=true")
        if cur.fetchone()["node_paused"]:
            return {"agents": [], "count": 0, "node_paused": True}
        block_ambiguous_inbound(cur)
        # Requests left unanswered are reminded here, before the query below
        # decides who is woken, so a reminder written now wakes its agent on
        # this same poll. Behind a savepoint and caught on purpose: every agent
        # on the node is woken through this function, and a fault in the
        # reminders must cost the reminders, never the waking. It is logged with
        # the traceback each time, so it cannot fail quietly.
        from routes.messages import sweep_reply_obligations
        cur.execute("SAVEPOINT reply_obligations")
        try:
            sweep_reply_obligations(cur)
            cur.execute("RELEASE SAVEPOINT reply_obligations")
        except Exception:
            log.exception("reply-obligation sweep failed; waking continues without it")
            cur.execute("ROLLBACK TO SAVEPOINT reply_obligations")
        cur.execute(
            "WITH pend AS ("
            # person: a person is waiting on it. A chat mention is internal mail,
            # but its writer waits in the chat, and with one chat per wake a busy
            # chat hour reached the 30-wake cap and the chats went silent (2026-10-29).
            "  SELECT a.slug AS agent, m.id AS mid, "
            # A reminder that came due wakes the agent for its chat in the same way,
            # and so does work passed down from a chat (origin): that chat's person's
            # clock, what it sends stays tied to the chat, no inbox to close it from.
            "         (m.metadata->>'kind' = 'chat' AND m.metadata->>'reason' = 'mentioned') AS person, "
            "         m.sender, m.content, "
            "         ((m.metadata->>'kind' = 'chat' AND m.metadata->>'reason' = 'mentioned') "
            "          OR m.metadata->>'source' = 'reminder' "
            "          OR left(m.metadata->'origin'->>'conversation', 5) = 'desk:') AS mention, "
            "         COALESCE(m.metadata->>'desk_id', substr(m.metadata->'origin'->>'conversation', 6)) AS desk, "
            "         ms.handed_at, ms.handed_count, ms.tags, "
            "         (SELECT u.email FROM sn_core.desks d JOIN sn_config.users u ON u.id = d.owner_user_id "
            "          WHERE d.id::text = COALESCE(m.metadata->>'desk_id', "
            "                                      substr(m.metadata->'origin'->>'conversation', 6))) AS owner, "
            "         (SELECT d.metadata->>'agent' FROM sn_core.desks d "
            "          WHERE d.id::text = COALESCE(m.metadata->>'desk_id', "
            "                                      substr(m.metadata->'origin'->>'conversation', 6))) AS chat_agent, "
            "         COALESCE((m.metadata->>'kind' = 'chat' AND m.metadata->>'reason' = 'mentioned') "
            "                  OR m.metadata->>'source' = 'reminder', false) AS direct "
            "  FROM sn_core.messages m "
            "  JOIN sn_core.message_state ms ON ms.message_id=m.id AND ms.status='new' "
            "  JOIN sn_config.agents a ON a.slug=m.recipient "
            "  WHERE m.direction='inbound' AND m.channel_type IN ('internal','system') "
            "    AND a.enabled AND a.archived_at IS NULL "
            "  UNION "
            "  SELECT a.slug, m.id, true AS person, m.sender, m.content, false, NULL, "
            "         ms.handed_at, ms.handed_count, ms.tags, NULL, NULL, false FROM sn_core.messages m "
            "  JOIN sn_core.message_state ms ON ms.message_id=m.id AND ms.status='new' "
            "  JOIN sn_config.channels c ON c.slug=m.channel_type "
            "  JOIN sn_config.agent_channels ac ON ac.channel_id=c.id AND ac.instance_ref=m.recipient AND ac.enabled "
            "  JOIN sn_config.agents a ON a.id=ac.agent_id "
            "  WHERE m.direction='inbound' AND m.channel_type NOT IN ('internal','system') AND m.channel_type <> ALL(%s::text[]) "
            "    AND a.enabled AND a.archived_at IS NULL "
            "    AND NOT EXISTS (SELECT 1 FROM sn_config.agent_channels other "
            "      JOIN sn_config.agents peer ON peer.id=other.agent_id "
            "      WHERE other.channel_id=ac.channel_id AND other.instance_ref=ac.instance_ref "
            "        AND other.enabled AND peer.enabled AND peer.archived_at IS NULL "
            "        AND peer.id<>a.id) "
            "), agg AS ("
            "  SELECT agent, count(DISTINCT mid) AS pending_messages, max(mid) AS max_id, "
            "         bool_or(person) AS attended, "
            "         array_agg(mid ORDER BY mid) AS pending_ids, "
            # Seen or not (message_state.handed_at): not seen wakes the agent;
            # seen and still open is reminded after WAKE_RETRY_SECONDS, at most
            # WAKE_REMINDERS times; then it is reported once as unhandled.
            "         array_agg(mid ORDER BY mid) FILTER (WHERE handed_at IS NULL) AS unseen_ids, "
            "         array_agg(mid ORDER BY mid) FILTER (WHERE handed_at IS NOT NULL "
            "             AND handed_count <= %s AND handed_at < now() - make_interval(secs => %s)) AS remind_ids, "
            "         array_agg(mid ORDER BY mid) FILTER (WHERE handed_count > %s "
            "             AND NOT ('unhandled' = ANY(tags))) AS exhausted_ids, "
            # The chat mentions among them, with their text: the Runner hands
            # them to the agent in the wake, since it cannot read the agent's
            # inbox itself (an agent identity is taken only on the unix socket).
            "         json_agg(json_build_object('id', mid, 'sender', sender, 'content', content, 'desk', desk, 'owner', owner, "
            "                                    'chat_agent', chat_agent, 'direct', direct) "
            "                  ORDER BY mid) FILTER (WHERE mention) AS mentions "
            "  FROM pend GROUP BY agent"
            # The second half of the signal. Messages are not the only
            # work: a task assigned to an agent is work too, and it cannot be
            # joined here -- the planner is a separate database in its own
            # container. So the planner PUSHES (POST /api/agents/<slug>/wake) and
            # the flag it raises is read here. Without this branch the only thing
            # that ever noticed a task was the per-agent cron, which is why the
            # cron could not be retired.
            "), flag AS ("
            "  SELECT a.slug AS agent, r.wake_requested_at "
            "  FROM sn_config.agents a "
            "  JOIN sn_core.agent_runtime r ON r.agent_slug=a.slug "
            "  WHERE r.wake_requested_at IS NOT NULL AND a.enabled AND a.archived_at IS NULL"
            ") "
            "SELECT COALESCE(agg.agent, flag.agent) AS agent, "
            "       COALESCE(agg.pending_messages, 0) AS pending_messages, "
            "       COALESCE(agg.max_id, 0) AS max_id, "
            "       COALESCE(agg.attended, false) AS attended, "
            "       COALESCE(agg.mentions, '[]'::json) AS mentions, "
            "       COALESCE(agg.pending_ids, '{}') AS pending_ids, "
            "       COALESCE(agg.unseen_ids, '{}') AS unseen_ids, "
            "       COALESCE(agg.remind_ids, '{}') AS remind_ids, "
            "       COALESCE(agg.exhausted_ids, '{}') AS exhausted_ids, "
            # A reminder counts from the end of the agent's last turn, not from
            # the wake: a turn longer than the interval used it up while the
            # agent was still working (2026-10-10).
            "       COALESCE(r.updated_at < now() - make_interval(secs => %s), true) AS idle, "
            "       r.wake_suspended_until AS wake_suspended_until, "
            "       flag.wake_requested_at AS wake_requested_at "
            "FROM agg FULL OUTER JOIN flag ON flag.agent = agg.agent "
            "LEFT JOIN sn_core.agent_runtime r ON r.agent_slug = COALESCE(agg.agent, flag.agent) "
            "WHERE (COALESCE(r.status,'free')='free' "
            "   OR (r.status='busy' AND r.lease_until IS NOT NULL AND r.lease_until < now()) "
            # Self-healing: a 'broken' agent (a past wake failure) is NOT dead forever.
            # If it still has pending work, retry it on a backoff (>=60s since it broke)
            # so a transient failure recovers on its own -- a successful wake flips it
            # back to free via the Runner's release. Without this, one wake-500 stranded
            # an agent permanently (nora was dead from 06-26 until a manual reset).
            "   OR (r.status='broken' AND r.updated_at < now() - interval '60 seconds')) "
            # do not wake agents that are budget-suspended until the
            # suspension time has passed (budget period reset). This prevents retrying
            # against a 402 wall and avoids marking them broken.
            "  AND (r.budget_suspended_until IS NULL OR r.budget_suspended_until <= now()) "
            "ORDER BY 1",
            (fast, WAKE_REMINDERS, WAKE_RETRY_SECONDS, WAKE_REMINDERS, WAKE_RETRY_SECONDS)
        )
        rows = cur.fetchall()
        rows = _apply_wake_cap(cur, rows)
    out = []
    for r in rows:
        due = sorted(set(r["unseen_ids"]) | (set(r["remind_ids"]) if r["idle"] else set()))
        out.append({"slug": r["agent"], "pending_messages": r["pending_messages"],
                    "max_id": r["max_id"],
                    "pending_ids": list(r["pending_ids"]),
                    "due_ids": due,
                    "exhausted_ids": list(r["exhausted_ids"]),
                    "attended": bool(r["attended"]),
                    "next_wake": _next_wake(r["mentions"], due),
                    "wake_requested_at": r["wake_requested_at"].isoformat()
                    if r["wake_requested_at"] else None})
    return {"agents": out, "count": len(out)}


#: A seen message still open is reminded after this long, at most this often.
WAKE_RETRY_SECONDS = int(_os.environ.get("SN_WAKE_RETRY_SECONDS", "60"))
WAKE_REMINDERS = int(_os.environ.get("SN_WAKE_REMINDERS", "3"))


def _next_wake(mentions: list, due: list) -> dict:
    """What the agent's next wake is for and which messages it is given, decided
    here where the message kinds are known (the Runner only launches it). A
    person waiting in a chat comes first, before colleagues' mail and before a
    task: the chat
    waiting longest, with every message of it that is due, one chat per wake so
    chats never share a context. Anything else is the general wake, given all
    that is due; it reads the inbox."""
    ready = [m for m in mentions if m["id"] in set(due)]
    if not ready:
        return {"reason": "general", "ids": list(due)}
    desk = ready[0]["desk"]
    chat = [m for m in ready if m["desk"] == desk]
    return {"reason": "chat", "desk": desk, "person": ready[0].get("owner"), "chat_agent": ready[0].get("chat_agent"),
            "ids": [m["id"] for m in chat], "messages": chat}


def _wake_counts(cur, slug: str) -> dict:
    """Wakes of this agent in the last hour and the last day, from the journal.

    Counts every wake, not only the unattended ones. The journal does not record
    which was which, and adding that would mean changing the runner as well; the
    caps are set high enough above ordinary work that the difference cannot
    matter, and the attended path is never blocked whatever the count says.
    Written down because a reader will otherwise assume it counts what its name
    suggests."""
    cur.execute(
        """SELECT count(*) FILTER (WHERE ts > now() - interval '1 hour') AS hour,
                  count(*) FILTER (WHERE ts > now() - interval '1 day') AS day,
                  min(ts) FILTER (WHERE ts > now() - interval '1 hour') AS oldest_hour,
                  min(ts) FILTER (WHERE ts > now() - interval '1 day') AS oldest_day
             FROM sn_core.events
            WHERE event_type = 'agent.woken' AND entity_id = %s
              AND ts > now() - interval '1 day'""",
        (slug,),
    )
    return cur.fetchone()


def _apply_wake_cap(cur, rows):
    """Hold back agents whose autonomous waking has run away.

    A candidate whose pending work includes a message from a PERSON is returned
    untouched, always. The owner writing to an agent must never be answered with
    silence because that agent was talking to its colleagues too much -- a limit
    that silences the front office is a limit that gets switched off, and then
    there is no limit at all.

    Everything else -- internal and system messages, and raised flags -- is
    autonomous work, and is what this cap governs."""
    kept = []
    for r in rows:
        if r["attended"]:
            kept.append(r)
            continue
        held = r["wake_suspended_until"]
        if held is not None and held > _dt.datetime.now(_dt.timezone.utc):
            continue
        c = _wake_counts(cur, r["agent"])
        hour = check_limit(cur, "wakes_unattended", used=c["hour"],
                           agent_slug=r["agent"], period="hour")
        day = check_limit(cur, "wakes_unattended", used=c["day"],
                          agent_slug=r["agent"], period="day")
        for decision in (hour, day):
            if decision["action"] == "warn":
                _warn_wake_limit(cur, r["agent"], decision)
        over_hour = not hour["allowed"]
        over_day = not day["allowed"]
        if not (over_hour or over_day):
            kept.append(r)
            continue
        # The suspension lifts itself when the window drains: the oldest wake
        # still inside the window, plus the window. Nobody has to touch the node.
        if over_day:
            until = c["oldest_day"] + _dt.timedelta(days=1)
            window, count, cap = "day", c["day"], int(day["limit"])
            decision = day
        else:
            until = c["oldest_hour"] + _dt.timedelta(hours=1)
            window, count, cap = "hour", c["hour"], int(hour["limit"])
            decision = hour
        _hold_agent(cur, r["agent"], until, window, count, cap, decision)
    return kept


def _warn_wake_limit(cur, slug: str, decision: dict) -> None:
    """One journal entry and optional owner alert per warning window."""
    first = record_event(cur, decision, actor="component:core-api",
                         agent_slug=slug, emit=emit)
    if not first:
        return
    try:
        sent = notify_owner(
            cur, decision, queue_message=owner_alert.queue_owner_email,
            source="wake-cap",
            text=(f"Agent {slug} has used {decision['used']:g} unattended wakes "
                  f"of a {decision['limit']:g} per-{decision['period']} limit."),
        )
        if sent:
            cur.execute("UPDATE sn_core.limit_notices SET notified_at=now() "
                        "WHERE code='wakes_unattended' AND scope_type='agent' "
                        "AND scope_id=%s AND period_start=%s AND kind='warn'",
                        (slug, decision["period_start"]))
    except owner_alert.OwnerUnreachable as exc:
        log.warning("wake warning on %s could not be reported: %s", slug, exc)


def _human_time(when) -> str:
    """A time a person reads. The exact one is in the event; this is the letter."""
    local = when.astimezone()
    today = _dt.datetime.now(local.tzinfo).date()
    if local.date() == today:
        return local.strftime("%H:%M today")
    if local.date() == today + _dt.timedelta(days=1):
        return local.strftime("%H:%M tomorrow")
    return local.strftime("%H:%M on %d %B")


def _hold_agent(cur, slug: str, until, window: str, count: int, cap: int,
                decision: dict) -> None:
    """Set the hold, say so in the journal, and tell the owner once.

    The email is best-effort in the sense that the node must not stop guarding
    when it cannot send; but unlike the budget path previously, a send that
    does not happen is not recorded as if it had."""
    cur.execute(
        """UPDATE sn_core.agent_runtime
              SET wake_suspended_until = %s, updated_at = now()
            WHERE agent_slug = %s
              AND (wake_suspended_until IS NULL OR wake_suspended_until < %s)
        RETURNING agent_slug""",
        (until, slug, until),
    )
    first_time = cur.fetchone() is not None
    record_event(cur, decision, actor="component:core-api", agent_slug=slug,
                 emit=emit, context={"until": until.isoformat()})
    emit(cur, actor="component:core-api", event_type="agent.wake_capped",
         entity_type="agent", entity_id=slug,
         payload={"window": window, "wakes": int(count), "cap": cap,
                  "until": until.isoformat(), "notified": first_time})
    if not first_time:
        return
    try:
        sent = notify_owner(
            cur, decision, queue_message=owner_alert.queue_owner_email,
            source="wake-cap",
            text=(
                f"Smart Node has stopped starting {slug} by itself.\n\n"
                f"In the last {window} it started {count} times without anybody "
                f"asking, and the limit is {cap}.\n\n"
                f"It starts again by itself at {_human_time(until)}, with "
                f"nothing for you to do.\n\n"
                "This limit is about how often an agent starts working without "
                "anybody asking -- colleagues writing to each other in a loop, "
                "for instance -- and not about money.\n\n"
                "Writing to this agent yourself still reaches it: a message from "
                "a person is never held back by this limit."
            ),
        )
        if sent:
            cur.execute("UPDATE sn_core.limit_notices SET notified_at=now() "
                        "WHERE code='wakes_unattended' AND scope_type='agent' "
                        "AND scope_id=%s AND period_start=%s AND kind='trip'",
                        (slug, decision["period_start"]))
    except owner_alert.OwnerUnreachable as exc:
        # Loud in the journal rather than silent: the guard held, the telling did
        # not, and the operator has to be able to find out which.
        log.warning("wake cap on %s could not be reported: %s", slug, exc)
        emit(cur, actor="component:core-api",
             event_type="agent.wake_capped_unreported",
             entity_type="agent", entity_id=slug, payload={"reason": str(exc)})


@router.get("/{slug}", dependencies=[Depends(requires("agents.read"))])
def get_agent(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    body: bool = Query(default=False),
):
    with cursor() as cur:
        cur.execute(
            f"SELECT {AGENT_COLS} FROM sn_config.agents a "
            "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
            "WHERE a.slug=%s AND a.archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"agent not found: {slug}")
    body_md = agent_fs.read_claude(slug) if body else None
    return _row_to_read(row, body_md).model_dump(mode="json")


def _model_refused(e: psycopg2.errors.CheckViolation) -> HTTPException:
    """The catalog refused the model: the caller's mistake, so 422, not 500.

    Any other check violation is not ours to translate and is re-raised by the caller.
    """
    return HTTPException(422, e.diag.message_primary or "model not in the catalog")


def _is_model_refusal(e: psycopg2.errors.CheckViolation) -> bool:
    return e.diag.constraint_name == "agents_model_in_catalog"


def _canonical_model(cur, model: str, slug: str, actor: str, *,
                     emit_alias: bool = True) -> str:
    """The <runtime>/<model> an incoming model means.

    Legacy alias, for one release: a caller written
    before the canonical shape still sends a bare catalog name ('sonnet') -- old
    panel tabs, scripts, agents that create agents from a prompt they learned. If
    exactly one enabled chat row carries that name, it is rewritten to that row
    and an event says who still sends the old form. Anything else is passed on
    untouched and the database decides (agents_model_in_catalog): a name two
    runtimes share is not guessed.
    """
    if not model or "/" in model or model == "none":
        return model
    cur.execute(
        "SELECT provider || '/' || model AS canonical FROM sn_config.models "
        "WHERE model = %s AND enabled AND modality = 'chat'",
        (model,),
    )
    rows = cur.fetchall() or []
    if len(rows) != 1:
        return model
    canonical = rows[0]["canonical"]
    if emit_alias:
        emit(cur, actor=actor, event_type="agent.model_legacy_alias",
             entity_type="agent", entity_id=slug,
             payload={"sent": model, "stored": canonical})
    return canonical


@router.post("", status_code=201, dependencies=[Depends(requires("agents.create"))])
def create_agent(payload: AgentCreate, created_by: str = "panel",
                 actor: str = Depends(get_provisioning_actor)):
    if not NAME_RE.match(payload.slug):
        raise HTTPException(400, f"invalid slug: {payload.slug}")
    # Provision this identity synchronously before publishing the agent.
    if not payload.os_user:
        payload.os_user = f"agent-{payload.slug}"
    # Same reasoning for the memory workspace, and it fails more quietly: with it
    # empty, compose_claude_md drops the whole block (lib/agent_fs.py), so the
    # agent is never told which workspace is its own and its memory tool falls
    # back to `default` -- on every wake path, with no error anywhere. Nothing on
    # a node born from canon ever sent a value. The slug is unique per node and
    # is already the agent's runtime identity (SN_AGENT_SLUG).
    if not (payload.mesh_workspace or "").strip():
        payload.mesh_workspace = payload.slug

    provisioned = False
    try:
        with cursor(commit=True) as cur:
            # Before anything is written: the agents row and the identity row both
            # take this tenant, and a NULL one makes an agent that cannot work.
            payload.tenant_id = _resolve_create_tenant(cur, payload.tenant_id)
            # The agent's account is looked up by its slug. An account under
            # that name that is a person's or an operator's would be reused,
            # and the agent would act with that account's role.
            cur.execute(
                "SELECT 1 FROM sn_config.users WHERE email = %s AND "
                "(role IS DISTINCT FROM 'agent' OR (actor_id IS NOT NULL AND actor_id <> %s))",
                (payload.slug, f"agent:{payload.slug}"),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, f"slug is already the name of an account: {payload.slug}")
            # Before the directory is laid out: agent.yaml carries the model too.
            sent_model = payload.model
            payload.model = _canonical_model(cur, payload.model, payload.slug,
                                             f"user:{created_by}", emit_alias=False)

            # Resolve or inline-create specialist
            specialist_id = None
            specialist_body = ""
            if payload.specialist_slug:
                cur.execute(
                    "SELECT id, body_md FROM sn_config.specialists "
                    "WHERE slug=%s AND archived_at IS NULL",
                    (payload.specialist_slug,),
                )
                r = cur.fetchone()
                if not r:
                    raise HTTPException(400, f"specialist not found: {payload.specialist_slug}")
                specialist_id = r["id"]
                specialist_body = r["body_md"]
            elif payload.specialist_body is not None:
                cur.execute(
                    "INSERT INTO sn_config.specialists "
                    "(slug, name, body_md, archetype, tenant_id, created_by, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, body_md",
                    (
                        payload.slug,
                        payload.title,
                        payload.specialist_body,
                        "specialist",
                        payload.tenant_id,
                        created_by,
                        created_by,
                    ),
                )
                r = cur.fetchone()
                specialist_id = r["id"]
                specialist_body = r["body_md"]
            # else: agent without specialist (allowed; CLAUDE.md falls back to stub)

            # Only this request may remove a directory it successfully provisioned.
            try:
                agent_fs.provision_agent_dir(
                    slug=payload.slug,
                    title=payload.title,
                    specialist_body=specialist_body,
                    model=payload.model,
                    role=payload.role,
                    description=payload.description,
                    perms=_perms_from_payload(payload),
                )
                provisioned = True
            except FileExistsError:
                raise HTTPException(409, f"directory already exists for agent: {payload.slug}")
            except Exception as e:
                raise HTTPException(500, f"failed to provision directory: {e}")

            # Then DB row
            try:
                cur.execute(
                    "INSERT INTO sn_config.agents "
                    "(tenant_id, department_id, slug, title, role, description, "
                    " specialist_id, model, enabled, capability, "
                    " os_user, schedule_cron, os_groups, mesh_workspace, fs_read, fs_write, "
                    " forbidden_fs, forbidden_behavior, created_by, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        payload.tenant_id, payload.department_id, payload.slug,
                        payload.title, payload.role, payload.description,
                        specialist_id, payload.model, payload.enabled, payload.capability,
                        payload.os_user, payload.schedule, payload.os_groups,
                        payload.mesh_workspace, payload.fs_read, payload.fs_write,
                        payload.forbidden_fs, payload.forbidden_behavior,
                        created_by, created_by,
                    ),
                )
                new_id = cur.fetchone()["id"]
            except Exception as e:
                if isinstance(e, psycopg2.errors.CheckViolation) and _is_model_refusal(e):
                    raise _model_refused(e)
                raise
            cur.execute("UPDATE sn_config.agents SET avatar = %s WHERE id = %s", (_next_avatar(cur), new_id))

            # Re-fetch with join to get specialist_slug populated
            cur.execute(
                f"SELECT {AGENT_COLS} FROM sn_config.agents a "
                "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
                "WHERE a.id=%s",
                (new_id,),
            )
            row = cur.fetchone()
            # The agent must also exist as a node identity, or every principal-gated
            # call it makes (messaging above all) is rejected with 403.
            _ensure_agent_identity(cur, payload.slug, payload.title, payload.tenant_id)
            # What every agent is given (the registry's default marks), now, not
            # at the next install; then its prompt and servers, from them.
            default_grants.apply(cur, new_id)
            _compose_and_write_claude(cur, payload.slug)
            # Prepare the response before the privileged side effect. Neither the row,
            # identity nor any bindings are visible to routing until this transaction
            # commits, so a message arriving immediately after 201 can already run.
            response = _row_to_read(row).model_dump(mode="json")
            result = runner_op(actor, "ensure_agent_users", {
                "agents": [{"slug": payload.slug, "os_user": payload.os_user}],
            })
            ensured = result.get("result") or {}
            # This operation reports per-agent errors in a successful HTTP response.
            if (result.get("ok") is not True or ensured.get("failed") != 0
                    or ensured.get("ensured") != 1
                    or not any(a.get("slug") == payload.slug
                               and a.get("os_user") == payload.os_user
                               for a in ensured.get("agents", []))):
                log.error("system user provisioning failed for %s: %s", payload.slug, ensured)
                raise HTTPException(502, "failed to provision the agent system user")
            # The runner audits in a separate DB transaction. Emitting first can
            # lock the same actor row and deadlock the synchronous call.
            if sent_model != payload.model:
                emit(cur, actor=f"user:{created_by}", event_type="agent.model_legacy_alias",
                     entity_type="agent", entity_id=payload.slug,
                     payload={"sent": sent_model, "stored": payload.model})
            # A new colleague changes every existing Team section. Do this only
            # after the privileged provisioning succeeded.
            _recompose_roster(cur, payload.slug, f"user:{created_by}")
            emit(cur, actor=f"user:{created_by}", event_type="agent.created",
                 entity_type="agent", entity_id=payload.slug,
                 payload={"role": payload.role, "model": payload.model})
    except Exception:
        if provisioned:
            agent_fs.remove_agent_dir(payload.slug)
        raise
    _register_planner_actor(payload.slug, payload.title)
    return response


# The AgentUpdate fields update_agent actually writes. A PUT carrying none of them
# is not a change, and must not be reported as one. department_id and
# policy_categories are declared on AgentUpdate but not written here; they are
# left out on purpose until that is decided, so sending them alone stays a no-op.
_UPDATE_WRITES = frozenset({
    "title", "role", "description", "model", "enabled", "schedule",
    "os_groups", "mesh_workspace", "fs_read", "fs_write", "forbidden_fs",
    "forbidden_behavior", "specialist_body", "avatar",
})


def _next_avatar(cur) -> str | None:
    """A picture for a new agent, chosen by the node: the next free one of the pack
    the team already uses (crew, the one every node has, when none does). An agent
    made through a tool had none, and a model has no business choosing among hundreds
    of files. None when the Studio and its pictures are not on this node."""
    cur.execute("SELECT avatar FROM sn_config.agents WHERE avatar IS NOT NULL AND archived_at IS NULL ORDER BY id")
    used = [r["avatar"] for r in cur.fetchall()]
    pack = next((a.split("/")[0] for a in used if "/" in a and not a.startswith("custom/")), "crew")
    try:
        files = sorted(f for f in _os.listdir(_os.path.join(AVATARS_DIR, pack)) if not f.startswith("."))
    except OSError:
        return None
    if not files:
        return None
    keys = [f"{pack}/{f}" for f in files]
    return next((k for k in keys if k not in used), keys[0])


def _avatar_key(value: str | None) -> str | None:
    """The stored avatar: a key inside Studio's /avatars store, or NULL.

    Blank clears it, because that is what the picker's Remove sends. The Studio
    serves the key as /avatars/<key>, so a key that could climb out of the store
    is refused rather than stored and left for every reader to guard against.
    """
    key = (value or "").strip()
    if not key:
        return None
    if key.startswith("/") or "\\" in key or ".." in key.split("/"):
        raise HTTPException(400, "avatar must be a key inside the avatars store, e.g. crew/alex.webp")
    return key


_AVATAR_UPLOAD_MAX_BYTES = 3 * 1024 * 1024
_AVATAR_UPLOAD_EXTENSIONS = {"webp", "png", "jpg", "jpeg", "gif"}


@router.post("/{slug}/avatar", status_code=201)
async def upload_agent_avatar(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    request: Request,
    ext: str = Query("png", min_length=2, max_length=8),
    principal: Principal = Depends(requires("agents.update")),
):
    """Store an uploaded avatar in durable node state and set its DB key.

    Studio is the read-only serving/BFF process. This principal-gated endpoint
    is the sole application writer, so a package update cannot own or replace
    the bytes and systemd can enforce the same split at the filesystem boundary.
    """
    extension = re.sub(r"[^a-z0-9]", "", ext.lower())
    if extension == "jpeg":
        extension = "jpg"
    if extension not in _AVATAR_UPLOAD_EXTENSIONS:
        raise HTTPException(415, f"unsupported image type: {extension or ext}")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty avatar")
    if len(data) > _AVATAR_UPLOAD_MAX_BYTES:
        raise HTTPException(
            413,
            f"avatar is {len(data)} bytes; the limit is {_AVATAR_UPLOAD_MAX_BYTES}",
        )
    if not SN_AVATAR_STATE_DIR.is_dir():
        raise HTTPException(503, "avatar state directory is not provisioned")

    token = uuid.uuid4().hex
    raw_path: Path | None = None
    encoded_path = SN_AVATAR_STATE_DIR / f".encode-{token}.webp"
    final_path: Path | None = None
    try:
        fd, raw_name = tempfile.mkstemp(
            prefix=f".upload-{token}-", suffix=f".{extension}",
            dir=SN_AVATAR_STATE_DIR,
        )
        raw_path = Path(raw_name)
        with _os.fdopen(fd, "wb") as stream:
            stream.write(data)
        _os.chmod(raw_path, 0o640)

        filename = f"{slug}-{token}.webp"
        final_path = SN_AVATAR_STATE_DIR / filename
        optimized = False
        try:
            result = subprocess.run(
                ["cwebp", "-quiet", "-q", "82", "-resize", "256", "256",
                 str(raw_path), "-o", str(encoded_path)],
                capture_output=True, timeout=20, check=False,
            )
            optimized = result.returncode == 0 and encoded_path.is_file()
        except (OSError, subprocess.TimeoutExpired):
            optimized = False
        if optimized:
            _os.chmod(encoded_path, 0o640)
            encoded_path.replace(final_path)
            raw_path.unlink(missing_ok=True)
            raw_path = None
        else:
            filename = f"{slug}-{token}.{extension}"
            final_path = SN_AVATAR_STATE_DIR / filename
            raw_path.replace(final_path)
            raw_path = None

        key = f"custom/{filename}"
        try:
            update_agent(
                slug,
                AgentUpdate(avatar=key, change_note="avatar uploaded"),
                updated_by=principal.username,
            )
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        return {"key": key, "url": f"/avatars/{key}"}
    finally:
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)
        encoded_path.unlink(missing_ok=True)


@router.put("/{slug}", dependencies=[Depends(requires("agents.update"))])
def update_agent(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    payload: AgentUpdate,
    updated_by: str = "panel",
):
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, version, specialist_id, os_user FROM sn_config.agents "
            "WHERE slug=%s AND archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"agent not found: {slug}")
        # The system user is set once, when the agent is created. The kernel proves
        # identity as uid -> os_user -> slug (auth.py), and the agent's logs, state
        # and work are owned by that user, so renaming it here cut the agent off
        # from the planner, secrets and messaging.
        # The same value is accepted as no change, because a form may echo it back.
        if payload.os_user is not None and payload.os_user != row["os_user"]:
            raise HTTPException(409, "os_user is set when the agent is created and cannot be changed")

        agent_id = row["id"]
        specialist_id = row["specialist_id"]

        # Nothing this route writes was sent: answer with the agent as it is. This
        # used to bump the version, write a changelog entry, recompose CLAUDE.md
        # and emit agent.updated for a change that never happened.
        if not (payload.model_fields_set & _UPDATE_WRITES):
            cur.execute(
                f"SELECT {AGENT_COLS} FROM sn_config.agents a "
                "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
                "WHERE a.slug=%s",
                (slug,),
            )
            return _row_to_read(cur.fetchone()).model_dump(mode="json")

        # Validated before any write, so a refused key leaves the agent untouched.
        # Tested by presence, not `is not None`: Remove sends "" and a client may
        # send null, and both must clear the picture.
        avatar_sent = "avatar" in payload.model_fields_set
        avatar = _avatar_key(payload.avatar) if avatar_sent else None

        # Bump version on any change (semver patch by default)
        a, b, c = (int(x) for x in row["version"].split("."))
        new_version = f"{a}.{b}.{c+1}"

        sets = ["updated_by=%s", "updated_at=now()", "version=%s"]
        params: list = [updated_by, new_version]
        if payload.title is not None:    sets.append("title=%s");    params.append(payload.title)
        if payload.role is not None:     sets.append("role=%s");     params.append(payload.role)
        if payload.description is not None: sets.append("description=%s"); params.append(payload.description)
        if payload.model is not None:
            payload.model = _canonical_model(cur, payload.model, slug, f"user:{updated_by}")
            sets.append("model=%s");    params.append(payload.model)
        if payload.enabled is not None:  sets.append("enabled=%s");  params.append(payload.enabled)
        if payload.schedule is not None: sets.append("schedule_cron=%s"); params.append(payload.schedule)
        if payload.os_groups is not None: sets.append("os_groups=%s"); params.append(payload.os_groups)
        # Blank means "back to the default", not "no workspace": clearing the field
        # in the panel must not silently send the agent's memory to `default`.
        if payload.mesh_workspace is not None: sets.append("mesh_workspace=%s"); params.append(payload.mesh_workspace.strip() or slug)
        if payload.fs_read is not None:  sets.append("fs_read=%s");  params.append(payload.fs_read)
        if payload.fs_write is not None: sets.append("fs_write=%s"); params.append(payload.fs_write)
        if payload.forbidden_fs is not None: sets.append("forbidden_fs=%s"); params.append(payload.forbidden_fs)
        if payload.forbidden_behavior is not None: sets.append("forbidden_behavior=%s"); params.append(payload.forbidden_behavior)
        if avatar_sent:                  sets.append("avatar=%s");   params.append(avatar)
        params.append(slug)
        try:
            cur.execute(
                f"UPDATE sn_config.agents SET {', '.join(sets)} WHERE slug=%s",
                params,
            )
        except psycopg2.errors.CheckViolation as e:
            if _is_model_refusal(e):
                raise _model_refused(e)
            raise

        # If specialist_body provided, update the linked specialist (or create one)
        if payload.specialist_body is not None:
            if specialist_id is None:
                cur.execute(
                    "INSERT INTO sn_config.specialists "
                    "(slug, name, body_md, archetype, created_by, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (slug, payload.title or slug, payload.specialist_body, "specialist", updated_by, updated_by),
                )
                new_sp_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE sn_config.agents SET specialist_id=%s WHERE id=%s",
                    (new_sp_id, agent_id),
                )
            else:
                cur.execute(
                    "UPDATE sn_config.specialists "
                    "SET body_md=%s, updated_at=now(), updated_by=%s "
                    "WHERE id=%s",
                    (payload.specialist_body, updated_by, specialist_id),
                )
            # CLAUDE.md is recomposed from the registry below (not written raw),
            # so a role edit and a policy/tool change go through one path.

        # Changelog entry
        note = payload.change_note or "fields updated via API"
        cur.execute(
            "INSERT INTO sn_config.changelog_entries "
            "(entity_kind, entity_id, version, reason, change_type, changed_by) "
            "VALUES ('agent', %s, %s, %s, 'patch', %s)",
            (agent_id, new_version, note, updated_by),
        )
        try:
            agent_fs.append_changelog(slug, new_version, note)
        except FileNotFoundError:
            log.warning("agent dir missing for %s; CHANGELOG.md not updated", slug)

        # Return joined row
        cur.execute(
            f"SELECT {AGENT_COLS} FROM sn_config.agents a "
            "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
            "WHERE a.slug=%s",
            (slug,),
        )
        row = cur.fetchone()

        # Re-render the /srv data projection (agent.yaml/config.json) from the
        # updated DB row, so the filesystem never drifts from the database.
        try:
            agent_fs.rerender_config(
                slug,
                title=row["title"], model=row["model"],
                role=row["role"], description=row["description"],
                perms=_perms_from_row(row),
            )
        except FileNotFoundError:
            log.warning("agent dir missing for %s; agent.yaml/config.json not re-rendered", slug)

        # Team includes each colleague's title, role, description and enabled
        # state. A change to one of those updates every active prompt.
        if any(getattr(payload, key) is not None for key in
               ("title", "role", "description", "enabled")):
            _recompose_roster(cur, slug, f"user:{updated_by}")
        else:
            _compose_and_write_claude(cur, slug)

        emit(cur, actor=f"user:{updated_by}", event_type="agent.updated",
             entity_type="agent", entity_id=slug,
             payload={"version": new_version})
    return _row_to_read(row).model_dump(mode="json")


@router.delete("/{slug}", status_code=204, dependencies=[Depends(requires("agents.archive"))])
def delete_agent(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.agents SET archived_at=now() "
            "WHERE slug=%s AND archived_at IS NULL RETURNING id, specialist_id",
            (slug,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"agent not found: {slug}")
        # Archive the linked specialist only if it was inline-created for this
        # agent (slug == agent slug). Shared specialists (different slug) stay.
        if row["specialist_id"]:
            cur.execute(
                "UPDATE sn_config.specialists SET archived_at=now() "
                "WHERE id=%s AND slug=%s AND archived_at IS NULL",
                (row["specialist_id"], slug),
            )
        cur.execute(
            "UPDATE sn_config.budgets SET enabled=false "
            "WHERE scope_type='agent' AND scope_id=%s AND enabled "
            "RETURNING id",
            (str(row["id"]),),
        )
        for budget in cur.fetchall() or []:
            emit(cur, actor="user:panel", event_type="budget.disabled",
                 entity_type="budget", entity_id=str(budget["id"]),
                 payload={"reason": "agent-archived", "agent": slug})
        _recompose_roster(cur, slug, "user:panel")
        emit(cur, actor="user:panel", event_type="agent.archived",
             entity_type="agent", entity_id=slug, payload={})
    try:
        agent_fs.archive_agent_dir(slug)
    except FileNotFoundError:
        log.warning("agent dir already missing for %s", slug)
    return None


# ---------------------------------------------------------- Grants (legacy, filesystem JSON)

def _resolve_agent_dir(slug: str) -> Path:
    if not NAME_RE.match(slug):
        raise HTTPException(400, f"invalid agent slug: {slug}")
    d = AGENTS_ROOT / slug
    if not d.is_dir():
        raise HTTPException(404, f"agent directory not found: {slug}")
    return d


class GrantsPayload(BaseModel):
    grants: dict[str, str] = Field(default_factory=dict)
    # Exact secret refs or dotted namespaces ending in .* available through the
    # read-only secrets MCP.  A `secrets: r--` tool grant without this list
    # grants no values: access to a tool is not access to the whole vault.
    # Optional for wire compatibility: older panel builds PUT only `grants`.
    # Omitting this field preserves the existing ref allowlist; sending []
    # deliberately revokes every ref.
    secret_refs: list[str] | None = None

    @field_validator("grants")
    @classmethod
    def _check(cls, v: dict[str, str]) -> dict[str, str]:
        for tool, mode in v.items():
            if not NAME_RE.match(tool):
                raise ValueError(f"bad tool name: {tool}")
            if not MODE_RE.match(mode):
                raise ValueError(f"bad mode '{mode}' for {tool} (expected like rw- / r-- / rwd)")
        return v

    @field_validator("secret_refs")
    @classmethod
    def _check_secret_refs(cls, refs: list[str] | None) -> list[str] | None:
        import re
        if refs is None:
            return None
        out = []
        for rule in refs:
            exact = rule[:-2] if rule.endswith(".*") else rule
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,40}(\.[a-zA-Z_][a-zA-Z0-9_-]{0,40}){0,5}", exact):
                raise ValueError(f"bad secret ref grant: {rule}")
            out.append(rule)
        return sorted(set(out))


def _db_grant_modes(slug: str) -> dict:
    """The agent's grants as they stand in sn_config.agent_tools, as {tool: mode}.

    grants.json is not the only writer of this table: seed-tools grants some
    rows straight into it on every core-api start (messages_search, my_wakes,
    secrets_resolve to every agent, node_summary to the configurator). A merge
    that read only grants.json rebuilt the rows without them, and
    _sync_agent_tools deleted them. Every update ran such a merge (onboarding),
    so every updated node lost those four until core-api next restarted.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT t.name, at.granted_caps FROM sn_config.agent_tools at "
            "JOIN sn_core.tools t ON t.id = at.tool_id "
            "JOIN sn_config.agents a ON a.id = at.agent_id "
            "WHERE a.slug = %s AND a.archived_at IS NULL",
            (slug,),
        )
        rows = cur.fetchall() or []
    return {
        r["name"]: "".join(ch if cap in (r["granted_caps"] or []) else "-"
                           for ch, cap in zip("rwd", ("read", "write", "delete")))
        for r in rows
    }


def _merge_grant_modes(existing: str, wanted: str) -> str:
    """Union two validated r/w/d masks without narrowing either one."""
    return "".join(
        capability if before != "-" or after != "-" else "-"
        for capability, before, after in zip("rwd", existing, wanted)
    )


@contextmanager
def _project_grants_file(path: Path, payload: dict):
    """Keep the file projection aligned if the database transaction fails."""
    previous = path.read_bytes() if path.exists() else None
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    if previous is not None:
        tmp.chmod(path.stat().st_mode & 0o777)
    try:
        tmp.replace(path)
        yield
    except BaseException:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            restore = path.with_suffix(".json.rollback.tmp")
            restore.write_bytes(previous)
            restore.chmod(path.stat().st_mode & 0o777)
            restore.replace(path)
        raise
    finally:
        tmp.unlink(missing_ok=True)


# ---- what the model is told about, per agent ---------------------------------
# Every MCP server in an agent's .mcp.json is described to the model on each
# request, all of its tools, whatever the grants say: a turn of twenty steps sends
# those schemas twenty times. The card lists them with their size, and the owner
# can take a server or a tool off; the runtime passes the list as --disallowedTools
# (gateway backends/cli_claude.py _agent_withheld). Sizes come from the tool
# registry (sn_core.tools: name, description, input schema): about 2.5 characters
# a token, measured against the door's own count. A server the registry does not
# describe is shown without a size.
_CHARS_PER_TOKEN = 2.5
# An agent answers chats and colleagues through messaging: never hidden.
_NEVER_WITHHELD = {"messaging"}


def _withheld_path(slug: str) -> Path:
    return _resolve_agent_dir(slug) / "withheld-tools.json"


def _read_withheld(slug: str) -> list:
    try:
        names = json.loads(_withheld_path(slug).read_text()).get("tools") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [n for n in names if isinstance(n, str) and n.startswith("mcp__")]


def _agent_servers(slug: str) -> list:
    try:
        return sorted(json.loads((_resolve_agent_dir(slug) / ".mcp.json").read_text())
                      .get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        return []


@router.get("/{slug}/model-tools", dependencies=[Depends(requires("agents.grants.read"))])
def agent_model_tools(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """The MCP servers and tools this agent's model is told about, each with its
    size in tokens, and which of them the owner has taken off."""
    servers = _agent_servers(slug)
    hidden = set(_read_withheld(slug))
    sizes: dict = {s: {} for s in servers}
    if servers:
        with cursor() as cur:
            cur.execute(
                "SELECT ms.slug AS server, t.name, "
                "       length(coalesce(t.name, '') || coalesce(t.description, '') "
                "              || coalesce(t.input_schema::text, '')) AS chars "
                "FROM sn_core.tools t JOIN sn_config.mcp_servers ms ON ms.id = t.mcp_server_id "
                "WHERE ms.slug = ANY(%s)", (servers,))
            for r in cur.fetchall():
                srv, name = r["server"], r["name"] or ""
                if name.startswith(srv + "-") and len(name) > len(srv) + 1:
                    sizes[srv][name[len(srv) + 1:]] = int(round((r["chars"] or 0) / _CHARS_PER_TOKEN))
    out = []
    for srv in servers:
        tools = [{"name": t, "tokens": n, "hidden": f"mcp__{srv}__{t}" in hidden}
                 for t, n in sorted(sizes[srv].items())]
        out.append({"server": srv, "hidden": f"mcp__{srv}" in hidden,
                    "locked": srv in _NEVER_WITHHELD,
                    "tokens": sum(t["tokens"] for t in tools) if tools else None,
                    "tools": tools})
    with cursor() as cur:
        cur.execute(
            "SELECT coalesce(input_tokens, 0) + coalesce(cache_read, 0) + coalesce(cache_write, 0) AS sent, ts "
            "FROM sn_core.model_calls WHERE agent = %s AND stop_reason IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (slug,))
        last = cur.fetchone()
    return {"agent": slug, "servers": out, "withheld": sorted(hidden),
            "last_request_tokens": int(last["sent"]) if last else None,
            "last_request_at": last["ts"].isoformat() if last else None}


class WithheldTools(BaseModel):
    tools: list[str] = Field(default_factory=list, max_length=500)


@router.put("/{slug}/withheld-tools", dependencies=[Depends(requires("agents.grants.write"))])
def put_withheld_tools(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                       body: WithheldTools, request: Request):
    """Take MCP servers or single tools off what this agent's model is offered.
    The whole list is replaced. It applies when the agent's process next starts."""
    servers = set(_agent_servers(slug))
    names: list = []
    for n in body.tools:
        parts = n.split("__") if isinstance(n, str) else []
        if len(parts) not in (2, 3) or parts[0] != "mcp" or parts[1] not in servers or (len(parts) == 3 and not parts[2]):
            raise HTTPException(status_code=422, detail=f"not one of this agent's MCP servers or tools: {n}")
        if parts[1] in _NEVER_WITHHELD:
            raise HTTPException(status_code=422, detail=f"{parts[1]} cannot be taken off: the agent answers through it")
        if n not in names:
            names.append(n)
    path = _withheld_path(slug)
    before = _read_withheld(slug)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tools": names}, indent=2) + "\n")
    _os.chmod(tmp, 0o644)
    _os.replace(tmp, path)
    with cursor(commit=True) as cur:
        actor = get_actor(request, request.headers.get("remote-user"))
        if not actor.startswith(("agent:", "component:", "system:", "user:", "os:")):
            actor = f"user:{actor}"
        emit(cur, actor=actor, event_type="grant.changed",
             entity_type="agent", entity_id=slug,
             payload={"field": "withheld_tools", "from": before, "to": names})
    return {"agent": slug, "withheld": names, "applies": "from the agent's next process start"}


@router.get("/{slug}/grants", dependencies=[Depends(requires("agents.grants.read"))])
def get_grants(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    # What the agent holds: grants.json and the rows granted only in the DB
    # (seed-tools writes messaging and its rows straight into agent_tools). A
    # page that showed the file alone, added one switch and saved the set back
    # took every DB-only row away (2026-10-30, eleven tools at once).
    d = _resolve_agent_dir(slug)
    f = d / "grants.json"
    data: dict = {}
    if f.is_file():
        try:
            data = json.loads(f.read_text()) or {}
        except Exception as e:
            raise HTTPException(500, f"failed to read grants.json: {e}")
    grants = dict(data.get("grants", {}) or {})
    for tool, mode in _db_grant_modes(slug).items():
        grants[tool] = _merge_grant_modes(grants[tool], mode) if tool in grants else mode
    return {"grants": grants, "secret_refs": data.get("secret_refs", [])}


@router.put("/{slug}/grants", dependencies=[Depends(requires("agents.grants.write"))])
def put_grants(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    body: GrantsPayload,
    request: Request,
    merge: bool = Query(False),
):
    d = _resolve_agent_dir(slug)
    f = d / "grants.json"
    # A capability that needs more than one catalogue row must not depend on the
    # operator remembering the second one. Reading tasks and filing them are two
    # rows because they are two HTTP calls, but nobody grants "file a task and
    # not read them" on purpose -- and an agent granted only the write is what
    # made an assistant answer "I can only create tasks" when asked what work
    # exists. Until a capability can group its rows, this pairs them here.
    #
    # messaging: the grant is a stdio MCP server, which only a CLI
    # session can run as the agent. An agent on an HTTP runtime reaches the same
    # four endpoints through these api rows instead (brin's tool loop), so they
    # come with the grant, at the same mode. seed-tools.py applies the same pairing
    # to agents that already hold messaging.
    #
    # planner: the same for the planner's reads and writes. Reads come at
    # r--; the two writes (comment, advance) at the lead's own mode and only when
    # that mode writes, so planner read-only never gains a write through pairing.
    # Capture prior grants BEFORE overwriting the file, so we can audit what
    # changed -- a revoked/reduced cap is the guardrail denying access, and that
    # denial must be visible in the event log to explain a later tool failure
    # (e.g. calendar write revoked -> "create denied").
    old_grants: dict = {}
    old_secret_refs: list[str] = []
    try:
        if f.exists():
            old_data = json.loads(f.read_text()) or {}
            old_grants = old_data.get("grants", {}) or {}
            old_secret_refs = old_data.get("secret_refs", []) or []
    except Exception:
        old_grants = {}
    if merge:
        # A merge keeps what the agent holds, and what it holds is the DB link as
        # well as the file: fold rows granted only in the DB into the base first.
        for tool, mode in _db_grant_modes(slug).items():
            old_grants[tool] = (_merge_grant_modes(old_grants[tool], mode)
                                if tool in old_grants else mode)
        body.grants = {
            tool: (
                _merge_grant_modes(old_grants[tool], wanted)
                if tool in old_grants else wanted
            )
            for tool, wanted in {**old_grants, **body.grants}.items()
        }
        if body.secret_refs is not None:
            body.secret_refs = sorted(set(old_secret_refs) | set(body.secret_refs))
    # Pair after merge: an incremental PUT may mention only the new grant, but
    # existing umbrella grants still need all of their catalogue projections.
    _PAIRED = {
        "planner": [("planner_tasks", "r--"), ("planner_task", "r--"),
                    ("planner_comments", "r--"), ("planner_search", "r--"),
                    ("planner_comment", "write"), ("planner_advance", "write")],
        "messaging": [("messages_inbox", None), ("messages_reply", None),
                      ("messages_send", None), ("messages_dismiss", None)],
        # The MCP permission is `secrets`; this catalogue row carries the
        # store's usage contract into the same agent's prompt.
        "secrets": [("secrets_resolve", "r--")],
    }
    _pairing_before = set(body.grants)
    for _lead, _mates in _PAIRED.items():
        if _lead not in body.grants:
            continue
        for _mate, _mode in _mates:
            if _mate in body.grants:
                continue
            if _mode == "write":
                if "w" in (body.grants[_lead] or ""):
                    body.grants[_mate] = body.grants[_lead]
                continue
            body.grants[_mate] = _mode or body.grants[_lead]
    _paired_added = set(body.grants) - _pairing_before
    if _paired_added & _PLANNER_API_ROWS and _runtime_kind(slug) == "cli":
        for _row_name in _paired_added & _PLANNER_API_ROWS:
            body.grants.pop(_row_name, None)
    # A ref allowlist is not a boundary if its subject can rewrite it. Keep the
    # legacy endpoint available for ordinary tool grants, but any transition
    # into or out of secrets access requires the same proved writer/owner
    # identity as the vault itself. Import lazily to avoid coupling route module
    # initialisation; secrets does not import agents.
    touches_secrets = (
        "secrets" in old_grants or "secrets" in body.grants
        or bool(old_secret_refs) or body.secret_refs is not None
    )
    if touches_secrets:
        from routes.secrets import _operator_actor
        _operator_actor(request, "grant", f"agent:{slug}")
    new_secret_refs = (body.secret_refs if body.secret_refs is not None
                       else sorted(old_secret_refs))
    # Attribute the mutation to the caller that actually made it. The seed's
    # trusted installer identity is already namespaced; a unix-socket caller is
    # kernel-proved as an agent; browser/proxy identities are users.
    event_actor = get_actor(request, request.headers.get("remote-user"))
    if not event_actor.startswith(("agent:", "component:", "system:", "user:", "os:")):
        if identity_strength(request) == "proved" and event_actor not in {
            "anonymous", "unknown-peer", "untrusted",
        }:
            event_actor = f"agent:{event_actor}"
        elif event_actor not in {"anonymous", "unknown-peer", "untrusted"}:
            event_actor = f"user:{event_actor}"
    # Reject before changing either the file or the DB. The rejected event has
    # its own committed transaction; raising inside the write transaction would
    # roll the audit event back with the refused PUT.
    with cursor(commit=True) as cur:
        cur.execute("SELECT name, id FROM sn_core.tools")
        known = {r["name"] for r in cur.fetchall() or []}
        unknown = sorted(set(body.grants) - known)
        for name in unknown:
            emit(cur, actor=event_actor, event_type="grant.rejected",
                 entity_type="agent", entity_id=slug,
                 payload={"tool": name, "reason": "unknown-tool"})
    if unknown:
        raise HTTPException(400, f"unknown tool grant: {', '.join(unknown)}")

    payload = {"grants": body.grants, "secret_refs": new_secret_refs,
               "schema_version": "0.2.0"}
    with _project_grants_file(f, payload), cursor(commit=True) as cur:
        cur.execute(
            f"SELECT {AGENT_COLS} FROM sn_config.agents a "
            "LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id "
            "WHERE a.slug=%s AND a.archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
        if row is not None:
            # 0. Heal identity: an agent created before _ensure_agent_identity
            #    shipped has no sn_config.users row, so granting it messaging
            #    would render tools it gets 403 from. Ensure it here too.
            _ensure_agent_identity(cur, slug, row["title"], row["tenant_id"])
            # 1. grants -> agent_tools (the DB grant link, source for fan-out).
            tools_before, tools_after = _sync_agent_tools(cur, row["id"], body.grants)
            # 1b. Audit grant changes: record revoked/reduced caps as
            # grant.revoked and additions/widenings as grant.changed, so the
            # event log explains why a later tool call was denied.
            def _caps(m: str | None) -> set[str]:
                return {cap for cap, mark in zip(("read", "write", "delete"), m or "")
                        if mark != "-"}
            _new = body.grants or {}
            audited = set()
            for _tool in sorted(set(old_grants) | set(_new)):
                _before, _after = old_grants.get(_tool), _new.get(_tool)
                if _before == _after:
                    continue
                audited.add(_tool)
                _denied = _after is None or not _caps(_before).issubset(_caps(_after))
                emit(
                    cur, actor=event_actor,
                    event_type="grant.revoked" if _denied else "grant.changed",
                    entity_type="agent", entity_id=slug,
                    payload={"tool": _tool, "from": _before, "to": _after},
                )
            for _tool in sorted(set(tools_before) | set(tools_after)):
                before_caps = tools_before.get(_tool)
                after_caps = tools_after.get(_tool)
                if before_caps == after_caps or _tool in audited:
                    continue
                denied = (after_caps is None or
                          not (before_caps or set()).issubset(after_caps))
                emit(cur, actor=event_actor,
                     event_type="grant.revoked" if denied else "grant.changed",
                     entity_type="agent", entity_id=slug,
                     payload={"tool": _tool,
                              "from": sorted(before_caps) if before_caps is not None else None,
                              "to": sorted(after_caps) if after_caps is not None else None,
                              "source": "agent_tools"})
            if sorted(old_secret_refs) != new_secret_refs:
                emit(
                    cur, actor=event_actor, event_type="grant.changed",
                    entity_type="agent", entity_id=slug,
                    payload={"tool": "secrets", "field": "secret_refs",
                             "from": sorted(old_secret_refs),
                             "to": new_secret_refs},
                )
            # 2. Re-render agent.yaml so its tools[] reflects the new grants.
            try:
                agent_fs.rerender_config(
                    slug, title=row["title"], model=row["model"],
                    role=row["role"], description=row["description"],
                    tools=sorted(body.grants.keys()), perms=_perms_from_row(row),
                )
            except FileNotFoundError:
                log.warning("agent dir missing for %s; agent.yaml not re-rendered after grants", slug)
            # 3. Render .mcp.json from enabled MCP servers whose slug matches a grant.
            _write_agent_mcp(cur, slug, body.grants.keys())
            # 4. Recompose CLAUDE.md -- the granted tools' contracts now apply.
            _compose_and_write_claude(cur, slug)
    return {"ok": True, "grants": body.grants,
            "secret_refs": new_secret_refs}


@router.post("/{slug}/regenerate", dependencies=[Depends(requires("agents.regenerate"))])
def regenerate_agent(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """Recompose one agent's CLAUDE.md from the registry, then recycle that
    agent's live gateway sessions so the change actually takes effect.

    Without the recycle this endpoint was a no-op in practice: the gateway holds
    a long-lived CLI session that read CLAUDE.md and .mcp.json when it started,
    so a newly granted tool stayed invisible and an edited persona stayed unused
    until an operator happened to restart the gateway. Nothing said so -- it
    returned 200 and the files on disk were correct. That produced three false
    diagnoses in one day, including a tool reported missing while its
    server was registered, running and reachable by hand."""
    with cursor(commit=True) as cur:
        ok = _compose_and_write_claude(cur, slug)
    if not ok:
        raise HTTPException(404, f"agent {slug!r} not found or not provisioned")
    return {"ok": True, "slug": slug, "sessions_recycled": _recycle_agent_sessions(slug)}


def _recycle_agent_sessions(slug: str) -> int:
    """Restart every live gateway session belonging to this agent so it re-reads
    the recomposed prompt and the regenerated .mcp.json. Returns how many were
    restarted.

    Best-effort and never fatal -- a recompose that landed on disk must not fail
    because the gateway is briefly unreachable -- but always logged, because a
    silent no-op here is the entire defect.

    The gateway has always been able to answer "which sessions belong to this
    agent": list_sessions takes agent_id and ai_sessions.agent_id has been
    populated correctly all along. It was simply never asked."""
    import os
    import urllib.error
    import urllib.request
    gw = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")

    def _call(url, method="GET"):
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    try:
        with cursor() as cur:
            cur.execute("SELECT id, tenant_id FROM sn_config.agents WHERE slug = %s", (slug,))
            row = cur.fetchone()
        if not row:
            return 0
        tenant_id = row.get("tenant_id") or 1
        url = (f"{gw}/api/v1/sessions/v2?tenant_id={tenant_id}"
               f"&agent_id={row['id']}&limit=200")
        status, body = _call(url)
        if status != 200:
            log.warning("session recycle %s: gateway list returned %s", slug, status)
            return 0
        done = 0
        for sess in (json.loads(body).get("sessions") or []):
            sid = sess.get("session_id")
            if not sid or sess.get("status") in ("completed", "cancelled"):
                continue
            try:
                st, _ = _call(f"{gw}/api/v1/sessions/{sid}/restart", method="POST")
                if st == 200:
                    done += 1
                else:
                    log.warning("session recycle %s: restart %s returned %s", slug, sid, st)
            except urllib.error.URLError as e:
                log.warning("session recycle %s: restart %s failed: %s", slug, sid, e)
        log.info("recomposed %s, recycled %d session(s)", slug, done)
        return done
    except Exception as e:
        log.warning("session recycle %s failed: %s -- the agent keeps its old prompt "
                    "until its session is restarted", slug, e)
        return 0


# ---------------------------------------------------------------------------
# System prompt resolver
# ---------------------------------------------------------------------------
#
# Serves an agent's system_prompt at runtime. Used by the gateway and brin when
# creating ai_sessions; replaces the old "system_prompt baked into agent.yaml".
#
# It renders _compose_body -- the same function that writes CLAUDE.md -- so the
# resolved prompt and the file are one text. It did not always: this endpoint
# assembled role + team + tool contracts + policies by its own code, the file
# carried nine more blocks, and an agent therefore knew different things about
# itself depending on which path woke it. Add a block to the composer and both
# paths get it; there is no second place to remember.
#
# fmt=json additionally reports WHICH policies applied and whether any was
# overridden. That is inspection of the parts; the prompt is `assembled`.

def _team_directory(cur, self_slug):
    """The agent's colleagues, derived from the agents catalog (DRY -- add an agent,
    the directory updates). Excludes self + test/probe rows."""
    cur.execute(
        "SELECT slug, role, title, description FROM sn_config.agents "
        "WHERE enabled AND archived_at IS NULL AND slug <> %s "
        "AND slug NOT LIKE 'demo-%%' AND slug NOT LIKE 'ztest%%' AND slug NOT LIKE 'zzz%%' "
        "AND slug NOT IN ('customer-1','tester') ORDER BY slug",
        (self_slug,))
    out = []
    for r in (cur.fetchall() or []):
        # A colleague needs BOTH names here. The slug is what the messaging tool
        # takes; the title is what a caller says out loud. `role` used to shadow
        # `title`, so an agent whose slug differs from their name was unreachable
        # by name: asked on a call to take something up with John, Alex answered
        # that he had no such colleague -- correctly, because his roster only ever
        # said "- foreman -- Foreman" (2026-08-06). Invisible until then
        # because alex's slug happens to BE his name.
        name = (r.get("title") or "").strip()
        role = (r.get("role") or "").strip()
        desc = (r.get("description") or "").strip()
        line = "- %s" % r["slug"]
        if name and name.lower() != r["slug"].lower():
            line += " (%s)" % name
        line += " -- %s" % (role or name or "colleague")
        if desc:
            line += ". " + desc[:130]
        out.append(line)
    return out


@router.get("/{slug}/system-prompt", dependencies=[Depends(requires("agents.system_prompt.read"))])
def get_system_prompt(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    fmt: str = Query("text", pattern="^(text|json)$"),
):
    """Render the agent's system_prompt from DB parts.

    fmt=text  -> plain markdown (Content-Type: text/markdown)
    fmt=json  -> structured { role, policies[], assembled }
    """
    with cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.slug, a.title, a.role, a.policy_categories,
                   s.slug AS spec_slug, s.body_md AS spec_body
              FROM sn_config.agents a
              LEFT JOIN sn_config.specialists s ON s.id = a.specialist_id
             WHERE a.slug = %s AND a.archived_at IS NULL
            """,
            (slug,),
        )
        agent = cur.fetchone()
        if agent is None:
            raise HTTPException(404, f"agent {slug!r} not found")

        cats = list(agent["policy_categories"] or [])

        # Per-agent overrides (action: include | exclude | override).
        cur.execute(
            """
            SELECT policy_slug, action, override_body_md
              FROM sn_config.agent_policy_opts
             WHERE agent_id = %s
            """,
            (agent["id"],),
        )
        opts = {row["policy_slug"]: row for row in (cur.fetchall() or [])}

        # Global policies are mandatory for every agent.  Category subscriptions
        # only select additional scoped policies; 'include' pulls one explicitly.
        forced_in = [s for s, o in opts.items() if o["action"] == "include"]
        forced_out = [s for s, o in opts.items() if o["action"] == "exclude"]

        cur.execute(
            """
            SELECT slug, category, title, severity, body_md
              FROM sn_config.policies
             WHERE enabled = true AND archived_at IS NULL
               AND (category = 'global' OR category = ANY(%s) OR slug = ANY(%s))
               AND slug <> ALL(%s)
             ORDER BY category, slug
            """,
            (cats, forced_in, forced_out),
        )
        policies = cur.fetchall() or []

        # Tool usage contracts for the agent's granted tools (same source the file
        # composition uses) so the gateway-resolved prompt matches CLAUDE.md.
        tool_contracts = _agent_tool_contracts(cur, agent["id"])
        # The prompt itself: rendered by the SAME composer that writes CLAUDE.md,
        # so a block added to one is in the other by construction. Before this,
        # the two were assembled by different code and had drifted apart -- the
        # resolved prompt was missing the memory workspace, the agent's own
        # channels, its contacts and (for the configurator) the release
        # capability catalogue, so an agent woken through the gateway was told
        # less about itself than the same agent woken from the file.
        assembled = _compose_body(cur, slug) or ""

    # Apply 'override' actions -- swap canonical body_md for override_body_md.
    rendered_policies = []
    for p in policies:
        o = opts.get(p["slug"])
        body = (
            o["override_body_md"]
            if o and o["action"] == "override" and o["override_body_md"]
            else p["body_md"]
        )
        rendered_policies.append({
            "slug": p["slug"],
            "category": p["category"],
            "title": p["title"],
            "severity": p["severity"],
            "body_md": body,
            "overridden": bool(o and o["action"] == "override" and o["override_body_md"]),
        })

    # fmt=json keeps its per-policy detail (which policy applied, whether it was
    # overridden) -- that is inspection, not the prompt. The prompt is `assembled`.
    contracts = [t for t in tool_contracts if (t.get("usage_md") or "").strip()]

    if fmt == "text":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(assembled, media_type="text/markdown; charset=utf-8")

    return {
        "agent_slug": agent["slug"],
        "agent_title": agent["title"],
        "specialist_slug": agent["spec_slug"],
        "policy_categories": cats,
        "tools": [t["name"] for t in contracts],
        "policies": rendered_policies,
        "assembled": assembled,
    }


@router.get("/{slug}/tools", dependencies=[Depends(requires("agents.tools.read"))])
def get_agent_tools(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """Granted tools for an agent with the fields a runtime needs to expose + dispatch
    them: input_schema (function spec), kind, http_method/http_path, integration endpoint.
    Catalog-driven -- the cloud chat loop builds tool specs and dispatches from this."""
    with cursor() as cur:
        cur.execute(
            # integration_slug so a runtime can tell a core-api tool from a
            # third-party one: brin stamps the service proof on the first and
            # must never send it to the second.
            "SELECT t.name, t.kind, t.title, t.description, t.input_schema, "
            "t.http_method, t.http_path, t.usage_md, i.endpoint, t.integration_slug "
            "FROM sn_config.agent_tools at "
            "JOIN sn_config.agents a ON a.id = at.agent_id "
            "JOIN sn_core.tools t ON t.id = at.tool_id "
            "LEFT JOIN sn_config.integrations i ON i.slug = t.integration_slug "
            "WHERE a.slug = %s AND a.archived_at IS NULL "
            "ORDER BY t.name",
            (slug,),
        )
        rows = cur.fetchall()
    return {"tools": [dict(r) for r in rows]}


class PolicyOptPayload(BaseModel):
    action: str  # include | exclude | clear


@router.get("/{slug}/policies", dependencies=[Depends(requires("agents.policies.read"))])
def get_agent_policies(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """Rule table for the agent: every global rule + this agent's own, each with an
    `applied` flag computed the same way the prompt is composed (category match or
    per-agent include, agent-own default-on, minus excludes)."""
    with cursor() as cur:
        cur.execute("SELECT id, policy_categories FROM sn_config.agents "
                    "WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, f"agent {slug!r} not found")
        cats = list(a["policy_categories"] or [])
        cur.execute("SELECT policy_slug, action FROM sn_config.agent_policy_opts WHERE agent_id=%s", (a["id"],))
        opts = {r["policy_slug"]: r["action"] for r in (cur.fetchall() or [])}
        cur.execute(
            "SELECT slug, title, category, body_md, agent_id FROM sn_config.policies "
            "WHERE enabled=true AND archived_at IS NULL "
            "AND (category='global' OR agent_id IS NULL OR agent_id=%s) "
            "ORDER BY (agent_id IS NOT NULL), category, slug",
            (a["id"],),
        )
        out = []
        for p in cur.fetchall() or []:
            scope = "agent" if p["agent_id"] is not None else "global"
            act = opts.get(p["slug"])
            if act == "exclude":
                applied = False
            elif act == "include":
                applied = True
            else:
                applied = (p["category"] == "global") or (scope == "agent") or (p["category"] in cats)
            out.append({"slug": p["slug"], "title": p["title"], "category": p["category"],
                        "body_md": p["body_md"], "scope": scope, "applied": applied})
    return {"policies": out}


@router.put("/{slug}/policies/{policy_slug}",
            dependencies=[Depends(requires("agents.policies.write"))])
def set_agent_policy(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    policy_slug: Annotated[str, PathParam(min_length=2, max_length=64)],
    body: PolicyOptPayload,
):
    """Toggle a rule for this agent. action: include (force on) | exclude (force off) |
    clear (back to default). Writes agent_policy_opts and recomposes CLAUDE.md."""
    if body.action not in ("include", "exclude", "clear"):
        raise HTTPException(400, "action must be include|exclude|clear")
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, f"agent {slug!r} not found")
        cur.execute("DELETE FROM sn_config.agent_policy_opts WHERE agent_id=%s AND policy_slug=%s",
                    (a["id"], policy_slug))
        if body.action != "clear":
            cur.execute("INSERT INTO sn_config.agent_policy_opts (agent_id, policy_slug, action) "
                        "VALUES (%s, %s, %s)", (a["id"], policy_slug, body.action))
        _compose_and_write_claude(cur, slug)
    return {"ok": True, "slug": slug, "policy": policy_slug, "action": body.action}


class ChannelBindPayload(BaseModel):
    enabled: bool = False
    instance_ref: str | None = None


@router.get("/{slug}/channels", dependencies=[Depends(requires("agents.channels.read"))])
def get_agent_channels(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """Channel catalogue with this agent's per-channel binding (enabled + instance_ref)."""
    with cursor() as cur:
        cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, f"agent {slug!r} not found")
        cur.execute(
            "SELECT c.slug AS channel_slug, c.label AS channel_label, c.status, "
            "c.enabled AS channel_enabled, "
            "ac.enabled AS bound_enabled, ac.instance_ref, ac.history_before, "
            "(ac.agent_id IS NOT NULL) AS bound "
            "FROM sn_config.channels c "
            "LEFT JOIN sn_config.agent_channels ac ON ac.channel_id = c.id AND ac.agent_id = %s "
            "ORDER BY c.id",
            (a["id"],),
        )
        # history_before is the answer to "why has it not replied to that one"
        # for anything older than the binding, so it is reported rather
        # than left for someone to find in the table.
        out = [{"channel_slug": r["channel_slug"], "channel_label": r["channel_label"],
                "status": r["status"], "enabled": bool(r["bound_enabled"]),
                # switched on for this node (sn_config.channels.enabled): a channel
                # whose code ships but that nobody set up is not one an agent has
                "channel_enabled": bool(r["channel_enabled"]),
                "instance_ref": r["instance_ref"], "bound": r["bound"],
                "history_before": (r["history_before"].isoformat()
                                   if r["history_before"] else None)}
               for r in (cur.fetchall() or [])]
    return {"channels": out}


@router.put("/{slug}/channels/{channel_slug}",
            dependencies=[Depends(requires("agents.channels.bind"))])
def set_agent_channel(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    channel_slug: Annotated[str, PathParam(min_length=1, max_length=64)],
    body: ChannelBindPayload,
):
    """Bind/unbind a channel for this agent (writes sn_config.agent_channels).

    Binding takes a HISTORY WATERMARK. Whatever is already sitting in
    the mailbox at this moment is history: it will be read and recorded, and
    none of it will be answered. Connecting a real business inbox without this
    replayed months of accumulated mail as live inbound, and the agent answered
    it -- one reply reached an outside party carrying the node's internal
    status.

    The watermark is carried over when the binding already points at the SAME
    instance_ref, and only then. Re-saving an unchanged mailbox is something a
    panel does on every save, and moving the watermark forward each time would
    quietly swallow anything that arrived since the last poll. Pointing the
    binding at a DIFFERENT mailbox is a new connection and takes a fresh one.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, f"agent {slug!r} not found")
        cur.execute("SELECT id FROM sn_config.channels WHERE slug=%s FOR UPDATE", (channel_slug,))
        c = cur.fetchone()
        if c is None:
            raise HTTPException(404, f"channel {channel_slug!r} not found")
        # Serialize API binders on this channel row: without the lock two
        # simultaneous requests can both observe an unbound mailbox.
        if body.enabled and body.instance_ref:
            cur.execute("SELECT a.slug, a.title FROM sn_config.agent_channels ac "
                        "JOIN sn_config.agents a ON a.id=ac.agent_id "
                        "WHERE ac.channel_id=%s AND ac.instance_ref=%s AND ac.enabled "
                        "AND a.enabled AND a.archived_at IS NULL AND a.id<>%s",
                        (c["id"], body.instance_ref, a["id"]))
            holders = cur.fetchall()
            if holders:
                names = ", ".join(f"{r['title']} ({r['slug']})" for r in holders)
                raise HTTPException(409, f"Mailbox {body.instance_ref!r} on {channel_slug} "
                                        f"is already received by {names}.")
        cur.execute("SELECT instance_ref, history_before FROM sn_config.agent_channels "
                    "WHERE agent_id=%s AND channel_id=%s", (a["id"], c["id"]))
        prev = cur.fetchone()
        keep = (prev is not None and prev["instance_ref"] == body.instance_ref)
        watermark = prev["history_before"] if keep else None
        cur.execute("DELETE FROM sn_config.agent_channels WHERE agent_id=%s AND channel_id=%s",
                    (a["id"], c["id"]))
        cur.execute("INSERT INTO sn_config.agent_channels "
                    "  (agent_id, channel_id, instance_ref, enabled, history_before) "
                    "VALUES (%s, %s, %s, %s, COALESCE(%s, now())) "
                    "RETURNING history_before",
                    (a["id"], c["id"], body.instance_ref, body.enabled, watermark))
        history_before = cur.fetchone()["history_before"]
    return {"ok": True, "slug": slug, "channel": channel_slug,
            "enabled": body.enabled,
            "instance_ref": body.instance_ref,
            "history_before": history_before.isoformat(),
            # The owner is connecting an inbox that may hold hundreds of
            # unanswered messages. That is a moment that deserves one sentence,
            # and the API is where it has to come from -- a caller cannot infer
            # it from a timestamp.
            "note": ("Reading what is already here; answering only what arrives "
                     "from now on."
                     if not keep else
                     "Unchanged binding -- the original connection time is kept.")}


# What the agent remembers about the people it talks to, for the agent page
# ("Remembers about you": see it, forget one thing, forget everything). Only a
# Flint agent keeps such a memory of its own (in its home, written as the
# agent), so the read and the forget both go through admin-runner. Other
# runtimes answer supported=false; their long-term memory is the memory
# companion's workspace, shown elsewhere.
def _memory_agent(slug: str) -> dict:
    with cursor() as cur:
        cur.execute(f"SELECT {_RUNTIME_SQL} AS runtime, a.os_user FROM sn_config.agents a "
                    "WHERE a.slug=%s AND a.archived_at IS NULL", (slug,))
        a = cur.fetchone()
    if a is None:
        raise HTTPException(404, f"agent {slug!r} not found")
    return a


@router.get("/{slug}/remembers", dependencies=[Depends(requires("agents.memory.read"))])
def get_agent_remembers(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                        principal: Principal = Depends(get_principal)):
    # store says where the list is kept, for the page to say so: "runtime" is the
    # runtime's own built-in memory (the only one read today); the node's memory
    # or an external store would answer with their own name
    a = _memory_agent(slug)
    from routes.runtimes import _RUNTIMES   # here: runtimes imports more of the app
    runtime = next((r[1] for r in _RUNTIMES if r[4] == a["runtime"]), a["runtime"])
    if a["runtime"] != "flint-cli" or not a["os_user"]:
        return {"supported": False, "store": None, "runtime": runtime, "items": []}
    out = runner_op(principal.username, "agent_memory", {"os_user": a["os_user"], "action": "list"}, timeout=40)
    return {"supported": True, "store": "runtime", "runtime": runtime, "items": (out.get("result") or {}).get("items", [])}


@router.delete("/{slug}/remembers", dependencies=[Depends(requires("agents.memory.forget"))])
def forget_agent_all(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                     principal: Principal = Depends(get_principal)):
    return _forget(slug, principal, {"action": "forget_all"})


@router.delete("/{slug}/remembers/{entry_id}", dependencies=[Depends(requires("agents.memory.forget"))])
def forget_agent_entry(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                       entry_id: Annotated[str, PathParam(min_length=1, max_length=80)],
                       principal: Principal = Depends(get_principal)):
    return _forget(slug, principal, {"action": "forget", "id": entry_id})


def _forget(slug: str, principal: Principal, args: dict) -> dict:
    a = _memory_agent(slug)
    if a["runtime"] != "flint-cli" or not a["os_user"]:
        raise HTTPException(409, "this agent keeps no memory of its own")
    out = runner_op(principal.username, "agent_memory", {"os_user": a["os_user"], **args}, timeout=40)
    removed = (out.get("result") or {}).get("removed", 0)
    with cursor(commit=True) as cur:
        emit(cur, actor=f"user:{principal.username}", event_type="agent.memory_forgotten",
             entity_type="agent", entity_id=slug, payload={"action": args["action"], "removed": removed})
    return {"removed": removed}


class PinPayload(BaseModel):
    pinned: bool


@router.put("/{slug}/documents/{guid}/pin",
            dependencies=[Depends(requires("agents.documents.pin.write"))])
def set_document_pin(
    slug: Annotated[str, PathParam(min_length=2, max_length=41)],
    guid: Annotated[str, PathParam(min_length=2, max_length=80)],
    body: PinPayload,
):
    """Pin / unpin one of the agent's documents. Pinned -> the document's
    content is pre-loaded into the prompt (a snapshot); unpinned -> listed and read on
    demand. Sets/clears the `pin` tag on the mesh doc and recomposes CLAUDE.md."""
    with cursor(commit=True) as cur:
        cur.execute("SELECT mesh_workspace FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
        if a is None or not a["mesh_workspace"]:
            raise HTTPException(404, f"agent or workspace not found: {slug}")
        payload = {"add_tags": ["pin"]} if body.pinned else {"remove_tags": ["pin"]}
        try:
            _mesh_patch("/" + guid, a["mesh_workspace"], payload)
        except Exception as e:
            raise HTTPException(502, f"mesh update failed: {e}")
        _PIN_CACHE.pop(guid, None)  # next compose re-fetches / forgets this doc
        _compose_and_write_claude(cur, slug)
    # Recompose alone left an open conversation on the old prompt: a session reads
    # CLAUDE.md only when it starts. Restart them, as regenerate does.
    recycled = _recycle_agent_sessions(slug)
    return {"ok": True, "slug": slug, "guid": guid, "pinned": body.pinned,
            "sessions_recycled": recycled}


# ---- Agent documents -------------------------------------------------
# The owner's attachments to an agent: mesh docs in its workspace tagged via:ui
# (text notes, uploads as markdown, Google links). Same storage and tags as the
# Studio backend (companions/studio/src/server.js), so both UIs show the same list; the
# design keeps pinned documents in the mesh workspace and injects them through
# the composed CLAUDE.md. Every change recomposes that file and restarts open
# sessions, so it takes effect immediately.

_DOC_MARKITDOWN = "/srv/tools/host-utils/venv/bin/markitdown"
_DOC_UPLOAD_MAX = 25 * 1024 * 1024


def _is_attachment(tags) -> bool:
    return any(str(x).startswith("via:ui") or str(x) in ("source:google-drive", "source:upload")
               for x in (tags or []))


def _mesh_send(method: str, path: str, workspace: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"X-Workspace": workspace, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = _urlreq.Request(MESH_API_URL + path, data=data, method=method, headers=headers)
    with _urlreq.urlopen(req, timeout=10, context=_MESH_CTX) as r:
        body = r.read()
        return json.loads(body) if body else {}


def _doc_workspace(slug: str) -> str:
    with cursor() as cur:
        cur.execute("SELECT mesh_workspace FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        a = cur.fetchone()
    if a is None:
        raise HTTPException(404, f"agent {slug!r} not found")
    if not a["mesh_workspace"]:
        raise HTTPException(409, "agent has no memory workspace")
    return a["mesh_workspace"]


def _documents_changed(slug: str) -> int:
    with cursor(commit=True) as cur:
        _compose_and_write_claude(cur, slug)
    return _recycle_agent_sessions(slug)


def _doc_kind(tags) -> str:
    t = [str(x) for x in (tags or [])]
    if "source:google-drive" in t:
        return "drive"
    if "source:upload" in t:
        return "file"
    return "text"


@router.get("/{slug}/documents", dependencies=[Depends(requires("agents.documents.read"))])
def list_documents(slug: Annotated[str, PathParam(min_length=2, max_length=41)]):
    """The owner's attachments only, newest first. The agent's own memory writes
    (notes, handoffs) are not documents and are left out."""
    ws = _doc_workspace(slug)
    try:
        listing = _mesh_get("/browse?limit=500", ws).get("docs") or []
    except Exception as e:
        raise HTTPException(502, f"mesh browse failed: {e}")
    out = []
    for d in listing:
        tags = d.get("t") or []
        if not _is_attachment(tags):
            continue
        preview = (d.get("p") or "").strip()
        title = (preview.splitlines()[0].strip() if preview else "Document")[:120]
        out.append({"guid": d.get("g"), "title": title, "kind": _doc_kind(tags),
                    "pinned": "pin" in [str(x) for x in tags]})
    return {"workspace": ws, "count": len(out), "documents": out}


class DocumentText(BaseModel):
    title: str = Field(default="", max_length=200)
    content: str = Field(min_length=1, max_length=200000)


@router.post("/{slug}/documents", status_code=201,
             dependencies=[Depends(requires("agents.documents.write"))])
def add_document_text(slug: Annotated[str, PathParam(min_length=2, max_length=41)], body: DocumentText):
    ws = _doc_workspace(slug)
    title = body.title.strip()
    text = body.content.strip()
    content = f"{title}\n\n{text}" if title else text
    try:
        res = _mesh_send("PUT", "/", ws, {"content": content, "tags": ["via:ui"], "source": "panel"})
    except Exception as e:
        raise HTTPException(502, f"mesh create failed: {e}")
    return {"guid": res.get("guid"), "sessions_recycled": _documents_changed(slug)}


@router.post("/{slug}/documents/upload", status_code=201,
             dependencies=[Depends(requires("agents.documents.write"))])
async def upload_document(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                          request: Request, name: str = Query("upload", max_length=200)):
    """Raw file body -> markdown via the host-utils markitdown -> mesh. Only the
    extracted text is kept; the binary is discarded (same as the Studio)."""
    ws = _doc_workspace(slug)
    if not Path(_DOC_MARKITDOWN).exists():
        raise HTTPException(503, "extractor unavailable (host-utils not provisioned)")
    name = re.sub(r"[\r\n]", "", name)[:200] or "upload"
    ext = re.sub(r"[^a-z0-9]", "", (name.rsplit(".", 1)[-1] if "." in name else "").lower())[:8]
    raw = await request.body()
    if len(raw) > _DOC_UPLOAD_MAX:
        raise HTTPException(413, "file too large")
    if not raw:
        raise HTTPException(400, "empty file")
    with tempfile.NamedTemporaryFile(suffix=("." + ext) if ext else "", delete=True) as f:
        f.write(raw)
        f.flush()
        try:
            proc = subprocess.run([_DOC_MARKITDOWN, f.name], capture_output=True, timeout=60)
        except Exception as e:
            raise HTTPException(502, f"extract failed: {e}")
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not text:
        raise HTTPException(422, "no text could be extracted from this file")
    tags = ["via:ui", "source:upload"] + ([f"format:{ext}"] if ext else [])
    try:
        res = _mesh_send("PUT", "/", ws, {"content": f"{name}\n\n{text}", "tags": tags, "source": "panel-upload"})
    except Exception as e:
        raise HTTPException(502, f"mesh create failed: {e}")
    return {"guid": res.get("guid"), "chars": len(text), "sessions_recycled": _documents_changed(slug)}


@router.get("/{slug}/documents/{guid}", dependencies=[Depends(requires("agents.documents.read"))])
def get_document(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                 guid: Annotated[str, PathParam(min_length=2, max_length=80)]):
    """One attachment's text as the agent has it (the extracted markdown for an
    upload, the pointer for a Google link). Attachments only, like delete: this is
    not a reader for the agent's private memory."""
    ws = _doc_workspace(slug)
    try:
        doc = _mesh_get("/" + guid, ws)
    except Exception:
        raise HTTPException(404, "document not found")
    tags = doc.get("tags") or []
    if not _is_attachment(tags):
        raise HTTPException(404, "document not found")
    content = doc.get("content") or ""
    lines = content.splitlines()
    return {"guid": guid, "title": (lines[0].strip() if lines else "Document")[:120],
            "kind": _doc_kind(tags), "pinned": "pin" in [str(x) for x in tags],
            "content": content, "updated_at": doc.get("updated_at")}


@router.delete("/{slug}/documents/{guid}", status_code=204,
               dependencies=[Depends(requires("agents.documents.delete"))])
def delete_document(slug: Annotated[str, PathParam(min_length=2, max_length=41)],
                    guid: Annotated[str, PathParam(min_length=2, max_length=80)]):
    """Removes an owner attachment. Refuses anything else: this route must not be a
    way to delete the agent's own memory."""
    ws = _doc_workspace(slug)
    try:
        doc = _mesh_get("/" + guid, ws)
    except Exception:
        raise HTTPException(404, "document not found")
    if not _is_attachment(doc.get("tags") or doc.get("t") or []):
        raise HTTPException(409, "not an attached document; the agent's own memory is not deleted here")
    try:
        _mesh_send("DELETE", "/" + guid, ws)
    except Exception as e:
        raise HTTPException(502, f"mesh delete failed: {e}")
    _documents_changed(slug)


class WakesRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90,
                      description="How far back to look. Default 7, max 90.")


@router.post("/me/wakes", response_model=dict)
def my_wakes(body: WakesRequest,
             principal: Principal = Depends(requires("agents.self.wakes.read"))):
    """How often you were woken, by what, and what it cost.

    THERE IS NO AGENT PARAMETER, ON PURPOSE. The answer is about the
    CALLER, and who the caller is comes from the door rather than the request:
    over the core-api unix socket the kernel names them. The moment this
    took a slug it would need a rule about who may ask after whom, and an agent
    could ask after anyone by writing a name.

    Why it exists: asked "why do you keep waking", an agent could only read its
    own crontab, which describes one of the ways it is woken and not the one that
    is actually used. It answered truthfully and under-reported itself to its
    owner, which is a governance problem rather than a reporting inconvenience.

    'asserted' is refused. A bare header over TCP is a claim, and an answer about
    spend must not be built on one.
    """
    if principal.strength not in ("proved", "session"):
        raise HTTPException(
            403,
            "your own wake history is answered only to an identity the node "
            "established: an agent over the core-api unix socket, or a signed-in "
            "session. A name asserted in a header is not enough to ask about spend.",
        )
    slug = principal.username
    with cursor() as cur:
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"'{slug}' is not an agent on this node")
        agent_id = row["id"]

        # Wakes, by what did the waking. entity_id carries the agent; the ACTOR is
        # the component that woke it, so filtering by actor finds nothing -- which
        # is the first thing anyone tries.
        cur.execute(
            """SELECT COALESCE(payload->>'via', 'unknown') AS via,
                      count(*) AS n, max(ts) AS latest
                 FROM sn_core.events
                WHERE event_type = 'agent.woken'
                  AND entity_id = %s
                  AND ts >= now() - make_interval(days => %s)
                GROUP BY 1 ORDER BY 2 DESC""",
            (slug, body.days),
        )
        by_via = [{"via": r["via"], "wakes": int(r["n"]),
                   "latest": r["latest"].isoformat() if r["latest"] else None}
                  for r in cur.fetchall()]

        # What those turns cost. Kept separate from the wake count rather than
        # joined: a wake that never reached the model has no session, and folding
        # the two would hide exactly that case.
        cur.execute(
            """SELECT count(*) AS sessions,
                      COALESCE(SUM((usage->>'cost_usd')::numeric), 0) AS cost,
                      COALESCE(SUM((usage->>'input_tokens')::int), 0)
                    + COALESCE(SUM((usage->>'cache_read_tokens')::int), 0)
                    + COALESCE(SUM((usage->>'cache_write_tokens')::int), 0) AS input_tokens,
                      COALESCE(SUM((usage->>'output_tokens')::int), 0) AS output_tokens
                 FROM sn_core.ai_sessions
                WHERE agent_id = %s
                  AND created_at >= now() - make_interval(days => %s)""",
            (agent_id, body.days),
        )
        u = cur.fetchone()

    total = sum(v["wakes"] for v in by_via)
    return {
        "agent": slug,
        "days": body.days,
        "wakes": total,
        "by_via": by_via,
        "sessions": int(u["sessions"]),
        "cost_usd": float(u["cost"]),
        # Input counts the cached halves too, because that is what was billed;
        # reporting the uncached part as the whole is what made a $0.10 turn look
        # like eight tokens.
        "input_tokens": int(u["input_tokens"]),
        "output_tokens": int(u["output_tokens"]),
        "note": ("Wakes and sessions are counted separately: a wake that ended "
                 "without calling the model has no session, so the two numbers "
                 "differing is information, not an error."),
    }
