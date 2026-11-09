"""Test stand for the panel inside the node: serves a copy of the panel
container's files and passes /api/* to core-api's unix socket as admin, as
Traefik and Authum do for panel.<domain>. Port 8097, loopback only.
python3 panel-stand.py <dir with the panel files, e.g. docker cp smart-node-panel:/usr/share/nginx/html>"""
import http.client
import http.server
import socket
import sys

ROOT = sys.argv[1]
SOCK = '/run/smart-node/api/core-api.sock'


class UConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost')

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCK)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, *a):
        pass

    def _api(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else None
        c = UConn()
        c.request(self.command, self.path, body=body, headers={
            'Remote-User': 'admin', 'Content-Type': self.headers.get('Content-Type', 'application/json'),
            'Accept': 'application/json'})
        r = c.getresponse()
        data = r.read()
        self.send_response(r.status)
        self.send_header('Content-Type', r.getheader('Content-Type', 'application/json'))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith('/api/'):
            return self._api()
        return super().do_GET()

    def do_POST(self):
        return self._api()

    def do_PATCH(self):
        return self._api()

    def do_PUT(self):
        return self._api()

    def do_DELETE(self):
        return self._api()


http.server.ThreadingHTTPServer(('127.0.0.1', 8097), H).serve_forever()
