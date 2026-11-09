#!/bin/bash
#
# heartbeat.sh -- Smart Node controller heartbeat companion.
#
# Emits controller.heartbeat event to core-api every minute. Sanity check
# that cron subsystem is alive and core-api is reachable from cron-runner.
#
# Wrapped by cron-runner.sh, so cron.started + cron.completed events
# also accumulate -- 3 events per minute total.

set -euo pipefail

CORE_API="${SN_CORE_API:-}"

# node identity comes from routing.env, not hostname(1).
# SMART_NODE_DOMAIN may already be in the environment (injected by the
# caller); if not, source routing.env. Fail clearly if unavailable.
HOSTNAME_FQDN="${SMART_NODE_DOMAIN:-}"
if [[ -z "$HOSTNAME_FQDN" && -r /etc/smart-node/routing.env ]]; then
    # shellcheck source=/dev/null
    source /etc/smart-node/routing.env
    HOSTNAME_FQDN="${SMART_NODE_DOMAIN:-}"
fi
if [[ -z "$HOSTNAME_FQDN" ]]; then
    echo "ERROR: SMART_NODE_DOMAIN is not set in routing.env. Cannot emit heartbeat." >&2
    exit 1
fi

UPTIME_SECS="$(awk '{print int($1)}' /proc/uptime)"
LOAD_1M="$(awk '{print $1}' /proc/loadavg)"

payload=$(cat <<JSON
{
  "actor": "system:heartbeat",
  "event_type": "controller.heartbeat",
  "entity_type": "host",
  "entity_id": "${HOSTNAME_FQDN}",
  "payload": {
    "uptime_secs": ${UPTIME_SECS},
    "load_1m": ${LOAD_1M}
  }
}
JSON
)

curl -fsS -X POST "${CORE_API}/api/events" \
  -H "Content-Type: application/json" \
  -d "${payload}" \
  --max-time 5 \
  -o /dev/null
