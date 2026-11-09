#!/usr/bin/env bash
# The post-catalogue update must reach the canonical composer and report errors.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
if [[ "${1:-}" == --baseline ]]; then
    git -C "$REPO" show "${2:?ref required}:node-versioning/recompose-agents.py" > "$TMP/recompose.py"
else
    cp "$REPO/node-versioning/recompose-agents.py" "$TMP/recompose.py"
fi
python3 - "$TMP" <<'PY'
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading

root = Path(sys.argv[1])
agents = root / 'agents'
(agents / 'john').mkdir(parents=True)
prompt = agents / 'john/CLAUDE.md'
prompt.write_text('old catalogue')
calls = []
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        assert self.path == '/api/agents'
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'agents': [{'slug': 'john'}, {'slug': 'unprovisioned'}]}).encode())
    def do_POST(self):
        calls.append((self.path, self.headers.get('Remote-User')))
        if self.server.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'composition failed')
        else:
            assert self.path == '/api/agents/john/regenerate'
            prompt.write_text('new catalogue')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
server = socketserver.UnixStreamServer(str(root / 'api.sock'), Handler)
server.fail = False
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
env = dict(os.environ, SN_AGENTS_ROOT=str(agents))
env.pop('SN_CORE_API_URL', None)
try:
    def run():
        return subprocess.run([sys.executable, str(root / 'recompose.py'),
                               '--socket', str(root / 'api.sock')],
                              env=env, capture_output=True, text=True)
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [('/api/agents/john/regenerate', None)], calls
    assert prompt.read_text() == 'new catalogue', 'existing John must receive the new catalogue'
    server.fail = True
    result = run()
    assert result.returncode != 0 and 'john FAIL' in result.stdout, 'composition failure must fail the update'
    print('PASS: existing John is recomposed over the socket without a PID/env dependency; failure exits nonzero')
finally:
    server.shutdown()
    server.server_close()
PY
