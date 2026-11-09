#!/usr/bin/env python3
"""Prove the Runner does not lose an agent or a message.

Both defects are invisible to inspection: the node reads perfectly healthy while
an agent is never woken again and a message has nobody coming for it. So this
harness CONSTRUCTS the failing state rather than checking a status, and it runs
the same scenarios against BOTH the pre-fix and post-fix runner so the difference
is demonstrated, not asserted.

    python3 e2e_wake_resilience.py                 # test the working tree
    python3 e2e_wake_resilience.py --baseline REF  # also run git:REF and expect it to FAIL

Every HTTP call is stubbed, so no model is called and no node is touched.
"""
import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "..", "runner.py")


# --- the fake node ----------------------------------------------------------

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class FakeNode:
    """One agent, one inbox, one scripted answer from brin."""

    def __init__(self, agent="alex", pending_ids=(), wake_status=200,
                 wake_body=None, wake_raises=None, after_states=None):
        self.agent = agent
        self.pending_ids = list(pending_ids)
        self.wake_status = wake_status
        self.wake_body = wake_body if wake_body is not None else {"content": "no work"}
        self.wake_raises = wake_raises
        # per-id status AFTER the wake; None = the agent changed nothing at all.
        # 'routed' is the state that matters here: brin hands a message to the
        # agent and the agent never finishes it, which is NOT the same as new and
        # is exactly as unfinished.
        self.after_states = after_states
        self._woken = False
        self.runtime_calls = []      # (action, error)
        self.dismissed = []
        self.events = []
        self.event_payloads = []
        self.wakes = 0

    def _states(self):
        if self._woken and self.after_states is not None:
            return dict(self.after_states)
        return {i: "new" for i in self.pending_ids}

    def _inbox_payload(self):
        states = self._states()
        return {
            "new_message_ids": [i for i in sorted(states) if states[i] == "new"],
            "conversation": [{"id": i, "direction": "inbound", "status": states[i]}
                             for i in sorted(states)],
        }

    async def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/api/agents/with-pending-work"):
            return Resp(200, {"agents": [{"slug": self.agent,
                                          "pending_messages": len(self.pending_ids),
                                          "max_id": max(self.pending_ids or [0]),
                                          "wake_requested_at": None}]})
        if url.endswith("/api/agents/state"):
            return Resp(200, {"by_status": {"busy": 0}})
        if url.endswith("/api/messages/inbox"):
            return Resp(200, self._inbox_payload())
        raise AssertionError("unexpected GET %s" % url)

    async def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/api/message"):
            self.wakes += 1
            self._woken = True
            if self.wake_raises:
                raise self.wake_raises
            return Resp(self.wake_status, self.wake_body)
        if "/runtime" in url:
            action = json.get("action")
            self.runtime_calls.append((action, json.get("error")))
            return Resp(200, {"claimed": action == "claim"})
        if url.endswith("/api/messages/dismiss"):
            self.dismissed.append(json["message_id"])
            return Resp(200, {"ok": True})
        if url.endswith("/api/events"):
            self.events.append(json.get("event_type"))
            self.event_payloads.append((json.get("event_type"), json.get("payload") or {}))
            return Resp(200, {"ok": True})
        raise AssertionError("unexpected POST %s" % url)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


async def drive(runner, node, ticks=1):
    """Run N ticks against the fake node, letting each launch finish."""
    runner._client = node
    for _ in range(ticks):
        await runner.tick()
        # let the fire-and-forget launch task run to completion
        for _ in range(50):
            if not runner._inflight:
                break
            await asyncio.sleep(0.001)
        # the backoff must not make the test wait a real minute
        for slug in list(getattr(runner, "_retry_after", {})):
            runner._retry_after[slug] = 0


def fresh(path, tag):
    """A runner module with clean module-level state for each scenario."""
    mod = load(path, "runner_%s" % tag)
    mod._inflight.clear()
    mod._last_woken.clear()
    mod._last_flag.clear()

    # Two reads the launch makes on its own, outside the fake client: the turn ceiling
    # from the gateway and who is waiting in the inbox. Left real they fail against a
    # closed port and every wake becomes a "transport error" before it is sent.
    async def _no_ceiling():
        return None

    async def _nobody(client, agent):
        return None

    if hasattr(mod, "refresh_turn_ceiling"):
        mod.refresh_turn_ceiling = _no_ceiling
    if hasattr(mod, "_waking_sender"):
        mod._waking_sender = _nobody
    if hasattr(mod, "runner_call_timeout"):
        mod.runner_call_timeout = lambda: 5.0       # the ceiling is read from the gateway
    if hasattr(mod, "_wake_failures"):
        mod._wake_failures.clear()
        mod._retry_after.clear()
    return mod


# --- the two scenarios ------------------------------------------------------

def scenario_timeout_must_not_condemn(path, tag):
    """brin answers 500 (its own gateway call timed out at 300s).

    PASS = the agent is retried again and again and is never marked broken.
    FAIL = a wake that failed marks it broken, and since a broken agent never appears
           in with-pending-work again, nothing ever wakes it back (a provider limit
           that resets in an hour was enough).
    """
    runner = fresh(path, tag + "_a")
    node = FakeNode(pending_ids=[100], wake_status=500, wake_body={})
    asyncio.run(drive(runner, node, ticks=1))

    actions = [a for a, _ in node.runtime_calls]
    broke_on_first = "broken" in actions
    retried = runner._last_woken.get("alex") in (None, 0)

    # and it keeps failing: still never condemned, still retried
    runner2 = fresh(path, tag + "_a2")
    node2 = FakeNode(pending_ids=[100], wake_status=500, wake_body={})
    asyncio.run(drive(runner2, node2, ticks=8))
    gave_up = "broken" in [a for a, _ in node2.runtime_calls]

    return {
        "condemned on the first failure": broke_on_first,
        "watermark rolled back so a retry is possible": retried,
        "wakes attempted over 8 ticks": node2.wakes,
        "marked broken at any point": gave_up,
        "ok": (not broke_on_first) and retried and node2.wakes > 4 and not gave_up,
    }


def scenario_pause_grows_to_a_ceiling(path, tag):
    """The gateway answers 200 with failed=true (the provider is at its limit).

    PASS = no broken, and the pause the Runner names in each agent.wake_failed event
           doubles from the base to the ceiling and stays there.
    FAIL = the agent is condemned after three, or the pause is not announced.
    """
    runner = fresh(path, tag + "_e")
    node = FakeNode(pending_ids=[100], wake_status=200,
                    wake_body={"failed": True, "content": "upstream failed"})
    asyncio.run(drive(runner, node, ticks=8))
    pauses = [p.get("retry_in_seconds") for t, p in node.event_payloads if t == "agent.wake_failed"]
    base, ceiling = runner.WAKE_RETRY_BACKOFF, runner.WAKE_RETRY_MAX_BACKOFF
    want = [min(base * 2 ** i, ceiling) for i in range(len(pauses))]
    return {
        "pauses announced": pauses,
        "marked broken": "broken" in [a for a, _ in node.runtime_calls],
        "ok": len(pauses) >= 6 and pauses == want and "broken" not in [a for a, _ in node.runtime_calls],
    }


def scenario_a_refused_wake_is_not_condemned(path, tag):
    """The gateway refuses the wake (403: a policy, a credential not set yet).

    PASS = retried, never broken, each try an event. FAIL = condemned for good on the
    first refusal, with nothing able to bring the agent back once the cause is fixed.
    """
    runner = fresh(path, tag + "_f")
    node = FakeNode(pending_ids=[100], wake_status=403, wake_body={})
    asyncio.run(drive(runner, node, ticks=5))
    actions = [a for a, _ in node.runtime_calls]
    return {
        "wakes attempted over 5 ticks": node.wakes,
        "marked broken": "broken" in actions,
        "wake_failed events": node.events.count("agent.wake_failed"),
        "ok": "broken" not in actions and node.wakes > 2 and node.events.count("agent.wake_failed") > 2,
    }


def scenario_a_failed_wake_is_not_a_reminder(path, tag):
    """A wake that never ran must not use up the attempts at an unread message.

    PASS = after failed wakes the unread tally for the agent is empty, so the message
           is still woken for when the limit lifts. FAIL = the failures counted as
           reminders and the Runner gave up on the message ("not waking for it again").
    """
    runner = fresh(path, tag + "_g")
    node = FakeNode(pending_ids=[100], wake_status=200, wake_body={"failed": True})
    asyncio.run(drive(runner, node, ticks=6))
    st = getattr(runner, "_unread", {}).get("alex")
    exhausted = bool(st and st[1] >= runner.UNREAD_MAX_ATTEMPTS)
    return {
        "unread tally": st,
        "gave up on the message": exhausted,
        "ok": not exhausted,
    }


def scenario_wake_must_close_what_woke_it(path, tag):
    """The wake completes; the agent answers in PROSE and closes nothing.

    PASS = the row that woke it is closed, and the fault is recorded as an event.
    FAIL = the row stays new; max_id never advances; the in-memory watermark then
           hides it forever and nobody is coming for that message.
    """
    runner = fresh(path, tag + "_b")
    node = FakeNode(
        pending_ids=[5150, 5151],
        wake_status=200,
        # the exact shape of the eight-day failure: a polite prose answer
        wake_body={"content": "The inbox has no new messages. "
                              "No action outstanding on my side."},
        # the agent handled 5151 itself and left 5150 open
        after_states={5150: "new", 5151: "processed"},
    )
    asyncio.run(drive(runner, node, ticks=1))
    return {
        "closed what it was woken for": node.dismissed,
        "left alone what the agent handled": 5151 not in node.dismissed,
        "fault recorded": "agent.inbox_autoclosed" in node.events,
        "ok": node.dismissed == [5150] and "agent.inbox_autoclosed" in node.events,
    }


def scenario_handled_message_is_untouched(path, tag):
    """The safety property of the fix: an agent that did its job is never
    reported as faulty and never has anything closed behind its back."""
    runner = fresh(path, tag + "_c")
    node = FakeNode(pending_ids=[7000], wake_status=200,
                    wake_body={"content": "no work"},
                    after_states={7000: "processed"})
    asyncio.run(drive(runner, node, ticks=1))
    return {
        "nothing dismissed": node.dismissed == [],
        "no fault event": "agent.inbox_autoclosed" not in node.events,
        "ok": node.dismissed == [] and "agent.inbox_autoclosed" not in node.events,
    }


def scenario_routed_is_not_finished(path, tag):
    """The state the first cut of this fix missed, caught on reference-node rather than in
    review: within 18 seconds of the wake the whole backlog moved new -> ROUTED.

    'routed' means brin handed the message to the agent and the agent never
    finished it. It is invisible to with-pending-work (which keys on 'new'), so a
    closure that also looked only at 'new' would have watched eleven messages move
    from one invisible bucket to another and reported success.

    PASS = a row left routed is closed. FAIL = it is not, and nobody is coming.
    """
    runner = fresh(path, tag + "_d")
    node = FakeNode(pending_ids=[5150], wake_status=200,
                    wake_body={"content": "Nothing outstanding on my side."},
                    after_states={5150: "routed"})
    asyncio.run(drive(runner, node, ticks=1))
    return {
        "closed the routed row": node.dismissed,
        "fault recorded": "agent.inbox_autoclosed" in node.events,
        "ok": node.dismissed == [5150],
    }


SCENARIOS = [
    ("a failed wake never condemns the agent", scenario_timeout_must_not_condemn),
    ("the pause grows to a ceiling", scenario_pause_grows_to_a_ceiling),
    ("a refused wake is retried too", scenario_a_refused_wake_is_not_condemned),
    ("a failed wake is not a reminder", scenario_a_failed_wake_is_not_a_reminder),
    ("a wake closes what woke it", scenario_wake_must_close_what_woke_it),
    ("a working agent is left alone", scenario_handled_message_is_untouched),
    ("routed is unfinished, not done", scenario_routed_is_not_finished),
]


def run_all(path, tag, expect_pass):
    print("\n=== %s ===" % tag)
    results = []
    for name, fn in SCENARIOS:
        try:
            out = fn(path, tag)
            ok = out.pop("ok")
        except Exception as e:
            out, ok = {"raised": repr(e)}, False
        results.append(ok)
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        for k, v in out.items():
            print("         %s: %s" % (k, v))
    if expect_pass:
        return all(results)
    # the baseline is supposed to fail; a baseline that passes means the test
    # does not actually discriminate and proves nothing
    return not all(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="git ref of the pre-fix runner, expected to FAIL")
    args = ap.parse_args()

    ok = run_all(os.path.abspath(RUNNER), "working tree (expected: PASS)", True)

    if args.baseline:
        repo = subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                       cwd=HERE, text=True).strip()
        blob = subprocess.check_output(
            ["git", "show", "%s:companions/brin/runner.py" % args.baseline],
            cwd=repo, text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(blob)
            base = fh.name
        base_fails = run_all(base, "baseline %s (expected: FAIL)" % args.baseline, False)
        os.unlink(base)
        ok = ok and base_fails

    print("\n%s" % ("ALL GOOD" if ok else "SOMETHING IS WRONG"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
