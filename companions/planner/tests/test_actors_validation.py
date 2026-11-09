"""Pure validation tests for ActorsRepo input.

DB-backed CRUD tests live in tests/integration.
"""

from __future__ import annotations

import pytest

from planner.repos.actors import ACTOR_ID_RE, _validate_id, ActorError


@pytest.mark.parametrize(
    "actor_id",
    ["claude", "ai-mic", "team.deliberation", "agent_1", "u1", "system",
     "component:hitl-bridge"],
)
def test_actor_id_valid(actor_id: str) -> None:
    assert ACTOR_ID_RE.match(actor_id)
    _validate_id(actor_id)  # does not raise


@pytest.mark.parametrize(
    "actor_id",
    [
        "",
        "Capital",
        "1leading-digit-ok-but-uppercase-NO".replace("ok", "OK"),
        "trailing space ",
        "with/slash",
        "@reserved",
        ".dotfirst",
        "x" * 65,
    ],
)
def test_actor_id_invalid(actor_id: str) -> None:
    assert not ACTOR_ID_RE.match(actor_id)
    with pytest.raises(ActorError):
        _validate_id(actor_id)
