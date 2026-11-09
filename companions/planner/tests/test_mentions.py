"""Mentions in task comments (planner.mentions).

A comment that names an actor with ``@name`` tells that actor. These tests pin
what counts as a mention and who is told, because both are easy to widen by
accident: comments are prose, and a parser that is too eager mails the owner
every time somebody pastes a log line with an ``@`` in it.
"""

from __future__ import annotations

import asyncio

import pytest

# conftest.py pins ``planner`` to this companion's src/.
from planner import mentions  # noqa: E402
from planner.mentions import excerpt, mentioned_names  # noqa: E402


# ---------------------------------------------------------------------------
# what counts as a mention
# ---------------------------------------------------------------------------

def test_a_name_after_an_at_sign_is_a_mention() -> None:
    assert mentioned_names("@hazel, this work order is for you.") == ["hazel"]


def test_several_names_come_back_in_order_and_once_each() -> None:
    assert mentioned_names("@bruno then @hazel, and @bruno again") == ["bruno", "hazel"]


def test_the_full_stop_that_ends_a_sentence_is_not_part_of_the_name() -> None:
    assert mentioned_names("Please look at this, @pearl.") == ["pearl"]


def test_an_email_address_is_not_a_mention() -> None:
    assert mentioned_names("write to someone@example.test about it") == []


@pytest.mark.parametrize("text", [
    "> @hazel wrote this earlier",
    "```\n@hazel\n```",
    "~~~\ncurl user@host\n@hazel\n~~~",
    "the decorator `@hazel` is shown, not addressed",
])
def test_a_name_in_a_quote_or_in_code_is_not_a_mention(text: str) -> None:
    assert mentioned_names(text) == []


def test_a_mention_after_a_closed_code_block_counts_again() -> None:
    assert mentioned_names("```\n@bruno\n```\n@hazel please check") == ["hazel"]


def test_no_text_no_mentions() -> None:
    assert mentioned_names(None) == [] and mentioned_names("") == []


def test_the_excerpt_is_one_line_and_bounded() -> None:
    assert excerpt("a\n\n  b   c") == "a b c"
    assert len(excerpt("x" * 500)) == 200


# ---------------------------------------------------------------------------
# who is told
# ---------------------------------------------------------------------------

class _Pool:
    """Stands in for the two reads announce_mentions makes."""

    def __init__(self, task: dict | None, actors: list[str]) -> None:
        self._task, self._actors = task, actors

    async def fetchrow(self, sql: str, *args):
        return self._task

    async def fetch(self, sql: str, *args):
        (asked,) = args
        return [{"id": a} for a in self._actors if a in asked]


TASK_IN_WORK = {"title": "A page", "status": "in_progress", "requester": "otis", "approved_at": None}


def _announce(monkeypatch, pool, author: str, content: str):
    fired: list[dict] = []
    monkeypatch.setattr(mentions, "fire_mention_notify", lambda **kw: fired.append(kw))
    told = asyncio.run(mentions.announce_mentions(
        pool, 7, comment_id=41, author=author, content=content))
    return told, fired


def test_a_mentioned_actor_is_told_with_the_task_and_the_comment(monkeypatch) -> None:
    told, fired = _announce(monkeypatch, _Pool(TASK_IN_WORK, ["hazel"]), "runner",
                            "[work order] demo/shop\n\n@hazel, this work order is for you.")
    assert told == ["hazel"]
    assert fired[0]["actor"] == "hazel" and fired[0]["task_id"] == 7
    assert fired[0]["title"] == "A page"
    assert fired[0]["summary"].startswith("comment 41 by runner: [work order] demo/shop")


def test_the_author_is_not_told_of_its_own_comment(monkeypatch) -> None:
    told, fired = _announce(monkeypatch, _Pool(TASK_IN_WORK, ["hazel", "bruno"]), "hazel",
                            "@hazel and @bruno")
    assert told == ["bruno"] and [f["actor"] for f in fired] == ["bruno"]


def test_a_name_that_is_no_actor_is_ignored(monkeypatch) -> None:
    told, fired = _announce(monkeypatch, _Pool(TASK_IN_WORK, ["hazel"]), "otis",
                            "@everyone and @hazel")
    assert told == ["hazel"] and len(fired) == 1


def test_a_comment_without_a_mention_reads_nothing(monkeypatch) -> None:
    class Untouched:
        async def fetchrow(self, *a):
            raise AssertionError("no mention, so the task must not be read")
    told, fired = _announce(monkeypatch, Untouched(), "otis", "plain words")
    assert told == [] and fired == []


def test_mentions_wait_while_an_agents_task_waits_for_approval(monkeypatch) -> None:
    waiting = {"title": "t", "status": "awaiting_approval", "requester": "otis", "approved_at": None}
    told, fired = _announce(monkeypatch, _Pool(waiting, ["hazel"]), "otis", "@hazel do this")
    assert told == [] and fired == []


def test_the_owner_mentions_through_that_gate(monkeypatch) -> None:
    waiting = {"title": "t", "status": "awaiting_approval", "requester": "otis", "approved_at": None}
    told, _ = _announce(monkeypatch, _Pool(waiting, ["hazel"]), "admin", "@hazel do this")
    assert told == ["hazel"]


def test_a_fault_while_announcing_does_not_fail_the_comment(monkeypatch) -> None:
    class Broken:
        async def fetchrow(self, *a):
            raise RuntimeError("database went away")
    told, fired = _announce(monkeypatch, Broken(), "otis", "@hazel")
    assert told == [] and fired == []
