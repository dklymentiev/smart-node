"""What the Runner tells an agent when it wakes it, and what it hands it.

core-api decides what a wake is for (with-pending-work next_wake): a person
waiting in a chat first, one chat per wake, and it keeps which messages a wake
was handed (message_state.handed_at). The Runner launches that: a chat wake
hands the agent that chat's messages, so it answers them without reading its
whole inbox; it used to get the generic "start with your inbox", read
everything and take five model calls and about 90 seconds to say hello
(2026-10-29).
"""
import asyncio
import os
import unittest

os.environ.setdefault("SN_CORE_API_URL", "http://127.0.0.1:1")
os.environ.setdefault("PLANNER_API_URL", "http://127.0.0.1:1")
os.environ.setdefault("AI_GATEWAY_URL", "http://127.0.0.1:1")

import runner  # noqa: E402


def agent(due, chat=None, flag=None):
    nw = ({"reason": "chat", "desk": "d1", "ids": list(chat),
           "messages": [{"id": i, "sender": "admin", "content": "message %d" % i} for i in chat]}
          if chat else {"reason": "general", "ids": list(due)})
    return {"slug": "otto", "pending_messages": len(due), "pending_ids": list(due),
            "due_ids": list(due), "exhausted_ids": [], "max_id": max(due or [0]),
            "next_wake": nw, "wake_requested_at": flag}


class WakeTextTest(unittest.TestCase):
    def test_a_chat_wake_hands_over_its_messages(self):
        text = runner._wake_text(runner._chat_batch(agent([227, 228], chat=[227, 228])))
        self.assertIn("#227 from admin", text)
        self.assertIn("message 228", text)
        self.assertIn("chat_history", text)
        self.assertNotIn("planner_next_task", text)
        # a reminder comes this way too, with no person having just written
        self.assertNotIn("a person mentioned you", text)

    def test_a_general_wake_keeps_the_general_text(self):
        self.assertIsNone(runner._chat_batch(agent([1])))
        self.assertEqual(runner._wake_text(None), runner.WAKE)


class LaunchTest(unittest.TestCase):
    """tick() with the network replaced: what it launches, with what."""

    def setUp(self):
        runner._last_flag.clear(); runner._inflight.clear()
        self.launched = []
        self.desks = []

        async def busy(_c):
            return 0

        async def report(_a):
            return None

        async def launch(slug, ref, prev_flag, wake, withhold, handed, scope, desk=None, person=None, ends=None):
            self.ends = ends
            self.launched.append((handed, scope, wake == runner.WAKE))
            self.desks.append(desk)
            self.person = person
        self.saved = (runner._pending, runner._busy_count, runner._launch, runner._report_exhausted)
        runner._busy_count, runner._launch, runner._report_exhausted = busy, launch, report

    def tearDown(self):
        runner._pending, runner._busy_count, runner._launch, runner._report_exhausted = self.saved
        runner._inflight.clear()

    def tick(self, a):
        async def pending(_c):
            return [a]
        runner._pending = pending
        asyncio.run(runner.tick())
        runner._inflight.clear()     # the turn is over

    def test_a_chat_wake_is_handed_and_scoped_to_its_chat(self):
        self.tick(agent([5, 7], chat=[7]))
        self.assertEqual(self.launched, [([7], [7], False)])
        # and says which chat it works for, so that chat shows the work live
        self.assertEqual(self.desks, ["d1"])

    def test_a_chat_wake_names_whose_chat_it_is(self):
        a = agent([7], chat=[7])
        a["next_wake"]["person"] = "owner"
        self.tick(a)
        self.assertEqual(self.person, "owner")

    def test_a_person_waiting_ends_the_turn_once_answered(self):
        self.tick(agent([7], chat=[7]))
        self.assertEqual(self.ends, [7])

    def test_work_between_agents_is_not_cut_after_the_first_answer(self):
        # A strategist given the draft it asked for thanked the colleague and was cut
        # off there, the person's result undelivered (2026-10-31).
        a = agent([7], chat=[7])
        a["next_wake"]["messages"][0]["direct"] = False
        a["next_wake"]["chat_agent"] = "bo"
        self.tick(a)
        self.assertIsNone(self.ends)

    def test_work_between_agents_is_told_whose_chat_it_is(self):
        batch = [{"id": 7, "sender": "ann", "content": "the draft", "direct": False}]
        text = runner._wake_text(batch, {"desk": "d1", "chat_agent": "bo"})
        self.assertIn("desk:d1", text)
        self.assertIn("the chat of bo", text)
        self.assertIn("whole", text)
        self.assertNotIn("a person waiting there", text)
        # two agents went on to a second version nobody had asked for (2026-11-01)
        self.assertIn("until they decide", text)
        # two agents thanked each other for ten minutes (2026-11-01)
        self.assertIn("asks nothing of you", text)

    def test_a_general_wake_works_for_no_chat(self):
        self.tick(agent([5]))
        self.assertEqual(self.desks, [None])

    def test_a_general_wake_is_handed_all_that_is_due_and_not_scoped(self):
        self.tick(agent([5]))
        self.assertEqual(self.launched, [([5], None, True)])

    def test_nothing_due_wakes_nobody(self):
        self.tick(agent([]))
        self.assertEqual(self.launched, [])

    def test_a_chat_goes_first_and_leaves_the_task_flag_standing(self):
        flag = "2026-10-29T17:00:00Z"
        self.tick(agent([7], chat=[7], flag=flag))
        self.assertEqual(self.launched, [([7], [7], False)])
        # the chat is answered; the flag still wakes the agent for its task
        self.tick(agent([], flag=flag))
        self.assertEqual(self.launched[-1], ([], None, True))
        # and only once
        self.tick(agent([], flag=flag))
        self.assertEqual(len(self.launched), 2)


class WhatAWakeClosesTest(unittest.TestCase):
    def test_a_chat_wake_closes_only_what_it_was_handed(self):
        # Another chat's reminder that came due meanwhile was closed unseen and
        # never answered (2026-10-31).
        self.assertEqual(runner._woke_it([5, 7, 9], [7]), [7])

    def test_a_general_wake_closes_all_it_found(self):
        self.assertEqual(runner._woke_it([5, 7], None), [5, 7])


class ChatTurnInboxTest(unittest.TestCase):
    def test_a_turn_in_a_chat_goes_without_the_inbox(self):
        # The inbox is the agent's, not the chat's: a reminder from another chat,
        # another person's among them, came with a person's message (2026-10-31).
        from core.executor import turn_withholds
        got = turn_withholds("desk:d1", None)
        self.assertIn("mcp__messaging__inbox_read", got)
        self.assertIn("mcp__messaging__inbox_dismiss", got)

    def test_other_turns_keep_what_they_were_given(self):
        from core.executor import turn_withholds
        self.assertIsNone(turn_withholds("runner-otto-1", None))
        self.assertEqual(turn_withholds("runner-otto-1", ["x"]), ["x"])


class SendAsksForAReplyTest(unittest.TestCase):
    """Whether an answer was needed was the model's guess, and it guessed wrong often
    enough that work vanished (2026-10-31). Asking is the default."""

    def payload(self, *a, **kw):
        import mcp_messaging
        return mcp_messaging._send_payload(*a, **kw)

    def test_a_message_to_a_colleague_asks_for_a_reply(self):
        self.assertIs(self.payload("otto", "do it")["needs_reply"], True)

    def test_it_can_be_left_off(self):
        self.assertNotIn("needs_reply", self.payload("otto", "fyi", needs_reply=False))

    def test_a_person_or_a_relay_cannot_be_asked(self):
        self.assertNotIn("needs_reply", self.payload("a@example.com", "hi"))
        self.assertNotIn("needs_reply", self.payload("desk:d1", "hi"))
        self.assertNotIn("needs_reply", self.payload("owner", "hi", channel="telegram"))


class WakeLanguageTest(unittest.TestCase):
    def test_the_wake_text_carries_no_other_language(self):
        # A turn with no conversation took its language from a Russian example
        # in this text and answered an English-speaking owner in Russian.
        self.assertTrue(runner.WAKE.isascii(), runner.WAKE)


if __name__ == "__main__":
    unittest.main()
