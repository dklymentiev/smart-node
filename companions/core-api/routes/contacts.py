"""Contacts -- the address book, and who may write to whom.

The door for the communication gate. Two halves, and
both are needed for the gate to be usable at all:

  * the CATALOG  -- who exists as a party, and how each is reachable
  * the GRANT    -- which agent may write to which contact, on which channels

Deny by default. An agent with no grants may write to nobody outside the node;
adding a contact does not grant it to anyone. That was the owner's decision on
2026-08-04, taken after an agent answered a marketing blast with the node's
internal status: having written to us is not consent to be answered.

WHY THIS IS AN API AND NOT A PANEL FORM. The configurator is supposed to set the
node up by conversation -- "let my assistant write to the accountant" should be something
an agent does, not something a person does in SQL. So this is granted as a tool
like any other, and an agent holding it manages the address book.

Every grant change RE-COMPOSES the agent's CLAUDE.md, because the prompt carries
a "# Who you may contact" section built from the same rows. The gate enforces and
the prompt informs; they read one table, so they cannot drift apart. Change a
grant without re-composing and the agent is working from a stale boundary.

Handles are stored as given and compared normalised (the gate lowercases both
sides and, for email, extracts the address out of `"Name" <a@b>`), so nothing
here has to clean them up first.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import Principal, get_principal
from db import cursor
from permissions import requires

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])
agent_contacts_router = APIRouter(prefix="/api/agents", tags=["contacts"])

KINDS = {"agent", "person", "customer", "supplier", "vendor", "lead", "partner", "other"}


class ChannelIn(BaseModel):
    channel: str          # channel slug, e.g. "email"
    handle: str


class ContactIn(BaseModel):
    display_name: str
    kind: str = "person"
    org_name: str | None = None
    notes: str = ""
    source: str = ""
    channels: list[ChannelIn] = []


class ContactPatch(BaseModel):
    display_name: str | None = None
    kind: str | None = None
    org_name: str | None = None
    notes: str | None = None
    enabled: bool | None = None


class GrantIn(BaseModel):
    channels: list[str] = []   # channel slugs this agent may use for this contact


def _channel_id(cur, slug: str) -> int:
    cur.execute("SELECT id FROM sn_config.channels WHERE slug = %s", (slug,))
    r = cur.fetchone()
    if r is None:
        raise HTTPException(400, f"unknown channel: {slug}")
    return r["id"]


def _contact_or_404(cur, cid: int) -> dict:
    cur.execute(
        "SELECT id, display_name, kind, org_name, notes, enabled "
        "FROM sn_config.contacts WHERE id = %s AND archived_at IS NULL", (cid,))
    r = cur.fetchone()
    if r is None:
        raise HTTPException(404, f"no contact {cid}")
    return r


def _agent_or_404(cur, slug: str) -> dict:
    cur.execute(
        "SELECT id, slug FROM sn_config.agents "
        "WHERE slug = %s AND archived_at IS NULL", (slug,))
    r = cur.fetchone()
    if r is None:
        raise HTTPException(404, f"no agent {slug}")
    return r


def _recompose(cur, slug: str) -> None:
    """Rewrite the agent's CLAUDE.md so its "# Who you may contact" section matches
    the grants that were just changed. Imported late: routes.agents owns the
    composition and importing it at module load would be circular."""
    try:
        from routes.agents import _compose_and_write_claude
        _compose_and_write_claude(cur, slug)
    except Exception as e:  # a prompt that failed to rewrite must not lose the grant
        log.warning("contacts: grant saved but CLAUDE.md not recomposed for %s: %s", slug, e)


def _with_channels(cur, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    cur.execute(
        "SELECT cc.id, cc.contact_id, ch.slug AS channel, cc.handle, cc.enabled "
        "FROM sn_config.contact_channels cc "
        "JOIN sn_config.channels ch ON ch.id = cc.channel_id "
        "WHERE cc.contact_id = ANY(%s) ORDER BY ch.slug, cc.handle", (ids,))
    by_contact: dict[int, list] = {}
    for c in cur.fetchall() or []:
        by_contact.setdefault(c["contact_id"], []).append(
            {"id": c["id"], "channel": c["channel"], "handle": c["handle"],
             "enabled": c["enabled"]})
    for r in rows:
        r["channels"] = by_contact.get(r["id"], [])
    return rows


# --------------------------------------------------------------- the catalog

@router.get("", dependencies=[Depends(requires("contacts.read"))])
def list_contacts(principal: Principal = Depends(get_principal)):
    with cursor() as cur:
        cur.execute(
            "SELECT id, display_name, kind, org_name, notes, enabled, created_at "
            "FROM sn_config.contacts WHERE archived_at IS NULL "
            "ORDER BY display_name")
        rows = _with_channels(cur, cur.fetchall() or [])
    return {"contacts": rows}


@router.post("", status_code=201,
             dependencies=[Depends(requires("contacts.create"))])
def create_contact(payload: ContactIn, principal: Principal = Depends(get_principal)):
    if payload.kind not in KINDS:
        raise HTTPException(400, f"unknown kind: {payload.kind} (one of {sorted(KINDS)})")
    if not payload.display_name.strip():
        raise HTTPException(400, "display_name is required")
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_config.contacts (kind, display_name, org_name, notes, source) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (payload.kind, payload.display_name.strip(), payload.org_name,
             payload.notes, payload.source or f"created by {principal.username}"))
        cid = cur.fetchone()["id"]
        for ch in payload.channels:
            cur.execute(
                "INSERT INTO sn_config.contact_channels (contact_id, channel_id, handle) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (cid, _channel_id(cur, ch.channel), ch.handle.strip()))
        rows = _with_channels(cur, [_contact_or_404(cur, cid)])
    return {"contact": rows[0]}


@router.patch("/{cid}", dependencies=[Depends(requires("contacts.update"))])
def update_contact(cid: int, payload: ContactPatch,
                   principal: Principal = Depends(get_principal)):
    sets, args = [], []
    for field in ("display_name", "kind", "org_name", "notes", "enabled"):
        val = getattr(payload, field)
        if val is not None:
            if field == "kind" and val not in KINDS:
                raise HTTPException(400, f"unknown kind: {val}")
            sets.append(f"{field} = %s")
            args.append(val)
    if not sets:
        raise HTTPException(400, "nothing to update")
    with cursor(commit=True) as cur:
        _contact_or_404(cur, cid)
        args.append(cid)
        cur.execute(f"UPDATE sn_config.contacts SET {', '.join(sets)}, updated_at = now() "
                    f"WHERE id = %s", tuple(args))
        rows = _with_channels(cur, [_contact_or_404(cur, cid)])
    return {"contact": rows[0]}


@router.delete("/{cid}", dependencies=[Depends(requires("contacts.archive"))])
def archive_contact(cid: int, principal: Principal = Depends(get_principal)):
    """Archive, not delete. The grants cascade away with it, so archiving a
    contact silently removes every agent's permission to write to them -- which
    is the safe direction."""
    with cursor(commit=True) as cur:
        _contact_or_404(cur, cid)
        cur.execute("SELECT DISTINCT a.slug FROM sn_config.agent_contacts g "
                    "JOIN sn_config.agents a ON a.id = g.agent_id WHERE g.contact_id = %s",
                    (cid,))
        affected = [r["slug"] for r in cur.fetchall() or []]
        cur.execute("UPDATE sn_config.contacts SET archived_at = now() WHERE id = %s", (cid,))
        cur.execute("DELETE FROM sn_config.agent_contacts WHERE contact_id = %s", (cid,))
        for slug in affected:
            _recompose(cur, slug)
    return {"archived": cid, "recomposed": affected}


@router.post("/{cid}/channels", status_code=201,
             dependencies=[Depends(requires("contacts.channels.write"))])
def add_channel(cid: int, payload: ChannelIn,
                principal: Principal = Depends(get_principal)):
    if not payload.handle.strip():
        raise HTTPException(400, "handle is required")
    with cursor(commit=True) as cur:
        _contact_or_404(cur, cid)
        cur.execute(
            "INSERT INTO sn_config.contact_channels (contact_id, channel_id, handle) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
            (cid, _channel_id(cur, payload.channel), payload.handle.strip()))
        rows = _with_channels(cur, [_contact_or_404(cur, cid)])
    return {"contact": rows[0]}


@router.delete("/{cid}/channels/{ch_id}",
               dependencies=[Depends(requires("contacts.channels.write"))])
def remove_channel(cid: int, ch_id: int, principal: Principal = Depends(get_principal)):
    with cursor(commit=True) as cur:
        _contact_or_404(cur, cid)
        cur.execute("DELETE FROM sn_config.contact_channels "
                    "WHERE id = %s AND contact_id = %s", (ch_id, cid))
    return {"removed": ch_id}


# ---------------------------------------------------------------- the grants

@agent_contacts_router.get("/{slug}/contacts",
                           dependencies=[Depends(requires("agents.contacts.read"))])
def list_grants(slug: str, principal: Principal = Depends(get_principal)):
    with cursor() as cur:
        a = _agent_or_404(cur, slug)
        cur.execute(
            "SELECT c.id, c.display_name, c.kind, c.org_name, "
            "       array_remove(array_agg(ch.slug ORDER BY ch.slug), NULL) AS channels "
            "  FROM sn_config.agent_contacts g "
            "  JOIN sn_config.contacts c ON c.id = g.contact_id "
            "  LEFT JOIN sn_config.channels ch ON ch.id = ANY (g.allowed_channels) "
            " WHERE g.agent_id = %s AND c.archived_at IS NULL "
            " GROUP BY c.id, c.display_name, c.kind, c.org_name "
            " ORDER BY c.display_name", (a["id"],))
        rows = cur.fetchall() or []
    return {"agent": slug, "contacts": rows}


def _reach_object(request: Request) -> str:
    """The (agent, contact) pair a reach route acts on, as its object."""
    return f"reach:{request.path_params['slug']}:{request.path_params['cid']}"


@agent_contacts_router.put("/{slug}/contacts/{cid}")
def grant_contact(slug: str, cid: int, payload: GrantIn,
                  principal: Principal = Depends(
                      requires("agents.contacts.write", object=_reach_object))):
    """Grant this agent permission to write to this contact on these channels.

    An empty channel list is a real answer and means "granted on nothing", which
    the gate treats as no permission. It is kept rather than rejected so a grant
    can be narrowed to nothing without deleting the row and losing the record
    that it once existed."""
    with cursor(commit=True) as cur:
        a = _agent_or_404(cur, slug)
        _contact_or_404(cur, cid)
        ids = [_channel_id(cur, s) for s in payload.channels]
        cur.execute(
            "INSERT INTO sn_config.agent_contacts (agent_id, contact_id, allowed_channels) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (agent_id, contact_id) DO UPDATE SET allowed_channels = EXCLUDED.allowed_channels",
            (a["id"], cid, ids))
        _recompose(cur, slug)
    return {"agent": slug, "contact": cid, "channels": payload.channels}


@agent_contacts_router.delete("/{slug}/contacts/{cid}")
def revoke_contact(slug: str, cid: int,
                   principal: Principal = Depends(
                       requires("agents.contacts.write", object=_reach_object))):
    with cursor(commit=True) as cur:
        a = _agent_or_404(cur, slug)
        cur.execute("DELETE FROM sn_config.agent_contacts "
                    "WHERE agent_id = %s AND contact_id = %s", (a["id"], cid))
        _recompose(cur, slug)
    return {"agent": slug, "revoked": cid}
