#!/usr/bin/env bash
#
# The secrets MCP server lets the node's sysadmin and foreman write.
#
# WHAT WAS MISSING. core-api already admitted them: capability sysadmin or
# configurator is an approved secrets writer in routes/secrets.py, and their
# role admin holds secrets.write and agents.grants.write. But mcp_secrets.py
# offered resolve_secret only, so a sysadmin agent asked to hand a ref to a
# colleague answered that granting is an operator action.
#
# THE PROPERTIES:
#   1. mcp_secrets.py offers set_secret and grant_secret_ref as MCP tools
#   2. set_secret is a POST to /api/secrets; grant_secret_ref a merge PUT on
#      /api/agents/{slug}/grants, never a plain PUT that would replace the set
#   3. the tools carry no policy of their own: no capability or slug check in
#      the MCP server, core-api decides
#   4. __main__ awaits run() only when it returns an awaitable (SDK 1.x blocks)
#   5. the seeded secrets contract names the write tools and who may use them
#
#   tests/secrets-mcp-write-tools.test.sh                  # tree, PASS
#   tests/secrets-mcp-write-tools.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "secrets MCP write tools (git:$BASELINE)"
else echo "secrets MCP write tools (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/mcp_secrets.py > "$D/mcp.py"
read_src companions/core-api/seed-tools.py  > "$D/seed.py"
for f in mcp.py seed.py; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# The body of one top-level function, up to the next top-level statement.
body_of() {
    awk -v name="$2" '
        $0 ~ "^(async )?def " name "\\(" { on = 1; print; next }
        on && /^[^ \t#]/ { exit }
        on { print }' "$1"
}

# ------------------------------------------------------------- 1. the tools
if grep -B1 -E '^async def set_secret\(ref: str, value: str\)' "$D/mcp.py" | grep -q '@mcp.tool()' \
   && grep -B1 -E '^async def grant_secret_ref\(agent: str, ref: str\)' "$D/mcp.py" | grep -q '@mcp.tool()'; then
    ok "set_secret and grant_secret_ref are MCP tools"
else
    bad "set_secret and grant_secret_ref are MCP tools"
fi

# --------------------------------------------------------------- 2. routes
SET=$(body_of "$D/mcp.py" set_secret)
GRANT=$(body_of "$D/mcp.py" grant_secret_ref)
if grep -q '_call("POST", ""' <<<"$SET" \
   && grep -q '"PUT", f"{AGENTS_PATH}/{slug}/grants"' <<<"$GRANT" \
   && grep -q 'params={"merge": "true"}' <<<"$GRANT"; then
    ok "set is a POST on the store, grant a merge PUT on the agent's grants"
else
    bad "set is a POST on the store, grant a merge PUT on the agent's grants"
fi

# ------------------------------------------------------ 3. no policy here
# No branch in either tool asks who the caller is (the docstrings may say it).
if [[ -n "$SET$GRANT" ]] && ! grep -qiE '^ +(if|elif|assert) .*(_me\(\)|capabilit|sysadmin|configurator|SN_AGENT_SLUG)' <<<"$SET$GRANT"; then
    ok "the MCP tools decide nothing: core-api admits or refuses"
else
    bad "the MCP tools decide nothing: core-api admits or refuses"
fi

# ------------------------------------------------------------ 4. __main__
if grep -q 'inspect.isawaitable(_r)' "$D/mcp.py" && ! grep -q 'asyncio.run(mcp.run())' "$D/mcp.py"; then
    ok "__main__ exits cleanly when run() is synchronous"
else
    bad "__main__ exits cleanly when run() is synchronous"
fi

# ------------------------------------------------------------ 5. contract
if grep -q '`set_secret`' "$D/seed.py" && grep -q '`grant_secret_ref`' "$D/seed.py" \
   && grep -q 'The Configurator and the "' "$D/seed.py" && grep -q '"sysadmin store credentials' "$D/seed.py"; then
    ok "the seeded secrets contract names the write tools and their users"
else
    bad "the seeded secrets contract names the write tools and their users"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
