#!/usr/bin/env bash
# Guard the backend behavior advertised by the messaging tool descriptions.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import ast
import email.utils
import pathlib
import sys
from types import SimpleNamespace

root = pathlib.Path(sys.argv[1])
messages = ast.parse((root / 'companions/core-api/routes/messages.py').read_text())
contacts = ast.parse((root / 'companions/communication/common/contacts.py').read_text())

def method(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)

class Router:
    def post(self, *args, **kw): return lambda fn: fn

class Cursor:
    def __init__(self):
        self.answers = [dict(id=7, channel_type='email', sender='buyer@example.test',
            recipient='agent@example.test', external_id='agent@example.test|<mail@example.test>',
            thread_id='<root@example.test>', content='Question', received_at=None,
            metadata={'subject': 'Proposal', 'message_id': '<mail@example.test>',
                      'references': '<root@example.test>',
                      'to_addrs': ['agent@example.test', 'other@example.test']}, tenant_id=1),
            {'id': 9}]
        self.insert = None
    def execute(self, sql, params=()):
        if sql.startswith('INSERT INTO sn_core.messages'):
            self.insert = params
    def fetchone(self): return self.answers.pop(0)
    def __enter__(self): return self
    def __exit__(self, *args): return False

cur = Cursor()
ns = {'router': Router(), 'Principal': object, 'MessageReply': object,
      'Depends': lambda dep: None, 'requires': lambda name, **kw: None,
      'get_principal': lambda: None, 'cursor': lambda **kw: cur,
      'HTTPException': Exception, 'Json': lambda value: value, 'email': email,
      '_NODE_DOMAIN': 'example.test', '_visible_message': lambda *args: {'id': 7},
      'emit': lambda *args, **kw: None}
names = ('_quote_original', '_wire_in_reply_to', '_recipient_rows', '_refuse_outside_wake', 'reply_to_message')
exec(compile(ast.Module(body=[method(messages, n) for n in names], type_ignores=[]),
             'messages.py', 'exec'), ns)
body = SimpleNamespace(to_message_id=7, body='Answer', cc=None, only_sender=True)
result = ns['reply_to_message'](body, SimpleNamespace(cross_tenant=True, username='agent', role='operator'))
assert result['threaded'] and result['to'] == 'buyer@example.test'
assert cur.insert[6] is None  # only_sender drops the original Cc audience
assert cur.insert[8]['in_reply_to'] == '<mail@example.test>'
assert cur.insert[8]['references'] == '<root@example.test> <mail@example.test>'
assert cur.insert[8]['subject'] == 'Re: Proposal'
print('  ok   private reply goes only to sender and keeps email thread headers')

gate = {'INTERNAL_CHANNELS': set(), 'normalise': lambda channel, addr: addr,
        'own_mailboxes': lambda channel: set(),
        'handles_matching': lambda channel, addr: [addr],
        '_DECIDE_BY_AGENT_SQL': 'grant query',
        'db': SimpleNamespace(query=lambda *args, **kw: [('agent', False)])}
exec(compile(ast.Module(body=[method(contacts, 'decide')], type_ignores=[]),
             'contacts.py', 'exec'), gate)
allowed, reason = gate['decide']('email', 'agent@example.test',
                                 'unknown@example.test', agent='agent')
assert not allowed and 'no contact grant' in reason and 'unknown@example.test' in reason
outbound = (root / 'companions/communication/send_outbound.py').read_text()
assert 'db.tell_sender(agent, mid, channel_type, recipient, why)' in outbound
print('  ok   contact gate refuses an ungranted address with an actionable reason')
PY
