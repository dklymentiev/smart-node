"""Core-api proxy over the node planner -- the fast-tier wrapper.

WHY: routing planner calls through core-api fixes the one inconsistency in the tool
ledger. comm.sh already routes CLI calls through core-api (which emits the required
events); task.sh was the outlier, hitting the planner directly via its container address
so its calls never landed in sn_core.events. The SAME core-api endpoint serves brin (Mode 2) and CLI agents
(Mode 1).

That last step went unwired for six weeks. This module said "point task.sh's
PLANNER_API_URL at /api/planner" and nothing did: the agent tools in
srv-skeleton kept their hardcoded planner address, so the choke point existed
and no agent passed through it. The proxy now carries agent identity and gives the planner
a credential -- so the direct path an agent used to take now refuses it rather
than merely being discouraged by a docstring.

It is a transparent passthrough: any subpath / method / query / body is proxied
unchanged, so callers only swap their base URL and no per-endpoint code lives
here. Around the forward it:
  - resolves the caller (Remote-User, trusted-source-gated by auth.get_actor),
  - enforces the agent's `planner` tool grant (the human owner is not gated),
  - emits paired events tool.invoked -> tool.succeeded/failed, parent-linked by
    the invoked event id so a run reads as one call_id per planner op.
"""
import json
import hmac
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool

from auth import (get_actor, identity_strength, agent_identity_unproved,
                  acting_for, peer_operator_identity)
from db import cursor
from events_emit import emit
from permissions import requires

router = APIRouter()

# The planner's own base. Kept identical to task.sh's historical default so this
# proxy is a drop-in: callers move from this URL to /api/planner, nothing else.
PLANNER_URL = os.environ.get("PLANNER_API_URL", "").rstrip("/")
_TIMEOUT = 30
PLANNER_OPERATOR_HEADER = "X-SN-Planner-Operator"


def _operator_token() -> str:
    return os.environ.get("SN_PLANNER_OPERATOR_TOKEN", "").strip()

# The planner requires a credential from 0.4.0 on, and this one is
# DELEGATING: it lets core-api present a credential and still name the caller it
# is acting for, so an entry made through the panel is authored by the person who
# made it rather than by the door they came through. Every other key on the node
# ignores that name.
#
# Read per call rather than at import, matching auth.proxy_secret(): the
# installer writes it into an EnvironmentFile, and reading it here means a
# restarted service picks up a rotated value with no code path caching the old
# one. Absent, the planner answers 401 and that is what the caller sees -- the
# honest failure, not a silent fall back to an unauthenticated call, which is
# what this task existed to remove.
def _planner_key() -> str:
    return os.environ.get("PLANNER_API_KEY", "").strip()


def _op_label(method: str, subpath: str) -> str:
    """Stable, low-cardinality name for the planner call -> the event payload.op.
    Keeps the ledger readable (planner.list / .get / .create / .update /
    .search / .comment) instead of raw REST paths with ids in them."""
    parts = subpath.strip("/").split("/")
    tail = parts[2:] if len(parts) > 2 and parts[0] == "api" else parts
    noun = tail[0] if tail else "root"
    if noun == "tasks":
        if len(tail) >= 2 and tail[1] == "search":
            return "planner.search"
        if len(tail) >= 3 and tail[2] == "comments":
            return "planner.comment" if method in ("POST", "PUT") else "planner.comments"
        if len(tail) >= 2:  # /tasks/{id}
            return "planner.update" if method in ("PATCH", "PUT") else "planner.get"
        return "planner.create" if method == "POST" else "planner.list"
    verb = {"GET": "read", "POST": "create", "PATCH": "update",
            "PUT": "update", "DELETE": "delete"}.get(method, method.lower())
    return f"planner.{noun}.{verb}"


def _caller(actor: str):
    """(event_actor, agent_id, is_agent) for the identity string.

    An agent (a row in sn_config.agents) gets an `agent:` actor and is grant-
    gated; any other trusted identity (the human owner/admin) gets `user:` and
    bypasses the tool grant -- grants constrain agents, not the operator."""
    if actor in ("", "anonymous", "untrusted"):
        return None, None, False
    if actor.startswith("component:"):
        return actor, None, False
    with cursor() as cur:
        cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (actor,))
        row = cur.fetchone()
    if row:
        return f"agent:{actor}", row["id"], True
    return f"user:{actor}", None, False


def _has_planner_grant(agent_id: int) -> bool:
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.agent_tools at "
            "JOIN sn_core.tools t ON t.id = at.tool_id "
            "WHERE at.agent_id = %s AND t.name = 'planner'", (agent_id,))
        return cur.fetchone() is not None


def _forward(method: str, subpath: str, qs: str, body: bytes,
             remote_user: str, content_type: str | None,
             operator_proof: str | None = None):
    """Blocking: proxy to the planner, return (status, out_bytes, out_ct)."""
    url = f"{PLANNER_URL}/{subpath}"
    if qs:
        url += f"?{qs}"
    req = urllib.request.Request(url, method=method, data=body if body else None)
    # Two headers doing two different jobs: the key says core-api may come in at
    # all, Remote-User says on whose behalf. The planner reads the second only
    # because the first is marked as delegating.
    req.add_header("Remote-User", remote_user)
    if operator_proof:
        req.add_header(PLANNER_OPERATOR_HEADER, operator_proof)
    key = _planner_key()
    if key:
        req.add_header("X-API-Key", key)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        ct = e.headers.get("Content-Type", "application/json") if e.headers else "application/json"
        return e.code, e.read(), ct


def _is_approval(method: str, subpath: str, body: bytes) -> bool:
    """Is this call trying to RECORD AN APPROVAL?

    An approval is a comment whose `comment_type` says so. It is recognised by
    what the call does, not by which script made it: the defect was never about
    approve.sh, it was that any caller on the node could post this body with
    any Remote-User it liked. A future client posting the same shape is caught
    by the same test.

    Unparseable bodies read as "not an approval" and fall through to the normal
    path -- the planner rejects them anyway, and guessing at malformed JSON is
    how a gate acquires a bypass.
    """
    if method == "POST" and any(
        subpath.rstrip("/").endswith(f"/{decision}")
        for decision in ("approve", "modify", "reject")
    ):
        return True
    if method not in ("POST", "PUT", "PATCH") or not body:
        return False
    if "comments" not in subpath:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("comment_type", "")).strip().lower() == "approval"


_READ_METHODS = ("GET", "HEAD")


def _found_nothing(status: int, out: bytes, out_ct: str | None) -> bool:
    """Did this READ come back with no rows?

    An eligibility poll that finds nothing is not an event, it is the absence of
    one, and journalling it twice (invoked + succeeded) is what filled the table:
    measured on archive-node over 48h, 8965 of 8986 tool.invoked rows were GETs --
    planner.list 7826 and planner.comments 1139 -- against 21 rows for every
    write the node performed. A journal that is 99.8% "I looked and there was
    nothing" is one nobody can reason from, which is the same complaint that got
    it filled in the first place.

    Only a SUCCESSFUL read qualifies. A failure is always journalled: an error is
    news whether or not it carried rows.

    Conservative by construction -- anything this cannot positively read as empty
    is journalled. An unparseable body, a non-JSON body, or an object with no
    collection in it (a single-record GET) all return False.
    """
    if status >= 400:
        return False
    if not out:
        return True
    if "json" not in (out_ct or ""):
        return False
    try:
        d = json.loads(out.decode("utf-8"))
    except Exception:
        return False
    if isinstance(d, list):
        return not d
    if isinstance(d, dict):
        # e.g. {"tasks": [], "total": 0} -- empty only if every collection is.
        lists = [v for v in d.values() if isinstance(v, list)]
        return bool(lists) and all(not v for v in lists)
    return False


def _handle(method: str, subpath: str, qs: str, body: bytes,
            actor: str, content_type: str | None, strength: str = "asserted",
            unproved_agent: bool = False, operator_proved: bool = False):
    op = _op_label(method, subpath)
    event_actor, agent_id, is_agent = _caller(actor)

    # A TCP identity with no session or kernel proof is never allowed to mutate
    # planner state.  Otherwise an agent could assert Remote-User: admin and
    # bypass both the owner gate and the agent grant gate.
    asserted_task_mutation = (
        method not in _READ_METHODS
        and strength == "asserted"
        and not operator_proved
        and subpath.strip("/").startswith("api/v1/tasks")
    )

    # An agent name with nothing behind it never reaches the planner.
    # Checked ABOVE the grant gate, and for the same reason the messaging route puts the
    # approval gate above it: whether a caller may wear this name at all must
    # not depend on how the named agent's grants happen to be configured.
    if unproved_agent or asserted_task_mutation:
        # Emit invoked + failed as a pair so the journal stays balanced.
        with cursor(commit=True) as cur:
            inv = emit(cur, actor=f"user:{actor}", event_type="tool.invoked",
                       entity_type="tool", entity_id="planner",
                       payload={"op": op, "method": method, "path": subpath,
                                "identity": strength})
            emit(cur, actor=f"user:{actor}", event_type="tool.failed",
                 entity_type="tool", entity_id="planner", parent_event_id=inv["id"],
                 payload={"op": op, "method": method,
                          "reason": ("agent identity asserted, not proved"
                                     if unproved_agent else
                                     "planner mutation requires a session or kernel proof")})
        detail = (
            f"'{actor}' names an agent; an agent identity is accepted only over "
            "the core-api unix socket, where the kernel establishes it"
            if unproved_agent else
            "planner mutations require an authenticated session or kernel-proved identity"
        )
        return 403, json.dumps({"detail": detail}).encode(), "application/json"

    # unauthenticated: never reaches the planner, still ledgered as a rejection.
    if event_actor is None:
        # Emit invoked + failed as a pair so the journal stays balanced.
        with cursor(commit=True) as cur:
            inv = emit(cur, actor=f"user:{actor or 'anonymous'}", event_type="tool.invoked",
                       entity_type="tool", entity_id="planner",
                       payload={"op": op, "method": method, "path": subpath,
                                "identity": "none"})
            emit(cur, actor=f"user:{actor or 'anonymous'}", event_type="tool.failed",
                 entity_type="tool", entity_id="planner", parent_event_id=inv["id"],
                 payload={"op": op, "method": method, "reason": "unauthenticated"})
        return 403, json.dumps({"detail": "unauthenticated"}).encode(), "application/json"

    # 1) tool.invoked -- committed BEFORE the forward; its id is the call_id.
    #
    # `strength` is on every planner event, not only the gated ones. Until now
    # an owner's approval and one recorded by something on the node claiming to
    # be the owner were indistinguishable afterwards -- all 26 approvals on the
    # reference node read `author=admin` and nothing said which was which.
    #
    # For a READ this is DEFERRED until the answer is known, because a
    # read that found nothing must leave no trace at all -- and the pair cannot
    # be suppressed after the fact, the journal being append-only by trigger.
    # A read is not a mutation, so nothing is lost by not showing it in flight;
    # for every other method the event is still committed before the forward,
    # exactly as before, and a read that is DENIED or FAILS still emits it.
    call_id = None

    def _invoked():
        nonlocal call_id
        if call_id is None:
            with cursor(commit=True) as cur:
                inv = emit(cur, actor=event_actor, event_type="tool.invoked",
                           entity_type="tool", entity_id="planner",
                           payload={"op": op, "method": method, "path": subpath,
                                    "identity": strength})
            call_id = inv["id"]
        return call_id

    if method not in _READ_METHODS:
        _invoked()

    # THE APPROVAL GATE. An approval is the control the owner relies on
    # for anything that commits them to a customer -- a price, a booking, a
    # date -- and it is meant to be the strong half of that gate. It therefore
    # needs an identity the node can PROVE, not one a caller asserted.
    #
    # It is checked here, above the grant gate, on purpose: a grant says which
    # tools an agent may use, and the answer to "may this agent approve as the
    # owner" must not depend on how its grants happen to be configured.
    if _is_approval(method, subpath, body) and strength != "session":
        # _invoked() OUTSIDE the with: cursor() takes a pooled connection, so
        # calling it inside would hold one and wait for a second.
        parent = _invoked()
        with cursor(commit=True) as cur:
            emit(cur, actor=event_actor, event_type="tool.failed",
                 entity_type="tool", entity_id="planner", parent_event_id=parent,
                 payload={"op": op, "reason": "approval requires an authenticated "
                                              "session, not an asserted identity",
                          "identity": strength})
        return 403, json.dumps({
            "detail": "recording an approval requires a signed-in session in "
                      "the panel; this request only asserted its identity"
        }).encode(), "application/json"

    # grant gate -- agents only. A denied call is ledgered and does NOT forward.
    if is_agent and not _has_planner_grant(agent_id):
        parent = _invoked()
        with cursor(commit=True) as cur:
            emit(cur, actor=event_actor, event_type="tool.failed",
                 entity_type="tool", entity_id="planner", parent_event_id=parent,
                 payload={"op": op, "reason": "no planner grant"})
        return 403, json.dumps({"detail": "planner tool not granted"}).encode(), "application/json"

    # 2) forward transparently.
    try:
        status, out, out_ct = _forward(method, subpath, qs, body, actor, content_type)
    except Exception as e:  # planner unreachable / socket error -> terminal failure
        parent = _invoked()
        with cursor(commit=True) as cur:
            emit(cur, actor=event_actor, event_type="tool.failed",
                 entity_type="tool", entity_id="planner", parent_event_id=parent,
                 payload={"op": op, "reason": str(e)[:200]})
        return 502, json.dumps({"detail": f"planner unreachable: {e}"}).encode(), "application/json"

    # 3) terminal event mirrors the planner's own success/failure -- unless this
    # was a read that found nothing, which is journalled as nothing.
    if method in _READ_METHODS and _found_nothing(status, out, out_ct):
        return status, out, out_ct
    ev = "tool.succeeded" if status < 400 else "tool.failed"
    parent = _invoked()
    with cursor(commit=True) as cur:
        # `method` travels with the TERMINAL event, not only with tool.invoked.
        # Whether a call read or wrote is known right here and was thrown away
        # right here, so every consumer had to infer it from the op NAME: the
        # Studio feed carried a hardcoded list of planner read-ops for exactly
        # that reason, which is planner knowledge living in a UI, and a new read
        # op would silently render as work. Saying it at the source costs one
        # field and removes the guessing everywhere downstream.
        ev_payload: dict = {"op": op, "method": method, "status": status,
                             "identity": strength}
        if status >= 400 and out_ct and "json" in out_ct:
            # Include the error body so the journal records which field failed
            # on schema errors (422) or what the planner refused.
            try:
                ev_payload["error"] = json.loads(out)
            except Exception:
                ev_payload["error"] = (out[:500].decode(errors="replace")
                                       if isinstance(out, bytes) else str(out)[:500])
        emit(cur, actor=event_actor, event_type=ev,
             entity_type="tool", entity_id="planner", parent_event_id=parent,
             payload=ev_payload)
    return status, out, out_ct


@router.api_route("/api/planner/{subpath:path}",
                  methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                  dependencies=[Depends(requires("planner.proxy"))])
async def proxy(subpath: str, request: Request, actor: str = Depends(get_actor)):
    # Component actors use their own constrained ports. The general proxy
    # accepts human and agent identities only.
    if actor.startswith("component:"):
        return Response(content=b'{"detail":"component actor requires its service port"}',
                        status_code=403, media_type="application/json")
    body = await request.body()
    # How strongly the identity is known travels WITH the call. Resolved once,
    # here, from the request itself -- _handle must never re-derive it from
    # headers, or there are two answers to one question.
    strength = identity_strength(request)
    operator_actor = peer_operator_identity(request)
    if operator_actor:
        actor = operator_actor
    operator_header_proof = bool(
        _operator_token()
        and hmac.compare_digest(
            request.headers.get(PLANNER_OPERATOR_HEADER, ""), _operator_token()
        )
    )
    # An agent name that nothing but a header carries. Resolved here for
    # the same reason as `strength` -- once, from the request -- and refused in
    # _handle rather than by raising, so the attempt lands in the ledger instead
    # of vanishing into a 403 nobody can audit later.
    unproved_agent = agent_identity_unproved(request, actor, strength)
    # ...unless a VERIFIED SERVICE is acting for that agent. auth.py's own
    # principal resolver already accepts that pair -- `if not _acting and
    # agent_identity_unproved(...)` -- and this route was the one place that
    # asked only the first half, so voice (which sends X-SN-Service-Auth /
    # X-SN-Service / X-SN-On-Behalf-Of and is permitted by SN_SERVICE_AGENTS)
    # could not read the task list at all. The strictness stays where
    # auth.py put it: in the authorisation list, not in the transport --
    # acting_for() returns a pair only when the secret verifies AND the
    # (service, agent) clause permits it.
    _acting = acting_for(request)
    if _acting and _acting[1] == (actor or "").strip().lower():
        unproved_agent = False
        # A verified service acting for this agent is PROOF of who is calling,
        # the same rule auth.get_principal applies (strength="proved" when
        # _acting). Left at "asserted", every planner write from brin on behalf
        # of an HTTP agent (planner_comment, planner_advance) was refused as an
        # asserted task mutation before the grant gate ran, and the journal
        # labelled delegated reads "asserted" too.
        strength = "proved"
    # Blocking db + urllib run off the event loop, matching the node's sync routes.
    status, out, out_ct = await run_in_threadpool(
        _handle, request.method, subpath, request.url.query, body,
        actor, request.headers.get("content-type"), strength, unproved_agent,
        operator_header_proof or bool(operator_actor))
    return Response(content=out, status_code=status, media_type=out_ct)
