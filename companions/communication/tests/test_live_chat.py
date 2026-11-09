"""A person writing to an agent on a live channel: what they must be able to
count on, whatever the agent is doing. docs/node/agent-reliability.md, level 2.

The cases come from one evening on a node in the field (2026-10-14): the owner
asked the dispatcher agent for two photos and two texts, the dispatcher's turns
ran past the channel's 120 s wait again and again, every one of them ended
`failed` at second 120 while the agent went on working, and the owner's next
messages, each a new turn, did the same. The last four, an edit of the text
among them, were never answered.

Three of them were written as xfail(strict) against that code and failed for
the reasons seen that evening; the marks came off with the fix.

Pure unit tests: the database, brin and the channel transport are fakes, so
what is asserted is what dispatch DECIDED.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

import dispatch  # noqa: E402

OWNER = "1000001"
BOT = "agent_b_bot"


@pytest.fixture(autouse=True)
def no_turn_left_running():
    """Each test starts with no turn of an earlier one still running."""
    dispatch._WORKERS.clear()
    yield
    for t in list(dispatch._WORKERS.values()):
        t.join(5)
    dispatch._WORKERS.clear()


def _until(cond, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not cond():
        _pause(0.02)
    return cond()


class Stop(BaseException):
    """Ends dispatch.main() after one scan; not an Exception, so the loop's own
    handlers do not swallow it."""


class Node:
    """The state dispatch reads and writes, recorded."""

    def __init__(self, monkeypatch, rows, think):
        self.rows = rows
        self.state = {}
        self.events = []
        self.asked = []
        self.delivered = []
        self.locks = set()
        mp = monkeypatch
        mp.setattr(dispatch, "FAST_CHANNELS", ["telegram"])
        mp.setattr(dispatch.db, "query", self._query)
        mp.setattr(dispatch, "_set_state", lambda mid, st, err=None: self.state.__setitem__(mid, st))
        mp.setattr(dispatch, "_resolve_mailbox", lambda rcpt, cid, ch: (rcpt, rcpt.split("@")[0]))
        mp.setattr(dispatch.contacts, "may_wake", lambda *a: (True, ""))
        mp.setattr(dispatch, "_claim_agent", self._claim)
        mp.setattr(dispatch, "_release_agent", lambda agent: self.locks.discard(agent))
        mp.setattr(dispatch, "_outbound_cursor", lambda: None)
        mp.setattr(dispatch, "_activity", lambda ch, chat: _nothing())
        mp.setattr(dispatch.db, "emit_turn", lambda agent, kind, mid, turn: self.events.append((kind, mid)))
        mp.setattr(dispatch.db, "emit_failed", lambda ch, mid, stage, exc, actor: self.events.append(("failed", mid)))
        mp.setattr(dispatch, "_DELIVERERS", {"telegram": self._deliver})
        mp.setattr(dispatch, "_ask_brin", self._ask)
        mp.setattr(dispatch.time, "sleep", self._sleep)
        self.think = think

    def _query(self, sql, args=None, fetch=False):
        if "FROM message_state" in sql:
            return [r for r in self.rows if self.state.get(r[0], "new") == "new"]
        return []

    def _claim(self, agent, ref):
        if agent in self.locks:
            return False
        self.locks.add(agent)
        return True

    def _ask(self, channel, agent, conversation_id, content, principal=None):
        self.asked.append((agent, content, time.monotonic()))
        return {"final_answer": self.think(agent, content), "actions": []}

    def _deliver(self, reply, sender, recipient, metadata, thread_id, external_id, agent):
        self.delivered.append((agent, reply))

    @staticmethod
    def _sleep(seconds):
        raise Stop()

    def scan_once(self, wait=True):
        with pytest.raises(Stop):
            dispatch.main()
        if wait:
            for t in list(dispatch._WORKERS.values()):
                t.join(5)


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def row(mid, text, agent="agent-b", chat=OWNER):
    # message_id, channel, sender, recipient, content, metadata, thread_id, external_id, channel_id
    return (mid, "telegram", chat, agent + "@" + BOT, text, {}, chat, "x%d" % mid, None)


# -- what holds today ---------------------------------------------------------

def test_a_message_for_a_busy_agent_waits_and_is_not_lost(monkeypatch):
    node = Node(monkeypatch, [row(1, "hello")], think=lambda a, c: "hi")
    node.locks.add("agent-b")                      # a wake or another chat holds the agent
    node.scan_once()
    assert node.asked == [] and node.state[1] == "new"
    node.locks.clear()
    node.scan_once()
    assert node.state[1] == "processed" and node.delivered == [("agent-b", "hi")]


def test_every_turn_ends_with_exactly_one_outcome(monkeypatch):
    def think(agent, content):
        if content == "boom":
            raise RuntimeError("brin returned no completed final answer")
        return "ok"

    node = Node(monkeypatch, [row(1, "fine"), row(2, "boom", agent="agent-c")], think=think)
    node.scan_once()
    outcomes = [k for k, _ in node.events if k in ("agent.turn_completed", "agent.turn_failed")]
    assert sorted(outcomes) == ["agent.turn_completed", "agent.turn_failed"]
    # the turn that gave no answer is not failed on the spot: it is put back for a
    # retry (the queue update is checked in test_failed_turn_retry.py)
    assert node.state[1] == "processed" and node.state[2] != "failed"


# -- criteria not met yet (2026-10-14) ----------------------------------------

def test_a_turn_longer_than_the_channel_wait_does_not_lose_the_message(monkeypatch):
    """The channel waits for the whole turn: brin is asked to have the gateway
    hold the answer as long as the turn may run, and dispatch waits longer
    than that. It used to give up at 120 s, under the gateway's own window."""
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"final_answer": "two photos are ready"}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(body=json, timeout=timeout)
        return Resp()

    monkeypatch.setattr(dispatch.requests, "post", post)
    monkeypatch.setenv("BRIN_API_KEY", "test")
    out = dispatch._ask_brin("telegram", "agent-b", OWNER, "make two photos")
    assert out["final_answer"] == "two photos are ready"
    assert seen["body"]["wait_seconds"] >= 3600          # the turn limit
    assert seen["timeout"] > seen["body"]["wait_seconds"]


def test_messages_written_during_a_turn_are_read_together_in_the_next_one(monkeypatch):
    texts = ["make two photos", "?", "send me the link", "?"]
    node = Node(monkeypatch, [row(i + 1, t) for i, t in enumerate(texts)], think=lambda a, c: "on it")
    node.scan_once()
    assert len(node.asked) == 1
    assert all(t in node.asked[0][1] for t in ("make two photos", "send me the link"))
    assert all(node.state[i + 1] == "processed" for i in range(4))
    assert len(node.delivered) == 1


def test_a_slow_agent_does_not_hold_up_another_agents_chat(monkeypatch):
    release = threading.Event()

    def think(agent, content):
        if agent == "agent-slow":
            release.wait(3)
        return "ok"

    node = Node(monkeypatch, [row(1, "long job", agent="agent-slow"), row(2, "quick one", agent="agent-b")],
                think=think)
    t0 = time.monotonic()
    worker = threading.Thread(target=lambda: _swallow(lambda: node.scan_once(wait=False)),
                              daemon=True)
    worker.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(a == "agent-b" for a, _, _ in node.asked):
        _pause(0.05)                               # time.sleep is the fake
    answered_b = [at for a, _, at in node.asked if a == "agent-b"]
    release.set()
    worker.join(5)
    assert answered_b and answered_b[0] - t0 < 2


def _swallow(fn):
    try:
        fn()
    except BaseException:  # noqa: BLE001 -- Stop or a failed raises; the asserts decide
        pass


def _pause(seconds):
    threading.Event().wait(seconds)


def test_a_message_written_while_the_agent_works_is_read_in_its_next_turn(monkeypatch):
    """Not dropped, not a turn of its own beside the running one: it waits, and
    the next turn reads it."""
    release = threading.Event()

    def think(agent, content):
        if content == "long job":
            release.wait(3)
        return "done: " + content

    node = Node(monkeypatch, [row(1, "long job")], think=think)
    node.scan_once(wait=False)                        # the turn is running
    assert _until(lambda: node.asked)
    node.rows.append(row(2, "and one more thing"))
    node.scan_once(wait=False)
    assert [c for _, c, _ in node.asked] == ["long job"]
    assert node.state.get(2, "new") == "new"
    release.set()
    for t in list(dispatch._WORKERS.values()):
        t.join(5)
    node.scan_once()
    assert [c for _, c, _ in node.asked] == ["long job", "and one more thing"]
    assert node.state == {1: "processed", 2: "processed"}


def test_each_email_is_still_its_own_turn(monkeypatch):
    node = Node(monkeypatch, [], think=lambda a, c: "re: " + c)
    monkeypatch.setitem(dispatch._DELIVERERS, "email", node._deliver)
    monkeypatch.setattr(dispatch, "FAST_CHANNELS", ["telegram", "email"])
    monkeypatch.setattr(dispatch.mailrules, "is_auto_generated", lambda *a: False)
    node.rows += [(1, "email", "a@x", "agent-b@x", "first", {}, "t1", "e1", None),
                  (2, "email", "a@x", "agent-b@x", "second", {}, "t1", "e2", None)]
    node.scan_once()
    node.scan_once()
    assert [c for _, c, _ in node.asked] == ["first", "second"]
