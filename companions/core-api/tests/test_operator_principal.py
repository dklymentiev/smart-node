"""The operator on the core-api socket resolves to its users row.

get_actor names a non-agent socket caller 'unknown-peer' on purpose, and that
stays. get_principal, which maps a caller to a users row, now falls back to
peer_operator_identity for the configured operator (root by default -> admin),
so root's GET /api/events is answered instead of 403. Found on a stand: sn-op,
running as root, could POST its events but not read them back, and its
single-use approval check could not tell a consumed approval from a fresh one.
"""
import socket
import sys
import types

import pytest
from fastapi import HTTPException

from test_kernel_identity import AGENT_SLUG, AGENT_UID, AGENT_USER, _on_socket, _on_tcp


def _fake_db(monkeypatch, users, os_user_to_slug=None, agents=frozenset()):
    os_user_to_slug = os_user_to_slug or {}

    class _Cur:
        def __init__(self):
            self._row = None

        def execute(self, sql, params=()):
            if "FROM sn_config.users" in sql:
                u = users.get(params[0])
                self._row = dict(u) if u else None
            elif "os_user" in sql:
                slug = os_user_to_slug.get(params[0])
                self._row = {"slug": slug} if slug else None
            else:
                self._row = {"1": 1} if params and params[0] in agents else None

        def fetchone(self):
            return self._row

        def fetchall(self):
            # Only the socket-credential lookup asks for every row, and no
            # credential is registered here.
            return []

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    fake = types.ModuleType("db")
    fake.cursor = lambda **k: _Ctx()
    monkeypatch.setitem(sys.modules, "db", fake)


ADMIN = {"id": 1, "tenant_id": 1, "role": "super-admin", "email": "admin",
         "actor_id": "user:admin", "actor_kind": "human"}


@pytest.fixture(autouse=True)
def _configured_owner(monkeypatch):
    """The owner the installer wrote. auth.py has no built-in name for it."""
    monkeypatch.setenv("SN_OPERATOR_ACTOR", "admin")


def _passwd(monkeypatch, table):
    import auth
    monkeypatch.setattr(auth.pwd, "getpwuid",
                        lambda uid: types.SimpleNamespace(pw_name=table[uid])
                        if uid in table else (_ for _ in ()).throw(KeyError(uid)))


def test_root_on_the_socket_is_the_operator_principal(monkeypatch):
    import auth
    _passwd(monkeypatch, {0: "root"})
    _fake_db(monkeypatch, {"admin": ADMIN})
    p = auth.get_principal(_on_socket(creds=(1, 0, 0)), None)
    assert p.username == "admin" and p.role == "super-admin"
    assert p.strength == "proved"


def test_get_actor_still_names_root_unknown_peer(monkeypatch):
    import auth
    _passwd(monkeypatch, {0: "root"})
    _fake_db(monkeypatch, {"admin": ADMIN})
    assert auth.get_actor(_on_socket(creds=(1, 0, 0)), None) == "unknown-peer"


def test_the_operator_actor_follows_its_setting(monkeypatch):
    import auth
    monkeypatch.setenv("SN_OPERATOR_ACTOR", "owner")
    _passwd(monkeypatch, {0: "root"})
    _fake_db(monkeypatch, {"owner": ADMIN})
    assert auth.get_principal(_on_socket(creds=(1, 0, 0)), None).username == "owner"


def test_another_non_agent_socket_caller_is_still_refused(monkeypatch):
    import auth
    _passwd(monkeypatch, {1234: "someone"})
    _fake_db(monkeypatch, {"admin": ADMIN})
    with pytest.raises(HTTPException) as e:
        auth.get_principal(_on_socket(creds=(1, 1234, 1234)), None)
    assert e.value.status_code == 403


def test_an_agent_on_the_socket_is_still_the_agent(monkeypatch):
    import auth
    _passwd(monkeypatch, {AGENT_UID: AGENT_USER})
    _fake_db(monkeypatch, {AGENT_SLUG: {"id": 9, "tenant_id": 1, "role": "agent",
                                        "email": AGENT_SLUG, "actor_id": "agent:" + AGENT_SLUG,
                                        "actor_kind": "agent"}},
             os_user_to_slug={AGENT_USER: AGENT_SLUG}, agents={AGENT_SLUG})
    assert auth.get_principal(_on_socket(), None).username == AGENT_SLUG


def test_tcp_is_untouched(monkeypatch):
    import auth
    _fake_db(monkeypatch, {"admin": ADMIN})
    with pytest.raises(HTTPException):
        auth.get_principal(_on_tcp(), None)
