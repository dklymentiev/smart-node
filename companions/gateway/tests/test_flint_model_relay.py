"""Flint calls the model through the door: at spawn its provider key leaves the
environment and a gateway token takes its place under the name Flint reads,
with FLINT_BASE_URL pointing at the door that speaks its provider's API."""
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402

import model_tokens  # noqa: E402
from backends import cli_claude  # noqa: E402
from fake_db import install  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    return install(monkeypatch, model_tokens)


def _env(**kw):
    return {"PATH": "/usr/bin", "SN_AGENT_SLUG": "agent-b", **kw}


def test_openrouter_key_is_replaced_by_a_token_and_the_openai_door(store):
    env = _env(FLINT_PROVIDER="openrouter", OPENROUTER_API_KEY="provider-key-secret")
    cli_claude._relay_flint(env, "s1", "agent-b", "fixture/model", "flint-cli")
    assert env["OPENROUTER_API_KEY"].startswith(model_tokens.PREFIX)
    assert "provider-key-secret" not in env.values()
    assert env["FLINT_BASE_URL"].endswith("/openai/v1")
    info = model_tokens.lookup(env["OPENROUTER_API_KEY"])
    assert (info["session_id"], info["agent"], info["provider"]) == ("s1", "agent-b", "flint-cli")


def test_openai_and_anthropic_providers_get_their_own_door(store):
    env = _env(FLINT_PROVIDER="openai", OPENAI_API_KEY="k", OPENROUTER_API_KEY="stray")
    cli_claude._relay_flint(env, "s2", "agent-b", "m", "flint-cli")
    assert set(env) & set(cli_claude._FLINT_KEYS) == {"OPENAI_API_KEY"}
    assert env["FLINT_BASE_URL"].endswith("/openai/v1")
    env = _env(FLINT_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
    cli_claude._relay_flint(env, "s3", "agent-b", "m", "flint-cli")
    assert env["ANTHROPIC_API_KEY"].startswith(model_tokens.PREFIX)
    assert env["FLINT_BASE_URL"].endswith("/anthropic/v1")


def test_a_provider_without_a_door_does_not_start(store):
    env = _env(FLINT_PROVIDER="groq", OPENROUTER_API_KEY="k")
    with pytest.raises(RuntimeError, match="no door"):
        cli_claude._relay_flint(env, "s4", "agent-b", "m", "flint-cli")


def test_the_rows_own_address_is_replaced_by_the_door(store):
    env = _env(FLINT_PROVIDER="openai", OPENAI_API_KEY="nvapi-k",
               FLINT_BASE_URL="https://integrate.api.nvidia.com/v1")
    cli_claude._relay_flint(env, "s5", "agent-b", "m", "flint-cli")
    assert env["FLINT_BASE_URL"].endswith("/openai/v1")
    assert "nvapi-k" not in env.values()
