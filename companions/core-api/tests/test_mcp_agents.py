"""The configurator's tools for agents: what each sends to core-api.

They exist because the only way to the agents API was curl through run_command,
and Flint's command guard refuses that with no operator attached: a configurator
on Flint could plan a team and create nobody (2026-10-31).
"""
import asyncio
import json

import mcp_agents


def _calls(monkeypatch, own_model="flint-cli/some-model"):
    sent = []

    async def fake(method, path, body=None):
        sent.append((method, path, body))
        if method == "GET":
            return 200, {"model": own_model, "tenant_id": 3}
        return 200, {}
    monkeypatch.setattr(mcp_agents, "_request", fake)
    monkeypatch.setenv("SN_AGENT_SLUG", "john")
    return sent


def test_a_new_agent_runs_on_the_model_and_tenant_of_the_one_creating_it(monkeypatch):
    """The API's default model is a Claude one: on a node with another provider
    the new agent could not have answered."""
    sent = _calls(monkeypatch)
    asyncio.run(mcp_agents.create_agent("smm", "SMM manager", "You run the Instagram.", role="SMM"))
    assert sent[0][:2] == ("GET", "/api/agents/john")
    method, path, body = sent[1]
    assert (method, path) == ("POST", "/api/agents")
    assert body == {"slug": "smm", "title": "SMM manager", "role": "SMM",
                    "specialist_body": "You run the Instagram.", "model": "flint-cli/some-model", "tenant_id": 3}


def test_a_refusal_comes_back_to_the_agent(monkeypatch):
    async def fake(method, path, body=None):
        return (200, {"model": "m"}) if method == "GET" else (403, {"detail": "no"})
    monkeypatch.setattr(mcp_agents, "_request", fake)
    monkeypatch.setenv("SN_AGENT_SLUG", "john")
    out = json.loads(asyncio.run(mcp_agents.create_agent("smm", "SMM", "x")))
    assert out["error"] == "HTTP 403" and out["detail"] == "no"


def test_update_sends_only_what_changes(monkeypatch):
    sent = _calls(monkeypatch)
    asyncio.run(mcp_agents.update_agent("smm", instructions="New text."))
    assert sent == [("PUT", "/api/agents/smm", {"specialist_body": "New text."})]


def test_delete_is_the_route_for_it(monkeypatch):
    sent = _calls(monkeypatch)
    asyncio.run(mcp_agents.delete_agent("smm"))
    assert sent == [("DELETE", "/api/agents/smm", None)]


class _Cur:
    def __init__(self, used):
        self.used = used

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return [{"avatar": a} for a in self.used]


def _store(tmp_path, monkeypatch, **packs):
    from routes import agents
    for pack, files in packs.items():
        (tmp_path / pack).mkdir()
        for f in files:
            (tmp_path / pack / f).write_bytes(b"x")
    monkeypatch.setattr(agents, "AVATARS_DIR", str(tmp_path))
    return agents


def test_a_new_agent_gets_the_next_free_picture_of_the_teams_pack(tmp_path, monkeypatch):
    agents = _store(tmp_path, monkeypatch, clay=["00.webp", "01.webp", "02.webp"], crew=["a.webp"])
    assert agents._next_avatar(_Cur(["clay/00.webp", "clay/01.webp"])) == "clay/02.webp"


def test_when_the_pack_is_used_up_pictures_repeat(tmp_path, monkeypatch):
    agents = _store(tmp_path, monkeypatch, clay=["00.webp"])
    assert agents._next_avatar(_Cur(["clay/00.webp"])) == "clay/00.webp"


def test_an_uploaded_picture_does_not_choose_the_pack(tmp_path, monkeypatch):
    agents = _store(tmp_path, monkeypatch, crew=["a.webp"])
    assert agents._next_avatar(_Cur(["custom/me.webp"])) == "crew/a.webp"


def test_no_store_leaves_no_picture(tmp_path, monkeypatch):
    agents = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(agents, "AVATARS_DIR", str(tmp_path / "missing"))
    assert agents._next_avatar(_Cur([])) is None
