"""Recompose provisioned agents after installing the release capability catalogue."""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slugs', nargs='*')
    parser.add_argument('--socket', default='/run/smart-node/api/core-api.sock')
    args = parser.parse_args()

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(args.socket)

    def call(method, path):
        conn = UnixConnection('localhost', timeout=180)
        try:
            conn.request(method, '/api/' + path)
            response = conn.getresponse()
            data = response.read()
            if response.status >= 300:
                raise RuntimeError(f'HTTP {response.status}: {data[:200].decode(errors="replace")}')
            return json.loads(data) if data else None
        finally:
            conn.close()

    root = Path(os.environ.get('SN_AGENTS_ROOT', '/srv/agents'))
    targets = args.slugs or [a['slug'] for a in call('GET', 'agents')['agents']
                            if (root / a['slug'] / 'CLAUDE.md').is_file()]
    failed = []
    for slug in targets:
        try:
            call('POST', 'agents/' + quote(slug, safe='') + '/regenerate')
            print(slug, 'OK')
        except (OSError, RuntimeError, http.client.HTTPException) as exc:
            print(slug, 'FAIL', exc)
            failed.append(slug)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
