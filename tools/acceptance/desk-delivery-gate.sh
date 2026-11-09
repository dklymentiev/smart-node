#!/bin/bash
# Live checks on throwaway desks. Runs on the node as admin (sudo for agent identities).
set -u
API=http://core-api
SOCK=/run/smart-node/api/core-api.sock
PSQL="sudo -u postgres psql -d smartnode -At"
pass=0; fail=0
ok()  { echo "[OK]    $*"; pass=$((pass+1)); }
bad() { echo "[FAIL]  $*"; fail=$((fail+1)); }

as_agent() { # user, json -> response body
  sudo -u "$1" curl -s --unix-socket $SOCK -H "Content-Type: application/json" -X POST http://x/api/messages/send -d "$2"
}
as_agent_code() { # user, json -> "<http code> <body>"
  sudo -u "$1" curl -s -w '\n%{http_code}' --unix-socket $SOCK -H "Content-Type: application/json" -X POST http://x/api/messages/send -d "$2" \
    | python3 -c "import sys; b=sys.stdin.read().rsplit(chr(10),1); print(b[-1], b[0])"
}
rows_with() { $PSQL -c "select count(*) from sn_core.messages where content like '%$1%'"; }
status_of() { $PSQL -c "select status from sn_core.message_state where message_id=$1"; }
wait_decided() { for i in $(seq 1 15); do s=$(status_of $1); [ "$s" != new ] && [ "$s" != routed ] && { echo $s; return; }; sleep 1; done; echo "$s"; }
history_has() { sudo -n curl -s --unix-socket "$SOCK" "$API/api/desks/$1/history" | grep -c -- "$2"; }
close_msg() { $PSQL -c "update sn_core.message_state set status='processed', processed_at=now(), updated_at=now() where message_id=$1" >/dev/null; }

mk_desk() { sudo -n curl -s --unix-socket "$SOCK" -H "Content-Type: application/json" -X POST "$API/api/desks" -d "{\"title\":\"4854 gate test\",\"agent_slug\":\"$1\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"; }
DT=$(mk_desk agent-a)
echo "test desk (agent-a): $DT"
TAG="gate-test-$(date +%s)"

# a. another agent writes into agent-a's desk with nothing to answer: refused AT SEND
#   , the refusal names the reply path, no row is written, nothing shown.
R=$(as_agent_code agent-max "{\"to\":\"desk:$DT\",\"body\":\"$TAG a: max into agent-a desk\"}")
[ "${R%% *}" = 403 ] && ok "a. max -> agent-a desk refused at send (403)" || bad "a. max -> agent-a desk answered ${R:0:120}"
echo "$R" | grep -q inbox_reply && ok "a. the refusal names inbox_reply" || bad "a. refusal does not name the reply path"
[ "$(rows_with "$TAG a")" = 0 ] && ok "a. no row written" || bad "a. a row was written"
[ "$(history_has $DT "$TAG a")" = 0 ] && ok "a. not in the desk history" || bad "a. VISIBLE in the desk history"

# b. the desk's own agent writes into it: delivered and shown.
R=$(as_agent agent-agent-a "{\"to\":\"desk:$DT\",\"body\":\"$TAG b: agent-a into own desk\"}")
MB=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
S=$(wait_decided $MB)
[ "$S" = processed ] && ok "b. agent-a -> own desk delivered (msg $MB)" || bad "b. own desk status=$S"
[ "$(history_has $DT "$TAG b")" -ge 1 ] && ok "b. shown in the desk history" || bad "b. NOT shown in the desk history"

# c. unknown desk: refused at send, no row.
R=$(as_agent_code agent-agent-a "{\"to\":\"desk:00000000-0000-0000-0000-000000000000\",\"body\":\"$TAG c\"}")
[ "${R%% *}" = 403 ] && ok "c. unknown desk refused at send (403)" || bad "c. unknown desk answered ${R:0:120}"
[ "$(rows_with "$TAG c")" = 0 ] && ok "c. no row written" || bad "c. a row was written"

# d+e+g. agent-a asks max on behalf of her desk; max answers with a plain SEND (the old prompt's way).
R=$(as_agent agent-agent-a "{\"to\":\"max\",\"body\":\"$TAG q: test question, please ignore\",\"origin\":{\"channel\":\"internal\",\"conversation\":\"desk:$DT\"}}")
MQ=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
R=$(as_agent agent-max "{\"to\":\"agent-a\",\"body\":\"$TAG answer: test answer, please ignore\"}")
MAN=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
close_msg $MAN   # nobody needs to wake agent-a for a test answer
Q=$(status_of $MQ)
[ "$Q" = processed ] && ok "d. the question was closed by the answer (msg $MQ)" || bad "d. question status=$Q"
META=$($PSQL -c "select metadata->>'reply_to_message_id', metadata->'origin'->>'conversation', metadata->>'relayed_by_system' from sn_core.messages where id=$MAN")
[ "$META" = "$MQ|desk:$DT|true" ] && ok "e. send coerced to a reply with the origin ($META)" || bad "e. answer metadata=$META"
RL=$($PSQL -c "select id from sn_core.messages where metadata->>'source'='system-relay' and (metadata->>'relay_of')::int=$MAN")
if [ -n "$RL" ]; then
  S=$(wait_decided $RL)
  [ "$S" = processed ] && ok "g. system relay delivered into the asker's desk (msg $RL)" || bad "g. relay status=$S"
  [ "$(history_has $DT "Answer from max")" -ge 1 ] && ok "g. desk shows 'Answer from max'" || bad "g. relay NOT shown"
else
  bad "g. no system relay row"
fi

# h. The case that lost answers on example-node 2026-09-25: agent-a asks max from
#    her desk, max answers with a SEND TO THE DESK (origin.conversation), not to
#    agent-a. It must become the reply and reach the desk through the system relay.
R=$(as_agent agent-agent-a "{\"to\":\"max\",\"body\":\"$TAG hq: test question, please ignore\",\"origin\":{\"channel\":\"internal\",\"conversation\":\"desk:$DT\"}}")
MHQ=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
R=$(as_agent_code agent-max "{\"to\":\"desk:$DT\",\"body\":\"$TAG h: answer into the desk, please ignore\"}")
[ "${R%% *}" = 200 ] && ok "h. max -> agent-a desk with an open question accepted" || bad "h. answered ${R:0:160}"
MH=$(echo "${R#* }" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
if [ -n "$MH" ]; then
  close_msg $MH
  META=$($PSQL -c "select recipient, metadata->>'reply_to_message_id', metadata->>'relayed_by_system' from sn_core.messages where id=$MH")
  [ "$META" = "agent-a|$MHQ|true" ] && ok "h. re-addressed to agent-a as the reply to $MHQ" || bad "h. answer row=$META"
  [ "$(status_of $MHQ)" = processed ] && ok "h. the question was closed" || bad "h. question status=$(status_of $MHQ)"
  RL=$($PSQL -c "select id from sn_core.messages where metadata->>'source'='system-relay' and (metadata->>'relay_of')::int=$MH")
  [ -n "$RL" ] && S=$(wait_decided $RL) || S=none
  [ "$S" = processed ] && ok "h. relayed into the desk (msg $RL)" || bad "h. relay status=$S"
  [ "$(history_has $DT "$TAG h:")" -ge 1 ] && ok "h. the desk shows the answer" || bad "h. answer NOT shown in the desk"
else
  bad "h. no answer row"
fi

# i. the same send once the question is answered: nothing open, refused again.
R=$(as_agent_code agent-max "{\"to\":\"desk:$DT\",\"body\":\"$TAG i: second write\"}")
[ "${R%% *}" = 403 ] && ok "i. no open question -> refused (403)" || bad "i. answered ${R:0:120}"

# j. the chat-channel door: the same agent names the same
#    foreign desk on channel web-chat. Refused at send, no row, even with a
#    question open, because an answer to a colleague is never a chat line.
R=$(as_agent agent-agent-a "{\"to\":\"max\",\"body\":\"$TAG jq: test question, please ignore\",\"origin\":{\"channel\":\"internal\",\"conversation\":\"desk:$DT\"}}")
MJQ=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))")
R=$(as_agent_code agent-max "{\"to\":\"desk:$DT\",\"channel\":\"web-chat\",\"body\":\"$TAG j: max into agent-a desk over web-chat\"}")
[ "${R%% *}" = 403 ] && ok "j. web-chat door into a foreign desk refused (403)" || bad "j. web-chat door answered ${R:0:160}"
[ "$(rows_with "$TAG j:")" = 0 ] && ok "j. no row written" || bad "j. a row was written"
[ -n "$MJQ" ] && close_msg $MJQ

# cleanup: the throwaway desk goes; the test rows stay as evidence.
sudo -n curl -s --unix-socket "$SOCK" -o /dev/null -w "cleanup desk %{http_code}\n" -X DELETE "$API/api/desks/$DT"
echo "PASS $pass  FAIL $fail"
