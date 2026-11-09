"""A loopback Remote-User header does not prove an admin-runner caller."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))


def _request(headers):
    return Request({
        "type": "http", "method": "POST", "path": "/operations/run_command",
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


def test_runner_requires_core_api_proof(monkeypatch):
    monkeypatch.setenv("SN_ADMIN_OWNER_ROLES", "super-admin")
    monkeypatch.setenv("SN_ADMIN_DB_USER", "sn_admin_runner")
    monkeypatch.setenv("SN_ADMIN_CLIENT_SECRET", "test-runner-secret")
    from auth import get_actor
    with pytest.raises(HTTPException) as exc:
        get_actor(_request({"Remote-User": "admin"}), "admin")
    assert exc.value.status_code == 403

    request = _request({
        "Remote-User": "someone-else",
        "X-SN-Service": "core-api",
        "X-SN-Service-Auth": "test-runner-secret",
        "X-SN-On-Behalf-Of": "admin",
    })
    assert get_actor(request, "someone-else") == "admin"

    with pytest.raises(HTTPException) as exc:
        get_actor(_request({
            "X-SN-Service": "core-api",
            "X-SN-Service-Auth": "wrong-secret",
            "X-SN-On-Behalf-Of": "admin",
        }), None)
    assert exc.value.status_code == 403
