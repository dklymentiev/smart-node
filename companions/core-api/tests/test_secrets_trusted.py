"""Unit tests for the /resolve trust boundary (no database needed).

The boundary used to be a literal 172.18.0.0/16 inside routes/secrets.py. On a host where
that range was already taken by another bridge, the node's own companions sat on
172.19.0.0/16 and every one of them was refused -- and since the refusal is a deliberately
generic 404, it read as "no such secret" rather than "not allowed".

The decision now lives once, in auth.trusted_client(), and is extended by the
SN_TRUSTED_CIDRS variable that auth.py already parsed for exactly this case.

EXTRA_TRUSTED is built at import time, so each case reloads auth and then routes.secrets.
peer_credentials is replaced rather than faked: it reads a real socket off the transport,
which a constructed request does not have, and what these cases are about is the decision
that follows it.
"""

import importlib
import os

import pytest

import auth
import routes.secrets as secrets


class _Request:
    """Only what the code under test reads. host=None models the unix socket."""

    def __init__(self, host):
        self.client = type("C", (), {"host": host})() if host is not None else None


def _load(cidrs=None, docker_cidr=None, on_socket=False):
    if cidrs is None:
        os.environ.pop("SN_TRUSTED_CIDRS", None)
    else:
        os.environ["SN_TRUSTED_CIDRS"] = cidrs
    if docker_cidr is None:
        os.environ.pop("SN_DOCKER_CIDR", None)
    else:
        os.environ["SN_DOCKER_CIDR"] = docker_cidr
    importlib.reload(auth)
    auth.peer_credentials = (lambda request: (1, 0, 0)) if on_socket else (lambda request: None)
    return importlib.reload(secrets)


@pytest.fixture(autouse=True)
def _restore_env():
    before = {key: os.environ.get(key) for key in ("SN_TRUSTED_CIDRS", "SN_DOCKER_CIDR")}
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(auth)
    importlib.reload(secrets)


def test_declared_network_is_trusted():
    """The case this fixes: a companion on the node's own bridge, declared in the env."""
    m = _load("172.19.0.0/16")
    assert m._trusted(_Request("172.19.0.4")) is True


def test_default_is_unchanged_when_unset():
    """Docker-network trust is disabled when no Docker CIDR is configured."""
    m = _load(None)
    assert m._trusted(_Request("172.19.0.4")) is False
    assert m._trusted(_Request("172.18.0.5")) is False


def test_declared_docker_network_is_trusted():
    m = _load(docker_cidr="172.18.0.0/16")
    assert m._trusted(_Request("172.18.0.5")) is True


def test_unix_socket_is_trusted():
    """No client address means the socket, which the write path already trusts."""
    m = _load(None, on_socket=True)
    assert m._trusted(_Request(None)) is True


def test_loopback_is_trusted():
    m = _load(None)
    assert m._trusted(_Request("127.0.0.1")) is True


def test_off_network_is_refused():
    m = _load("172.19.0.0/16")
    assert m._trusted(_Request("203.0.113.7")) is False


def test_unparseable_client_is_refused():
    m = _load("172.19.0.0/16")
    assert m._trusted(_Request("not-an-ip")) is False


def test_secrets_keeps_no_network_list_of_its_own():
    """The point of the change: one definition, not two that can drift apart.

    Asserted against the module's objects rather than its text -- a test that greps source
    would fail on a docstring that merely MENTIONS the old constant, which is a
    change-detector and not a check.
    """
    import ipaddress
    m = _load(None)
    nets = [n for n, v in vars(m).items()
            if isinstance(v, (ipaddress.IPv4Network, ipaddress.IPv6Network))]
    assert nets == []
    assert not hasattr(m, "DOCKER0_NET")
    assert not hasattr(m, "TRUSTED_NETS")
