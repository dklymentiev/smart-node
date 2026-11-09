#!/usr/bin/env bash
#
# A person's history is read AS somebody, and an agent reads only its part of it
#
# WHAT WENT WRONG. GET /api/messages/by-contact and POST /api/messages/search
# took no principal at all. Any caller named any person and received their whole
# conversation across every channel, bodies included. brin folds by-contact into
# every turn with a known person and voice into every call, so each agent was
# handed the owner's conversations with its colleagues; and messages_search is a
# catalog tool granted to every agent. an earlier field report scoped the list and left
# these two open, saying so.
#
# WHY IT WAS NOT A ONE-LINE Depends. The three callers named nobody provably:
# brin's catalog dispatcher sent a bare Remote-User over TCP, which core-api
# refuses for an agent name, and brin's ledger and voice sent no
# headers. All three read a refusal as "no history", so a bare Depends would
# have been a silent outage. They now carry the earlier change service proof acting for
# the agent, and the catalog dispatcher sends it ONLY to core-api tools.
#
# THE PROPERTIES:
#   1. both routes take a principal and scope by the list's rule
#   2. /search reuses _party_clause with named placeholders: one rule, three reads
#   3. the tools view exposes integration_slug; the catalog dispatcher stamps the
#      service proof on core-api rows only, never on a third-party endpoint
#   4. brin's ledger and voice ask as the agent of the turn or call
#
#   tests/person-history-scoped-to-party.test.sh                  # tree, PASS
#   tests/person-history-scoped-to-party.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "person history scoped to its party (git:$BASELINE)"
else echo "person history scoped to its party (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py        > "$D/messages.py"
read_src companions/core-api/routes/agents.py          > "$D/agents.py"
read_src companions/brin/core/catalog_tools.py         > "$D/catalog_tools.py"
read_src companions/brin/services/ledger_service.py    > "$D/ledger_service.py"
read_src companions/voice/backend/main.py              > "$D/voice.py"
for f in messages agents catalog_tools ledger_service voice; do
    [[ -s "$D/$f.py" ]] || { bad "sources are readable ($f.py)"; echo; echo "FAIL"; exit 1; }
done

# Body of a top-level function: from its def to the next top-level def, class
# or decorator. A plain awk range /^def x(/,/^def / closes on its own first
# line, which is why this is a flag.
body_of() {  # file, name
    awk -v n="$2" 'f && /^(def|async def|class|@)/{exit} $0 ~ "^(async )?def "n"\\("{f=1} f' "$1"
}

# ------------------------------------------------------------ 1+2. the routes
for route in messages_by_contact messages_search; do
    b=$(body_of "$D/messages.py" "$route")
    if grep -q 'principal: Principal = Depends(requires("messages.read"))' <<<"$b"; then
        ok "$route takes a principal"
    else
        bad "$route takes a principal -- it answers anyone about anyone"
    fi
    if grep -q 'if _must_scope(principal, False):' <<<"$b"; then
        ok "$route scopes by the list's rule"
    else
        bad "$route scopes by the list's rule"
    fi
done
if grep -q '_party_clause(principal.username, "%(me)s")' <<<"$(body_of "$D/messages.py" messages_search)"; then
    ok "/search reuses the party rule with named placeholders"
else
    bad "/search reuses the party rule with named placeholders"
fi

# --------------------------------------------------- 3. tools view + dispatch
# Matched on the SELECT list: the JOIN below it has always named the column.
if grep -q 'i.endpoint, t.integration_slug' <<<"$(body_of "$D/agents.py" get_agent_tools)"; then
    ok "the tools view says which integration a tool belongs to"
else
    bad "the tools view says which integration a tool belongs to"
fi
if grep -q 'headers=_api_headers(r, self.agent_slug),' "$D/catalog_tools.py"; then
    ok "the catalog dispatcher builds its headers per row"
else
    bad "the catalog dispatcher builds its headers per row"
fi

# ------------------------------------------------------------ 4. the callers
# The second grep asks WHO the ledger reads as, so it stops at the agent and
# ignores what follows: the call now accepts a thread argument and a match
# on the closing paren failed on formatting while the property it guards was
# untouched. A property test that breaks on an added argument teaches people
# to pass the gate rather than to keep the property.
if grep -q 'headers=_agent_headers(agent_slug),' <<<"$(body_of "$D/ledger_service.py" fetch_person_history)" \
   && grep -q 'await fetch_person_history(person_identity, agent_slug' "$D/ledger_service.py"; then
    ok "brin's ledger asks as the agent of the turn"
else
    bad "brin's ledger asks as the agent of the turn"
fi
if grep -q 'headers=_svc_headers(agent))' <<<"$(body_of "$D/voice.py" fetch_crosschannel_context)" \
   && grep -q 'or uid, brin_agent)' "$D/voice.py"; then
    ok "voice asks as the agent on the call"
else
    bad "voice asks as the agent on the call"
fi

# --------------------------------------------------------- the pure pieces
# Capture the helper's status explicitly. A process-substitution producer runs
# asynchronously, so its non-zero status is otherwise invisible to this shell.
pure_output="$(python3 - "$D/messages.py" "$D/catalog_tools.py" <<'PY'
import ast, sys

def load(path, names, consts=(), ns=None):
    tree = ast.parse(open(path).read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in consts for t in n.targets))]
    ns = dict(ns or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), ns)
    return ns

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

pc = load(sys.argv[1], ("_party_clause",)).get("_party_clause")
if pc is None:
    check("one party rule exists for every read", False)
else:
    try:
        named, _ = pc("alex", "%(me)s")
        check("the named form carries no positional placeholder",
              "%s" not in named and named.count("%(me)s") == 4)
    except TypeError:
        check("the party rule takes a placeholder style", False)
    pos, params = pc("alex")
    check("the positional form is unchanged for the list", pos.count("%s") == 4 == len(params))

c = load(sys.argv[2], ("_api_headers", "_is_core_api"), ("SERVICE_NAME", "_CORE_API_INTEGRATION"),
         {"SERVICE_SECRET": "s3cret"})
h = c.get("_api_headers")
if h is None:
    check("the catalog dispatcher has a per-row header rule", False)
    sys.exit(0)
core = h({"integration_slug": "core-api"}, "alex")
check("a core-api tool carries the service proof for the agent",
      core.get("X-SN-Service-Auth") == "s3cret" and core.get("X-SN-On-Behalf-Of") == "alex"
      and core.get("Remote-User") == "alex")
other = h({"integration_slug": "dataforseo"}, "alex")
check("a third-party tool never receives the node's secret",
      "X-SN-Service-Auth" not in other and "X-SN-On-Behalf-Of" not in other)
check("a row that names no integration never receives it either",
      "X-SN-Service-Auth" not in h({}, "alex"))
h.__globals__["SERVICE_SECRET"] = ""
check("a node without the secret sends what it sent before",
      h({"integration_slug": "core-api"}, "alex") == {"Remote-User": "alex"})
PY
)"
pure_status=$?
if (( pure_status != 0 )); then
    printf '%s\n' "$pure_output" | sed 's/^/  python: /'
    bad "pure-piece harness executes without an exception"
else
    while IFS= read -r line; do
        case "$line" in
            ok*)   ok "${line#ok }";;
            FAIL*) bad "${line#FAIL }";;
        esac
    done <<<"$pure_output"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
