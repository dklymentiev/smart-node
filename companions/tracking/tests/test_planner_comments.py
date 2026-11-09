"""The log of a task is read through a bounded index, never as one unbounded answer.

A task that several agents pass work through grows a log of tens of kilobytes. These
tests hold the caps: whatever the log holds, one answer stays small, says what it
left out, and every comment is one call away by id.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import mcp_planner as mp  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 3, 11, 21, 0, tzinfo=timezone(timedelta(hours=-5), "CDT"))


def _run(coro):
    return asyncio.run(coro)


def _log(n: int, size: int = 1200, start_id: int = 100) -> list[dict]:
    """n comments, one a minute, oldest first, alternating authors."""
    t0 = NOW - timedelta(minutes=n)
    return [
        {
            "id": start_id + i,
            "task_id": 7,
            "author": ("agent-a", "agent-b", "owner")[i % 3],
            "content": f"comment {i} " + ("x" * size),
            "comment_type": None,
            "created_at": (t0 + timedelta(minutes=i)).astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        for i in range(n)
    ]


@pytest.fixture
def backend(monkeypatch):
    """Serve a log from a stubbed _call the way the planner's REST route does."""
    seen: list = []

    def serve(log):
        async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
            seen.append((method, path, params))
            rows = list(log)
            p = params or {}
            if p.get("comment_id") is not None:
                rows = [c for c in rows if c["id"] == p["comment_id"]]
            if p.get("q"):
                rows = [c for c in rows if p["q"].lower() in c["content"].lower()]
            if p.get("last") is not None:
                rows = rows[-p["last"]:]
            return json.dumps(rows)

        monkeypatch.setattr(mp, "_call", fake_call)
        return seen

    return serve


# ---------------------------------------------------------------------------
# relative time, worked out by the tool and not by the reader
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=3), "3 min ago"),
        (timedelta(minutes=59), "59 min ago"),
        (timedelta(hours=2), "2 h ago (19:00)"),
        (timedelta(hours=22), "22 h ago (yesterday 23:00)"),
        (timedelta(hours=30), "1 d ago (yesterday 15:00)"),
        (timedelta(days=3), "3 d ago (Mar 8 21:00)"),
    ],
)
def test_ago_is_relative_with_the_local_clock_from_an_hour_on(delta, expected) -> None:
    stamp = (NOW - delta).astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert mp._ago(stamp, NOW) == expected


def test_a_stamp_in_the_future_reads_as_just_now() -> None:
    stamp = (NOW + timedelta(minutes=5)).astimezone(UTC).isoformat()
    assert mp._ago(stamp, NOW) == "just now"


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

def test_the_index_of_a_long_log_is_bounded_and_says_what_it_left_out() -> None:
    out = mp._comment_index(_log(60), NOW)
    assert out["comments"] == 60
    assert len(out["index"]) == mp._INDEX_ROWS
    assert out["index"][-1]["id"] == 159 and out["index"][0]["id"] == 120
    gone = out["earlier_not_listed"]
    assert gone["count"] == 20 and gone["ids"] == "100-119"
    assert "before_id=120" in gone["page_back"]
    assert sum(gone["by_author"].values()) == 20
    assert [c["id"] for c in out["newest_in_full"]] == [157, 158, 159]


def test_the_answer_stays_small_however_large_the_log() -> None:
    log = _log(300, size=5000)  # 1.5 MB of log
    out = json.dumps(mp._comment_index(log, NOW), ensure_ascii=False)
    assert len(out) < 14000


def test_each_index_line_shows_only_the_first_words() -> None:
    log = _log(2, size=3000)
    row = mp._comment_index(log, NOW)["index"][0]
    assert row["chars"] > 3000 and len(row["head"]) <= mp._INDEX_HEAD + 3


def test_the_newest_in_full_are_cut_and_say_how_to_read_the_rest() -> None:
    out = mp._comment_index(_log(5, size=4000), NOW)
    last = out["newest_in_full"][-1]
    assert len(last["text"]) == mp._NEWEST_CHARS
    assert f"comment_id={last['id']}" in last["cut"]


def test_paging_back_lists_older_comments_and_leaves_out_the_full_text() -> None:
    out = mp._comment_index(_log(60), NOW, before_id=120)
    assert [c["id"] for c in out["index"]] == list(range(100, 120))
    assert "newest_in_full" not in out and "earlier_not_listed" not in out


def test_an_empty_log_answers_without_failing() -> None:
    out = mp._comment_index([], NOW)
    assert out["comments"] == 0 and out["index"] == [] and "first" not in out


def test_index_times_are_relative_not_utc_stamps() -> None:
    out = mp._comment_index(_log(3), NOW)
    assert out["index"][-1]["when"] == "1 min ago"
    assert "Z" not in json.dumps(out["index"])


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------

def test_the_tool_without_arguments_answers_with_the_index(backend) -> None:
    backend(_log(60))
    out = json.loads(_run(mp.planner_comments(7)))
    assert out["comments"] == 60 and "index" in out


def test_comment_id_reads_one_comment_in_full(backend) -> None:
    seen = backend(_log(10, size=500))
    out = json.loads(_run(mp.planner_comments(7, comment_id=104)))
    assert out["id"] == 104 and out["text"].startswith("comment 4 ") and "more" not in out
    assert seen[-1][2] == {"comment_id": 104}


def test_a_long_comment_is_read_on_with_offset(backend) -> None:
    backend(_log(1, size=45000, start_id=1))
    first = json.loads(_run(mp.planner_comments(7, comment_id=1)))
    assert len(first["text"]) == mp._ONE_COMMENT_CHARS and "offset=20000" in first["more"]
    third = json.loads(_run(mp.planner_comments(7, comment_id=1, offset=40000)))
    assert "more" not in third and len(third["text"]) == first["chars"] - 40000


def test_a_comment_not_on_the_task_is_an_error_not_an_empty_answer(backend) -> None:
    backend(_log(3))
    out = json.loads(_run(mp.planner_comments(7, comment_id=999)))
    assert "error" in out


def test_search_lists_matches_newest_first_with_a_snippet(backend) -> None:
    log = _log(30, size=200)
    log[5]["content"] = "owner approved the second list, next create accounts for hello"
    log[20]["content"] = "accounts created on dev.to"
    backend(log)
    out = json.loads(_run(mp.planner_comments(7, q="accounts")))
    assert out["hits"] == 2 and [m["id"] for m in out["matches"]] == [120, 105]
    assert "accounts" in out["matches"][1]["snippet"]


def test_last_returns_the_newest_few_capped(backend) -> None:
    backend(_log(40, size=5000))
    out = json.loads(_run(mp.planner_comments(7, last=50)))
    assert len(out) == mp._LAST_MAX
    assert all(len(c["text"]) == mp._LAST_CHARS and "comment_id" in c["cut"] for c in out)


def test_a_planner_error_passes_through_instead_of_looking_like_an_empty_log(monkeypatch) -> None:
    async def fake_call(method, path, body=None, params=None):  # noqa: ARG001
        return json.dumps({"error": "HTTP 403", "detail": "no"})

    monkeypatch.setattr(mp, "_call", fake_call)
    for kw in ({}, {"comment_id": 1}, {"q": "x"}, {"last": 3}):
        assert json.loads(_run(mp.planner_comments(7, **kw)))["error"] == "HTTP 403"


def _answer(choice: str, comment: str) -> str:
    """A signed answer as the bridge writes it: the words inside a long envelope."""
    return json.dumps({"answer": {"choice": choice, "comment": comment},
                       "proof_token": "p" * 16000})


def test_a_signed_answer_shows_the_persons_words_not_the_envelope() -> None:
    log = _log(2)
    log[1]["content"] = _answer("approve", "ok, this one better, the next task will be to create accounts on these platforms")
    row = mp._comment_index(log, NOW)["index"][1]
    assert row["chars"] > 16000
    assert row["head"].startswith("PERSON'S DECISION approve: ok, this one better")
    assert "create accounts on these platforms" in row["head"]
    assert "proof_token" not in row["head"]


def test_a_long_decision_is_cut_at_its_own_longer_limit() -> None:
    log = _log(1)
    log[0]["content"] = _answer("reject", "w" * 1000)
    row = mp._comment_index(log, NOW)["index"][0]
    assert len(row["head"]) <= mp._DECISION_HEAD + 3


def test_text_that_only_looks_like_an_answer_is_shown_as_it_is() -> None:
    log = _log(1)
    log[0]["content"] = '{"answer": not json ' + "z" * 300
    row = mp._comment_index(log, NOW)["index"][0]
    assert row["head"].startswith('{"answer": not json') and len(row["head"]) <= mp._INDEX_HEAD + 3


def test_the_newest_in_full_also_unwraps_a_signed_answer() -> None:
    log = _log(1)
    log[0]["content"] = _answer("approve", "go ahead")
    newest = mp._comment_index(log, NOW)["newest_in_full"][0]
    assert newest["text"] == "PERSON'S DECISION approve: go ahead"
