"""Unit tests for the decision format (no database required).

The format exists because the previous one was prose: a flow gate asked whether
an owner's comment contained the word "reject", then whether it contained
"revise". These tests hold the shape that replaced it, including the cases that
made the prose version wrong.
"""

from __future__ import annotations

import pytest

from planner import decisions


def test_compose_puts_the_decision_on_the_first_line() -> None:
    body = decisions.compose(decisions.MODIFY, "move the third paragraph up")
    assert body.splitlines()[0] == "DECISION: modify"
    assert "move the third paragraph up" in body


def test_compose_without_a_note_is_still_readable() -> None:
    assert decisions.compose(decisions.APPROVE) == "DECISION: approve"


def test_compose_refuses_a_decision_nobody_declared() -> None:
    with pytest.raises(ValueError):
        decisions.compose("maybe")


@pytest.mark.parametrize("decision", decisions.VALID)
def test_round_trip(decision: str) -> None:
    note = "because of the price line"
    assert decisions.parse(decisions.compose(decision, note)) == (decision, note)


def test_a_note_that_mentions_another_decision_is_still_a_note() -> None:
    """The failure the prose version had: a word anywhere flipped the answer."""
    body = decisions.compose(decisions.APPROVE, "do not reject the next one, revise it")
    assert decisions.parse(body)[0] == decisions.APPROVE


def test_an_ordinary_comment_is_not_a_decision() -> None:
    assert decisions.parse("looks good to me, ship it") == (None, None)
    assert decisions.parse("") == (None, None)
    assert decisions.parse(None) == (None, None)


def test_a_decision_word_nobody_declared_is_not_a_decision() -> None:
    assert decisions.parse("DECISION: ponder\n\nhmm") == (None, None)


def test_case_and_whitespace_do_not_change_the_answer() -> None:
    assert decisions.parse("  decision:  Reject \n\nthe photo is wrong") == (
        decisions.REJECT,
        "the photo is wrong",
    )


def test_the_tag_carries_the_same_decision() -> None:
    assert decisions.tag_for(decisions.MODIFY) == "decision:modify"
    for decision in decisions.VALID:
        assert decisions.tag_for(decision).startswith(decisions.TAG_PREFIX)
