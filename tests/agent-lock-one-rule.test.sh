#!/usr/bin/env bash
#
# One turn per agent, and one implementation of the rule.
#
# WHAT WENT WRONG. Two services decide to run an agent: the Runner
# wakes one that has pending work, and the communication dispatcher answers a
# chat message on a fast channel. The Runner claimed the node's agent lock
# through core-api and skipped when it was refused. The dispatcher never did --
# it talks to brin and had no reason to know sn_core.agent_runtime existed.
#
# So the two paths ran the same agent at the same time. Seen live on the field
# node: the owner asked in telegram how much RAM the
# node had while the wake carrying that answer was already running, and got the
# figures twice, in two languages, with "I have no direct access to system
# metrics" in between -- said by the turn that did not know the other one was at
# that moment carrying exactly that access.
#
# THE PROPERTIES:
#
#   1. the rule exists once, as sn_core.claim_agent / sn_core.release_agent
#   2. core-api CALLS it and no longer carries its own copy -- no INSERT INTO
#      agent_runtime and no UPDATE ... status='free' left in the route
#   3. the dispatcher calls it too, which is the caller that was missing
#   4. a refused claim puts the message back to 'new' rather than failing it:
#      the next tick retries, ordering holds, nothing is lost
#   5. the lock is released in a `finally`, so a turn that raises still gives
#      it back
#   6. both functions are SECURITY DEFINER with a pinned search_path -- the
#      dispatcher's role must never need UPDATE on the runtime table
#   7. the dispatcher's role is granted EXECUTE and nothing more
#
# Property 2 is the one worth a test on its own. A rule that decides whether two
# things may run at once, implemented twice, is the defect -- not a symptom of it.
#
#   tests/agent-lock-one-rule.test.sh                  # working tree, PASS
#   tests/agent-lock-one-rule.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent lock, one rule (git:$BASELINE)"
else echo "agent lock, one rule (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src bootstrap/B02-sn-core-schema.sql     > "$D/schema.sql"
read_src companions/core-api/routes/agents.py > "$D/agents.py"
read_src companions/communication/dispatch.py > "$D/dispatch.py"
read_src companions/communication.sh          > "$D/communication.sh"
for f in schema.sql agents.py dispatch.py communication.sh; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# ------------------------------------------------------------------------ 1
for fn in claim_agent release_agent; do
    if grep -q "CREATE OR REPLACE FUNCTION sn_core.$fn" "$D/schema.sql"; then
        ok "sn_core.$fn is defined in the baked schema"
    else
        bad "sn_core.$fn is defined in the baked schema"
    fi
done

# ------------------------------------------------------------------------ 2
route=$(awk '/^def agent_runtime_action/,/^@router/' "$D/agents.py")
if grep -q 'sn_core.claim_agent' <<< "$route" && grep -q 'sn_core.release_agent' <<< "$route"; then
    ok "the core-api route calls the functions"
else
    bad "the core-api route calls the functions"
fi
if grep -q 'INSERT INTO sn_core.agent_runtime (agent_slug,status,current_ref' <<< "$route"; then
    bad "the core-api route still carries its own copy of the claim"
else
    ok "the core-api route carries no second copy of the claim"
fi
if grep -q "UPDATE sn_core.agent_runtime SET status='free'" <<< "$route"; then
    bad "the core-api route still carries its own copy of the release"
else
    ok "the core-api route carries no second copy of the release"
fi

# ------------------------------------------------------------------------ 3
if grep -q 'SELECT sn_core.claim_agent' "$D/dispatch.py"; then
    ok "the dispatcher takes the lock -- the caller that was missing"
else
    bad "the dispatcher takes the lock -- the caller that was missing"
fi

# ------------------------------------------------------------------------ 4
# A turn reads every message of one conversation together, so the claim
# is taken once for the batch, under the newest message ref, and a refusal
# puts every message of the batch back.
if grep -q 'ref = "dispatch:%s" % last.message_id' "$D/dispatch.py" \
   && grep -q 'if not _claim_agent(agent, ref):' "$D/dispatch.py" \
   && grep -q '_set_state(i, "new")' <(sed -n '/if not _claim_agent(agent, ref)/,+3p' "$D/dispatch.py"); then
    ok "a refused claim puts the message back to new, not to failed"
else
    bad "a refused claim puts the message back to new, not to failed"
fi

# ------------------------------------------------------------------------ 5
# Not "awk | grep -q": with pipefail, grep -q leaving at its first match can SIGPIPE awk, and the
# pipeline then reads as failed or passed by timing. Read the section whole, then look in it.
section="$(awk '/if not _claim_agent/,/_set_state\(message_id, "processed"\)/' "$D/dispatch.py")"
if grep -qE '^\s*finally:\s*$' <<< "$section"; then
    ok "the lock is released in a finally -- a raising turn still gives it back"
else
    bad "the lock is released in a finally -- a raising turn still gives it back"
fi

# ------------------------------------------------------------------------ 6
# Every sn_core function in this file, not just these two: a definer function
# with an unpinned search_path is a way in, and the count is what keeps the
# next one honest.
fns=$(grep -c 'CREATE OR REPLACE FUNCTION sn_core\.' "$D/schema.sql")
sec=$(awk '
    /^CREATE OR REPLACE FUNCTION / {
        core = ($0 ~ /^CREATE OR REPLACE FUNCTION sn_core\./)
    }
    core && /LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, sn_core, sn_config, public/ {
        count++; core = 0
    }
    END { print count + 0 }
' "$D/schema.sql")
if [[ "$fns" -ge 2 && "$sec" == "$fns" ]]; then
    ok "all $fns sn_core functions are SECURITY DEFINER with a pinned search_path"
else
    bad "all sn_core functions are SECURITY DEFINER with a pinned search_path ($sec of $fns)"
fi

# ------------------------------------------------------------------------ 7
if grep -q 'GRANT EXECUTE ON FUNCTION sn_core.claim_agent' "$D/communication.sh" \
   && grep -q 'GRANT EXECUTE ON FUNCTION sn_core.release_agent' "$D/communication.sh"; then
    ok "the dispatcher role is granted EXECUTE"
else
    bad "the dispatcher role is granted EXECUTE"
fi
if grep -qE 'GRANT [^;]*UPDATE[^;]* ON TABLE sn_core.agent_runtime' "$D/communication.sh"; then
    bad "the dispatcher role was given UPDATE on the runtime table -- the lock is not worth that"
else
    ok "the dispatcher role has no write grant on the runtime table"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
