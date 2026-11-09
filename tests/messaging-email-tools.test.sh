#!/usr/bin/env bash
# The CLI MCP and the re-seeded HTTP tool catalogue must expose the same mail
# arguments that core-api already accepts. Run with --baseline REF for red proof.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASELINE=""
[[ "${1:-}" != "--baseline" ]] || BASELINE="${2:?baseline ref required}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for path in companions/brin/mcp_messaging.py companions/core-api/seed-tools.py; do
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:$path" > "$TMP/$(basename "$path")"
    else
        cp "$ROOT/$path" "$TMP/$(basename "$path")"
    fi
done
# seed-tools runs beside its lib/ on a node; PYTHONPATH finds the real one.
export PYTHONPATH="$ROOT/companions/core-api"
python3 - "$TMP/mcp_messaging.py" "$TMP/seed-tools.py" <<'PY'
import ast
import asyncio
import importlib.util
import json
import os
import sys
import types
from typing import Optional

mcp = ast.parse(open(sys.argv[1]).read())
seed_src = open(sys.argv[2]).read()
seed = ast.parse(seed_src)

def function(name):
    return next(n for n in mcp.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

class MCP:
    def tool(self):
        return lambda fn: fn

calls = []
async def call(method, path, body):
    calls.append((method, path, body))
    return '{}'

ns = {'Optional': Optional, 'os': os, 'mcp': MCP(), '_call': call}
nodes = [function(name) for name in ('_send_payload', 'send_message', 'inbox_reply')]
exec(compile(ast.Module(body=nodes, type_ignores=[]), sys.argv[1], 'exec'), ns)

async def check_calls():
    await ns['send_message']('customer@example.test', 'Hello', subject='Proposal',
                             cc=['buyer@example.test'])
    assert calls[-1] == ('POST', '/send', {'to': 'customer@example.test',
        'body': 'Hello', 'subject': 'Proposal', 'cc': ['buyer@example.test']})
    print('  ok   MCP send passes subject and cc to core-api')
    await ns['inbox_reply'](41, 'Answer', only_sender=True)
    assert calls[-1] == ('POST', '/reply', {'to_message_id': 41, 'body': 'Answer',
                                           'only_sender': True})
    print('  ok   MCP reply passes only_sender to core-api')
    await ns['inbox_reply'](41, 'Answer', cc=['buyer@example.test'])
    assert calls[-1][2]['cc'] == ['buyer@example.test']
    print('  ok   MCP reply passes cc to core-api')

asyncio.run(check_calls())

rows = [n for n in ast.walk(seed) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_msg_tools' for t in n.targets)]
assert len(rows) == 1
tools = {r[0]: r for r in ast.literal_eval(rows[0].value)}
for name, props in [('messages_send', {'subject', 'cc'}),
                    ('messages_reply', {'cc', 'only_sender'})]:
    row = tools[name]
    assert props <= row[5]['properties'].keys(), (name, row[5])
    assert not (props & set(row[5]['required']))
    assert 'email' in row[4].lower() and 'contact' in row[4].lower()
    print(f'  ok   seed exposes optional {name} mail fields and grant wording')
assert 'input_schema     = EXCLUDED.input_schema' in seed_src

# Run the real seed against a small existing-row DB double. The seed has many
# unrelated inserts; only the two pre-existing messaging rows are observed.
existing = {name: {'properties': {'to': {}}} for name in ('messages_send', 'messages_reply')}
class Cursor:
    rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=()):
        if params and isinstance(params[0], str) and params[0] in existing \
                and 'INSERT INTO sn_core.tools' in sql:
            assert 'ON CONFLICT (name) DO UPDATE' in sql
            assert 'input_schema     = EXCLUDED.input_schema' in sql
            existing[params[0]] = json.loads(params[5])
    def fetchone(self): return None
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return Cursor()
    def close(self): pass
sys.modules['psycopg2'] = types.SimpleNamespace(connect=lambda **kw: Connection())
spec = importlib.util.spec_from_file_location('seed_tools', sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.main()
assert {'subject', 'cc'} <= existing['messages_send']['properties'].keys()
assert {'cc', 'only_sender'} <= existing['messages_reply']['properties'].keys()
print('  ok   running the update seed replaces existing messaging schemas')
PY
