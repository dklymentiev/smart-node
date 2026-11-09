"""Unit tests for the stage vocabulary (no database required).

The transition matrix is gone (migration 0010): any stage may follow any
other, and the rules that remain are guards inside the pipeline. What this
file now pins is the vocabulary itself: the legacy names, the three states,
and that the two retired names collapse onto in_progress.
"""

from __future__ import annotations

import pytest

from planner import statuses as st
from planner.pipeline import (
    STAGES,
    TRANSITIONS,
    allowed_from,
    is_valid_transition,
)
from planner.repos.tasks import _VALID_STATUSES


def test_three_states_and_eight_stages() -> None:
    assert st.STATES == ("queued", "in_progress", "closed")
    assert set(STAGES) == {
        "open", "awaiting_approval", "in_progress", "pending_review",
        "blocked", "escalated", "done", "rejected",
    }


def test_every_input_name_is_valid_for_the_repo() -> None:
    for name in st.STATES + st.LEGACY_STAGES + tuple(st.LEGACY_ALIASES):
        assert name in _VALID_STATUSES


def test_retired_names_collapse_to_in_progress() -> None:
    assert st.normalize_stage("assessing") == "in_progress"
    assert st.normalize_stage("incident") == "in_progress"
    assert st.normalize_stage("blocked") == "blocked"
    assert st.is_stage("assessing") and not st.is_state("assessing")
    assert st.is_state("closed") and not st.is_stage("closed")


def test_no_matrix_every_other_stage_is_reachable() -> None:
    for source in STAGES:
        assert TRANSITIONS[source] == frozenset(s for s in STAGES if s != source)
        assert allowed_from(source) == TRANSITIONS[source]


def test_no_self_loops() -> None:
    for source in STAGES:
        assert source not in TRANSITIONS[source]
        assert not is_valid_transition(source, source)


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("in_progress", "rejected"),   # the move the flows used to fake with a hop
        ("in_progress", "done"),
        ("blocked", "done"),
        ("done", "in_progress"),
        ("rejected", "open"),
        ("awaiting_approval", "done"),
    ],
)
def test_any_move_between_different_stages_is_allowed(from_status: str, to_status: str) -> None:
    assert is_valid_transition(from_status, to_status)


def test_aliases_compare_as_their_stage() -> None:
    assert not is_valid_transition("assessing", "in_progress")
    assert is_valid_transition("incident", "done")


def test_allowed_from_returns_frozenset() -> None:
    assert isinstance(allowed_from("open"), frozenset)
    assert allowed_from("assessing") == allowed_from("in_progress")


def test_compat_mode_defaults_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANNER_STATUS_COMPAT", raising=False)
    assert st.compat_mode() == "legacy"
    monkeypatch.setenv("PLANNER_STATUS_COMPAT", "NEW")
    assert st.compat_mode() == "new"
    monkeypatch.setenv("PLANNER_STATUS_COMPAT", "whatever")
    assert st.compat_mode() == "legacy"
