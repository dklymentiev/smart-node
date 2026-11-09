#!/usr/bin/env python3
"""Wake parked Content Article runs only after HITL verifies an answer or expiry."""
import glob
import json
import os
import subprocess
import sys

FLOW = os.path.join(os.environ.get('CONTENT_ARTICLE_HITL_FLOW_DIR',
                                   '/var/lib/smart-node/workflows/flows/content-article-hitl'), 'logic')
sys.path.insert(0, FLOW)
import _lib as L

ROOT = os.environ.get('CONTENT_ARTICLE_HITL_TASKS_DIR', '/var/lib/smart-node/workflows/tasks')
HITL = ['/opt/smart-node/hitl/venv/bin/python', '/opt/smart-node/hitl/caller/hitl.py']
ENV = {**os.environ, 'HITL_STATE': os.environ.get('CONTENT_ARTICLE_HITL_STATE_DIR', '/var/lib/smart-node/hitl-flow-caller'),
       'HITL_URL': os.environ.get('HITL_URL', os.environ.get('HITL_ORIGIN', ''))}


def check(request_id):
    p = subprocess.run([*HITL, 'check', request_id], env=ENV, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {'status': 'error', 'reason': (p.stderr or p.stdout)[-240:]}


def task_id(folder):
    inputs = json.load(open(os.path.join(folder, 'artifacts', 'inputs.json')))
    return str(inputs['task_id'])


def tags(tid):
    st, data = L.api('GET', L.P + tid + '/tags')
    if st != 200 or not isinstance(data, list):
        raise RuntimeError(f'task {tid} tags HTTP {st}')
    return data


def poll(path):
    with open(path) as f:
        state = json.load(f)
    if state.get('status') != 'pending' or not state.get('announced'):
        return 'skip'
    tid = task_id(os.path.dirname(os.path.dirname(path)))
    if 'blocked:approval' not in tags(tid):
        return 'unblocked'
    answer = check(state['request_id'])
    status = answer.get('status')
    if status == 'pending':
        return 'pending'
    if status not in ('answered', 'expired'):
        raise RuntimeError(f'HITL {state["request_id"]}: {answer.get("reason") or status}')
    if status == 'answered':
        # The caller library has already verified the signed device record.
        content = f'HITL signed answer received for request {state["request_id"]}. Resuming the workflow.'
    else:
        content = f'HITL request {state["request_id"]} expired without an answer. Resuming the workflow to stop safely.'
    L.must('POST', L.P + tid + '/comments', {'content': content})
    L.must('DELETE', L.P + tid + '/tags/blocked:approval')
    return status


def main():
    counts = {}
    for path in glob.glob(ROOT + '/*/artifacts/hitl-request.json'):
        try:
            result = poll(path)
        except Exception as exc:
            result = 'error'
            print(f'HITL POLLER ERROR | {path} | {exc}', file=sys.stderr)
        counts[result] = counts.get(result, 0) + 1
    print(json.dumps(counts, sort_keys=True))
    return 1 if counts.get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
