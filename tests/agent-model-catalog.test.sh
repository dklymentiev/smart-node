#!/usr/bin/env bash
#
# The models an agent may be given are the enabled chat rows of sn_config.models,
# and nothing else states them.
#
# WHAT WENT WRONG. The set was stated twice: the catalog sn_config.models, and a
# CHECK constraint agents_model_check that enumerated model ids (plus the same
# list again as a pydantic Literal in core-api). The two drifted apart in both
# directions. On example-node the catalog had gemini-flash-latest enabled and the
# CHECK refused it, so no agent could be set to the one non-Claude model the node
# had a key for; the CHECK allowed ids the catalog never had. Adding a model meant
# an ALTER TABLE.
#
# WHAT IS CHECKED. Behaviour, against a throwaway postgres cluster built by this
# script from the real B02 files (schema + seed). Nothing on the host is touched.
#
#   fresh install
#     new-row        a catalog row added with an INSERT is accepted as an agent
#                    model, on INSERT and on UPDATE, with no schema change
#     seeded         the seeded models (sonnet/opus/haiku) are accepted
#     unknown        a model with no catalog row is refused, SQLSTATE 23514,
#                    and the message names the catalog
#     disabled       a disabled catalog row is refused
#     non-chat       an enabled row of another modality is refused (the panel
#                    lists chat rows only; the two sets must be equal)
#     none           'none' (no LLM) is accepted without a row
#     no-enum        no constraint on sn_config.agents enumerates model ids
#     reapply        applying the schema twice is clean and leaves one trigger
#   update from the previous release ($OLD_REF, default v1.12.41);
#                    explicitly skipped when that ref is unavailable
#     untouched      agents set before the update keep their model, version and
#                    updated_at, including one on an id the catalog never had
#     other-field    such an agent can still be saved from the panel with its
#                    model unchanged (Save always sends the model)
#     check-gone     agents_model_check is dropped on the updated node
#   core-api
#     no-literal     schemas/agent.py does not enumerate model ids again
#     422            routes/agents.py translates the catalog refusal (it would
#                    otherwise surface as a 500)
#
# USAGE
#   tests/agent-model-catalog.test.sh                 # working tree, expect PASS
#   tests/agent-model-catalog.test.sh --baseline REF  # git:REF, expect FAIL
#
# Needs postgres server binaries (initdb, pg_ctl). PG_BIN overrides the lookup.
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

src() {  # src <ref|""> <path> <dest>
    if [[ -n "$1" ]]; then
        git -C "$ROOT" show "$1:$2" > "$3" 2>/dev/null \
            || { echo "cannot read $2 at $1" >&2; exit 2; }
    else
        cp "$ROOT/$2" "$3"
    fi
}

src "$BASELINE" bootstrap/B02-schema.sql             "$WORK/core.sql"
src "$BASELINE" bootstrap/B02-sn-core-schema.sql     "$WORK/schema.sql"
src "$BASELINE" bootstrap/B02-sn-core-seed.sql       "$WORK/seed.sql"
src "$BASELINE" companions/core-api/schemas/agent.py "$WORK/agent_schema.py"
src "$BASELINE" companions/core-api/routes/agents.py "$WORK/agents_route.py"
OLD_SKIP_REASON=""
for old_path in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    if ! git -C "$ROOT" cat-file -e "$OLD_REF:$old_path" 2>/dev/null; then
        OLD_SKIP_REASON="cannot read $old_path at $OLD_REF"
        break
    fi
done
if [[ -z "$OLD_SKIP_REASON" ]]; then
    src "$OLD_REF" bootstrap/B02-schema.sql         "$WORK/old-core.sql"
    src "$OLD_REF" bootstrap/B02-sn-core-schema.sql "$WORK/old-schema.sql"
    src "$OLD_REF" bootstrap/B02-sn-core-seed.sql   "$WORK/old-seed.sql"
fi

echo "agent model = catalog row (${BASELINE:+git:}${BASELINE:-working tree}; update from $OLD_REF)"

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

# Files in the order B02-persistence.sh applies them: kernel, sn_core/sn_config, seed.
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
# try <db> <sql>: prints the SQLSTATE of the failure, or "ok"
try() {
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

# Agent models are <provider>/<model> of a catalog row now, and a row
# states its vendor (tests/agent-model-runtime.test.sh covers both).
q fresh "INSERT INTO sn_config.models (provider, model, label, modality, enabled, vendor)
         VALUES ('claude-cli', 'claude-haiku-4-5', 'Haiku 4.5', 'chat', true, 'anthropic'),
                ('gemini', 'gemini-flash-latest', 'Gemini Flash', 'chat', true, 'google'),
                ('claude-cli', 'retired-model', 'Retired', 'chat', false, 'anthropic'),
                ('openrouter', 'some/image-model', 'Image', 'image', true, 'some')
         ON CONFLICT (provider, model) DO NOTHING" >/dev/null  # gemini is seeded now

r="$(try fresh "$(agent a-new claude-cli/claude-haiku-4-5)")"
[[ "$r" == ok ]] && ok "new-row: agent created on a model added as a catalog row" \
                 || bad "new-row: agent on a new catalog row refused ($r)"

q fresh "$(agent a-upd claude-cli/sonnet)" >/dev/null
r="$(try fresh "UPDATE sn_config.agents SET model='gemini/gemini-flash-latest' WHERE slug='a-upd'")"
[[ "$r" == ok ]] && ok "new-row: agent moved to an enabled catalog row (gemini-flash-latest)" \
                 || bad "new-row: agent move to gemini-flash-latest refused ($r)"

seeded_ok=1
for m in sonnet opus haiku; do
    [[ "$(try fresh "$(agent "a-$m" "claude-cli/$m")")" == ok ]] || seeded_ok=0
done
((seeded_ok)) && ok "seeded: sonnet, opus, haiku accepted" \
              || bad "seeded: a seeded catalog model was refused"

r="$(try fresh "$(agent a-unknown claude-cli/gpt-9-imaginary)")"
msg="$(q fresh "$(agent a-unknown2 claude-cli/gpt-9-imaginary)")"
if [[ "$r" == 23514 && "$msg" == *sn_config.models* ]]; then
    ok "unknown: a model with no catalog row refused (23514, names the catalog)"
else
    bad "unknown: model with no catalog row -> '$r' / ${msg:0:120}"
fi

r="$(try fresh "$(agent a-retired claude-cli/retired-model)")"
[[ "$r" == 23514 ]] && ok "disabled: a disabled catalog row refused" \
                    || bad "disabled: disabled catalog row -> '$r'"

r="$(try fresh "$(agent a-image openrouter/some/image-model)")"
[[ "$r" == 23514 ]] && ok "non-chat: an image-modality row refused" \
                    || bad "non-chat: image-modality row -> '$r'"

r="$(try fresh "$(agent a-none none)")"
[[ "$r" == ok ]] && ok "none: 'none' (no LLM) accepted" || bad "none: 'none' refused ($r)"

enum="$(q fresh "SELECT conname FROM pg_constraint
                  WHERE conrelid='sn_config.agents'::regclass AND contype='c'
                    AND pg_get_constraintdef(oid) ~ '''sonnet'''")"
[[ -z "$enum" ]] && ok "no-enum: no CHECK on sn_config.agents enumerates model ids" \
                 || bad "no-enum: $enum still enumerates model ids"

if apply fresh "$WORK/reapply" "$WORK/core.sql" "$WORK/schema.sql" "$WORK/seed.sql"; then
    n="$(q fresh "SELECT count(*) FROM pg_trigger
                   WHERE tgrelid='sn_config.agents'::regclass AND tgname='agents_model_in_catalog'")"
    [[ "$n" == 1 ]] && ok "reapply: schema re-applied cleanly, one catalog trigger" \
                    || bad "reapply: catalog trigger count after re-apply = $n"
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
    q upd "$(agent u-sonnet sonnet); $(agent u-opus opus);
           $(agent u-legacy anthropic/claude-sonnet-4.5)" >/dev/null
    # sonnet/opus are rewritten to claude-cli/... by the runtime resolver (checked there); what
    # must hold here is that an id the catalog never had is left as it is, and
    # that no agent's version or updated_at moves.
    snap="SELECT slug, CASE WHEN slug = 'u-legacy' THEN model END, version, updated_at
            FROM sn_config.agents ORDER BY slug"
    before="$(q upd "$snap")"
    if apply upd "$WORK/upd.new" "$WORK/core.sql" "$WORK/schema.sql" "$WORK/seed.sql"; then
        after="$(q upd "$snap")"
        [[ "$before" == "$after" && -n "$before" ]] \
            && ok "untouched: a legacy id keeps its model; no version or updated_at moves" \
            || bad "untouched: agent rows changed by the update"
        # The panel's Save always sends the model with the other fields, so a title
        # edit reaches the database as SET title=..., model=<unchanged>. A bare
        # SET title would not fire an UPDATE OF model trigger at all.
        r="$(try upd "UPDATE sn_config.agents SET title='renamed', model='anthropic/claude-sonnet-4.5'
                       WHERE slug='u-legacy'")"
        [[ "$r" == ok ]] && ok "other-field: agent on a legacy id still editable on its title" \
                         || bad "other-field: title edit of a legacy-model agent refused ($r)"
        n="$(q upd "SELECT count(*) FROM pg_constraint WHERE conname='agents_model_check'")"
        [[ "$n" == 0 ]] && ok "check-gone: agents_model_check dropped on the updated node" \
                        || bad "check-gone: agents_model_check still present after update"
    else
        bad "new B02 applies over $OLD_REF"; tail -5 "$WORK/upd.new"
    fi
fi

# ---------------------------------------------------------------- core-api
echo " core-api"
if grep -Eq '^Model *= *Literal\[' "$WORK/agent_schema.py"; then
    bad "no-literal: schemas/agent.py enumerates model ids in a Literal"
else
    ok "no-literal: schemas/agent.py does not enumerate model ids"
fi
if grep -q 'agents_model_in_catalog' "$WORK/agents_route.py" \
   && grep -q 'HTTPException(422' "$WORK/agents_route.py"; then
    ok "422: routes/agents.py translates the catalog refusal"
else
    bad "422: routes/agents.py does not translate the catalog refusal"
fi

echo
if ((FAILED)); then echo "FAILED: $FAILED"; exit 1; fi
echo "ALL PASS"
