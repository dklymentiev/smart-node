#!/usr/bin/env bash
#
# An agent's model is always stated as <runtime>/<model>, and the vendor is a
# column of the catalog, not a segment of the id.
#
# WHAT WENT WRONG. A model id without a slash silently meant "the Claude CLI".
# That implicit rule decided which runtime ran the turn (gateway) and whether
# brin ran its own tool loop at all (brin: no slash = opaque CLI, skip the loop;
# any slash = orchestrated, send it to openrouter). The runtime axis
# ("the mode switch is the agent's model field") existed everywhere in code and
# was expressible nowhere in configuration: an agent could not be put on another
# runtime, and "what did we spend on OpenAI models" could only be answered by
# parsing model ids.
#
# WHAT IS CHECKED. Behaviour, against a throwaway postgres cluster built by this
# script from the real B02 files (schema + seed), and the real gateway routing
# function imported with python. Nothing on the host is touched.
#
#   fresh install
#     canonical      claude-cli/sonnet (a seeded row as <provider>/<model>) is
#                    accepted as an agent model; the bare 'sonnet' is refused
#     slashes        a runtime model id that itself holds slashes
#                    (openrouter/openai/gpt-5) is accepted from its catalog row
#     default        an agent inserted without a model gets claude-cli/sonnet
#     vendor         every catalog row has a vendor, the column is NOT NULL,
#                    and seeded claude-cli rows are anthropic
#     spend          spend per vendor is one GROUP BY over ai_sessions.vendor:
#                    a session is stamped with its catalog vendor on insert,
#                    whether the writer stored the canonical id (session
#                    service) or provider + runtime model (stateless chat)
#     reapply        applying the schema twice is clean
#   update from the previous release ($OLD_REF, default v1.12.41);
#                    explicitly skipped when that ref is unavailable
#     migrated       agents on sonnet/opus become claude-cli/sonnet, claude-cli/opus
#     kept           'none' and an id no catalog row names are left as they are;
#                    version and updated_at of every agent are unchanged
#     vendor-backfill  the existing gemini row gets vendor google
#   gateway (companions/gateway/model_route.py)
#     route          claude-cli/sonnet -> cli + sonnet; openrouter/openai/gpt-5
#                    -> openrouter + openai/gpt-5 (rest never parsed); a runtime
#                    the gateway has no provider for is refused, not guessed
#     legacy         bare 'sonnet' -> cli; 'anthropic/x' -> openrouter as-is
#                    (one release)
#   brin
#     mode-from-data brin's tool-loop decision reads the runtime kind that
#                    core-api derives from sn_config.cli_providers, and nothing in
#                    executor.py decides the mode by looking for a slash
#   core-api
#     runtime-kind   GET /api/agents/{slug} carries runtime + runtime_kind,
#                    derived from sn_config.cli_providers
#     legacy-alias   a bare model sent to create/update is rewritten to the one
#                    catalog row it names, with an event
#   surfaces
#     no-bare        no shipped default, seed or agent-facing prompt writes a bare
#                    sonnet/opus/haiku as an agent model
#
# USAGE
#   tests/agent-model-runtime.test.sh                 # working tree, expect PASS
#   tests/agent-model-runtime.test.sh --baseline REF  # git:REF, expect FAIL
#
# Needs postgres server binaries (initdb, pg_ctl) and python3. PG_BIN overrides.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
OLD_REF="${OLD_REF:-v1.12.41}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
if [[ ! -x "$PG_BIN/initdb" ]]; then
    echo "cannot run: no initdb (set PG_BIN)" >&2
    exit 2
fi

WORK="$(mktemp -d)"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# src <ref|""> <path> <dest>: required inputs must be present and readable.
src() {
    if [[ -n "$1" ]]; then
        git -C "$ROOT" show "$1:$2" > "$3" 2>/dev/null \
            || { echo "cannot read $2 at $1" >&2; exit 2; }
    else
        cp "$ROOT/$2" "$3" \
            || { echo "cannot read $2 from working tree" >&2; exit 2; }
    fi
}

src "$BASELINE" bootstrap/B02-schema.sql                 "$WORK/core.sql"
src "$BASELINE" bootstrap/B02-sn-core-schema.sql         "$WORK/schema.sql"
src "$BASELINE" bootstrap/B02-sn-core-seed.sql           "$WORK/seed.sql"
src "$BASELINE" companions/gateway/model_route.py        "$WORK/model_route.py"
src "$BASELINE" companions/brin/core/executor.py         "$WORK/executor.py"
src "$BASELINE" companions/core-api/routes/agents.py     "$WORK/agents_route.py"
src "$BASELINE" companions/core-api/schemas/agent.py     "$WORK/agent_schema.py"
src "$BASELINE" companions/core-api/lib/agent_fs.py      "$WORK/agent_fs.py"
src "$BASELINE" bootstrap/onboarding-seed.sh             "$WORK/onboarding-seed.sh"
src "$BASELINE" companions/tracking.sh                   "$WORK/tracking.sh"
src "$BASELINE" bootstrap/smart-node-ui/src/assets/shell.js        "$WORK/shell.js"
src "$BASELINE" bootstrap/smart-node-ui/src/agents/new/index.html  "$WORK/new.html"
src "$BASELINE" bootstrap/smart-node-ui/src/agents/edit/index.html "$WORK/edit.html"
OLD_SKIP_REASON=""
for old_path in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    if ! git -C "$ROOT" cat-file -e "$OLD_REF:$old_path" 2>/dev/null; then
        OLD_SKIP_REASON="cannot read $old_path at $OLD_REF"
        break
    fi
done
if [[ -z "$OLD_SKIP_REASON" ]]; then
    # The update fixture must be complete; never pass an empty substitute to psql.
    for old_pair in 'bootstrap/B02-schema.sql:old-core.sql' \
                    'bootstrap/B02-sn-core-schema.sql:old-schema.sql' \
                    'bootstrap/B02-sn-core-seed.sql:old-seed.sql'; do
        old_path="${old_pair%%:*}"
        old_dest="${old_pair#*:}"
        git -C "$ROOT" show "$OLD_REF:$old_path" > "$WORK/$old_dest" \
            || { echo "cannot read $old_path at $OLD_REF" >&2; exit 2; }
    done
fi

echo "agent model = <runtime>/<model> (${BASELINE:+git:}${BASELINE:-working tree}; update from $OLD_REF)"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }

PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN" >/dev/null

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

apply() {  # apply <db> <log> <file>...
    local db="$1" log="$2" f; shift 2
    for f in "$@"; do
        "${PSQL[@]}" -d "$db" -f "$f" >>"$log" 2>&1 || return 1
    done
}
fresh_db() {  # fresh_db <name> <core> <schema> <seed>
    "${PSQL[@]}" -c "CREATE DATABASE $1 OWNER smartnode" >/dev/null
    apply "$1" "$WORK/$1.apply" "$2" "$3" "$4"
}
q()   { "${PSQL[@]}" -d "$1" -At -c "$2" 2>&1; }
try() {  # prints the SQLSTATE of the failure, or "ok"
    local out
    out="$("${PSQL[@]}" -d "$1" -At -v VERBOSITY=verbose -c "$2" 2>&1)" && { echo ok; return; }
    printf '%s\n' "$out" | sed -n 's/^ERROR: *\([0-9A-Z]\{5\}\):.*/\1/p' | head -1
}
agent() { echo "INSERT INTO sn_config.agents (slug, title, model) VALUES ('$1', '$1', '$2')"; }

# ---------------------------------------------------------------- fresh install
echo " fresh install"
if ! fresh_db fresh "$WORK/core.sql" "$WORK/schema.sql" "$WORK/seed.sql"; then
    bad "B02 schema+seed apply to an empty database"; tail -5 "$WORK/fresh.apply"
    echo "FAILED: $FAILED"; exit 1
fi

r="$(try fresh "$(agent a-canon claude-cli/sonnet)")"
b="$(try fresh "$(agent a-bare sonnet)")"
if [[ "$r" == ok && "$b" == 23514 ]]; then
    ok "canonical: claude-cli/sonnet accepted, bare 'sonnet' refused by the database"
else
    bad "canonical: claude-cli/sonnet -> '$r', bare sonnet -> '$b' (want ok / 23514)"
fi

# The vendor column may not exist on a baseline; the insert then fails and so
# does the check that needs it, which is the point.
q fresh "INSERT INTO sn_config.models (provider, model, label, modality, enabled, vendor)
         VALUES ('openrouter', 'openai/gpt-5', 'GPT-5 via OpenRouter', 'chat', true, 'openai')" >/dev/null
r="$(try fresh "$(agent a-slashes openrouter/openai/gpt-5)")"
[[ "$r" == ok ]] && ok "slashes: openrouter/openai/gpt-5 accepted from its catalog row" \
                 || bad "slashes: openrouter/openai/gpt-5 -> '$r'"

q fresh "INSERT INTO sn_config.agents (slug, title) VALUES ('a-default', 'a-default')" >/dev/null
d="$(q fresh "SELECT model FROM sn_config.agents WHERE slug='a-default'")"
[[ "$d" == claude-cli/sonnet ]] && ok "default: an agent without a model gets claude-cli/sonnet" \
                                || bad "default: agent without a model got '$d'"

nn="$(q fresh "SELECT is_nullable FROM information_schema.columns
                WHERE table_schema='sn_config' AND table_name='models' AND column_name='vendor'")"
nulls="$(q fresh "SELECT count(*) FROM sn_config.models WHERE vendor IS NULL")"
anth="$(q fresh "SELECT string_agg(DISTINCT vendor, ',') FROM sn_config.models WHERE provider='claude-cli'")"
if [[ "$nn" == NO && "$nulls" == 0 && "$anth" == anthropic ]]; then
    ok "vendor: NOT NULL column, every row set, claude-cli rows are anthropic"
else
    bad "vendor: nullable='$nn' null-rows='$nulls' claude-cli vendors='$anth'"
fi

# Two sessions, written the two ways the gateway writes them: the session service
# stores the canonical id; the stateless chat stores provider + runtime model.
q fresh "INSERT INTO sn_core.ai_sessions (tenant_id, type, model, provider, usage)
         VALUES (1, 'chat', 'claude-cli/sonnet', 'claude-cli', '{\"cost_usd\": 0.25}'),
                (1, 'chat', 'openai/gpt-5', 'openrouter', '{\"cost_usd\": 1.5}')" >/dev/null
spend="$(q fresh "SELECT string_agg(vendor || '=' || s, ',' ORDER BY vendor) FROM (
                    SELECT vendor, sum((usage->>'cost_usd')::numeric) s
                      FROM sn_core.ai_sessions GROUP BY vendor) x")"
[[ "$spend" == "anthropic=0.25,openai=1.5" ]] \
    && ok "spend: per-vendor spend is a GROUP BY over ai_sessions.vendor" \
    || bad "spend: per-vendor query gave '${spend:0:160}'"

if apply fresh "$WORK/reapply" "$WORK/core.sql" "$WORK/schema.sql" "$WORK/seed.sql"; then
    ok "reapply: schema re-applied cleanly"
else
    bad "reapply: schema failed on second apply"; tail -3 "$WORK/reapply"
fi

# ---------------------------------------------------------------- update path
echo " update from $OLD_REF"
if [[ -n "$OLD_SKIP_REASON" ]]; then
    echo "  SKIPPED update from $OLD_REF: $OLD_SKIP_REASON"
elif ! fresh_db upd "$WORK/old-core.sql" "$WORK/old-schema.sql" "$WORK/old-seed.sql"; then
    bad "old B02 ($OLD_REF) applies"; tail -5 "$WORK/upd.apply"
else
    q upd "INSERT INTO sn_config.models (provider, model, label, modality, enabled)
           VALUES ('gemini', 'gemini-flash-latest', 'Gemini Flash', 'chat', true)" >/dev/null
    q upd "$(agent u-sonnet sonnet); $(agent u-opus opus); $(agent u-none none);
           $(agent u-legacy anthropic/claude-sonnet-4.5)" >/dev/null
    before="$(q upd "SELECT slug, version, updated_at FROM sn_config.agents ORDER BY slug")"
    if apply upd "$WORK/upd.new" "$WORK/core.sql" "$WORK/schema.sql" "$WORK/seed.sql"; then
        got="$(q upd "SELECT string_agg(slug || '=' || model, ',' ORDER BY slug) FROM sn_config.agents")"
        want="u-legacy=anthropic/claude-sonnet-4.5,u-none=none,u-opus=claude-cli/opus,u-sonnet=claude-cli/sonnet"
        [[ "$got" == "$want" ]] && ok "migrated: sonnet/opus -> claude-cli/..., none and an uncatalogued id kept" \
                                || bad "migrated: agents after update = $got"
        after="$(q upd "SELECT slug, version, updated_at FROM sn_config.agents ORDER BY slug")"
        [[ "$before" == "$after" ]] && ok "kept: version and updated_at unchanged by the migration" \
                                    || bad "kept: the migration changed version or updated_at"
        v="$(q upd "SELECT vendor FROM sn_config.models WHERE provider='gemini'")"
        [[ "$v" == google ]] && ok "vendor-backfill: the existing gemini row is google" \
                             || bad "vendor-backfill: gemini row vendor = '$v'"
    else
        bad "new B02 applies over $OLD_REF"; tail -5 "$WORK/upd.new"
    fi
fi

# ---------------------------------------------------------------- gateway
echo " gateway"
route="$(cd "$WORK" && python3 - <<'PY' 2>&1
from model_route import route_model, UnknownRuntime
known = {"cli", "openrouter", "gemini"}
def r(m):
    try:
        return "%s|%s" % route_model(m, known)
    except UnknownRuntime:
        return "refused"
for m in ["claude-cli/sonnet", "openrouter/openai/gpt-5", "codex-cli/astra",
          "sonnet", "anthropic/claude-sonnet-4.5"]:
    print(m, r(m))
PY
)"
want_route="claude-cli/sonnet cli|sonnet
openrouter/openai/gpt-5 openrouter|openai/gpt-5
codex-cli/astra refused"
[[ "$(printf '%s\n' "$route" | head -3)" == "$want_route" ]] \
    && ok "route: runtime from the first segment, rest untouched, unknown runtime refused" \
    || bad "route: got: $(printf '%s' "$route" | head -3 | tr '\n' ';')"
want_legacy="sonnet cli|sonnet
anthropic/claude-sonnet-4.5 openrouter|anthropic/claude-sonnet-4.5"
[[ "$(printf '%s\n' "$route" | sed -n 4,5p)" == "$want_legacy" ]] \
    && ok "legacy: bare name -> cli, vendor-prefixed -> openrouter (one release)" \
    || bad "legacy: got: $(printf '%s' "$route" | sed -n 4,5p | tr '\n' ';')"

# ---------------------------------------------------------------- brin
echo " brin"
if grep -Eq '"/" (not )?in model' "$WORK/executor.py"; then
    bad "mode-from-data: executor.py still decides the mode by looking for a slash"
elif grep -q 'runtime_kind' "$WORK/executor.py" \
     && ! grep -q '"provider": "openrouter"' "$WORK/executor.py"; then
    ok "mode-from-data: brin reads runtime_kind and names no runtime in the tool loop"
else
    bad "mode-from-data: executor.py does not read runtime_kind, or still pins openrouter"
fi

# ---------------------------------------------------------------- core-api
echo " core-api"
if grep -q 'cli_providers' "$WORK/agents_route.py" && grep -q 'runtime_kind' "$WORK/agent_schema.py"; then
    ok "runtime-kind: agent read carries runtime_kind derived from cli_providers"
else
    bad "runtime-kind: agent read has no runtime_kind from cli_providers"
fi
if grep -q 'agent.model_legacy_alias' "$WORK/agents_route.py"; then
    ok "legacy-alias: a bare model is rewritten to its catalog row, with an event"
else
    bad "legacy-alias: no rewrite of a bare model in routes/agents.py"
fi

# ---------------------------------------------------------------- surfaces
echo " surfaces"
bare=""
grep -Eq '^\s*model: Model = "(sonnet|opus|haiku)"' "$WORK/agent_schema.py" && bare+=" schemas/agent.py"
grep -Eq 'model[^=]*=\s*"(sonnet|opus|haiku)"' "$WORK/agent_fs.py" && bare+=" agent_fs.py"
grep -Eq '"model": *"(sonnet|opus|haiku)"' "$WORK/onboarding-seed.sh" && bare+=" onboarding-seed.sh"
grep -Eq '\\?"model\\?": *\\?"(sonnet|opus|haiku)' "$WORK/tracking.sh" && bare+=" tracking.sh"
grep -Eq "\|\| *'(sonnet|opus)'|\? *'opus' *: *'sonnet'" "$WORK/shell.js" && bare+=" shell.js"
grep -Eq 'option value="(sonnet|opus|haiku)"' "$WORK/new.html" "$WORK/edit.html" && bare+=" agents/{new,edit}"
[[ -z "$bare" ]] && ok "no-bare: no shipped default or seed writes a bare model name" \
                 || bad "no-bare: bare model names still written by:$bare"

echo
if ((FAILED)); then echo "FAILED: $FAILED"; exit 1; fi
echo "ALL PASS"
