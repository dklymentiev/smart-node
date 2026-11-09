"""An in-memory sn_core.model_tokens behind db.query, for tests that run
without a database. Understands exactly the statements model_tokens.py
issues; anything else is an error, so a new statement gets a fake on purpose."""
import re
import time


class FakeDB:
    def __init__(self):
        self.rows = []          # dicts: digest, session_id, agent, os_user, model, provider, revoked_at, revoke_reason
        self.cli_providers = [  # sn_config.cli_providers as a test sees it
            {"slug": "claude-cli", "binary_path": "/usr/local/bin/claude",
             "supported_models": ["sonnet", "haiku", "opus"], "env_vars": {}, "enabled": True},
        ]
        self.statements = []
        self.fail = False       # raise on every call, to play a database that is down

    def query(self, sql, args=None, fetch=False, as_dict=False):
        self.statements.append(sql)
        if self.fail:
            raise RuntimeError("database down")
        args = tuple(args or ())
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO sn_core.model_tokens"):
            digest, session_id, agent, os_user, model, provider = args
            assert all(r["digest"] != digest for r in self.rows), "digest must be unique"
            self.rows.append({"digest": digest, "session_id": session_id, "agent": agent,
                              "os_user": os_user, "model": model, "provider": provider,
                              "revoked_at": None, "revoke_reason": None, "issued_at": time.time()})
            return None
        if s.startswith("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = 'reissued'"):
            (session_id,) = args
            self._revoke(lambda r: r["session_id"] == session_id, "reissued")
            return None
        if s.startswith("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = %s WHERE session_id"):
            reason, session_id = args
            self._revoke(lambda r: r["session_id"] == session_id, reason)
            return None
        if s.startswith("UPDATE sn_core.model_tokens SET revoked_at = now(), revoke_reason = %s WHERE revoked_at IS NULL"):
            (reason,) = args
            n = self._revoke(lambda r: True, reason)
            return [(i,) for i in range(n)]
        if s.startswith("DELETE FROM sn_core.model_tokens"):
            self.rows = [r for r in self.rows if r["revoked_at"] is None]
            return None
        if s.startswith("SELECT count(*) FROM sn_core.model_tokens"):
            return [(sum(1 for r in self.rows if r["revoked_at"] is None),)]
        if s.startswith("SELECT session_id, agent, os_user, model, provider FROM sn_core.model_tokens"):
            (digest,) = args
            found = [r for r in self.rows if r["digest"] == digest and r["revoked_at"] is None]
            return [{k: r[k] for k in ("session_id", "agent", "os_user", "model", "provider")}
                    for r in found]
        if s.startswith("SELECT slug, binary_path, supported_models, env_vars, enabled FROM sn_config.cli_providers"):
            return [dict(r) for r in self.cli_providers]
        if s.startswith("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s"):
            (slug,) = args
            return [{"env_vars": r["env_vars"]} for r in self.cli_providers if r["slug"] == slug]
        if s.startswith("SELECT 1 FROM sn_config.models"):
            return []                                   # an empty catalog
        raise AssertionError("FakeDB does not know this statement: " + s)

    def _revoke(self, pred, reason):
        n = 0
        for r in self.rows:
            if r["revoked_at"] is None and pred(r):
                r["revoked_at"] = time.time()
                r["revoke_reason"] = reason
                n += 1
        return n

    def live(self):
        return [r for r in self.rows if r["revoked_at"] is None]


def install(monkeypatch, module):
    """Put a FakeDB behind `module.db.query`, drop the module's cache, return it."""
    fake = FakeDB()
    monkeypatch.setattr(module.db, "query", fake.query)
    if hasattr(module, "forget"):
        module.forget()
    return fake


_ = re  # keep the import for ad-hoc matching in tests
