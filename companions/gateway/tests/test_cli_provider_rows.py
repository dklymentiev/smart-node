"""A cli_providers row that runs the Claude Code binary is a backend under its
own slug: resolved, listed by the backends report, accepted by the runtime;
a disabled row or one of another binary is none of that."""
import asyncio
import os
from unittest.mock import patch

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backends  # noqa: E402
import main  # noqa: E402
import runtime_main  # noqa: E402
from backends.cli_claude import ClaudeCliBackend  # noqa: E402
from fake_db import install  # noqa: E402

LOCAL = {"slug": "local-cli", "binary_path": "/usr/local/bin/claude",
         "supported_models": ["qwen2.5:0.5b"], "enabled": True,
         "env_vars": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"}}
CODEX = {"slug": "codex-cli", "binary_path": "/usr/local/bin/codex",
         "supported_models": ["gpt-5"], "enabled": True, "env_vars": {}}


@pytest.fixture
def rows(monkeypatch):
    store = install(monkeypatch, backends)          # one db module: backends, main, model_tokens
    store.cli_providers.append(dict(LOCAL))
    store.cli_providers.append(dict(CODEX))
    backends.forget_rows()
    yield store
    backends.forget_rows()


def test_a_claude_code_row_is_a_backend_under_its_own_slug(rows):
    backend = backends.resolve_backend("local-cli/qwen2.5:0.5b")
    assert isinstance(backend, ClaudeCliBackend)
    assert (backend.provider_name, backend.runtime_name) == ("local-cli", "claude-cli")
    assert backends.resolve_backend("claude-cli/sonnet").provider_name == "claude-cli"
    assert backends.upstreams()["local-cli"] == "cli"
    assert backends.claude_cli_upstreams() == {"claude-cli", "local-cli"}
    assert backends.cli_model_supported("local-cli", "qwen2.5:0.5b")
    assert not backends.cli_model_supported("local-cli", "sonnet")


def test_a_row_of_another_binary_or_a_disabled_row_is_not(rows):
    with pytest.raises(ValueError, match="codex-cli"):
        backends.resolve_backend("codex-cli/gpt-5")
    assert "codex-cli" not in backends.upstreams()
    rows.cli_providers[1]["enabled"] = False           # local-cli switched off
    backends.forget_rows()
    with pytest.raises(ValueError):
        backends.resolve_backend("local-cli/qwen2.5:0.5b")
    assert "local-cli" not in backends.upstreams()


def test_the_rows_are_cached_and_the_last_copy_serves_while_the_database_is_away(rows):
    assert "local-cli" in backends.upstreams()
    rows.fail = True
    backends.forget_rows()
    assert "local-cli" not in backends.upstreams()      # nothing read yet, nothing kept
    rows.fail = False
    backends._rows["at"] = 0.0                          # a failed read is retried after a few seconds
    assert "local-cli" in backends.upstreams()
    rows.fail = True
    backends._rows["at"] = 0.0                          # the copy is stale, the database is down
    assert "local-cli" in backends.upstreams()          # the last copy serves


def test_the_backends_report_lists_the_row(rows):
    with patch.object(main.shutil, "which", lambda b: "/usr/local/bin/claude" if "claude" in str(b) else None), \
         patch.object(main.secrets_store, "get_provider_key", return_value=None), \
         patch.object(main, "FLINT_ENABLED", False):
        reported = {b["id"]: b for b in asyncio.run(main.list_backends())["backends"]}
    assert reported["local-cli"]["kind"] == "cli" and reported["local-cli"]["status"] == "available"
    assert reported["claude-cli"]["status"] == "available"
    assert "codex-cli" not in reported


def test_the_runtime_accepts_a_session_on_the_row_and_refuses_the_rest(rows, monkeypatch):
    monkeypatch.setattr(runtime_main, "ALLOWED_IPS", ["testclient", "127.0.0.1"])
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        raise RuntimeError("stop here: the model check passed")

    with TestClient(runtime_main.app) as client:
        monkeypatch.setattr(runtime_main.session_manager, "create_session", fake_create)
        r = client.post("/api/v1/sessions", json={"service": "test", "model": "codex-cli/gpt-5"})
        assert r.status_code == 400 and "Claude CLI" in r.json()["detail"]
        r = client.post("/api/v1/sessions", json={"service": "test", "model": "local-cli/gpt-5"})
        assert r.status_code == 400 and "not allowed" in r.json()["detail"]
        r = client.post("/api/v1/sessions", json={"service": "test", "model": "local-cli/qwen2.5:0.5b"})
        assert r.status_code == 500 and seen["model"] == "qwen2.5:0.5b"
        r = client.post("/api/v1/sessions", json={"service": "test", "model": "claude-cli/sonnet"})
        assert r.status_code == 500 and seen["model"] == "sonnet"
