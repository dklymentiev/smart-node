#!/bin/bash
#
# secretary.sh -- Smart Node workflows companion: the secretary.
#
# Installed by companions/workflows.sh to /opt/smart-node/workflows/ and
# run on a cron tick (via the Smart Node cron-runner). The secretary is
# the SCHEDULED trigger of the runtime model -- the sibling of brin (the
# realtime dispatcher): same brain, different alarm clock.
#
# It scans the B02 messages registry (the message_state work queue) for
# unhandled inbound work -- channels where nobody waits in real time
# (e.g. email) -- and is the path that wakes the brain for it.
#
# v0.1.0 scope: the scheduled scan + the dispatcher-reachability probe.
# The brain-wake dispatch protocol (handing a message_state row to the
# brain and advancing its status) is a follow-up -- the same deferred
# item as the dispatcher companion's registry read path.
#
# Exits 0 always -- a cron job must not fail loudly; degraded states are
# logged, not raised.

set -uo pipefail

PERSISTENCE_ENV="/etc/smart-node/persistence.env"
LOG="/var/log/smart-node/secretary.log"
DISPATCHER_HEALTH="http://127.0.0.1:8101/health"

ts() { date -Iseconds; }
note() { echo "$(ts) secretary: $*" >> "$LOG" 2>/dev/null || true; }

if [[ ! -f "$PERSISTENCE_ENV" ]]; then
    note "persistence contract missing ($PERSISTENCE_ENV) -- nothing to do"
    exit 0
fi
# shellcheck source=/dev/null
source "$PERSISTENCE_ENV"

cluster="${SMART_NODE_PG_CLUSTER:-}"
core_db="${SMART_NODE_PG_CORE_DB:-}"
if [[ -z "$cluster" || -z "$core_db" ]]; then
    note "persistence contract incomplete -- nothing to do"
    exit 0
fi

# Count unhandled inbound work in the messages registry. message_state
# advances new -> routed -> processed; 'new' is the secretary's queue.
pending=$(runuser -u postgres -- psql --cluster "$cluster" -d "$core_db" -tAc \
    "SELECT count(*) FROM message_state WHERE status='new'" 2>/dev/null)

if [[ -z "$pending" ]]; then
    note "messages registry not available -- nothing to do (degraded)"
    exit 0
fi

note "$pending unhandled message(s) in the registry"

if (( pending > 0 )); then
    if curl -sf --max-time 4 "$DISPATCHER_HEALTH" >/dev/null 2>&1; then
        note "dispatcher reachable -- brain-wake dispatch is a v0.2 follow-up"
    else
        note "dispatcher not reachable -- scheduled work left for the next tick"
    fi
fi

exit 0
