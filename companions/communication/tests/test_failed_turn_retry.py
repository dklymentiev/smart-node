"""A message whose turn failed is tried again, and the person is told when it still
cannot be answered.

A node in the field (2026-10-21): the owner's Telegram messages to one agent, five
in two days, the last at 11:30. Each started a turn, the turn ended `failed` after
about five minutes with "brin returned no completed final answer", the message went
to `failed` and stayed there. Nobody was told, nothing tried it again, and the owner
heard nothing for half an hour.

The properties:

  1. a turn that failed while the agent was thinking puts the message back in the
     queue, not before the first delay, and says nothing to the person yet
  2. the retry is read again once the delay has passed, and when it succeeds the
     answer is delivered and the message is processed
  3. after the last retry the message is failed and the person is told, once, in
     the language they wrote in
  4. a turn that failed while DELIVERING an answer it had is not run again
  5. several messages of one turn are told about once

Pure unit tests on the harness of test_live_chat: the database, brin and the channel
transport are fakes, so what is asserted is what dispatch DECIDED.
"""
import pytest

import dispatch
from test_live_chat import Node, no_turn_left_running, row  # noqa: F401 -- autouse fixture


class RetryNode(Node):
    """Node with the retry bookkeeping of message_state recorded."""

    def __init__(self, monkeypatch, rows, think):
        super().__init__(monkeypatch, rows, think)
        self.attempts, self.not_before, self.delays = {}, {}, []
        self.told, self.notices = set(), []
        self.clock = 0
        monkeypatch.setattr(dispatch, "RETRY_DELAYS", [60, 300], raising=False)
        monkeypatch.setitem(dispatch._DELIVERERS, "telegram", self._deliver)

    def _query(self, sql, args=None, fetch=False):
        if "SELECT COALESCE(wake_attempts" in sql:
            return [(self.attempts.get(args[0], 0),)]
        if "SET status = 'new'" in sql:
            mid = args[3]
            self.state[mid] = "new"
            self.attempts[mid] = args[1]
            self.delays.append(args[2])
            self.not_before[mid] = self.clock + args[2]
            return []
        if "array_append(tags" in sql:
            mid = args[1]
            if mid in self.told:
                return []
            self.told.add(mid)
            return [(mid,)]
        if "FROM message_state" in sql:
            return [r for r in self.rows
                    if self.state.get(r[0], "new") == "new" and self.not_before.get(r[0], 0) <= self.clock]
        return []

    def _deliver(self, reply, sender, recipient, metadata, thread_id, external_id, agent, notice=None):
        (self.notices if notice else self.delivered).append((agent, reply))


def _stage(exc, stage):
    exc.sn_stage = stage
    return exc


def test_a_failed_think_goes_back_in_the_queue_and_the_person_is_not_told_yet(monkeypatch):
    def think(agent, content):
        raise RuntimeError("brin returned no completed final answer")

    node = RetryNode(monkeypatch, [row(1, "https://x.test/post")], think=think)
    node.scan_once()
    assert node.state[1] == "new" and node.attempts[1] == 1 and node.delays == [60]
    assert node.notices == [] and node.delivered == []
    assert ("failed", 1) not in node.events, "a message that will be retried is not recorded as failed"
    assert ("agent.turn_failed", 1) in node.events, "the failed turn is still on the record"


def test_the_retry_waits_for_its_delay_and_then_delivers(monkeypatch):
    calls = []

    def think(agent, content):
        calls.append(content)
        if len(calls) == 1:
            raise RuntimeError("brin returned no completed final answer")
        return "here is the answer"

    node = RetryNode(monkeypatch, [row(1, "question")], think=think)
    node.scan_once()
    node.clock = 30
    node.scan_once()
    assert len(calls) == 1, "read again before the delay has passed"
    node.clock = 61
    node.scan_once()
    assert len(calls) == 2
    assert node.state[1] == "processed" and node.delivered == [("agent-b", "here is the answer")]
    assert node.notices == []


def test_after_the_last_retry_the_message_is_failed_and_the_person_told_once(monkeypatch):
    def think(agent, content):
        raise RuntimeError("brin returned no completed final answer")

    node = RetryNode(monkeypatch, [row(1, "One more competitor")], think=think)
    for clock in (0, 61, 400):
        node.clock = clock
        node.scan_once()
    assert node.state[1] == "failed" and node.attempts[1] == 2
    assert len(node.notices) == 1
    assert "could not answer" in node.notices[0][1]
    node.clock = 5000
    node.scan_once()
    assert len(node.notices) == 1, "the person is told more than once"


def test_the_notice_is_in_english_for_an_english_message(monkeypatch):
    def think(agent, content):
        raise RuntimeError("brin returned no completed final answer")

    node = RetryNode(monkeypatch, [row(1, "another competitor")], think=think)
    monkeypatch.setattr(dispatch, "RETRY_DELAYS", [], raising=False)
    node.scan_once()
    assert node.state[1] == "failed"
    assert len(node.notices) == 1 and node.notices[0][1].startswith("I could not answer")


def test_a_failed_delivery_is_not_run_again(monkeypatch):
    node = RetryNode(monkeypatch, [row(1, "question")], think=lambda a, c: "an answer")

    def boom(*a, **k):
        raise RuntimeError("telegram is down")

    monkeypatch.setitem(dispatch._DELIVERERS, "telegram", boom)
    node.scan_once()
    assert len(node.asked) == 1, "the turn was spent twice"
    assert node.state[1] == "failed" and node.attempts.get(1, 0) == 0
