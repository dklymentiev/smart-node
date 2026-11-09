"""Per-turn actions and memory must survive interleaved /api/message responses.

The entire request handler is compiled unchanged, without its FastAPI decorator
or application startup. The complete Executor and catalog normalizer are imported.
Only external services, routing and JSONResponse transport are replaced. Async
events schedule the race; no timing assumptions, network or model calls are used.
"""

import argparse
import ast
import asyncio
import contextvars
import importlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from dataclasses import dataclass, field
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument("--baseline")
args, test_args = parser.parse_known_args()
scratch = tempfile.TemporaryDirectory(prefix="turn-response-baseline-")
brin = ROOT / "companions/brin"
if args.baseline:
    brin = pathlib.Path(scratch.name)
    for relative in ("core/executor.py", "core/catalog_tools.py", "main.py"):
        target = brin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(
            ["git", "show", f"{args.baseline}:companions/brin/{relative}"], cwd=ROOT,
        ))


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


async def nothing(*_args, **_kwargs):
    return None


async def empty_text(*_args, **_kwargs):
    return ""


async def identity(value):
    return value


def forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected external call")


@dataclass
class DirectAgent:
    specialist_id: str
    message: str
    allow_commands: bool = False


class SmartSearchRequest(types.SimpleNamespace):
    pass


@dataclass
class SmartSearchResult:
    documents: list = field(default_factory=list)
    total: int = 0
    query: str = ""
    search_strategy: str = "fixture"

    def model_dump(self):
        return {"documents": [vars(doc) for doc in self.documents],
                "total": self.total, "query": self.query,
                "search_strategy": self.search_strategy}


class ToolsConfig:
    def get_memory(self):
        return types.SimpleNamespace(enabled=True, mode="auto-inject", limit=5,
                                     min_score=0.3, max_context_length=500)


stub("core", __path__=[str(brin / "core")])
stub("core.logging_config", correlation_id_var=contextvars.ContextVar("test-correlation", default="-"))
stub("core.turn_budget", gateway_call_timeout=lambda: 600, refresh_turn_ceiling=nothing)
stub("core.specialist_loader", SpecialistLoader=object)
stub("core.tools", ToolsConfig=ToolsConfig)
stub("core.attachment_delivery", content_for_agent=identity)
stub("core.mcp_client")
stub("services")
stub("services.ledger_service", build_ledger_sections=empty_text)
stub("models")
stub("models.actions", DirectAgent=DirectAgent, ReinWorkflow=type("ReinWorkflow", (), {}))
stub("models.memory", SmartSearchRequest=SmartSearchRequest,
     SmartSearchResult=SmartSearchResult, MemoryContextResult=types.SimpleNamespace)
stub("httpx", AsyncClient=forbidden)
ex = importlib.import_module("core.executor")


class JSONResponse:
    def __init__(self, payload, status_code=200):
        self.body = json.dumps(payload).encode()
        self.status_code = status_code


class Memory:
    async def search(self, request):
        agent = request.workspace
        docs = [] if request.query.endswith("plain") else [types.SimpleNamespace(
            guid=f"doc-{agent}", content=f"memory owned by {agent}", similarity_score=0.9,
        )]
        return SmartSearchResult(documents=docs, total=len(docs), query=request.query)

    def format_context(self, docs, _limit):
        return types.SimpleNamespace(context=docs[0].content + "\n")


def action(agent, index=1):
    return {"id": f"{agent}-tool-{index}", "name": "fixture_read",
            "input": {"path": f"/fixtures/{agent}/owner-note.txt"}}


class Catalog:
    def __init__(self, agent, http):
        self.agent, self.http = agent, http
        self.dispatched = []

    def openai_specs(self):
        return ([{"type": "function", "function": {"name": "fixture_read",
                 "parameters": {"type": "object"}}}] if self.http else [])

    def deferred_names(self):
        return []

    async def dispatch(self, name, params):
        self.dispatched.append((name, params))
        return f"tool result owned by {self.agent}"


class Harness:
    def __init__(self, http_agents=(), child_turn=False):
        self.http_agents = set(http_agents)
        self.catalogs = {agent: Catalog(agent, agent in self.http_agents) for agent in ("alpha", "beta")}
        self.executor = ex.Executor(types.SimpleNamespace(get=lambda _id: None), ToolsConfig(), Memory())
        self.rounds = {}
        self.persist = nothing

        async def session(_specialist, _prompt, _model, **kwargs):
            return kwargs["agent_slug"]

        async def runtime(agent):
            return ("fixture/model", "http" if agent in self.http_agents else "cli")

        async def history(*_args, **_kwargs):
            return []

        async def stateless(_model, _system, _history, _specs, **kwargs):
            agent = kwargs["agent"]
            key = (agent, kwargs["raw_user_content"])
            round_num = self.rounds.get(key, 0)
            self.rounds[key] = round_num + 1
            calls = []
            if round_num == 0 and not key[1].endswith("plain"):
                calls = [{"id": action(agent)["id"], "type": "function", "function": {
                    "name": "fixture_read", "arguments": json.dumps(action(agent)["input"]),
                }}]
            return {"content": f"answer from {agent}", "tool_calls": calls}

        self.executor._get_or_create_session = session
        self.executor._agent_runtime = runtime
        self.executor._assembled_prompt = empty_text
        self.executor._load_persisted_history = history
        self.executor._persist_tool_truth = nothing
        self.executor._gateway_chat_stateless = stateless
        if child_turn:
            original = self.executor.run_direct

            async def in_child(*a, **kw):
                async def collect():
                    return [chunk async for chunk in original(*a, **kw)]
                for chunk in await asyncio.create_task(collect()):
                    yield chunk

            self.executor.run_direct = in_child

        async def route(message, context):
            return DirectAgent(context["user_id"].split(":")[1], message)

        async def persist(*a, **kw):
            await self.persist(*a, **kw)

        namespace = dict(DirectAgent=DirectAgent, os=os, uuid=uuid, time=time, asyncio=asyncio,
                         refresh_turn_ceiling=nothing,
                         executor=self.executor, MessageRequest=types.SimpleNamespace,
                         JSONResponse=JSONResponse, router=types.SimpleNamespace(route=route),
                         logger=logging.getLogger("test-route"), _parse_agent_command=lambda _m: None,
                         agent_registry=types.SimpleNamespace(get_active=lambda user: types.SimpleNamespace(
                             name=user, history_dir="unused", workspace=user.split(":")[1])),
                         session_history=types.SimpleNamespace(get_or_start_session=lambda *a, **kw: None,
                                                               append=lambda *a, **kw: None),
                         try_fast_path=nothing, _persist_webchat_turn=persist,
                         _desk_ensure=nothing, _desk_turn_begin=nothing,
                         _desk_event=nothing, _desk_turn_end=nothing, _COULD_NOT_ANSWER="failed")
        tree = ast.parse((brin / "main.py").read_text())
        executor_import = next(n for n in tree.body if isinstance(n, ast.ImportFrom)
                               and n.module == "core.executor")
        handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_message")
        handler.decorator_list = []
        exec(compile(ast.Module(body=[executor_import, handler], type_ignores=[]), "main.py", "exec"), namespace)
        self.handler = namespace["send_message"]

    async def ask(self, agent, plain=False):
        req = types.SimpleNamespace(message=f"{agent}-{'plain' if plain else 'tools'}",
                                    context={"user_id": f"desk:{agent}"}, agent=agent,
                                    model="fixture/model", channel="web", stream=False,
                                    wait_seconds=None)
        result = await self.handler(req)
        assert result.status_code == 200
        return json.loads(result.body)

    def services(self):
        async def load(agent):
            return self.catalogs[agent]

        class Client:
            async def __aenter__(client):
                return client

            async def __aexit__(client, *_args):
                pass

            async def post(client, url, json):
                agent = url.split("/sessions/")[1].split("/")[0]
                calls = [] if json["content"].endswith("plain") else [action(agent)]
                return types.SimpleNamespace(status_code=200, json=lambda: {
                    "response": {"content": f"answer from {agent}", "tool_calls": calls},
                })

        return (patch.object(ex.CatalogTools, "load", side_effect=load),
                patch.object(ex.httpx, "AsyncClient", side_effect=lambda **kw: Client()))


class IsolationTests(unittest.TestCase):
    def assert_own_response(self, response, agent, plain=False):
        self.assertEqual(response["content"], f"answer from {agent}")
        with self.subTest(agent=agent, artifact="actions"):
            self.assertEqual(response.get("actions", []), [] if plain else [action(agent)])
        with self.subTest(agent=agent, artifact="memory"):
            expected = None if plain else {
                "documents": [{"guid": f"doc-{agent}", "content": f"memory owned by {agent}",
                               "similarity_score": 0.9}],
                "total": 1, "query": f"{agent}-tools", "search_strategy": "fixture",
            }
            self.assertEqual(response.get("memory"), expected)

    async def concurrent(self, http_agents=(), child_turn=False):
        harness = Harness(http_agents, child_turn)
        alpha_finished, release_alpha = asyncio.Event(), asyncio.Event()

        async def persist(_ctx, agent, *_args):
            if agent == "alpha":
                alpha_finished.set()
                await release_alpha.wait()
            else:
                release_alpha.set()

        harness.persist = persist

        async def beta_after_alpha():
            await alpha_finished.wait()
            return await harness.ask("beta")

        load, client = harness.services()
        with load, client:
            results = await asyncio.wait_for(asyncio.gather(harness.ask("alpha"), beta_after_alpha()), 2)
        for agent, response in zip(("alpha", "beta"), results):
            self.assert_own_response(response, agent)

    def test_concurrent_cli_agents_keep_own_actions_and_memory(self):
        asyncio.run(self.concurrent())

    def test_concurrent_cli_and_http_agents_with_turns_in_child_tasks(self):
        asyncio.run(self.concurrent(http_agents=("beta",), child_turn=True))

    def test_concurrent_http_agents_keep_own_dispatched_actions_and_memory(self):
        asyncio.run(self.concurrent(http_agents=("alpha", "beta")))

    def test_sequential_cli_then_http_turn_without_tools_or_memory(self):
        async def run():
            harness = Harness(http_agents=("beta",))
            load, client = harness.services()
            with load, client:
                self.assert_own_response(await harness.ask("alpha"), "alpha")
                self.assert_own_response(await harness.ask("beta", plain=True), "beta", plain=True)

        asyncio.run(run())

    def test_sequential_same_agent_changes_backend_without_carrying_artifacts(self):
        async def run():
            harness = Harness()
            load, client = harness.services()
            with load, client:
                self.assert_own_response(await harness.ask("alpha"), "alpha")
                harness.http_agents.add("alpha")
                harness.catalogs["alpha"].http = True
                self.assert_own_response(await harness.ask("alpha", plain=True), "alpha", plain=True)

        asyncio.run(run())


if __name__ == "__main__":
    try:
        unittest.main(argv=[sys.argv[0], *test_args], verbosity=2)
    finally:
        scratch.cleanup()
