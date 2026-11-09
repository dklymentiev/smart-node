"""[recent activity] shows what the agent did, not the node's permission checks.

Ten auth.permission_granted per turn used to fill the section's limit and push
out the actions it exists for (2026-10-29: a reminder.set was the one real line
among them).
"""
import asyncio

from services import ledger_service


class _Resp:
    status_code = 200

    def __init__(self, events):
        self._events = events

    def json(self):
        return {"events": self._events}


class _Client:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return _Resp(self._events)


def test_auth_events_do_not_crowd_out_actions(monkeypatch):
    events = [{"event_type": "auth.permission_granted", "ts": "t", "payload": {}}] * 30
    events.append({"event_type": "reminder.set", "ts": "t", "payload": {}})
    monkeypatch.setattr(ledger_service.httpx, "AsyncClient", lambda **k: _Client(events))
    rows = asyncio.run(ledger_service.fetch_recent_activity("foreman"))
    assert [r["what"] for r in rows] == ["reminder.set"]
