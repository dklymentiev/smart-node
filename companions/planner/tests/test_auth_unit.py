"""Unit-level tests for auth helpers (no DB required)."""

from __future__ import annotations

from planner.auth import API_KEY_PREFIX, _hash_key, generate_plaintext_key


def test_generate_plaintext_has_prefix() -> None:
    k = generate_plaintext_key()
    assert k.startswith(API_KEY_PREFIX)
    # token_urlsafe(32) yields ~43 chars, plus prefix
    assert len(k) > len(API_KEY_PREFIX) + 30


def test_generate_plaintext_is_unique() -> None:
    keys = {generate_plaintext_key() for _ in range(100)}
    assert len(keys) == 100


def test_hash_is_stable_and_long() -> None:
    h1 = _hash_key("secret")
    h2 = _hash_key("secret")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_hash_differs_for_different_inputs() -> None:
    assert _hash_key("a") != _hash_key("b")
    assert _hash_key("") != _hash_key("a")
