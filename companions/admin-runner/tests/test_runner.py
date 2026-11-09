"""Trust-boundary tests for identity resolution.

Authorization is now a per-op (role, permission) check in
`permissions.require_permission`; the former `auth.require_owner` is gone
and its tests with it. The identity
side is still here: get_actor must only trust callers that reach the runner
over loopback / docker0 / SN_TRUSTED_CIDRS. Any widening of that boundary
should fail these tests first.
"""
from fastapi import Request

import auth
import operations


def _request(host):
    return Request({"type": "http", "client": (host, 1234), "headers": []})


def test_a_name_from_loopback_is_not_an_actor_without_core_api_proof(monkeypatch):
    """Loopback says where a call came from, not who made it. A name with no
    proof behind it is refused."""
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("SN_ADMIN_CLIENT_SECRET", "test-runner-secret")
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(_request("127.0.0.1"), "alice@example.com")
    assert exc.value.status_code == 403
    assert "core-api service proof" in exc.value.detail


def test_get_actor_rejects_offnet(monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("SN_ADMIN_CLIENT_SECRET", "test-runner-secret")
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(_request("8.8.8.8"), "alice@example.com")
    assert exc.value.status_code == 403
    assert "outside trusted networks" in exc.value.detail


def test_node_status_is_pure():
    r = operations._node_status({})
    assert "hostname" in r and "disk_root" in r and r["disk_root"]["total"] > 0


def test_require_owner_is_gone():
    """The owner gate is replaced by per-op permission checks. A future
    refactor that resurrects require_owner would reopen the single
    chokepoint this test is here to keep closed."""
    assert not hasattr(auth, "require_owner")
