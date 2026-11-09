"""Replay multi-request CLI turns through the real backend and handoff gate.

Only external boundaries are replaced: subprocess pipes, DB and unused HTTP
dependencies. --baseline loads production modules from a git revision while
running these same assertions. No model, database or service is contacted.
"""

import argparse
import asyncio
import importlib
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument("--baseline")
args, test_args = parser.parse_known_args()
scratch = tempfile.TemporaryDirectory(prefix="request-usage-baseline-")
gateway = ROOT / "companions/gateway"
if args.baseline:
    gateway = pathlib.Path(scratch.name)
    for relative in ("backends/__init__.py", "backends/cli_claude.py",
                     "context_handoff.py", "session_service.py"):
        target = gateway / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(
            ["git", "show", f"{args.baseline}:companions/gateway/{relative}"], cwd=ROOT,
        ))


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected external call")


stub("db", query=forbidden, with_transaction=forbidden)
stub("budget", record_spend=forbidden)
stub("secrets_store", get=forbidden)
# model_door subclasses httpx classes (AsyncByteStream, AsyncBaseTransport) and
# annotates with httpx.Request at import, so any name it reads must exist.
# The stub answers every other name with an empty class; only AsyncClient,
# the one that would reach the network, stays forbidden.
_httpx = stub("httpx", AsyncClient=forbidden)
_httpx.__getattr__ = lambda name: type(name, (), {})
stub("psycopg2")
stub("psycopg2.extras", Json=forbidden)
stub("config", CLI_PATH="unused", FLINT_PATH="unused", FLINT_ENABLED=False,
     flint_stdio_safe=lambda: False,
     FLINT_SKIP_PERMISSIONS=False, FLINT_SKIP_PERMISSIONS_USERS=frozenset(),
     spawn_env=forbidden, CLI_DEFAULT_CWD="unused",
     SKIP_PERMISSIONS=False, CLI_RUNAS_ENABLED=False, SUDO_PATH="unused",
     KILL_PATH="unused", STREAM_LIMIT=1024 * 1024, WRAPPER_PATH="unused",
     HOST="127.0.0.1", PORT=8000, DOOR_URL="http://127.0.0.1:8000",
     RUNTIME_URL="http://127.0.0.1:8002", MODEL_RELAY=False,
     CONTEXT_HANDOFF_RETRY_COOLDOWN=60, CONTEXT_HANDOFF_RETRY_GROWTH=1.2)
sys.path.insert(0, str(gateway))
cli = importlib.import_module("backends.cli_claude")
cli.turn_limit_seconds = lambda _session_id: 600.0  # isolated replay has no registry DB
handoff = importlib.import_module("context_handoff")
service_module = importlib.import_module("session_service")


def provider_usage(context, output=300):
    return {"input_tokens": 17, "cache_read_input_tokens": context - 1017,
            "cache_creation_input_tokens": 1000, "output_tokens": output}


def assistant(request_id, usage, content):
    return {"type": "assistant", "message": {
        "id": request_id, "usage": usage, "content": [content],
    }}


def turn(requests, copies=3, with_records=True):
    """Each provider request emits text/tool/text events with the same id."""
    events = []
    totals = dict.fromkeys(provider_usage(75901), 0)
    for index, (request_id, context) in enumerate(requests):
        usage = provider_usage(context)
        for field in totals:
            totals[field] += usage[field]
        if with_records:
            blocks = [{"type": "text", "text": "checking"},
                      {"type": "tool_use", "id": f"tool-{index}",
                       "name": "read", "input": {"path": "fixture.txt"}},
                      {"type": "text", "text": "done"}]
            events.extend(assistant(request_id, usage, block) for block in blocks[:copies])
    events.append({"type": "result", "subtype": "success", "usage": totals,
                   "cost_usd": 3.71, "result": "done"})
    return events, totals


class InputPipe:
    def write(self, data):
        self.envelope = json.loads(data)

    async def drain(self):
        pass


def process_for(events):
    output = asyncio.StreamReader()
    output.feed_data(b"".join((json.dumps(event) + "\n").encode() for event in events))
    output.feed_eof()
    process = types.SimpleNamespace(returncode=None, stdin=InputPipe(), stdout=output)

    async def wait():
        process.returncode = 1
        return 1

    process.wait = wait
    return process


async def replay(events, session_id="usage-replay", first_attempt=None):
    process = process_for(events)
    backend = cli.ClaudeCliBackend()

    async def respawn(*_args, **_kwargs):
        cli._PROCESSES[session_id] = process

    initial = process if first_attempt is None else process_for(first_attempt)
    with patch.dict(cli._PROCESSES, {session_id: initial}), \
         patch.object(cli, "_load_session_runtime", return_value={"model": "claude-cli/test"}), \
         patch.object(cli, "_session_has_turns", return_value=True), \
         patch.object(backend, "create", side_effect=respawn):
        response = await backend.send(session_id, [], "continue the tool loop")
    assert process.stdin.envelope["message"]["content"] == "continue the tool loop"
    return response


IDENTITY = {"enabled": True, "threshold_tokens": 120000, "scope_type": "agent",
            "agent_slug": "test-agent", "conversation": "desk:test"}
SESSION = {"tenant_id": 1, "agent_id": 7, "is_opaque": True}


class UsageTests(unittest.TestCase):
    def setUp(self):
        handoff._HANDOFF_BACKOFF.clear()
        handoff._HANDOFF_IN_PROGRESS.clear()

    def assert_no_handoff(self, usage):
        events = []
        backend = types.SimpleNamespace(send=forbidden, close=forbidden)
        service = types.SimpleNamespace(get_history=forbidden)
        with patch.object(handoff, "_identity_and_limit", return_value=IDENTITY), \
             patch.object(handoff, "emit_event", side_effect=lambda *a: events.append(a)):
            result = asyncio.run(handoff.maybe_handoff(
                service, "test-session", SESSION, backend, usage,
            ))
        self.assertIsNone(result)
        self.assertEqual(events, [])

    def test_long_tool_loop_measures_one_request_and_does_not_retire(self):
        requests = [(f"req-{i}", 60000 + i * 589) for i in range(28)]
        requests[-1] = ("req-27", 75901)
        events, totals = turn(requests)
        response = asyncio.run(replay(events))
        self.assertGreater(sum(totals[k] for k in
                               ("input_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens")), 1800000)
        self.assertEqual(handoff.turn_context_tokens(response.usage), 75901)
        self.assert_no_handoff(response.usage)
        # The final result remains the billing total; deduplication changes
        # request rows, not the CLI's measured cost or token rollup.
        self.assertEqual(response.usage["cost_usd"], 3.71)
        self.assertEqual(response.usage["cache_read_tokens"], totals["cache_read_input_tokens"])
        self.assertEqual(len(response.tool_calls), 28)

    def test_duplicate_events_produce_one_persisted_row_per_request_id(self):
        events, _ = turn([("req-a", 72000), ("req-b", 75901)])
        response = asyncio.run(replay(events))
        # Exercise the real persistence method; inspect what it sends to DB.
        with patch.object(service_module.db, "query") as query:
            service_module.SessionService().record_usage("test-session", **{
                key: value for key, value in response.usage.items() if key != "cumulative"
            })
        rows = json.loads(query.call_args.args[1][6])
        self.assertEqual([row["request_id"] for row in rows], ["req-a", "req-b"])
        self.assertEqual(rows[-1]["cache_read_tokens"], 74884)

    def test_repeated_id_replaces_usage_without_becoming_the_latest_request(self):
        events, _ = turn([("req-a", 72000), ("req-b", 75901)], copies=1)
        events.insert(-1, assistant("req-a", provider_usage(73000, output=600),
                                    {"type": "text", "text": "updated block"}))
        response = asyncio.run(replay(events))
        rows = response.usage["requests"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["output_tokens"], 600)
        self.assertEqual(rows[0]["cache_read_tokens"], 71983)
        self.assertEqual(rows[-1]["request_id"], "req-b")
        self.assertEqual(handoff.turn_context_tokens(response.usage), 75901)

    def test_missing_request_ids_are_not_collapsed(self):
        events, _ = turn([(None, 72000), (None, 75901)], copies=1)
        response = asyncio.run(replay(events))
        self.assertEqual(len(response.usage["requests"]), 2)
        self.assertEqual(handoff.turn_context_tokens(response.usage), 75901)

    def test_retry_discards_failed_attempt_rows_and_request_index(self):
        events, _ = turn([("req-a", 72000), ("req-b", 75901)])
        failed = [assistant("req-b", provider_usage(130000),
                            {"type": "text", "text": "failed attempt"})]
        response = asyncio.run(replay(events, first_attempt=failed))
        self.assertEqual([row["request_id"] for row in response.usage["requests"]],
                         ["req-a", "req-b"])
        self.assertNotIn("failed attempt", response.content)
        self.assertEqual(handoff.turn_context_tokens(response.usage), 75901)

    def test_cli_rollup_without_request_records_is_unknown_and_does_not_retire(self):
        events, _ = turn([(f"req-{i}", 75901) for i in range(28)], with_records=False)
        response = asyncio.run(replay(events))
        self.assertEqual(handoff.turn_context_tokens(response.usage), 0)
        self.assert_no_handoff(response.usage)

    def test_single_response_usage_and_latest_request_semantics_stay_intact(self):
        flat = {"input_tokens": 17, "cache_read_tokens": 119000,
                "cache_write_tokens": 2000, "output_tokens": 999999}
        self.assertEqual(handoff.turn_context_tokens(flat), 121017)
        self.assertEqual(handoff.turn_context_tokens(dict(flat, requests=[flat, {
            "input_tokens": 17, "cache_read_tokens": 74884, "cache_write_tokens": 1000,
        }])), 75901)


if __name__ == "__main__":
    try:
        unittest.main(argv=[sys.argv[0], *test_args], verbosity=2)
    finally:
        scratch.cleanup()
