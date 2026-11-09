#!/usr/bin/env bash
# Registry ceiling and the timeouts outside the gateway stay ordered.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT"
if [[ "${1:-}" == "--baseline" ]]; then
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  git -C "$ROOT" archive "${2:-origin/main}" companions/brin companions/gateway companions/core-api | tar -x -C "$WORK"
fi
python3 - "$WORK" <<'PY'
import ast, asyncio, importlib.util, os, pathlib, sys, types
root=pathlib.Path(sys.argv[1]); helper=root/'companions/brin/core/turn_budget.py'
sys.modules['httpx'] = types.SimpleNamespace(AsyncClient=None, HTTPError=Exception)
# The helper proves itself to core-api with the service credential.
sys.path.insert(0, str(root/'companions/brin'))
os.environ['SN_SERVICE_AUTH_SECRET']='stand-secret'
sent=[]
spec=importlib.util.spec_from_file_location('turn_budget_under_test',helper)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
os.environ['SN_CORE_API_URL']='http://stand.test'
os.environ['SN_TURN_MAX_SECONDS']='10'  # stale environment must be ignored
bound=[900]
fail=[False]
class Response:
    def raise_for_status(self): pass
    def json(self): return {'seconds':bound[0]}
class Client:
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def get(self, url, headers=None):
        sent.append(headers)
        if fail[0]: raise OSError('core-api restarting')
        return Response()
mod.httpx.AsyncClient=Client
asyncio.run(mod.refresh_turn_ceiling())
assert (mod.turn_ceiling(),mod.gateway_call_timeout(),mod.runner_call_timeout()) == (900,930,960)
print('ok   callers use the cached registered ceiling')
assert sent[-1] == {'X-SN-Service': 'brin', 'X-SN-Service-Auth': 'stand-secret'}, sent[-1]
print('ok   the registry read carries the service proof')
bound[0]=60
assert mod.turn_ceiling() == 900, 'ordinary callers must not make HTTP requests'
asyncio.run(mod.refresh_turn_ceiling())
assert (mod.turn_ceiling(),mod.gateway_call_timeout(),mod.runner_call_timeout()) == (60,90,120)
print('ok   an async refresh takes effect without a restart')
fail[0]=True
assert asyncio.run(mod.refresh_turn_ceiling()) == 60
assert mod.gateway_call_timeout() == 90
print('ok   a core-api restart retains the last bound')
body=(root/'companions/gateway/backends/cli_claude.py').read_text()
assert 'turn_limit_seconds(session_id)' in body and 'timeout=remaining' in body
assert 'timeout=min(READLINE_TIMEOUT, remaining)' not in body
print('ok   gateway enforces the agent profile and tolerates a quiet tool')
for path,needle in [('core/executor.py','gateway_call_timeout()'),('mcp_server.py','gateway_call_timeout()'),('runner.py','runner_call_timeout()')]:
    assert needle in (root/'companions/brin'/path).read_text(), path
print('ok   brin and runner use a longer derived wait')
assert 'await refresh_turn_ceiling()' in (root/'companions/brin/main.py').read_text()
assert 'await refresh_turn_ceiling()' in (root/'companions/brin/runner.py').read_text()
print('ok   desk and runner refresh asynchronously at turn start')

# Compile the real async route without its HTTP decorator. Its first direct
# turn after a process restart must load the bound before the executor reads it.
mod._last_ceiling = None
fail[0] = False
observed = []
class DirectAgent:
    pass
class Executor:
    async def run_direct(self, *_args, **_kwargs):
        observed.append(mod.gateway_call_timeout())
        yield 'answer'
class ResponseBody:
    def __init__(self, value, **_kwargs): self.value = value
async def route(*_args): return DirectAgent()
async def launch(_agent, _kind, work):
    assert await work == 'answer'
    return 'call-1'
tree = ast.parse((root/'companions/brin/main.py').read_text())
handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name == 'send_message_async')
handler.decorator_list = []
scope = dict(os=os, DirectAgent=DirectAgent, MessageRequest=object, JSONResponse=ResponseBody,
             refresh_turn_ceiling=mod.refresh_turn_ceiling,
             router=types.SimpleNamespace(route=route),
             agent_registry=types.SimpleNamespace(get_active=lambda _user: None),
             executor=Executor(), launch_async=launch)
exec(compile(ast.Module(body=[handler], type_ignores=[]), 'main.py', 'exec'), scope)
request = types.SimpleNamespace(message='hello', context={'user_id':'channel:caller'},
                                channel='telegram', agent='agent', model='fixture')
result = asyncio.run(scope['send_message_async'](request))
assert result.value == {'call_id':'call-1','status':'running','agent':'agent'}
assert observed == [90], observed
print('ok   first /message/async turn after restart loads bound before gateway call')
PY
