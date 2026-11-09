"""Replay customer replies without a model, network, database or actual send.

Use the existing source-test scaffolds to import the full CLI backend and brin
Executor and execute the complete /api/message handler. Execute the gateway's
unchanged completion function and the full communication module, including
its actual email/Telegram deliverers. Replace only external boundaries.
--baseline loads the production code from that revision for the same checks.
"""
import ast
import asyncio
import contextlib
import importlib.util
import json
import logging
import pathlib
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Both helpers support --baseline and import real production modules.
gateway = load('gateway_replay_support', ROOT / 'companions/gateway/tests/request_usage_regression.py')
brin = load('brin_replay_support', ROOT / 'companions/brin/tests/turn_response_regression.py')


def source(relative):
    if gateway.args.baseline:
        return subprocess.check_output(
            ['git', 'show', f'{gateway.args.baseline}:{relative}'], cwd=ROOT).decode()
    return (ROOT / relative).read_text()


def compile_function(relative, name, namespace):
    node = next(n for n in ast.parse(source(relative)).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(tree, relative, 'exec'), namespace)
    return namespace[name]


WORK = 'I need to check an internal constraint before replying.'
MORE_WORK = 'The private routing check is complete; drafting the reply.'
FINAL = 'Thank you for your order. Do you have any allergies?'
FULL = '\n\n'.join((WORK, MORE_WORK, FINAL))


def cli_events(result=FINAL, subtype='success', include_text=True):
    events = []
    if include_text:
        for index, text in enumerate((WORK, MORE_WORK, FINAL)):
            blocks = [{'type': 'text', 'text': text}]
            if index < 2:
                blocks.append({'type': 'tool_use', 'id': f'tool-{index}',
                               'name': 'fixture_read', 'input': {'path': 'private-note.txt'}})
            events.append({'type': 'assistant', 'message': {
                'id': f'request-{index}', 'content': blocks}})
    event = {'type': 'result', 'subtype': subtype}
    if result is not None:
        event['result'] = result
    events.append(event)
    return events


async def complete(response):
    stored = []

    async def send(*_args):
        return response

    def append(*args, **kwargs):
        stored.append(kwargs)
        return 101

    # The function runs the backend turn inside the model door's caller scope
    # and tells a turn-limit stop from other errors; neither matters to what is
    # replayed here, so the door scope is a no-op and the limit error a plain class.
    namespace = {'logger': logging.getLogger('gateway-test'), 'asyncio': asyncio,
                 '_door_caller': lambda *_a: contextlib.nullcontext(),
                 'TurnLimitExceeded': type('TurnLimitExceeded', (Exception,), {}),
                 'session_service': types.SimpleNamespace(append_message=append, record_usage=lambda *a, **k: None),
                 'budget': types.SimpleNamespace(record_spend=lambda **k: [], check_budget=lambda **k: (True, '', {})),
                 # names the function takes from its module: the door identity of the call
                 # (a context manager) and the turn-limit error and its wording
                 '_door_caller': lambda _sess, _session_id: contextlib.nullcontext(),
                 'TurnLimitExceeded': type('TurnLimitExceeded', (RuntimeError,), {}),
                 'turn_limit_phrase': lambda seconds: str(seconds)}
    function = compile_function('companions/gateway/sessions_v2_routes.py', '_complete_turn_async', namespace)
    payload = await function('fixture-session', {'tenant_id': 1},
                             types.SimpleNamespace(send=send), [], 'customer request', 100)
    return payload, stored


async def desk_answer(payload):
    harness = brin.Harness()
    persisted = []

    async def persist(_ctx, _agent, _message, content):
        persisted.append(content)

    harness.persist = persist

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *args, **kwargs):
            return types.SimpleNamespace(status_code=200, json=lambda: payload)

    catalog_patch, _ = harness.services()
    with catalog_patch, patch.object(brin.ex.httpx, 'AsyncClient', side_effect=lambda **kw: Client()):
        answer = await harness.ask('alpha')
    return answer, persisted


def outbound(answer, channel='email'):
    rows, states, events = [], [], []
    brin.stub('common', contacts=types.SimpleNamespace(may_wake=lambda *a: (True, 'allowed')),
              db=types.SimpleNamespace(record_outbound=lambda **kw: rows.append(kw),
                                       emit_turn=lambda *a: events.append(a)),
              mailrules=types.SimpleNamespace(is_auto_generated=lambda *a: False))
    brin.stub('common.config', LOG=lambda *a: None,
              get=lambda _key, default='': default, require=lambda _key: 'fixture-key')
    brin.stub('requests', post=lambda *a, **kw: types.SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: answer))
    brin.stub('adapters')
    brin.stub('adapters.telegram', adapter=types.SimpleNamespace(_channel_id=lambda: 'fixture-bot'))
    module = types.ModuleType('dispatch_reply_under_test')
    exec(compile(source('companions/communication/dispatch.py'), 'dispatch.py', 'exec'), module.__dict__)
    module._resolve_mailbox = lambda *a: ('agent@example.test', 'alpha')
    module._claim_agent = lambda *a: True
    module._release_agent = lambda *a: None
    module._set_state = lambda _id, state, *a: states.append(state)
    module._activity = lambda *a: contextlib.nullcontext()
    module._outbound_cursor = lambda: None
    error = None
    try:
        module._handle(1, channel, 'customer@example.test', 'agent@example.test',
                       'customer request', {'chat_id': 123, 'subject': 'Order'},
                       'fixture-thread', '<fixture@example.test>')
    except Exception as exc:
        error = exc
    return rows, states, events, error


class FinalAnswerTests(unittest.TestCase):
    def test_cli_result_is_separate_from_all_assistant_text(self):
        response = asyncio.run(gateway.replay(cli_events(result='Authoritative final answer.')))
        self.assertEqual(response.content, FULL)
        self.assertEqual(getattr(response, 'final_answer', None), 'Authoritative final answer.')

    def test_email_queues_only_final_answer_through_the_complete_chain(self):
        self.assert_chain('email')

    def test_telegram_queues_only_final_answer_through_the_complete_chain(self):
        self.assert_chain('telegram')

    def assert_chain(self, channel):
        async def run():
            response = await gateway.replay(cli_events())
            payload, stored = await complete(response)
            answer, persisted = await desk_answer(payload)
            return payload, stored, answer, persisted

        payload, stored, answer, persisted = asyncio.run(run())
        # What the chat keeps is the answer; the notes between steps were live only.
        self.assertEqual(stored[0]['content'], FINAL)
        self.assertEqual(payload['response']['content'], FINAL)
        self.assertEqual(answer['content'], FINAL)
        self.assertEqual(persisted, [FINAL])
        rows, states, _, error = outbound(answer, channel)
        self.assertIsNone(error)
        self.assertEqual(states[-1:], ['processed'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['channel_type'], channel)
        self.assertEqual(rows[0]['content'], FINAL)
        self.assertNotIn(WORK, rows[0]['content'])
        self.assertNotIn(MORE_WORK, rows[0]['content'])
        self.assertEqual(answer.get('final_answer'), FINAL)

    def test_office_keeps_the_answer_and_actions(self):
        async def run():
            payload, stored = await complete(await gateway.replay(cli_events()))
            answer, persisted = await desk_answer(payload)
            return stored, answer, persisted

        stored, answer, persisted = asyncio.run(run())
        self.assertEqual(stored[0]['content'], FINAL)
        self.assertEqual(answer['content'], FINAL)
        self.assertEqual(persisted, [FINAL])
        self.assertEqual([item['id'] for item in answer['actions']], ['tool-0', 'tool-1'])

    def test_result_only_cli_turn_still_has_an_external_answer(self):
        async def run():
            payload, _ = await complete(await gateway.replay(cli_events(include_text=False)))
            answer, _ = await desk_answer(payload)
            return answer

        answer = asyncio.run(run())
        self.assertEqual(answer.get('final_answer'), FINAL)
        rows, _, _, error = outbound(answer)
        self.assertIsNone(error)
        self.assertEqual(rows[0]['content'], FINAL)

    def test_absent_empty_or_error_result_never_sends_working_text(self):
        for result, subtype in ((None, 'success'), ('', 'success'), ('\n ', 'success'),
                                ('Provider failure detail', 'error_during_execution')):
            with self.subTest(result=result, subtype=subtype):
                async def run():
                    payload, _ = await complete(await gateway.replay(cli_events(result, subtype)))
                    answer, _ = await desk_answer(payload)
                    return answer

                answer = asyncio.run(run())
                self.assertEqual(answer['content'], FULL)
                rows, _, _, error = outbound(answer)
                self.assertEqual(rows, [])
                self.assertIsInstance(error, RuntimeError)
                self.assertEqual(error.sn_stage, 'think')

    def test_legacy_or_incomplete_brin_response_does_not_fall_back_to_content(self):
        for fields in ({}, {'final_answer': None}, {'final_answer': ''},
                       {'final_answer': [FINAL]}, {'final_answer': FINAL, 'failed': True},
                       {'final_answer': FINAL, 'working': True}, {'final_answer': FINAL, 'stopped': True}):
            with self.subTest(fields=fields):
                rows, _, _, error = outbound({'content': FULL, **fields})
                self.assertEqual(rows, [])
                self.assertIsInstance(error, RuntimeError)

    def test_http_backend_single_reply_and_tool_round_are_distinguished(self):
        from backends import BackendResponse
        for tool_calls, expected in ((None, FINAL), ([{'id': 'tool'}], '')):
            with self.subTest(tool_calls=tool_calls):
                payload, _ = asyncio.run(complete(BackendResponse(content=FINAL, tool_calls=tool_calls)))
                self.assertEqual(payload['response'].get('final_answer'), expected)

    def test_http_tool_loop_exports_only_its_terminal_round(self):
        async def run():
            harness = brin.Harness(http_agents=('alpha',))
            original = harness.executor._gateway_chat_stateless

            async def stateless(*args, **kwargs):
                data = await original(*args, **kwargs)
                data['content'] = WORK if data['tool_calls'] else FINAL
                return data

            harness.executor._gateway_chat_stateless = stateless
            load_catalog, client = harness.services()
            with load_catalog, client:
                return await harness.ask('alpha')

        answer = asyncio.run(run())
        self.assertEqual(answer['content'], FINAL)
        self.assertEqual(answer.get('final_answer'), FINAL)
        rows, _, _, error = outbound(answer, 'telegram')
        self.assertIsNone(error)
        self.assertEqual(rows[0]['content'], FINAL)


if __name__ == '__main__':
    try:
        unittest.main(argv=[sys.argv[0], *gateway.test_args], verbosity=2)
    finally:
        gateway.scratch.cleanup()
        brin.scratch.cleanup()
