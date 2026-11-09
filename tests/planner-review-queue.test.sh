#!/usr/bin/env bash
# Execute the real MCP reader against the unchanged planner listing contract.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$ROOT" "$@" <<'PY'
import argparse
import ast
import asyncio
import copy
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

root = pathlib.Path(sys.argv[1])
parser = argparse.ArgumentParser()
parser.add_argument('--baseline')
args, test_args = parser.parse_known_args(sys.argv[2:])
scratch = tempfile.TemporaryDirectory(prefix='planner-read-baseline-')
path = root / 'companions/tracking/mcp_planner.py'
if args.baseline:
    path = pathlib.Path(scratch.name) / 'mcp_planner.py'
    path.write_bytes(subprocess.check_output(
        ['git', 'show', f'{args.baseline}:companions/tracking/mcp_planner.py'], cwd=root))


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


class Server:
    def __init__(self, *_args):
        pass

    def tool(self):
        return lambda function: function


stub('mcp')
stub('mcp.server', MCPServer=Server)
stub('httpx', AsyncClient=None, AsyncHTTPTransport=lambda **kwargs: object())
spec = importlib.util.spec_from_file_location('planner_reader_under_test', path)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

# Use the vendored API's actual list_tasks function and parameter defaults.
# No reviewer query or new endpoint is invented by this adapter. Only its DB
# repository and TaskOut validation boundary are substituted.
api_path = root / 'companions/planner/src/planner/routers/tasks.py'
tree = ast.parse(api_path.read_text())
listing = next(node for node in tree.body
               if isinstance(node, ast.AsyncFunctionDef) and node.name == 'list_tasks')
listing.decorator_list = []

# list_tasks checks status_filter against planner.statuses (the status model of
# migration 0010), imported there as _st. Load the real module; only the
# database driver it imports is stubbed.
if 'asyncpg' not in sys.modules:
    stub('asyncpg', Pool=object, Connection=object, Record=dict)
statuses_spec = importlib.util.spec_from_file_location(
    'planner_statuses_under_test', root / 'companions/planner/src/planner/statuses.py')
statuses = importlib.util.module_from_spec(statuses_spec)
statuses_spec.loader.exec_module(statuses)


class TaskOut:
    @staticmethod
    def model_validate(row):
        return copy.deepcopy(row)


# Rows as the planner answers this reader: its three statuses (0010), with
# why a queued task waits and how a closed one ended.
def task(task_id, status, assignee=None, reviewer=None, wait=None, outcome=None):
    return {'id': task_id, 'title': f'fixture task {task_id}', 'status': status,
            'state': status, 'wait_reason': wait, 'close_outcome': outcome,
            'assignee': assignee, 'reviewer': reviewer, 'priority': 'high'}


def review(task_id, assignee=None, reviewer=None):
    return task(task_id, 'queued', assignee, reviewer, wait='review')


ROWS = [task(1, 'in_progress', 'review-agent', 'other-reviewer'),
        task(2, 'in_progress', 'implementer', 'review-agent'),
        review(3, 'implementer', 'review-agent'),
        review(4, 'review-agent', 'other-reviewer'),
        review(5, 'review-agent', 'review-agent'),
        task(6, 'queued', 'review-agent'),
        task(7, 'queued', 'implementer', 'review-agent'),
        task(8, 'queued', 'somebody', 'somebody-else'),
        task(9, 'closed', 'implementer', 'review-agent', outcome='completed'),
        task(10, 'closed', 'review-agent', outcome='completed'),
        review(11, 'somebody', 'somebody-else'),
        task(12, 'queued', 'review-agent', wait='approval'),
        task(13, 'queued', 'implementer', 'review-agent', wait='person')]


class API:
    def __init__(self, rows=ROWS, fail_at=None):
        self.rows = copy.deepcopy(rows)
        self.calls = []
        self.fail_at = fail_at
        namespace = {'Query': lambda default=None, **kw: default, 'TaskOut': TaskOut,
                     '_repo': lambda request: self, '_st': statuses}
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, listing], type_ignores=[]))
        exec(compile(module, str(api_path), 'exec'), namespace)
        self.endpoint = namespace['list_tasks']

    async def list(self, assignee=None, status=None, wait_reason=None, limit=100, offset=0, **kwargs):
        rows = [row for row in self.rows
                if (assignee is None or row['assignee'] == assignee)
                and (status is None or row['status'] == status)
                and (wait_reason is None or row['wait_reason'] == wait_reason)]
        # Match the existing newest-first listing order (fixtures have increasing dates).
        return sorted(rows, key=lambda row: row['id'], reverse=True)[offset:offset + limit]

    def client(self, **kwargs):
        api = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def request(self, method, url, headers, params, json):
                if method != 'GET' or json is not None:
                    raise AssertionError('my_tasks attempted a write')
                if not url.endswith('/api/planner/api/v1/tasks'):
                    raise AssertionError(f'unexpected endpoint: {url}')
                api.calls.append((method, copy.deepcopy(params), headers['Remote-User']))
                if api.fail_at and api.fail_at(params):
                    return types.SimpleNamespace(status_code=503, text='fixture read failed',
                                                 content=b'error', json=lambda: {})
                # The vocabulary is the planner middleware's business, not the
                # listing's: it must be asked for, and it is not a filter.
                query = dict(params)
                if query.pop('vocabulary', None) != 'new':
                    raise AssertionError('the reader did not ask for the three statuses')
                result = await api.endpoint(request=None, **query)
                return types.SimpleNamespace(status_code=200, content=b'rows', json=lambda: result)

        return Client()

    def read(self, **kwargs):
        with patch.dict(os.environ, SN_AGENT_SLUG='review-agent', SN_PLANNER_OPERATOR_TOKEN=''), \
             patch.object(mcp.os.path, 'exists', return_value=True), \
             patch.object(mcp.httpx, 'AsyncClient', side_effect=self.client):
            return json.loads(asyncio.run(mcp.planner_my_tasks(**kwargs)))


class ReadQueueTests(unittest.TestCase):
    def test_default_includes_review_for_reviewer_only(self):
        # Per the current review-queue contract: a task in review where the
        # actor is only the assignee (not the reviewer) is excluded from the
        # default queue (it is waiting on someone else to confirm, not on
        # the executor). In review where the actor is the reviewer, or both
        # roles, stays in the queue.
        rows = API().read()
        ids = [row['id'] for row in rows]
        self.assertTrue({3, 5}.issubset(ids), ids)
        self.assertNotIn(4, ids)
        self.assertEqual(ids.count(5), 1)

    def test_explicit_status_reads_assignee_or_reviewer(self):
        self.assertEqual({row['id'] for row in API().read(status='queued')}, {3, 4, 5, 6, 7, 12, 13})
        self.assertEqual({row['id'] for row in API().read(status='queued', wait_reason='review')},
                         {3, 4, 5})

    def test_default_includes_resumable_and_new_work_for_both_roles(self):
        # 4 is in review as executor only, filtered per the current
        # review-queue contract. 12 waits for approval and 13 for a person:
        # someone else's move. Every other row where the actor is assignee or
        # reviewer and the task is not closed stays in the default queue,
        # started work first, then reviews, then new work.
        ids = [row['id'] for row in API().read()]
        self.assertEqual(set(ids), {1, 2, 3, 5, 6, 7})
        self.assertEqual(set(ids[:2]), {1, 2})
        self.assertEqual(set(ids[2:4]), {3, 5})

    def test_review_beyond_first_page_of_unrelated_tasks_is_found(self):
        rows = [review(i, 'another-agent', 'another-reviewer') for i in range(2, 503)]
        rows.append(review(1, 'implementer', 'review-agent'))
        api = API(rows)
        self.assertEqual([row['id'] for row in api.read(status='queued', wait_reason='review')], [1])
        self.assertTrue(any(call[1].get('offset', 0) > 0 for call in api.calls))

    def test_all_matching_pages_are_read_even_when_every_task_is_assigned(self):
        api = API([task(i, 'in_progress', 'review-agent') for i in range(1, 502)])
        self.assertEqual(len(api.read(status='in_progress')), 501)

    def test_explicit_status_remains_exact_including_closed_reviews(self):
        self.assertEqual({row['id'] for row in API().read(status='in_progress')}, {1, 2})
        self.assertEqual({row['id'] for row in API().read(status='closed')}, {9, 10})

    def test_read_does_not_reassign_or_transition_tasks_or_expose_others(self):
        api = API()
        before = copy.deepcopy(api.rows)
        rows = api.read()
        self.assertEqual(api.rows, before)
        self.assertTrue(all(row['assignee'] == 'review-agent' or row['reviewer'] == 'review-agent'
                            for row in rows))
        self.assertTrue(all(method == 'GET' and actor == 'review-agent'
                            for method, _params, actor in api.calls))
        for row in rows:
            self.assertEqual(row, next(original for original in before if original['id'] == row['id']))

    def test_late_review_page_failure_is_visible_instead_of_a_partial_queue(self):
        rows = [task(1, 'in_progress', 'review-agent')]
        rows.extend(review(i, 'implementer', 'another-reviewer') for i in range(2, 504))
        api = API(rows, fail_at=lambda params:
                  params.get('status_filter') == 'queued' and params.get('offset', 0) > 0)
        result = api.read()
        self.assertIsInstance(result, dict)
        self.assertEqual(result['error'], 'HTTP 503')


try:
    unittest.main(argv=[sys.argv[0], *test_args], verbosity=2)
finally:
    scratch.cleanup()
PY
