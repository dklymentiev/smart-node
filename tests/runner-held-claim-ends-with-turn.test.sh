#!/usr/bin/env bash
#
# A held claim ends with the turn, not with the lease.
#
# WHAT WENT WRONG. A wake whose turn outlives the gateway's synchronous window
# comes back 202, and the Runner keeps the claim on purpose: releasing it while
# the turn is alive relaunches the agent on top of itself. Nothing ever
# told the Runner that such a turn had ENDED, so the claim stood for the whole
# RUNNER_LEASE_SECONDS, and the idle agent was absent from with-pending-work and
# deaf. On the field node a letter that arrived three minutes after a turn ended
# waited twenty-two minutes for the lease.
#
# TAKEN AS A PROBLEM, NOT AS THE DIFF. The field PR watched /proc for a claude
# process owned by the agent's account. Our gateway keeps that process alive
# after the turn on purpose (--resume) and reaps it after SN_CLI_IDLE_TIMEOUT,
# 900s, so the process table would have released a quarter of an hour late.
# The Runner asks the gateway instead, which knows when the turn ended.
#
# THE PROPERTIES:
#   1. a 202 wake is recorded as a hold, with its ref
#   2. the sweep runs every loop, ahead of tick(); a new launch drops any hold
#   3. the turn is looked up by the conversation brin opens for the wake,
#      "<agent>:<ref>", and brin/executor still build it that way
#   4. every uncertainty holds: no session, a gateway error, an answer without
#      in_progress, an exception
#   5. over on HELD_MISSES sweeps in a row releases; a live turn resets the count
#   6. an expired lease is forgotten, never released: the claim may be someone
#      else's by then
#   7. the release happens only while current_ref is still this wake's, because
#      sn_core.release_agent frees whoever holds the agent
#
#   tests/runner-held-claim-ends-with-turn.test.sh                  # tree, PASS
#   tests/runner-held-claim-ends-with-turn.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "runner: a held claim ends with the turn (git:$BASELINE)"
else echo "runner: a held claim ends with the turn (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/runner.py        > "$D/runner.py"
read_src companions/brin/core/executor.py > "$D/executor.py"
read_src companions/brin/main.py          > "$D/main.py"
for f in runner executor main; do
    [[ -s "$D/$f.py" ]] || { bad "sources are readable ($f.py)"; echo; echo "FAIL"; exit 1; }
done

# -------------------------------------------------------------- the wiring
if awk '/elif r.status_code == 202:/,/elif r.status_code in \(400/' "$D/runner.py" \
     | grep -q '_held\[agent\] = (_time.monotonic(), ref, 0)'; then
    ok "a 202 wake is recorded as a hold, with its ref"
else
    bad "a 202 wake is recorded as a hold, with its ref"
fi

main_body=$(awk 'f && /^(def|async def|class|if __name__)/{exit} /^async def main\(/{f=1} f' "$D/runner.py")
sweep_line=$(grep -n 'await _sweep_held_claims(_http())' <<<"$main_body" | head -1 | cut -d: -f1)
tick_line=$(grep -n 'await tick()' <<<"$main_body" | head -1 | cut -d: -f1)
if [[ -n "$sweep_line" && -n "$tick_line" && "$sweep_line" -lt "$tick_line" ]]; then
    ok "the sweep runs every loop, ahead of tick()"
else
    bad "the sweep runs every loop, ahead of tick()"
fi

if awk 'f && /^(def|async def)/{exit} /^async def tick\(/{f=1} f' "$D/runner.py" \
     | grep '_held.pop(slug, None)' >/dev/null; then
    ok "a new launch drops any stale hold on the agent"
else
    bad "a new launch drops any stale hold on the agent"
fi

# The conversation key is a contract across three files: the Runner passes ref
# as user_id, brin's main passes user_id as conversation_id, and the executor
# prefixes the agent. If any of the three changes, the lookup finds nothing and
# every hold waits out the lease again, silently.
if grep -q '_wake_ctx = {"user_id": ref}' "$D/runner.py" \
   && grep -q 'conversation_id=user_id' "$D/main.py" \
   && grep -q 'convo_key = f"{agent_slug}:{conversation_id}"' "$D/executor.py" \
   && grep -q '"conversation": "%s:%s" % (agent, ref)' "$D/runner.py"; then
    ok "the wake's session is found by the conversation brin opens for it"
else
    bad "the wake's session is found by the conversation brin opens for it"
fi

sweep_body=$(awk 'f && /^(def|async def)/{exit} /^async def _sweep_held_claims\(/{f=1} f' "$D/runner.py")
if grep -q 'if await _holder_ref(client, agent) != ref:' <<<"$sweep_body"; then
    ok "a release happens only while the claim is still this wake's"
else
    bad "a release happens only while the claim is still this wake's"
fi

# ------------------------------------------------------------- the decisions
while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/runner.py" <<'PY'
import ast, asyncio, os, sys
tree = ast.parse(open(sys.argv[1]).read())
want_fn = ("_hold_verdict", "_turn_running")
want_const = ("LEASE_SECONDS", "HELD_MISSES")
keep = [n for n in tree.body
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want_fn)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want_const for t in n.targets))]
ns = {"os": os, "GATEWAY": "http://gw", "GATEWAY_TENANT": "1"}
exec(compile(ast.Module(body=keep, type_ignores=[]), "runner.py", "exec"), ns)
if "_hold_verdict" not in ns or "_turn_running" not in ns:
    print("FAIL _hold_verdict and _turn_running exist -- the claim waits out the lease")
    sys.exit(0)
v, lease, need = ns["_hold_verdict"], ns["LEASE_SECONDS"], ns["HELD_MISSES"]

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

check("a live turn holds and resets the count", v(60, True, 1) == ("running", 0))
check("the gateway not knowing holds, and keeps the count", v(60, None, 1) == ("unknowable", 1))
check("one sweep of 'over' is not yet a release", need < 2 or v(60, False, 0) == ("settling", 1))
check("over on HELD_MISSES sweeps in a row releases", v(60, False, need - 1) == ("release", need))
check("an expired lease is forgotten, whatever the gateway says",
      v(lease, False, need - 1)[0] == "expired" and v(lease + 5, True, 0)[0] == "expired")

class R:
    def __init__(self, code, body): self.status_code, self._b = code, body
    def json(self): return self._b

class C:
    """A gateway that answers from a script: url fragment -> response or exception."""
    def __init__(self, table): self.table, self.urls = table, []
    async def get(self, url, params=None):
        self.urls.append((url, params))
        for frag, resp in self.table.items():
            if frag in url:
                if isinstance(resp, Exception): raise resp
                return resp
        return R(404, {})

def run(table):
    c = C(table)
    return asyncio.run(ns["_turn_running"](c, "alex", "runner-alex-9-3")), c

sess = R(200, {"sessions": [{"session_id": "s1"}]})
got, c = run({"/sessions/v2": sess, "/s1/activity": R(200, {"in_progress": False})})
check("a finished turn reads as over", got is False)
check("the lookup names the wake's own conversation",
      c.urls and (c.urls[0][1] or {}).get("conversation") == "alex:runner-alex-9-3")
got, _ = run({"/sessions/v2": sess, "/s1/activity": R(200, {"in_progress": True, "elapsed": 400})})
check("a running turn reads as running", got is True)
got, _ = run({"/sessions/v2": R(200, {"sessions": []})})
check("no session found is 'cannot tell', never 'over'", got is None)
got, _ = run({"/sessions/v2": R(502, {})})
check("a gateway error is 'cannot tell'", got is None)
got, _ = run({"/sessions/v2": sess, "/s1/activity": R(200, {})})
check("an answer without in_progress is 'cannot tell'", got is None)
got, _ = run({"/sessions/v2": RuntimeError("connection refused")})
check("an exception is 'cannot tell'", got is None)
PY
)

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
