"""An agent on the node must not be able to approve as the owner.

The defect these cover: every agent reaches the controller from the trusted
network, so `Remote-User: admin` from an agent WAS the owner as far as the API
was concerned. The only thing preventing it was a sentence in the agent's own
instructions -- prompt text, and anything that edits the prompt is past it.

Each test constructs the state in which the wrong answer is possible:

  * an agent asserting the owner's name is refused an approval
  * the owner, whose session the proxy vouched for, is NOT refused
  * a node with no secret configured refuses EVERYONE rather than passing
    everyone, because a gate that opens when misconfigured is not a gate
  * the ledger can tell the two apart afterwards, which it could not before

The paired positive matters as much as the negative here: a refusal test alone
would pass just as well against a gate that refuses everything, which is the
shape of a check that cannot fail.
"""
import json
import sys
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

SECRET = "a" * 64


class _Req:
    """The two things auth reads off a request: where it came from, and what
    headers it carries."""

    def __init__(self, headers=None, ip="172.18.0.1"):
        self.headers = headers or {}

        class _C:
            host = ip
        self.client = _C()


# --------------------------------------------------------------------------
# identity_strength -- the one place the question is answered
# --------------------------------------------------------------------------

def test_the_proxy_proof_makes_a_session(monkeypatch):
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_Req({auth.PROXY_PROOF_HEADER: SECRET})) == "session"


def test_a_bare_remote_user_is_only_asserted(monkeypatch):
    """This is the agent's position: on the trusted network, no proof."""
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_Req({"Remote-User": "admin"})) == "asserted"


def test_a_wrong_proof_is_asserted_not_session(monkeypatch):
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_Req({auth.PROXY_PROOF_HEADER: "b" * 64})) == "asserted"


def test_a_prefix_of_the_secret_is_not_enough(monkeypatch):
    """Guards the comparison itself. `startswith`, or an accidental truncation
    anywhere in the chain, would let a guessable prefix through."""
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_Req({auth.PROXY_PROOF_HEADER: SECRET[:-1]})) == "asserted"


def test_with_no_secret_configured_nothing_is_a_session(monkeypatch):
    """Fail closed. If an unset secret meant 'trust everyone', a node that
    never ran the installer would keep the exact hole this closes -- and it is
    the nodes nobody updated that most need the guard."""
    import auth
    monkeypatch.delenv("SN_PROXY_AUTH_SECRET", raising=False)
    assert auth.identity_strength(_Req({auth.PROXY_PROOF_HEADER: SECRET})) == "asserted"


# --------------------------------------------------------------------------
# Principal / require_session
# --------------------------------------------------------------------------

def test_a_principal_defaults_to_the_weaker_strength():
    """A Principal built by an older caller or a test must not read as proven.
    A missing field is not evidence."""
    from auth import Principal
    p = Principal(username="admin", user_id=1, tenant_id=1, role="super-admin")
    assert p.strength == "asserted"
    assert p.proven is False


def test_require_session_admits_a_proven_owner(monkeypatch):
    from auth import Principal, require_session
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    p = Principal(username="admin", user_id=1, tenant_id=1,
                  role="super-admin", strength="session")
    assert require_session(p) is p


def test_require_session_refuses_an_asserted_owner(monkeypatch):
    """Being the super-admin is not the point -- an agent can claim to be the
    super-admin. What is refused is the lack of proof, not the name."""
    from fastapi import HTTPException
    from auth import Principal, require_session
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    p = Principal(username="admin", user_id=1, tenant_id=1,
                  role="super-admin", strength="asserted")
    with pytest.raises(HTTPException) as e:
        require_session(p)
    assert e.value.status_code == 403


def test_an_unconfigured_node_says_so_instead_of_blaming_the_caller(monkeypatch):
    """503 and not 403: the caller did nothing wrong and retrying will not
    help. Whoever reads the log needs to be sent to core-api.sh, not to the
    owner's login."""
    from fastapi import HTTPException
    from auth import Principal, require_session
    monkeypatch.delenv("SN_PROXY_AUTH_SECRET", raising=False)
    p = Principal(username="admin", user_id=1, tenant_id=1, role="super-admin")
    with pytest.raises(HTTPException) as e:
        require_session(p)
    assert e.value.status_code == 503
    assert "SN_PROXY_AUTH_SECRET" in str(e.value.detail)


# --------------------------------------------------------------------------
# What counts as an approval
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,subpath,body,expected", [
    ("POST", "api/v1/tasks/7/comments", b'{"comment_type":"approval"}', True),
    ("POST", "api/v1/tasks/7/comments", b'{"comment_type":"APPROVAL"}', True),
    ("POST", "api/v1/tasks/7/comments", b'{"comment_type":" approval "}', True),
    ("POST", "api/v1/tasks/7/comments", b'{"comment_type":"note"}', False),
    ("POST", "api/v1/tasks/7/comments", b'{"content":"looks fine"}', False),
    ("GET", "api/v1/tasks/7/comments", b"", False),
    ("POST", "api/v1/tasks", b'{"comment_type":"approval"}', False),
    ("POST", "api/v1/tasks/7/comments", b"not json at all", False),
    ("POST", "api/v1/tasks/7/comments", b'["approval"]', False),
    ("POST", "api/v1/tasks/7/approve", b'{}', True),
    ("POST", "api/v1/tasks/7/modify", b'{}', True),
    ("POST", "api/v1/tasks/7/reject", b'{}', True),
])
def test_recognises_an_approval_by_what_it_does(method, subpath, body, expected):
    """Recognised by the shape of the call, not by which client made it. The
    defect was never about one script; it was that any caller could post this
    body under any name."""
    from routes.planner import _is_approval
    assert _is_approval(method, subpath, body) is expected


# --------------------------------------------------------------------------
# The gate, as _handle actually applies it
# --------------------------------------------------------------------------

@pytest.fixture
def planner_handle(monkeypatch):
    """routes.planner._handle with the database, the ledger and the forward
    replaced. Returns (handle, events, forwarded) so a test can assert both
    what happened and what was written down about it."""
    from routes import planner as planner_route

    events = []
    forwarded = []

    class _Cur:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return {"id": 1}

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    def _emit(cur, actor, event_type, entity_type, entity_id,
              payload=None, parent_event_id=None):
        events.append({"actor": actor, "type": event_type,
                       "payload": payload or {}})
        return {"id": len(events)}

    def _forward(method, subpath, qs, body, remote_user, content_type):
        forwarded.append({"method": method, "subpath": subpath,
                          "remote_user": remote_user})
        return 201, b'{"id": 1}', "application/json"

    monkeypatch.setattr(planner_route, "cursor", lambda **k: _Ctx())
    monkeypatch.setattr(planner_route, "emit", _emit)
    monkeypatch.setattr(planner_route, "_forward", _forward)
    # The caller is a known agent with the planner grant, so nothing BUT the
    # identity gate can be what refuses it.
    monkeypatch.setattr(planner_route, "_caller",
                        lambda actor: (f"agent:{actor}", 7, True))
    monkeypatch.setattr(planner_route, "_has_planner_grant", lambda aid: True)
    return planner_route._handle, events, forwarded


APPROVAL = b'{"content":"approve","comment_type":"approval"}'


def test_an_agent_cannot_approve_as_the_owner(planner_handle):
    """The acceptance, directly: an agent shell sends the owner's name with a
    fully granted tool, and the approval does not reach the planner."""
    handle, events, forwarded = planner_handle
    status, out, _ = handle("POST", "api/v1/tasks/7/comments", "", APPROVAL,
                            "admin", "application/json", "asserted")
    assert status == 403
    assert forwarded == [], "the approval must not reach the planner at all"
    assert "session" in json.loads(out)["detail"]


def test_the_owner_with_a_real_session_still_approves(planner_handle):
    """The paired positive. Without it, a gate that refuses everything would
    pass the test above and nobody would notice until the owner tried."""
    handle, events, forwarded = planner_handle
    status, _, _ = handle("POST", "api/v1/tasks/7/comments", "", APPROVAL,
                          "admin", "application/json", "session")
    assert status == 201
    assert len(forwarded) == 1


def test_ordinary_planner_work_is_untouched(planner_handle):
    """An agent must still do its job. Gating everything on a browser session
    would lock out the agents the node exists to run."""
    handle, events, forwarded = planner_handle
    status, _, _ = handle("POST", "api/v1/tasks", "", b'{"title":"x"}',
                          "some-agent", "application/json", "proved")
    assert status == 201
    assert len(forwarded) == 1


def test_asserted_owner_name_cannot_create_active_work(planner_handle):
    """An agent cannot turn a bare TCP Remote-User: admin into owner work."""
    handle, events, forwarded = planner_handle
    status, out, _ = handle("POST", "api/v1/tasks", "", b'{}',
                            "admin", "application/json", "asserted")
    assert status == 403
    assert forwarded == []
    assert "mutations require" in json.loads(out)["detail"]


def test_proved_operator_bridge_can_write_planner(planner_handle):
    """The owner bridge carries its separate operator proof over TCP."""
    handle, events, forwarded = planner_handle
    status, _, _ = handle("POST", "api/v1/tasks", "", b'{"title":"x"}',
                          "admin", "application/json", "asserted",
                          operator_proved=True)
    assert status == 201
    assert len(forwarded) == 1


def test_a_plain_comment_is_not_gated(planner_handle):
    """The gate is on approvals, not on comments. An agent reporting progress
    is not asking the owner to commit to anything. The agent still arrives over
    the kernel-proved socket; a bare TCP assertion cannot mutate any task."""
    handle, events, forwarded = planner_handle
    status, _, _ = handle("POST", "api/v1/tasks/7/comments", "",
                          b'{"content":"done","comment_type":"note"}',
                          "some-agent", "application/json", "proved")
    assert status == 201
    assert len(forwarded) == 1


def test_the_ledger_tells_the_two_apart(planner_handle):
    """Before this, an owner's approval and one recorded by something on the
    node claiming to be the owner were the same row afterwards: all 26 on the
    reference node read author=admin and nothing said which was which."""
    handle, events, _ = planner_handle
    handle("POST", "api/v1/tasks/7/comments", "", APPROVAL,
           "admin", "application/json", "session")
    handle("POST", "api/v1/tasks/9/comments", "", APPROVAL,
           "admin", "application/json", "asserted")
    identities = [e["payload"].get("identity") for e in events]
    assert "session" in identities
    assert "asserted" in identities


def test_the_refusal_is_recorded_not_only_returned(planner_handle):
    """A refused approval that leaves no trace is indistinguishable from an
    approval nobody attempted."""
    handle, events, _ = planner_handle
    handle("POST", "api/v1/tasks/7/comments", "", APPROVAL,
           "admin", "application/json", "proved")
    failed = [e for e in events if e["type"] == "tool.failed"]
    assert len(failed) == 1
    assert "approval requires an authenticated session" in failed[0]["payload"]["reason"]
