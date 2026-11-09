#!/bin/bash
#
# cron-runner.sh -- Smart Node cron job wrapper.
#
# Installed by stage B01-core at
#   /usr/local/lib/smart-node/cron-runner.sh
#
# Every Smart Node cron job (a line in /etc/cron.d/smart-node-*) invokes
# its command THROUGH this wrapper. It adds two things a bare cron line
# lacks:
#
#   1. Per-job concurrency lock -- if the previous tick of the SAME job
#      is still running, this tick exits cleanly (exit 0) instead of
#      stacking. Prevents pile-up when a job runs longer than its
#      schedule interval.
#   2. Per-job run log -- a START/END line with exit code + duration,
#      plus the command's captured output, appended to
#      /var/log/smart-node/cron/<job>.log (size-capped, one rotation).
#
# No database. At bootstrap the cron.d file IS the registry; a DB-backed
# cron_jobs table is a later (B02+/companion) concern.
#
# Usage (from a cron.d line, written by register_cron in lib.sh):
#   */5 * * * * root /usr/local/lib/smart-node/cron-runner.sh JOB CMD...
#
# Exit code: the wrapped command's exit code; 0 when skipped (lock held)
# or on a usage error of this wrapper is 2.

set -o pipefail

# cron hands us a bare PATH (/usr/bin:/bin). Tools installed under /usr/local/bin --
# `claude` above all -- are then simply not found, and a wrapped agent wake dies with
# "failed to run command 'claude'" while still reporting exit=0, i.e. it fails SILENTLY
# every tick. Guarantee the standard admin PATH here rather than in each cron.d file.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

JOB="${1:-}"
shift || true
CMD="$*"

if [[ -z "$JOB" || -z "$CMD" ]]; then
    echo "Usage: cron-runner.sh <job-name> <command...>" >&2
    exit 2
fi
if [[ ! "$JOB" =~ ^[a-z][a-z0-9-]*$ ]]; then
    echo "cron-runner: invalid job name '$JOB' (lowercase letter, then alnum/dash)" >&2
    exit 2
fi

# Core-API base for event emission. Best-effort; never fails the job.
CORE_API="${SN_CORE_API:-}"

emit_event() {
    # emit_event <event_type> <payload-json>
    local event_type="$1" payload="$2"
    curl -sS --max-time 3 -X POST -H 'Content-Type: application/json' \
        -d "{\"actor\":\"system:cron\",\"event_type\":\"${event_type}\",\"entity_type\":\"cron\",\"entity_id\":\"${JOB}\",\"payload\":${payload}}" \
        "${CORE_API}/api/events" >/dev/null 2>&1 || true
}

LOG_DIR="/var/log/smart-node/cron"
LOG="$LOG_DIR/$JOB.log"
LOCK="/run/lock/smart-node-cron-$JOB.lock"
MAX_LOG_BYTES=$(( 5 * 1024 * 1024 ))   # rotate at 5 MiB, keep one .1 backup

# Best-effort: the dir is normally created by B01-core ensure_dirs.
mkdir -p "$LOG_DIR" 2>/dev/null || true

ts() { date -Iseconds; }

# logline LINE -- append to the job log if the dir is writable. Never
# fails the job (set -o pipefail safe; no set -e here on purpose).
logline() {
    [[ -d "$LOG_DIR" && -w "$LOG_DIR" ]] || return 0
    printf '%s\n' "$*" >> "$LOG" 2>/dev/null || true
}

# Size-cap the log: one rotation to .1, then start fresh.
if [[ -f "$LOG" ]]; then
    sz=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    if (( sz > MAX_LOG_BYTES )); then
        mv -f "$LOG" "$LOG.1" 2>/dev/null || true
    fi
fi

# Per-job lock. If the lock file cannot be opened (e.g. /run/lock not
# writable) we run UNLOCKED rather than skip -- fail-open, the job is
# more important than the anti-pile-up guard.
LOCKED=1
exec 200>"$LOCK" 2>/dev/null || LOCKED=0
if (( LOCKED )) && ! flock -n 200; then
    logline "[$(ts)] SKIP job=$JOB reason=\"previous instance still running\""
    exit 0
fi

START=$(date +%s)
logline "[$(ts)] START job=$JOB pid=$$"
emit_event "cron.started" "{\"pid\":${$}}"

TMP=$(mktemp)
bash -c "$CMD" >"$TMP" 2>&1
RC=$?

DURATION=$(( $(date +%s) - START ))

# Append captured output (if any), then a result line.
if [[ -s "$TMP" && -d "$LOG_DIR" && -w "$LOG_DIR" ]]; then
    cat "$TMP" >> "$LOG" 2>/dev/null || true
fi
logline "[$(ts)] END job=$JOB exit=$RC duration=${DURATION}s"
if [[ $RC -eq 0 ]]; then
    emit_event "cron.completed" "{\"exit_code\":${RC},\"duration_ms\":$((DURATION*1000))}"
else
    emit_event "cron.failed" "{\"exit_code\":${RC},\"duration_ms\":$((DURATION*1000))}"
fi

# Record cron_runs row (best-effort, by-job slug lookup).
START_ISO=$(date -Iseconds -d "@$START")
END_ISO=$(date -Iseconds)
SNIPPET=""
if [[ -s "$TMP" ]]; then
    SNIPPET=$(tail -c 4096 "$TMP" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ')
fi
curl -sS --max-time 3 -X POST -H 'Content-Type: application/json' \
    -d "{\"started_at\":\"${START_ISO}\",\"finished_at\":\"${END_ISO}\",\"exit_code\":${RC},\"duration_ms\":$((DURATION*1000)),\"output_snippet\":\"${SNIPPET}\"}" \
    "${CORE_API}/api/crons/by-job/${JOB}/runs" >/dev/null 2>&1 || true

# Pass the command's output + exit code through, so a cron MAILTO (if the
# operator set one) still delivers failures.
cat "$TMP"
rm -f "$TMP"
exit "$RC"
