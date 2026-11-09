"""Agent console: every turn of an agent in flight, whatever started it,
and the turn it finished last. In-process state only; no database is touched."""
import time

import backends.cli_claude as c


def _reset():
    for d in (c._SESSION_CONVERSATION, c._SESSION_AGENT, c._AGENT_LAST_TURN,
              c._TURN_STARTED, c._TURN_STATUS, c._TURN_STEPS, c._TURN_STEP_SEQ):
        d.clear()


def test_agent_console_covers_runner_and_desk_turns_of_one_agent():
    _reset()
    c._SESSION_CONVERSATION.update({
        "s1": "agent-b:runner-agent-b-6832-72",   # started by the runner, no desk
        "s2": "agent-b:desk:abc",
        "s3": "agent-a:runner-agent-a-0-23",
        "s4": "agent-b:runner-agent-b-1-1",       # idle: no turn in flight
    })
    now = time.monotonic()
    c._TURN_STARTED.update({"s1": now - 65, "s2": now - 5, "s3": now - 3})
    c._push_step("s1", "tool", "Bash: ls /work")
    c._push_step("s1", "say", "Looking at the tree")

    out = c.agent_activity("agent-b")
    assert sorted(t["session_id"] for t in out["turns"]) == ["s1", "s2"]
    s1 = next(t for t in out["turns"] if t["session_id"] == "s1")
    assert [s["text"] for s in s1["steps"]] == ["Bash: ls /work", "Looking at the tree"]
    assert [t["session_id"] for t in c.agent_activity("agent-a")["turns"]] == ["s3"]
    assert c.agent_activity("ghost") == {"agent": "ghost", "turns": [], "last": None}


def test_metadata_slug_wins_over_the_conversation_prefix():
    _reset()
    c._SESSION_CONVERSATION["s9"] = "voice:call-1"
    c._SESSION_AGENT["s9"] = "agent-b"
    c._TURN_STARTED["s9"] = time.monotonic()
    assert [t["session_id"] for t in c.agent_activity("agent-b")["turns"]] == ["s9"]


def test_finished_turn_stays_visible_until_the_next_one_finishes():
    _reset()
    c._SESSION_CONVERSATION["s1"] = "agent-b:runner-agent-b-6832-72"
    c._TURN_STARTED["s1"] = time.monotonic() - 30
    c._push_step("s1", "tool", "Read: README.md")
    # what send()'s finally does
    c._remember_last_turn("s1")
    c._TURN_STARTED.pop("s1")

    out = c.agent_activity("agent-b")
    assert out["turns"] == []
    assert out["last"]["session_id"] == "s1"
    assert out["last"]["elapsed"] >= 30
    assert [s["text"] for s in out["last"]["steps"]] == ["Read: README.md"]
