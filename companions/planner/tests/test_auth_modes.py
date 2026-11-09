"""Unit tests for DD-008 auth-mode helpers.

These cover the pure parts of ``planner.auth`` that do not need a DB:
``get_auth_mode`` env parsing and ``_normalize_proxy_user`` input cleaning.
Integration tests for end-to-end behaviour under each mode live in
``tests/integration/test_auth_modes.py``.
"""

from __future__ import annotations

import pytest

from planner.auth import (
    DEFAULT_PROXY_HEADER,
    ENV_AUTH_MODE,
    ENV_PROXY_HEADER,
    _normalize_proxy_user,
    _proxy_header_name,
    get_auth_mode,
)


class TestGetAuthMode:
    def test_default_is_dev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_AUTH_MODE, raising=False)
        assert get_auth_mode() == "dev"

    @pytest.mark.parametrize("value", ["dev", "proxy", "apikey"])
    def test_accepts_each_valid_mode(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(ENV_AUTH_MODE, value)
        assert get_auth_mode() == value

    @pytest.mark.parametrize("value", [" DEV ", "Proxy", "APIKEY"])
    def test_normalizes_case_and_whitespace(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(ENV_AUTH_MODE, value)
        assert get_auth_mode() == value.strip().lower()

    @pytest.mark.parametrize("value", ["", "open", "basic", "oauth", "dev2"])
    def test_rejects_invalid_mode(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(ENV_AUTH_MODE, value)
        with pytest.raises(RuntimeError) as exc:
            get_auth_mode()
        assert ENV_AUTH_MODE in str(exc.value)


class TestProxyHeaderName:
    def test_default_is_remote_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_PROXY_HEADER, raising=False)
        assert _proxy_header_name() == DEFAULT_PROXY_HEADER == "Remote-User"

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PROXY_HEADER, "X-Forwarded-User")
        assert _proxy_header_name() == "X-Forwarded-User"


class TestNormalizeProxyUser:
    def test_component_actor_keeps_its_namespace(self) -> None:
        assert _normalize_proxy_user("component:hitl-bridge") == "component:hitl-bridge"
        assert _normalize_proxy_user("component:BAD/NAME") is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("alice", "alice"),
            ("Alice", "alice"),
            ("  bob  ", "bob"),
            ("alice.smith", "alice.smith"),
            ("user_42", "user_42"),
            ("user-42", "user-42"),
            ("Alice@Example.com", "alice.example.com"),
            ("first last", "first.last"),
            ("a" * 100, "a" * 64),  # truncated to 64
        ],
    )
    def test_normalizes_valid_inputs(self, raw: str, expected: str) -> None:
        assert _normalize_proxy_user(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "...", "@@@", "."])
    def test_unsalvageable_returns_none(self, raw: str) -> None:
        assert _normalize_proxy_user(raw) is None

    def test_lowercases_consistently(self) -> None:
        assert _normalize_proxy_user("CAROL") == "carol"

    def test_strips_leading_trailing_dots(self) -> None:
        assert _normalize_proxy_user("...dave...") == "dave"
