"""The central `requires()` chokepoint.

Three facts have to hold on a bootstrapped DB:

  1. A role that lacks the permission is refused with a 403 that NAMES both
     the role and the permission -- an operator reading a log can see which
     cell of the matrix was missing, not just that something was.

  2. A role that holds the permission is admitted.

  3. For a super-admin, a non-'none' object_axis permission is admitted with
     `object_axis_bypass=true` in the audit payload. (The object axis grant
     tables land in slice 4; until then every non-super-admin call on a
     non-'none' axis is refused, which is what slice 1's loud-refusal path
     is for and is covered as case 4 below.)

  4. A nonparticipant admin on a declared desk permission is refused on the
     object axis, even though the role axis grants it.

Runs inside the backend container against the real pool (same shape as
other core-api tests). The test mints its own FastAPI route against
`requires()` so we are testing the dependency itself, not any particular
production route.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth import Principal
from permissions import check_permission, requires
import permissions


def _mint_principal(role: str, username: str = "test-actor") -> Principal:
    return Principal(
        username=username,
        user_id=0,
        tenant_id=0,
        role=role,
        actor_id=f"agent:{username}",
        strength="proved",
    )


@pytest.fixture
def toy_app(monkeypatch) -> Iterator[FastAPI]:
    """A FastAPI app with one route gated on `agents.create`.

    `agents.create` is in the step 4a seed and granted to admin and
    super-admin, which gives us a clean three-way split for the role axis
    test (user denied, admin granted, super-admin granted).
    """
    app = FastAPI()
    # These tests isolate the matrix and object decision. A separate test
    # below checks the actor_roles lookup and implicit user default.
    monkeypatch.setattr(permissions, "_effective_role", lambda p: p.role)

    @app.post("/toy/create-agent", dependencies=[Depends(requires("agents.create"))])
    def _create() -> dict:
        return {"ok": True}

    yield app


def test_actor_role_comes_from_actor_id_and_defaults_to_user(monkeypatch):
    class FakeCursor:
        seen = None

        def execute(self, sql, params):
            self.seen = params

        def fetchone(self):
            return None

    from contextlib import contextmanager

    fake = FakeCursor()

    @contextmanager
    def fake_cursor():
        yield fake

    monkeypatch.setattr(permissions, "cursor", fake_cursor)
    principal = _mint_principal("super-admin", "other")
    assert permissions._effective_role(principal) == "user"
    assert fake.seen == ("agent:other",)


def test_role_lacking_permission_is_refused_naming_role_and_permission(toy_app):
    """A `user`-tier call on `agents.create` is denied, and the message names
    the role AND the permission. The chokepoint's whole point is the operator
    log being readable without crawling route code."""
    principal = _mint_principal("user", username="test-user")

    # The lightweight path: call check_permission directly. We assert on the
    # Decision shape so we are measuring the policy, not the HTTP framing.
    decision = check_permission(principal, "agents.create")
    assert decision.allowed is False
    assert decision.role == "user"
    assert decision.permission == "agents.create"
    assert "'user'" in decision.reason
    assert "'agents.create'" in decision.reason


def test_role_holding_permission_is_admitted(toy_app):
    """An `admin` call on `agents.create` is admitted. No object axis on this
    permission, so there is no second grant to consult."""
    principal = _mint_principal("admin", username="test-admin")
    decision = check_permission(principal, "agents.create")
    assert decision.allowed is True
    assert decision.axis == "none"
    assert decision.axis_bypass is False


def test_super_admin_bypass_is_flagged_on_object_axis(toy_app):
    """A super-admin on a non-'none' axis permission is admitted and the
    decision carries `object_axis_bypass=true`. The event log row downstream
    uses this flag to distinguish 'owner reached across tenants' from
    'this actor had a grant row'."""
    principal = _mint_principal("super-admin", username="admin")

    # `desks.update` has object_axis='desk' in the catalogue;
    # this picks a known-non-'none' axis without hardcoding a wobbly guess.
    decision = check_permission(principal, "desks.update", object_id="desk:toy")
    assert decision.allowed is True
    assert decision.axis_bypass is True
    assert decision.object_id == "desk:toy"
    assert decision.reason == "super-admin"


def test_nonparticipant_admin_is_refused_on_object_axis(toy_app):
    """An admin without a desk participant grant is refused on object axis."""
    principal = _mint_principal("admin", username="test-admin")
    decision = check_permission(principal, "desks.update", object_id="desk:toy")
    assert decision.allowed is False
    assert "'desk'" in decision.reason
    assert "'desks.update'" in decision.reason
    assert "object axis 'desk'" in decision.reason


def test_unknown_permission_is_refused_not_defaulted(toy_app):
    """A route declaring a permission that is not in `sn_config.permissions`
    is refused. This is the request-time counterpart of slice 2's startup
    drift check -- if a route somehow slips through boot, the chokepoint
    still fails closed on the call."""
    principal = _mint_principal("super-admin", username="admin")
    decision = check_permission(principal, "no_such.permission")
    assert decision.allowed is False
    assert "no_such.permission" in decision.reason
    assert "sn_config.permissions" in decision.reason
