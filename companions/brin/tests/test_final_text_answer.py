"""A wake's final text is its answer to the message it was woken for.

On a chat or email turn the final text has always been the answer; on a wake it
was thrown away. An agent asked to report back that wrote its report in plain
text left the person with nothing, also when it closed the message itself and
wrote "Delivered" (2026-10-31). Whether a reply exists is core-api's to say
(core-api tests/test_reminder_recipient.py); here, what the Runner sends it.
"""

import asyncio
import os

os.environ.setdefault("SN_CORE_API_URL", "http://core-api.test")
os.environ.setdefault("AI_GATEWAY_URL", "http://gateway.test")
os.environ.setdefault("BRIN_API_KEY", "test")
os.environ.setdefault("SN_SERVICE_AUTH_SECRET", "test-proof")

import runner  # noqa: E402


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class Client:
    def __init__(self, rows=()):
        self.rows, self.posts = list(rows), []

    async def get(self, url, **kw):
        return Resp(200, {"conversation": self.rows})

    async def post(self, url, **kw):
        self.posts.append((url.rsplit("/", 1)[-1], kw["json"]))
        return Resp(200, {"ok": True})


def row(mid, status="new", **meta):
    return {"id": mid, "direction": "inbound", "status": status, "metadata": meta}


def owed(rows):
    return asyncio.run(runner._owed_answers(Client(rows), "bo"))


def run(before, answer):
    c = Client()
    asyncio.run(runner._answer_with_final_text(c, "bo", before, answer))
    return c.posts


class Seen(Client):
    """core-api takes an agent's name from a service only with the proof; a bare
    Remote-User is refused (403), and every read and write here silently did nothing."""
    def __init__(self, rows=()):
        super().__init__(rows)
        self.headers = []

    async def get(self, url, **kw):
        self.headers.append(kw["headers"])
        return await super().get(url, **kw)

    async def post(self, url, **kw):
        self.headers.append(kw["headers"])
        return await super().post(url, **kw)


def test_every_call_as_the_agent_carries_the_proof():
    c = Seen([row(7, needs_reply=True)])
    asyncio.run(runner._owed_answers(c, "bo"))
    asyncio.run(runner._inbox_state(c, "bo"))
    asyncio.run(runner._waking_sender(c, "bo"))
    asyncio.run(runner._dismiss(c, "bo", 7))
    asyncio.run(runner._answer_with_final_text(c, "bo", {7}, "Done."))
    assert len(c.headers) == 5
    assert all(h["X-SN-Service-Auth"] and h["X-SN-On-Behalf-Of"] == "bo" for h in c.headers)


def test_owed_is_what_asked_for_a_reply_or_a_chat_mention():
    assert owed([row(1, needs_reply=True), row(2, kind="chat"), row(3)]) == {1, 2}


def test_a_request_held_open_is_still_owed():
    assert owed([row(7, status="awaiting_reply", needs_reply=True)]) == {7}


def test_the_text_goes_to_core_api_with_everything_owed():
    assert run({7, 9}, " Done. ") == [("final-text", {"message_ids": [7, 9], "body": "Done."})]


def test_nothing_owed_sends_nothing():
    assert run(set(), "Noted.") == []


def test_no_reply_is_chosen_silence_and_closes_the_requests():
    assert run({7}, "[no_reply].") == [("dismiss", {"message_id": 7, "auto": False})]


def test_an_empty_turn_says_nothing_and_leaves_the_request_open():
    assert run({7}, "  ") == []


def test_a_failed_send_does_not_break_the_wake():
    class Broken(Client):
        async def post(self, url, **kw):
            raise OSError("down")
    asyncio.run(runner._answer_with_final_text(Broken(), "bo", {7}, "Done."))


def test_a_budget_warning_is_an_event_too(monkeypatch):
    class Budget(Client):
        async def get(self, url, **kw):
            return Resp(200, {"warnings": [{"period": "monthly", "limit_usd": 10, "current_spend_usd": 9}]})

    async def quiet(text):
        pass
    monkeypatch.setattr(runner, "_brin_notify", quiet)
    c = Budget()
    asyncio.run(runner._check_budget_warnings(c))
    (posted,) = [j for u, j in c.posts if u == "events"]
    assert posted["event_type"] == "budget.warning" and "monthly" in posted["payload"]["text"]
    assert posted["payload"]["period"] == "monthly"


def _row(mid, metadata):
    return {"id": mid, "direction": "inbound", "status": "new", "metadata": metadata}


def test_a_colleague_passing_on_work_from_a_persons_chat_is_owed_an_answer():
    # The configurator sent a first task from the owner's chat without needs_reply;
    # the new agent wrote seven good questions as its final text, and they were
    # thrown away (2026-10-31).
    origin = {"origin": {"channel": "internal", "conversation": "desk:d1"}, "source": "comm-send"}
    assert asyncio.run(runner._owed_answers(Client([_row(7, origin)]), "bo")) == {7}


def test_a_colleague_saying_thanks_is_not():
    thanks = {"source": "comm-send", "reply_to_message_id": 3}
    assert asyncio.run(runner._owed_answers(Client([_row(7, thanks)]), "bo")) == set()


def test_a_reply_inside_such_work_is_not_owed():
    origin = {"channel": "internal", "conversation": "desk:d1"}
    thanks = {"origin": origin, "reply_to_message_id": 4}
    assert asyncio.run(runner._owed_answers(Client([_row(8, thanks)]), "bo")) == set()
