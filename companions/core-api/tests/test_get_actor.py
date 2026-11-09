"""get_actor() must return a NAME whether it is injected or called directly (no database).

Twelve call sites pass the header explicitly; four call get_actor(request) with no second
argument, which leaves the FastAPI Header() sentinel in place. The sentinel is truthy, so it
used to be returned as the actor and then blew up downstream:

    TypeError: argument of type 'Header' is not iterable

in emit() -> _ensure_actor() -> `":" in actor`, AFTER routes/secrets.py had already written
the secret. The caller saw a 500 for a write that succeeded, and nothing reached the audit
log.

peer_credentials is replaced rather than faked: it reads a real socket off the transport,
which a constructed request does not have.
"""

import importlib

import pytest

import auth


class _Request:
    def __init__(self, host="172.18.0.9", headers=None):
        self.client = type("C", (), {"host": host})() if host is not None else None
        self.headers = headers or {}
        self.method = "GET"


@pytest.fixture(autouse=True)
def _tcp_caller(monkeypatch):
    """Every case here is a TCP caller; the socket path returns before the header matters."""
    # A network is trusted only when the node declares it: declare the one
    # the default caller above is on.
    monkeypatch.setenv("SN_TRUSTED_CIDRS", _Request().client.host)
    importlib.reload(auth)
    monkeypatch.setattr(auth, "peer_credentials", lambda request: None)


def test_direct_call_returns_a_string():
    """The regression: a direct call used to return the Header object itself."""
    actor = auth.get_actor(_Request())
    assert isinstance(actor, str)


def test_direct_call_without_a_header_is_anonymous():
    assert auth.get_actor(_Request(headers={})) == "anonymous"


def test_direct_call_reads_the_header_itself():
    assert auth.get_actor(_Request(headers={"remote-user": "admin"})) == "admin"


def test_explicit_argument_still_wins():
    """The twelve call sites that pass the header must keep working unchanged."""
    assert auth.get_actor(_Request(headers={"remote-user": "ignored"}), "admin") == "admin"


def test_untrusted_source_is_still_refused_a_name():
    """A header from off-network must not become an identity."""
    assert auth.get_actor(_Request(host="203.0.113.7",
                                   headers={"remote-user": "admin"})) == "untrusted"


def test_the_returned_actor_survives_the_audit_path():
    """Reproduces the exact line that raised: events_emit._ensure_actor does

        prefix = actor.split(":", 1)[0] if ":" in actor else actor

    On a Header sentinel that is a TypeError, and it fired after the secret was written.
    """
    actor = auth.get_actor(_Request())
    prefix = actor.split(":", 1)[0] if ":" in actor else actor
    assert isinstance(prefix, str) and prefix
