#!/usr/bin/env bash
# Exercise the real seed SQL against existing specialist rows. Normal restarts
# preserve edits; an installer refresh updates only the configurator, in place.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
if [[ "${1:-}" == --baseline ]]; then
    git -C "$REPO" show "${2:?ref required}:companions/core-api/seed-specialists.py" > "$TMP/seed.py"
else
    cp "$REPO/companions/core-api/seed-specialists.py" "$TMP/seed.py"
fi
python3 - "$TMP" <<'PY'
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import types

root = Path(sys.argv[1])
db = sqlite3.connect(':memory:')
db.execute("ATTACH DATABASE ':memory:' AS sn_config")
db.execute('''CREATE TABLE sn_config.specialists (
    id INTEGER PRIMARY KEY, slug TEXT, name TEXT, archetype TEXT, body_md TEXT,
    archived_at TEXT, created_by TEXT, updated_by TEXT, updated_at TEXT)''')
db.create_function('now', 0, lambda: 'installed')
db.executemany('INSERT INTO sn_config.specialists VALUES (?,?,?,?,?,?,?,?,?)', [
    (3, 'configurator', 'Local title', 'autonomous', 'old brief', None, 'seed', 'panel', 'before'),
    (4, 'analyst', 'Analyst', 'specialist', 'owner edit', None, 'seed', 'panel', 'before'),
    (5, 'configurator', 'Retired', 'autonomous', 'archived brief', 'yesterday', 'seed', 'panel', 'before'),
])
class Cursor:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params):
        self.inner = db.execute(sql.replace('%s', '?'), params)
    def fetchone(self): return self.inner.fetchone()
    @property
    def rowcount(self): return self.inner.rowcount
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): db.commit()
    def cursor(self): return Cursor()
    def close(self): pass
sys.modules['psycopg2'] = types.SimpleNamespace(connect=lambda **kw: Connection())
sys.modules['yaml'] = types.SimpleNamespace(safe_load=lambda fm: dict(
    line.split(': ', 1) for line in fm.strip().splitlines()))
for slug in ('configurator', 'analyst'):
    (root / f'{slug}.md').write_text(
        f'---\nslug: {slug}\nname: Shipped title\narchetype: autonomous\n---\nnew {slug} brief\n')
os.environ['SN_SPECIALISTS_SEED'] = str(root)
spec = importlib.util.spec_from_file_location('seed', root / 'seed.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
def run(*args):
    sys.argv = ['seed.py', *args]
    seed.main()
def rows(): return db.execute('SELECT * FROM sn_config.specialists ORDER BY id').fetchall()
before = rows()
run()
assert rows() == before, 'ordinary restart must preserve existing bodies'
run('--refresh-configurator')
after = rows()
assert after[0][0:4] == before[0][0:4], 'existing specialist identity and local metadata survive'
assert after[0][4] == 'new configurator brief\n', 'installer must refresh the existing body'
assert after[1:] == before[1:], 'other specialists and archived rows must survive'
run('--refresh-configurator')
assert rows() == after, 'repeat install must be idempotent'
db.execute("UPDATE sn_config.specialists SET body_md='later owner edit' WHERE id=3")
run()
assert rows()[0][4] == 'later owner edit', 'ordinary restart must preserve later edits'
print('PASS: installer refreshes existing configurator in place; restart, other rows and repeat install are safe')
PY
