#!/usr/bin/env bash
# Communication CLI -- thin wrapper over the node-local core-api messages API.
#
# The secretary works ONLY with the messages registry: it reads inbound, and
# writes its reply as an OUTBOUND row via /reply (core-api builds the threaded
# row and marks the inbound processed). It NEVER sends -- the communication
# outbound-pass transmits queued rows on their channel. No SMTP here, no DB,
# no business logic, no hardcoded secrets.
#
# Identity: every call acts AS the agent identity in $SN_AGENT_SLUG, sent as the
# Remote-User header. The script FAILS CLOSED if SN_AGENT_SLUG is unset.
#
# Endpoint: the core-api unix socket, $SN_CORE_API_SOCKET (default
# /run/smart-node/api/core-api.sock). $SN_CORE_API_URL overrides it for a test
# bed that has no socket; an agent identity is refused over that door.
set -euo pipefail

SOCK="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
API="${SN_CORE_API_URL:-}"   # empty = use the socket; see _api below
WHO="${SN_AGENT_SLUG:?SN_AGENT_SLUG not set -- refusing to call core-api without an identity (set by the gateway from the agent folder)}"

usage() {
  cat <<'EOF'
Usage: comm <command> [options]

Commands:
  inbox [--limit N] [--no-mark]      List unhandled inbound messages (status=new)
                                     addressed to YOU -- your slug, or any mailbox
                                     bound to you. Reading IS the delivery receipt:
                                     every listed message is marked 'routed', so it
                                     leaves the new-queue and stops re-waking you.
                                     Pass --no-mark to peek without taking delivery.
  reply --to <msg_id> [--body TEXT] [--cc a@x,b@y] [--only-sender]
                                     Reply to an inbound message in-thread.
                                     By default the reply KEEPS THE ROOM: everyone
                                     the letter was addressed to, minus you and the
                                     sender, stays on copy, the way reply-all works
                                     in a mail client. You do not retype addresses.
                                     --only-sender answers the sender alone.
                                     --cc sets the copies explicitly and overrides
                                     both. Email only.
                                     Body from --body or stdin. Records an outbound
                                     row and marks the inbound processed. Does NOT
                                     send -- the outbound-pass transmits it.
  take  --to <msg_id>                Mark an inbound message routed (taken into work)
  close --to <msg_id>                Mark an inbound message processed (no reply)
  fail  --to <msg_id> --error E      Mark an inbound message failed
  thread <thread_id>                 Show the full conversation (all messages in a thread)
  send  --to <recipient> [--subject S] [--body TEXT] [--cc a@x,b@y] [--needs-reply]
                                     Originate a NEW thread (not a reply). Recipient is
                                     an agent slug (internal mail) or an external address
                                     (email/chat). Records the row; the outbound-pass
                                     transmits external rows. Body from --body or stdin.
                                     --needs-reply (a colleague agent only): the node
                                     keeps the message open until it is answered or
                                     dismissed, reminds the recipient, and tells you if
                                     it stays unanswered.
EOF
}

# curl against core-api as the current agent identity.
#
# Over the SOCKET, because the TCP door refuses an agent identity outright: a
# Remote-User there is an assertion anything on the node could make, so core-api
# answers 403 and names the socket in the detail. Calling the address made every
# command here fail on every node -- the secretary could not read the inbox it
# exists to work. On the socket SO_PEERCRED names the calling process,
# which it never sends and cannot forge; the header stays as attribution only.
#
# Error body is NOT discarded. curl -f used to swallow the response body
# on 4xx/5xx and hand the caller only "curl: (22) The requested URL returned
# error: 400", hiding the detail core-api puts in the body (e.g. "no active
# agent 'pm (project-manager)' on this node"). Body is now captured in a tmpfile; HTTP
# status from -w '%{http_code}'; on >=400 the body goes to stderr so the caller
# can read WHY the call failed.
_api() {
  local method="$1" path="$2"; shift 2
  local _tmp _code
  _tmp="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$_tmp'" RETURN
  if [ -n "$API" ]; then
    # Explicitly pointed elsewhere: a test bed with no socket, nothing else.
    _code=$(curl -sS -o "$_tmp" -w '%{http_code}' -X "$method" "$API$path" \
              -H "Remote-User: $WHO" "$@")
  elif [ -S "$SOCK" ]; then
    _code=$(curl -sS -o "$_tmp" -w '%{http_code}' --unix-socket "$SOCK" \
              -X "$method" "http://localhost$path" \
              -H "Remote-User: $WHO" "$@")
  else
    # Never fall through to the address. That answers 403, and a 403 here reads
    # to the agent above as "nothing in the inbox".
    echo "comm: no core-api socket at $SOCK, and an agent identity is accepted" \
         "only there (set SN_CORE_API_URL only on a test bed)" >&2
    return 1
  fi
  if [ "${_code:-0}" -ge 400 ] 2>/dev/null; then
    cat "$_tmp" >&2
    return 1
  fi
  cat "$_tmp"
}

# This tool no longer writes to the journal. core-api records every message row
# it writes, in the same transaction as the row, so an emit from here is
# the same fact told twice -- and told worse: it goes over a second HTTP call
# that `|| true` discards on failure, and `message.reply` keyed entity_id off
# `.message_id`, the RFC822 Message-ID, so those events did not join to the row
# they described. The fact belongs with the write.

cmd_inbox() {
  local limit=50 mark=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --limit)   limit="$2"; shift 2;;
      --no-mark) mark=0; shift;;
      *) echo "inbox: unknown arg: $1" >&2; exit 2;;
    esac
  done
  # Recipient-scoped: only THIS agent's inbound (its slug or its mailboxes).
  local out; out="$(_api GET "/api/messages?direction=inbound&status=new&mine=true&limit=$limit")"
  # Reading IS the delivery receipt: mark each fetched message 'routed' (delivered)
  # so it leaves the new-queue and is not re-woken. Pass --no-mark to peek only.
  if [ "$mark" = 1 ]; then
    printf '%s' "$out" | jq -r '.messages[].id' 2>/dev/null | while read -r _id; do
      [ -n "$_id" ] || continue
      printf '{"status":"routed"}' | _api PATCH "/api/messages/$_id/state" \
        -H "Content-Type: application/json" -d @- >/dev/null 2>&1 || true
    done
  fi
  printf '%s\n' "$out"
}

cmd_reply() {
  local to="" body="" cc="" only_sender=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --to)   to="$2"; shift 2;;
      --body) body="$2"; shift 2;;
      --cc)   cc="$2"; shift 2;;
      # The route keeps the room by default; this is the private answer.
      --only-sender) only_sender="1"; shift;;
      *) echo "reply: unknown arg: $1" >&2; exit 2;;
    esac
  done
  [ -n "$to" ] || { echo "reply: --to <msg_id> required" >&2; exit 2; }
  [ -n "$body" ] || body="$(cat)"
  [ -n "$body" ] || { echo "reply: empty body (pass --body or pipe via stdin)" >&2; exit 2; }
  jq -nc --argjson to "$to" --arg body "$body" --arg cc "$cc" --arg only "$only_sender" \
    '{to_message_id:$to, body:$body}
     + (if $only=="" then {} else {only_sender:true} end)
     + (if $cc=="" then {} else {cc: ($cc|split(",")|map(gsub("^\\s+|\\s+$";""))|map(select(length>0)))} end)' \
    | _api POST "/api/messages/reply" -H "Content-Type: application/json" -d @-
}

_state() {
  local to="$1" status="$2" err="${3:-}"
  [ -n "$to" ] || { echo "--to <msg_id> required" >&2; exit 2; }
  jq -nc --arg s "$status" --arg e "$err" \
       '{status:$s} + (if $e=="" then {} else {error:$e} end)' \
    | _api PATCH "/api/messages/$to/state" -H "Content-Type: application/json" -d @-
}

cmd_take()  { local to=""; while [ $# -gt 0 ]; do case "$1" in --to) to="$2"; shift 2;; *) echo "take: unknown arg: $1" >&2; exit 2;; esac; done; _state "$to" routed; }
cmd_close() { local to=""; while [ $# -gt 0 ]; do case "$1" in --to) to="$2"; shift 2;; *) echo "close: unknown arg: $1" >&2; exit 2;; esac; done; _state "$to" processed; }
cmd_fail()  { local to="" err=""; while [ $# -gt 0 ]; do case "$1" in --to) to="$2"; shift 2;; --error) err="$2"; shift 2;; *) echo "fail: unknown arg: $1" >&2; exit 2;; esac; done; _state "$to" failed "$err"; }

cmd_send() {
  local to="" body="" subject="" channel="" cc="" needs_reply=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --to)      to="$2"; shift 2;;
      --body)    body="$2"; shift 2;;
      --subject) subject="$2"; shift 2;;
      --channel) channel="$2"; shift 2;;
      --cc)      cc="$2"; shift 2;;
      --needs-reply) needs_reply="1"; shift;;
      *) echo "send: unknown arg: $1" >&2; exit 2;;
    esac
  done
  [ -n "$to" ] || { echo "send: --to <recipient> required" >&2; exit 2; }
  [ -n "$body" ] || body="$(cat)"
  [ -n "$body" ] || { echo "send: empty body (pass --body or pipe via stdin)" >&2; exit 2; }
  jq -nc --arg to "$to" --arg body "$body" --arg subject "$subject" --arg channel "$channel" --arg cc "$cc" --arg nr "$needs_reply" \
    '{to:$to, body:$body}
     + (if $nr=="" then {} else {needs_reply:true} end)
     + (if $subject=="" then {} else {subject:$subject} end)
     + (if $channel=="" then {} else {channel:$channel} end)
     + (if $cc=="" then {} else {cc: ($cc|split(",")|map(gsub("^\\s+|\\s+$";""))|map(select(length>0)))} end)' \
    | _api POST "/api/messages/send" -H "Content-Type: application/json" -d @-
}

cmd_thread() {
  local tid=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) tid="$2"; shift 2;;
      -*) echo "thread: unknown arg: $1" >&2; exit 2;;
      *) tid="$1"; shift;;
    esac
  done
  [ -n "$tid" ] || { echo "thread: <thread_id> required" >&2; exit 2; }
  local enc; enc="$(jq -rn --arg t "$tid" '$t|@uri')"
  _api GET "/api/messages?thread_id=$enc&limit=200"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  inbox)  cmd_inbox "$@";;
  reply)  cmd_reply "$@";;
  take)   cmd_take "$@";;
  close)  cmd_close "$@";;
  fail)   cmd_fail "$@";;
  thread) cmd_thread "$@";;
  send)   cmd_send "$@";;
  ""|-h|--help|help) usage;;
  *) echo "comm: unknown command: $cmd" >&2; usage; exit 2;;
esac
