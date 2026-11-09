#!/usr/bin/env bash
#
# node-exceptions.sh -- what went wrong on this node, from the node's own journal.
#
# WHY THIS EXISTS. The architecture already says "Events: every component writes
# here", and every component does. What it never gained is a READER. On 2026-08-25
# eight defects were found by hand in one day; the journal had been carrying the
# evidence for all of them, counted, the whole time. Measured on reference-node that day:
#
#     tool.invoked  475     tool.succeeded 223     tool.failed 90
#
# 475 - (223 + 90) = 162 tool calls with NO outcome recorded at all -- not a
# success, not a failure, they simply stop. That is what an agent hanging looks
# like from the outside, and it was sitting in one table.
#
# SO THIS READS STATE, NOT LOGS. Exceptions here are swallowed fail-soft before
# they reach a log (a knowledge write whose mesh snapshot failed still answers
# 201). What survives is the JOURNAL and the stuck rows, and those cannot be
# swallowed.
#
# TWO RULES IT OBEYS, both learned the hard way:
#   - SILENT WHEN CLEAN. It prints nothing and exits 0 when there is nothing to
#     say. A daily "all good" stops being read inside a week, and then the one
#     that matters is not read either.
#   - COUNT, DO NOT QUOTE. "51594 refusals, first at X, last at Y" is an alarm;
#     the last twenty log lines are wallpaper.
#
# Usage:  node-exceptions.sh [--since '24 hours'] [--all]
#         --all   print the clean sections too (for a human checking the checker)
# Exit:   0 nothing to report (or --all), 1 findings, 2 could not run.

set -uo pipefail

SINCE="24 hours"
SHOW_ALL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --since) SINCE="${2:?}"; shift 2 ;;
        --all)   SHOW_ALL=1; shift ;;
        --help|-h) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

DB="${SN_DB:-smartnode}"
psql_q() { sudo -u postgres psql -tAq -d "$DB" -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null; }

psql_q "SELECT 1" >/dev/null || { echo "cannot reach the node database ($DB)" >&2; exit 2; }

FINDINGS=0
say() { printf '%s\n' "$*"; }
section() {
    local title="$1" body="$2"
    if [[ -n "$body" ]]; then
        FINDINGS=$((FINDINGS + 1))
        say ""; say "## $title"; say ""; printf '%s\n' "$body"
    elif (( SHOW_ALL )); then
        say ""; say "## $title -- clean"
    fi
}

# 1. Tool calls that were never accounted for. A BALANCE, not a pairing.
#
#    It cannot be a pairing, and that is worth stating rather than faking:
#    `tool.invoked` carries the TOOL NAME in entity_id, not the invocation, and
#    `parent_event_id` -- the column that would link an outcome to its cause -- is
#    set on 0 of 475 invocations (and on 263 of 141689 events overall). So the
#    journal cannot say whether a PARTICULAR call finished. Measured 2026-08-25,
#    filed as its own defect.
#
#    What it CAN say is the balance, and a balance that does not close is still
#    the hang signal: 475 invoked, 313 accounted for, 162 with no outcome of any
#    kind. Reported as arithmetic, so nobody reads it as 162 identified hangs.
section "Tool calls not accounted for (invoked, no outcome recorded)" "$(psql_q "
WITH w AS (SELECT event_type, count(*) AS n FROM sn_core.events
            WHERE event_type IN ('tool.invoked','tool.succeeded','tool.failed')
              AND ts > now() - interval '$SINCE' GROUP BY event_type)
SELECT 'invoked ' || i.n || ', succeeded ' || coalesce(s.n,0) || ', failed ' || coalesce(f.n,0)
       || ' -> ' || (i.n - coalesce(s.n,0) - coalesce(f.n,0)) || ' with no outcome'
  FROM (SELECT n FROM w WHERE event_type='tool.invoked') i
  LEFT JOIN (SELECT n FROM w WHERE event_type='tool.succeeded') s ON true
  LEFT JOIN (SELECT n FROM w WHERE event_type='tool.failed') f ON true
 WHERE i.n - coalesce(s.n,0) - coalesce(f.n,0) > 0")"

# 2. Recorded failures, grouped by what failed rather than listed.
section "Tool failures" "$(psql_q "
SELECT coalesce(entity_id,'(unnamed)') || ' -- ' || count(*) || 'x'
  FROM sn_core.events
 WHERE event_type = 'tool.failed' AND ts > now() - interval '$SINCE'
 GROUP BY entity_id ORDER BY count(*) DESC LIMIT 12")"

# 3. Refusals. A denial is a decision, not an error -- but a WALL of them means
#    something is asking wrongly, and that allowed a broken caller to go unseen for a release.
section "Messages refused (routing denied / blocked)" "$(psql_q "
SELECT event_type || ' -- ' || count(*) || 'x, last ' || to_char(max(ts),'MM-DD HH24:MI')
  FROM sn_core.events
 WHERE event_type IN ('message.routing_denied','message.blocked')
   AND ts > now() - interval '$SINCE'
 GROUP BY event_type ORDER BY count(*) DESC")"

# 4. Crons: a BALANCE again, for a blunter reason.
#
#    The two halves of a cron run are keyed differently and cannot be joined by
#    anything: `cron.started` records the cron's NAME (collect-services) and
#    `cron.completed` records its numeric id (1). Pairing them by entity_id
#    reported 5038 unfinished runs on a node where the totals differ by ONE --
#    a check that cries wolf is worse than no check, so it does arithmetic and
#    says only what the totals support.
section "Cron runs that did not report completion" "$(psql_q "
WITH w AS (SELECT event_type, count(*) AS n FROM sn_core.events
            WHERE event_type IN ('cron.started','cron.completed')
              AND ts > now() - interval '$SINCE' GROUP BY event_type)
SELECT 'started ' || s.n || ', completed ' || coalesce(c.n,0)
       || ' -> ' || (s.n - coalesce(c.n,0)) || ' unaccounted'
  FROM (SELECT n FROM w WHERE event_type='cron.started') s
  LEFT JOIN (SELECT n FROM w WHERE event_type='cron.completed') c ON true
 WHERE s.n - coalesce(c.n,0) > 0")"

# 5. Inbound that nobody picked up. The registry is the ground truth here: an
#    agent that was never woken leaves the row at 'new', and no exception is
#    raised anywhere -- this is exactly how a node goes quiet while every
#    service reports healthy.
section "Inbound messages still unprocessed after 30 minutes" "$(psql_q "
SELECT count(*) || ' waiting, oldest ' || to_char(min(m.received_at),'MM-DD HH24:MI')
  FROM sn_core.messages m JOIN sn_core.message_state s ON s.message_id = m.id
 WHERE m.direction = 'inbound' AND s.status = 'new'
   AND m.received_at < now() - interval '30 minutes'
HAVING count(*) > 0")"

# 6. Outbound that failed to leave. Distinct from a refusal: this one was meant
#    to go and did not.
section "Outbound messages in failed state" "$(psql_q "
SELECT count(*) || ' failed, last ' || to_char(max(s.updated_at),'MM-DD HH24:MI')
  FROM sn_core.message_state s JOIN sn_core.messages m ON m.id = s.message_id
 WHERE s.status = 'failed' AND m.direction = 'outbound'
   AND s.updated_at > now() - interval '$SINCE'
HAVING count(*) > 0")"

# 7. Sessions that died, grouped by error text. This is how a node in the field found
#    that 32 of its 205 failures were one cause, not a fleet-wide freeze.
section "Agent sessions that failed" "$(psql_q "
SELECT count(*) || 'x -- ' || left(coalesce(nullif(error,''),'(no error recorded)'), 90)
  FROM sn_core.ai_sessions
 WHERE status = 'failed' AND coalesce(updated_at, created_at) > now() - interval '$SINCE'
 GROUP BY left(coalesce(nullif(error,''),'(no error recorded)'), 90)
 ORDER BY count(*) DESC LIMIT 8")"

# 8. Agents that cannot act at all. Not an event -- a precondition. An agent with
#    no os_user has every tool call refused at the door, and says so only in
#    prose to whoever is talking to it.
section "Enabled agents with no OS user (every tool call will be refused)" "$(psql_q "
SELECT string_agg(slug, ', ' ORDER BY slug)
  FROM sn_config.agents
 WHERE enabled AND archived_at IS NULL AND os_user IS NULL
HAVING count(*) > 0")"

# 9. Knowledge that was accepted and never delivered. The API answers 201 whether
#    or not the mesh snapshot succeeded, so the row itself is the only
#    place the failure survives.
section "Knowledge rows with no delivered snapshot" "$(psql_q "
SELECT count(*) || ' rows, agents: ' || string_agg(DISTINCT a.slug, ', ')
  FROM sn_config.agent_knowledge k JOIN sn_config.agents a ON a.id = k.agent_id
 WHERE k.archived_at IS NULL AND a.archived_at IS NULL
   AND NOT (k.metadata ? 'mesh_guid')
HAVING count(*) > 0")"

if (( FINDINGS == 0 )) && (( ! SHOW_ALL )); then
    exit 0            # silent when clean, on purpose
fi

if (( FINDINGS > 0 )); then
    say ""
    say "-- $FINDINGS section(s) with findings, window: last $SINCE, node $(hostname -f 2>/dev/null || hostname)"
    exit 1
fi
exit 0
