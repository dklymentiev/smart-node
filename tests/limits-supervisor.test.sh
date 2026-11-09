#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$ROOT/companions/core-api" python3 - <<'PY'
from decimal import Decimal
from lib.supervisor import resolve_profile, check_limit, record_event

default = dict(code='turn_seconds', scope_type='default', scope_id='*', period='turn',
               value=Decimal('600'), action='pause', warn_at_pct=80, notify=True)
override = dict(default, scope_type='agent', scope_id='new-agent', value=Decimal('90'))

class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.claimed = set()
        self.params = None
    def execute(self, sql, args):
        self.params = args
        self.sql = sql
    def fetchall(self): return self.rows
    def fetchone(self):
        key = self.params
        if key in self.claimed: return None
        self.claimed.add(key)
        return {'code': key[0]}

cur = Cursor([default])
assert resolve_profile(cur, 'turn_seconds', agent_slug='new-agent', period='turn')['value'] == 600
print('ok   new agent inherits node default')
cur.rows = [default, override]
effective = resolve_profile(cur, 'turn_seconds', agent_slug='new-agent', period='turn')
assert effective['value'] == 90
assert resolve_profile(cur, 'turn_seconds', agent_slug='other', period='turn')['value'] == 600
print('ok   override affects only its agent')
cur.rows = [default]
assert resolve_profile(cur, 'turn_seconds', agent_slug='new-agent', period='turn')['value'] == 600
print('ok   clearing override restores default')

decision = check_limit(cur, 'turn_seconds', used=90, profile=override)
assert not decision['allowed'] and decision['action'] == 'pause'
events = []
def emit(_cur, **kw): events.append(kw)
assert record_event(cur, decision, actor='component:gateway', agent_slug='new-agent', emit=emit,
                    context={'session_id': 'one'})
assert not record_event(cur, decision, actor='component:gateway', agent_slug='new-agent', emit=emit,
                        context={'session_id': 'one'},
                        when=__import__('datetime').datetime.fromisoformat(decision['period_start']))
assert len(events) == 1 and events[0]['event_type'] == 'limit_tripped'
assert events[0]['payload']['limit'] == 90
print('ok   one recorded trip carries the effective value and limit')
PY
