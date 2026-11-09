"""An agent's name must come from the kernel, not from what it typed.

The defect these cover: `routes/planner.py` resolves the caller from
`Remote-User` and then evaluates THAT agent's grants, so an agent naming another
agent was measured against the other agent's permissions. Every agent on the node
reaches the controller from the trusted network, so the name was a capability
that any process could type.

core-api now also listens on a unix socket, where SO_PEERCRED reports the peer's
uid -- a value the peer never sends and therefore cannot lie about.

Each test constructs the state in which the wrong answer is possible. The one
that matters most is `test_the_header_is_ignored_on_the_socket`: it sends the
OWNER's name, which is the name worth stealing, and asserts the kernel wins.

Two properties are guarded here that are easy to lose in a later refactor:

  * on the socket the header is IGNORED, not merely outranked -- including when
    the uid resolves to nothing. A fallback to the header at that point would
    put the forgeable path back underneath the unforgeable one.
  * `proved` does NOT satisfy require_session. It says which PROCESS is calling;
    an approval needs a human. An agent whose identity is proved must still be
    refused, and that is asserted below rather than assumed.
"""
import ipaddress
import socket
import struct
import sys
import types
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

SECRET = "a" * 64
AGENT_UID = 988
AGENT_GID = 994
AGENT_USER = "agent-foreman"
AGENT_SLUG = "foreman"


# --------------------------------------------------------------------------
# Fakes: a request that did or did not arrive on a unix socket
# --------------------------------------------------------------------------

class _Sock:
    def __init__(self, family, creds=(4242, AGENT_UID, AGENT_GID)):
        self.family = family
        self._creds = creds

    def getsockopt(self, level, optname, buflen):
        return struct.pack("3i", *self._creds)


class _Transport:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, name):
        return self._sock if name == "socket" else None


class _Cycle:
    """Stands in for uvicorn's RequestResponseCycle -- the object that owns the
    ASGI receive callable and holds the transport."""

    def __init__(self, transport):
        self.transport = transport

    async def receive(self):        # noqa: D401 -- only its __self__ is read
        return {}


class _Req:
    def __init__(self, headers=None, ip="172.18.0.1", sock=None):
        self.headers = headers or {}
        # get_actor reads the method: an asserted human name is refused on a
        # mutation and still accepted on a read. Every case here is a read.
        self.method = "GET"

        class _C:
            host = ip
        self.client = _C()
        if sock is not None:
            self.receive = _Cycle(_Transport(sock)).receive


def _on_socket(headers=None, creds=(4242, AGENT_UID, AGENT_GID)):
    return _Req(headers, sock=_Sock(socket.AF_UNIX, creds))


def _on_tcp(headers=None):
    return _Req(headers)


@pytest.fixture
def known_agent(monkeypatch):
    """The node as the tests want it: uid 988 is agent-foreman, and
    sn_config.agents maps that os_user to the slug `foreman`."""
    import auth

    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name=AGENT_USER)
                        if uid == AGENT_UID else (_ for _ in ()).throw(KeyError(uid)))
    _install_fake_db(monkeypatch, {AGENT_USER: AGENT_SLUG}, agents={AGENT_SLUG})


def _install_fake_db(monkeypatch, os_user_to_slug, agents=frozenset()):
    """A `db` module with just the cursor auth.py reaches for. Injected into
    sys.modules because auth imports it lazily, inside the functions."""
    class _Cur:
        def __init__(self):
            self._row = None

        def execute(self, sql, params=()):
            if "actor_credentials" in sql:
                # No registered socket credential on this node: the operator
                # is resolved from SN_OPERATOR_USERS / SN_OPERATOR_ACTOR.
                self._row = None
            elif "os_user" in sql:
                slug = os_user_to_slug.get(params[0])
                self._row = {"slug": slug} if slug else None
            else:                       # the agent_identity_unproved lookup
                self._row = {"1": 1} if params[0] in agents else None

        def fetchone(self):
            return self._row

        def fetchall(self):
            return [self._row] if self._row else []

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    fake = types.ModuleType("db")
    fake.cursor = lambda **k: _Ctx()
    monkeypatch.setitem(sys.modules, "db", fake)


# --------------------------------------------------------------------------
# peer_credentials -- can we see the kernel's answer at all
# --------------------------------------------------------------------------

def test_a_request_with_no_transport_yields_no_credentials():
    """The guard. auth reaches through uvicorn's internals, so anything
    unexpected must cost the caller `proved` and nothing else -- never raise,
    never invent."""
    import auth
    assert auth.peer_credentials(_on_tcp()) is None


def test_a_tcp_socket_yields_no_credentials():
    """SO_PEERCRED is a unix-socket fact. An AF_INET peer must not be read as
    one, or a TCP caller would acquire an identity it never proved."""
    import auth
    assert auth.peer_credentials(_Req(sock=_Sock(socket.AF_INET))) is None


def test_a_unix_socket_yields_the_peer_uid():
    import auth
    assert auth.peer_credentials(_on_socket()) == (4242, AGENT_UID, AGENT_GID)


# --------------------------------------------------------------------------
# peer_identity -- uid -> os_user -> agent
# --------------------------------------------------------------------------

def test_the_uid_resolves_to_the_agent_that_owns_it(known_agent):
    import auth
    assert auth.peer_identity(_on_socket()) == AGENT_SLUG


def test_a_uid_with_no_passwd_entry_names_nobody(known_agent):
    """Answering the wrong name would be worse than answering none."""
    import auth
    assert auth.peer_identity(_on_socket(creds=(1, 4321, 4321))) is None


def test_a_uid_that_owns_no_agent_row_names_nobody(monkeypatch):
    """root and the service accounts reach the socket too. They are not agents."""
    import auth
    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name="root"))
    _install_fake_db(monkeypatch, {})
    assert auth.peer_identity(_on_socket(creds=(1, 0, 0))) is None


def test_operator_identity_is_separate_from_agent_identity(monkeypatch):
    import auth
    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name="root"))
    _install_fake_db(monkeypatch, {})
    assert auth.peer_identity(_on_socket(creds=(1, 0, 0))) is None
    monkeypatch.setenv("SN_OPERATOR_ACTOR", "owner@example.test")
    assert auth.peer_operator_identity(_on_socket(creds=(1, 0, 0))) == "owner@example.test"
    # No built-in owner name: an unconfigured node names nobody.
    monkeypatch.delenv("SN_OPERATOR_ACTOR")
    assert auth.peer_operator_identity(_on_socket(creds=(1, 0, 0))) is None


# --------------------------------------------------------------------------
# identity_strength -- one place answers how strongly we know it
# --------------------------------------------------------------------------

def test_the_socket_makes_an_identity_proved(known_agent, monkeypatch):
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_on_socket()) == "proved"


def test_operator_socket_is_proved_without_becoming_an_agent(monkeypatch):
    import auth
    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name="root"))
    _install_fake_db(monkeypatch, {})
    monkeypatch.setenv("SN_OPERATOR_ACTOR", "owner@example.test")
    assert auth.identity_strength(_on_socket(creds=(1, 0, 0))) == "proved"


def test_a_session_still_outranks_the_socket(known_agent, monkeypatch):
    """They answer different questions: proved says which PROCESS, session says
    a human authenticated. The operations gated on session are the ones a person
    is relied upon to have decided."""
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    req = _on_socket({auth.PROXY_PROOF_HEADER: SECRET})
    assert auth.identity_strength(req) == "session"


def test_tcp_is_still_only_asserted(monkeypatch):
    import auth
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    assert auth.identity_strength(_on_tcp({"Remote-User": "admin"})) == "asserted"


def test_the_strength_scale_is_ordered():
    import auth
    assert auth._strength_at_least("proved", "asserted")
    assert auth._strength_at_least("session", "proved")
    assert not auth._strength_at_least("asserted", "proved")
    assert not auth._strength_at_least("proved", "session")


# --------------------------------------------------------------------------
# get_actor -- the headline
# --------------------------------------------------------------------------

def test_the_header_is_ignored_on_the_socket(known_agent):
    """THE defect, constructed. The request carries the OWNER's name -- the one
    worth stealing -- and the kernel says agent-foreman. The kernel wins."""
    import auth
    assert auth.get_actor(_on_socket({"Remote-User": "admin"}), "admin") == AGENT_SLUG


def test_an_unresolvable_peer_does_not_fall_back_to_the_header(monkeypatch):
    """The property most likely to be lost in a refactor. If a uid that owns no
    agent row quietly used Remote-User instead, the forgeable path would sit
    underneath the unforgeable one and the socket would prove nothing."""
    import auth
    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name="root"))
    _install_fake_db(monkeypatch, {})
    assert auth.get_actor(_on_socket({"Remote-User": "admin"}, creds=(1, 0, 0)),
                          "admin") == "unknown-peer"


def test_off_the_socket_nothing_changed(monkeypatch):
    """The paired positive for the whole mechanism: services and the panel keep
    the TCP bind and keep working exactly as before."""
    import auth
    monkeypatch.setattr(auth, "DOCKER0_NET", ipaddress.ip_network("172.18.0.0/16"))
    assert auth.get_actor(_on_tcp({"Remote-User": "admin"}), "admin") == "admin"


def test_an_untrusted_source_is_still_refused_a_name():
    import auth
    req = _Req({"Remote-User": "admin"}, ip="8.8.8.8")
    assert auth.get_actor(req, "admin") == "untrusted"


# --------------------------------------------------------------------------
# agent_identity_unproved -- who may wear an agent's name
# --------------------------------------------------------------------------

def test_an_agent_name_asserted_over_tcp_is_unproved(monkeypatch):
    import auth
    _install_fake_db(monkeypatch, {}, agents={AGENT_SLUG})
    assert auth.agent_identity_unproved(_on_tcp(), AGENT_SLUG, "asserted") is True


def test_the_same_name_over_the_socket_is_fine(monkeypatch):
    import auth
    _install_fake_db(monkeypatch, {}, agents={AGENT_SLUG})
    assert auth.agent_identity_unproved(_on_tcp(), AGENT_SLUG, "proved") is False


def test_a_human_session_may_also_carry_an_agent_name(monkeypatch):
    """The panel relays the browser user; an operator acting on an agent's
    behalf is a person who signed in, not a process claiming a name."""
    import auth
    _install_fake_db(monkeypatch, {}, agents={AGENT_SLUG})
    assert auth.agent_identity_unproved(_on_tcp(), AGENT_SLUG, "session") is False


def test_a_service_identity_is_untouched(monkeypatch):
    """The rule is deliberately narrow. runner, voice, gateway and cron-runner
    are NOT rows in sn_config.agents, so they keep the TCP bind they already
    use. Without this the release would have taken the node's own services
    down."""
    import auth
    _install_fake_db(monkeypatch, {}, agents={AGENT_SLUG})
    for service in ("runner", "voice", "gateway", "cron-runner"):
        assert auth.agent_identity_unproved(_on_tcp(), service, "asserted") is False


@pytest.mark.parametrize("sentinel", ["", "anonymous", "untrusted", "unknown-peer"])
def test_the_stand_in_names_are_not_agents(monkeypatch, sentinel):
    import auth
    _install_fake_db(monkeypatch, {}, agents={AGENT_SLUG})
    assert auth.agent_identity_unproved(_on_tcp(), sentinel, "asserted") is False


# --------------------------------------------------------------------------
# The safety property: proved is not a substitute for a person
# --------------------------------------------------------------------------

def test_a_proved_agent_still_cannot_approve(monkeypatch):
    """Measured on a live node as 403, and guarded here so a regression in the
    scale cannot quietly promote `proved` into an approval."""
    from fastapi import HTTPException
    from auth import Principal, require_session
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", SECRET)
    p = Principal(username=AGENT_SLUG, user_id=2, tenant_id=1,
                  role="agent", strength="proved")
    assert p.proven is False
    with pytest.raises(HTTPException) as e:
        require_session(p)
    assert e.value.status_code == 403
