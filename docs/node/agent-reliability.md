# Agent reliability: acceptance criteria

What a node must guarantee about its agents, from the process up to the
conversation with a person. Each criterion names the check that holds it.
"xfail" means the test exists, states the criterion and fails today; the mark
comes off with the fix. "stand" means an end-to-end run on a test node against
a deterministic Anthropic-compatible mock (no real tokens), "planned" means no
check exists yet.

The bar: the owner never has to look inside. The sysadmin agent gets an
alert with an action for every failure below; the owner gets an escalation
and one line a day.

## Level 0. The process

| # | Criterion | Check | Status |
|---|-----------|-------|--------|
| 0.1 | Stop ends every process of the agent within 5 s, mid-turn included, and leaves no orphan | gateway `test_agent_lifecycle.py`, stand scenarios | passes |
| 0.2 | A stopped turn answers `stopped` with who stopped it, never 200 with an empty answer | `test_a_stopped_turn_answers_stopped_with_who_stopped_it` | passes |
| 0.3 | After enable, the next message resumes the same session, 100% of the time | `test_the_idle_sweep_leaves_a_dead_process_to_the_send_that_holds_its_lock`, stand soak | passes |
| 0.4 | At the process cap a turn is refused with 503 and Retry-After; the session stays usable | `test_a_spawn_cap_refusal_leaves_the_session_usable` | passes |
| 0.5 | 30 minutes of 10 agents with Stop every 3 minutes: 0 bad turns, 100% resumes, process count equals live sessions | stand soak | passes on the stand; to become a release gate |

## Level 1. The turn

| # | Criterion | Check | Status |
|---|-----------|-------|--------|
| 1.1 | Every turn ends with exactly one outcome: ok, stopped or failed | `test_every_turn_ends_with_exactly_one_outcome`, `test_an_error_result_is_a_failed_turn_journalled_not_an_answer` | passes |
| 1.2 | For a live process the node knows what it waits on (model, tool, cli) and how long it has been silent, streaming included | gateway `test_liveness.py`, stand scenarios HANG / STALL / tool hang | passes |
| 1.3 | A turn silent for more than 120 s raises `agent.stuck` for the sysadmin agent; policy may stop it | planned | planned |
| 1.4 | A provider that fails is classified upstream, retries are bounded, other agents are not touched | stand scenario FAILS 529 | passes |
| 1.5 | Mock and local models cost 0 | planned | fails (about $0.11 a turn) |

## Level 2. A person on a live channel

See `companions/communication/tests/test_live_chat.py`.

| # | Criterion | Check | Status |
|---|-----------|-------|--------|
| 2.1 | A message for a busy agent waits and is never lost | `test_a_message_for_a_busy_agent_waits_and_is_not_lost` | passes |
| 2.2 | A turn longer than the channel's wait does not lose the message: the answer reaches the person when the turn ends | `test_a_turn_longer_than_the_channel_wait_does_not_lose_the_message` | passes |
| 2.3 | Messages written during a turn are read together in the next turn, like a chat: one turn, one answer | `test_messages_written_during_a_turn_are_read_together_in_the_next_one` | passes |
| 2.4 | One agent's long turn does not hold up another agent's chat | `test_a_slow_agent_does_not_hold_up_another_agents_chat` | passes |
| 2.5 | A person never waits more than 60 s in silence: either the answer or one status line ("working on X since N s") | stand | planned |
| 2.6 | A person can stop a running turn from the chat ("stop") | stand | planned |

## Level 3. Agents working together

| # | Criterion | Check | Status |
|---|-----------|-------|--------|
| 3.1 | A task for an agent that cannot be woken is answered within 30 s: "unavailable since T, sysadmin notified", the task stays queued | stand | planned |
| 3.2 | When that agent is healthy again it takes the task by itself and the person is told | stand | planned |
| 3.3 | A task is refused at hand-over when the receiving agent lacks a working tool for it, not an hour later | stand | planned |
| 3.4 | Twenty agents passing tasks to each other for 30 minutes: no task lost, none answered twice | stand | planned |

## Level 4. Changes and decisions

| # | Criterion | Check | Status |
|---|-----------|-------|--------|
| 4.1 | A changed key or tool config reaches a live session at its next turn, without a manual restart | stand | planned |
| 4.2 | A decision recorded through HITL is not asked again | stand | planned |
| 4.3 | Every alert names its action for the sysadmin agent in the runbook | review | planned |
