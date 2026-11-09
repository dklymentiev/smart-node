"""An agent can choose to say nothing, and then nothing is sent.

A node in the field (2026-10-05): every agent copied on a mail thread answered
it, each answer went to the whole thread, and each answer woke the others. The
node's rules told an agent to stay silent when a mail was not for it, but
dispatch had no silent outcome: any closing text was mailed, and an empty
answer failed the turn.

A final answer of exactly NO_REPLY now completes the turn and delivers
nothing. Anything more than that word is an answer and is delivered as before.

Pure unit tests on the harness of test_live_chat: the database, brin and the
channel transport are fakes, so what is asserted is what dispatch DECIDED.
"""
import pytest

import dispatch
from test_live_chat import Node, no_turn_left_running, row  # noqa: F401 -- autouse fixture


def _email(mid, text):
    return (mid, "email", "a@x.test", "agent-b@node.test", text, {}, "t1", "e%d" % mid, None)


def _mail_node(monkeypatch, rows, think):
    node = Node(monkeypatch, rows, think=think)
    monkeypatch.setitem(dispatch._DELIVERERS, "email", node._deliver)
    monkeypatch.setattr(dispatch, "FAST_CHANNELS", ["telegram", "email"])
    monkeypatch.setattr(dispatch.mailrules, "is_auto_generated", lambda *a: False)
    return node


@pytest.mark.parametrize("answer", ["NO_REPLY", "  no_reply\n", "[NO_REPLY]", "NO_REPLY.", "(No_Reply)"])
def test_no_reply_sends_nothing_and_completes_the_turn(monkeypatch, answer):
    node = _mail_node(monkeypatch, [_email(1, "fyi, not for you")], think=lambda a, c: answer)
    node.scan_once()
    assert node.delivered == []
    assert node.state[1] == "processed"
    assert ("agent.turn_completed", 1) in node.events
    assert not any(k in ("agent.turn_failed", "failed") for k, _ in node.events)


def test_an_answer_that_mentions_no_reply_is_still_delivered(monkeypatch):
    text = "NO_REPLY was my first thought, but here are the figures: 42."
    node = _mail_node(monkeypatch, [_email(1, "figures?")], think=lambda a, c: text)
    node.scan_once()
    assert node.delivered == [("agent-b", text)]


def test_messages_read_together_can_be_answered_with_silence(monkeypatch):
    node = Node(monkeypatch, [row(1, "ok"), row(2, "thanks")], think=lambda a, c: "NO_REPLY")
    node.scan_once()
    assert len(node.asked) == 1
    assert node.delivered == []
    assert node.state == {1: "processed", 2: "processed"}
