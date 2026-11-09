"""Confirm-gate for destructive ops. The owner is present in the superchat, so a
dangerous op is staged (returns a single-use, TTL'd token) and only executes when
the same owner re-submits the token. In-memory by design (a pending confirmation
should not survive a restart).
"""
import secrets
import time
from dataclasses import dataclass

import config


@dataclass
class _Pending:
    op: str
    args: dict
    owner: str
    expires_at: float


class ConfirmStore:
    def __init__(self):
        self._by_token = {}

    def stage(self, op: str, args: dict, owner: str) -> str:
        token = secrets.token_urlsafe(18)
        self._by_token[token] = _Pending(op, args, owner,
                                          time.monotonic() + config.CONFIRM_TTL_SECS)
        return token

    def consume(self, token: str, op: str, owner: str):
        p = self._by_token.pop(token, None)
        if p is None or p.expires_at < time.monotonic():
            return None
        if p.op != op or p.owner != owner:
            return None
        return p.args


store = ConfirmStore()
