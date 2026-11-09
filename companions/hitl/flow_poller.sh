#!/bin/bash
# Cron registry entry point for the Content Article HITL approval poller.
set -euo pipefail
if [[ -r /etc/smart-node/hitl.env ]]; then
    set -a
    source /etc/smart-node/hitl.env
    set +a
fi
set -a
source /etc/smart-node/content-article-hitl-poller.env
source /etc/smart-node/planner-key-runner.env
set +a
export PLANNER_API_URL=http://127.0.0.1:8080
exec /usr/sbin/runuser --preserve-environment -u smart-node-dispatcher -- \
    /opt/smart-node/workflows/venv/bin/python /opt/smart-node/hitl/flow_poller.py
