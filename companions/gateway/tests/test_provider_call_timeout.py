"""The provider call follows the caller's limit, not the shared client's 60 s.

Why this exists: /api/v1/chat is not streamed, so nothing arrives until the
completion is whole and the read timeout bounds the entire call. With the shared
client's fixed 60 s, an agent on a slow model was cut mid-turn on 2026-10-11 and
the gateway logged an empty error, because an httpx timeout has no message.

The stub provider answers after two seconds. The limits are scaled down to keep
the test quick; what is asserted is which number decides.

Runs two ways, like test_budget.py:
    pytest tests/test_provider_call_timeout.py
    ./venv/bin/python tests/test_provider_call_timeout.py
Needs the gateway environment (SMARTNODE_DB_DSN is read at import), no database rows.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from models import Message  # noqa: E402
from providers.base import BaseProvider  # noqa: E402
from providers.openrouter import OpenRouterProvider  # noqa: E402

DELAY = 2.0


class _SlowProvider(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(DELAY)
        body = json.dumps({"id": "x", "usage": {},
                           "choices": [{"message": {"content": "late answer"}}]}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client gave up, which is what one of the tests asks for

    def log_message(self, *args):
        pass


def _call(timeout):
    """One chat call against the slow stub. Returns (answer or exception, seconds it took)."""
    server = HTTPServer(("127.0.0.1", 0), _SlowProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def go():
        provider = OpenRouterProvider(api_key="stub", model="stub/model")
        provider.base_url = "http://127.0.0.1:%d" % server.server_port
        started = time.monotonic()
        try:
            answer = await provider.chat([Message(role="user", content="hi")],
                                         model="stub/model", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- the tests inspect it
            answer = exc
        took = time.monotonic() - started
        await provider.close()
        return answer, took

    try:
        return asyncio.run(go())
    finally:
        # Waits for the stub to finish its sleep, which is why the clock is read above.
        server.shutdown()


def test_a_call_slower_than_its_limit_is_cut_at_the_limit():
    answer, took = _call(0.5)
    assert isinstance(answer, httpx.ReadTimeout), answer
    # The empty message is why the handler has to name the type.
    assert str(answer) == ""
    assert took < DELAY


def test_a_call_within_its_limit_completes():
    answer, took = _call(30)
    assert answer["content"] == "late answer"
    assert took >= DELAY


def test_no_limit_given_means_the_client_default():
    assert BaseProvider.call_timeout(None) is httpx.USE_CLIENT_DEFAULT
    limit = BaseProvider.call_timeout(3600)
    assert limit.read == 3600.0 and limit.connect == 10.0


def test_the_chat_handler_takes_its_limit_from_the_registry():
    # Static on purpose: the handler needs a live app to run, and the point is
    # that the number comes from the turn-limit registry and not from a literal.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "main.py"), encoding="utf-8") as fh:
        source = fh.read()
    assert "timeout=turn_limits.agent_turn_limit_seconds(request.agent)" in source
    assert 'detail=f"chat request failed: {type(e).__name__}"' in source


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("ALL OK")
