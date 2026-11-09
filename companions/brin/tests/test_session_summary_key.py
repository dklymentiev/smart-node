"""The session summary calls OpenRouter with the key from the environment and
nothing else. It used to fall back to a key written into the source, so every
node shipped with it."""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import session_summary as ss  # noqa: E402


class Client:
    calls = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        Client.calls.append(headers)
        raise RuntimeError("no network in tests")


def test_no_key_in_the_environment_means_no_call(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(ss.httpx, "AsyncClient", Client)
    Client.calls = []
    assert asyncio.run(ss.SessionSummary()._extract_facts("hello")) is None
    assert Client.calls == []


def test_the_key_used_is_the_one_in_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-key")
    monkeypatch.setattr(ss.httpx, "AsyncClient", Client)
    Client.calls = []
    asyncio.run(ss.SessionSummary()._extract_facts("hello"))
    assert Client.calls == [{"Authorization": "Bearer test-only-key",
                             "Content-Type": "application/json"}]


def test_no_provider_key_is_written_into_the_source():
    root = pathlib.Path(__file__).resolve().parents[3]
    hits = [str(p) for p in root.joinpath("companions").rglob("*.py")
            if "sk-or-v1-" in p.read_text(errors="ignore") and p.name != pathlib.Path(__file__).name]
    assert hits == []
