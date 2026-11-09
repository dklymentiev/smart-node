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

# agent processes run under umask 002 so files land 664 and directories
# 2775. The default POSIX ACLs on /work class-homes give group:rwx, but a file
# written with the default cron umask (022) lands 644 and the group inheritance
# silently degrades to r-- on the first write. Cron is where every agent wakes,
# so this is the one place that enforces it. Companion services set UMask=002
# in their own .service files. Interactive shells keep their default.
umask 002

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

LOG_DIR="/var/log/smart-node/cron"
LOG="$LOG_DIR/$JOB.log"
LOCK="/run/lock/smart-node-cron-$JOB.lock"
MAX_LOG_BYTES=$(( 5 * 1024 * 1024 ))   # rotate at 5 MiB, keep one .1 backup

# Run-status reporting to core-api: open a cron_runs row at START (BUSY) and
# close it at END (FREE) -- the DB mirror of the per-job flock, queryable by the
# panel. The report goes over core-api's unix socket: there the kernel names
# the caller, and core-api admits the run report from the node's own root
# account. A name typed in a header over TCP proves nothing and is refused.
# A job that runs as another account is not admitted and leaves no run row.
# ALWAYS fail-open: a reporting failure must never affect the job (set
# SN_CRON_REPORT=0 to disable). The wrapper -- not run.sh -- owns this, because it
# always regains control after the command (even on timeout/crash) and can close
# the row.
CORE_SOCKET="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
REPORT="${SN_CRON_REPORT:-1}"
RUN_ID=""

# Best-effort: the dir is normally created by B01-core ensure_dirs.
mkdir -p "$LOG_DIR" 2>/dev/null || true
# The wrapper itself runs as root, while its umask is 002 so agent work files
# retain the shared-group contract.  Cron logs are evidence, not agent work
# areas: keep each log root-writable but not group-writable, otherwise the
# permissions surface quite correctly rejects the generated file.
if [[ -f "$LOG" ]]; then
    chmod 0644 "$LOG" 2>/dev/null || true
fi

ts() { date -Iseconds; }

# logline LINE -- append to the job log if the dir is writable. Never
# fails the job (set -o pipefail safe; no set -e here on purpose).
logline() {
    [[ -d "$LOG_DIR" && -w "$LOG_DIR" ]] || return 0
    printf '%s\n' "$*" >> "$LOG" 2>/dev/null || true
    chmod 0644 "$LOG" 2>/dev/null || true
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

# Open the run row (BUSY). Fail-open: empty RUN_ID just means no DB mirror this
# run; a crashed core-api never blocks the job, and a never-closed row is reaped
# by the panel's stale-guard.
if [[ "$REPORT" == "1" ]] && command -v curl >/dev/null 2>&1; then
    RUN_ID=$(curl -fsS -m 5 --unix-socket "$CORE_SOCKET" \
        -X POST "http://core-api/api/crons/by-job/$JOB/runs/start" 2>/dev/null \
        | sed -n 's/.*"run_id":[[:space:]]*\([0-9]\{1,\}\).*/\1/p') || RUN_ID=""
fi

TMP=$(mktemp)
bash -c "$CMD" >"$TMP" 2>&1
RC=$?

DURATION=$(( $(date +%s) - START ))

# Close the run row (FREE) with the outcome. Numeric-only body => no escaping.
if [[ "$REPORT" == "1" && -n "$RUN_ID" ]] && command -v curl >/dev/null 2>&1; then
    curl -fsS -m 5 --unix-socket "$CORE_SOCKET" \
        -X POST "http://core-api/api/crons/runs/$RUN_ID/finish" \
        -H "Content-Type: application/json" \
        -d "{\"exit_code\": $RC, \"duration_ms\": $(( DURATION * 1000 ))}" \
        >/dev/null 2>&1 || true
fi

# Append captured output (if any), then a result line.
if [[ -s "$TMP" && -d "$LOG_DIR" && -w "$LOG_DIR" ]]; then
    cat "$TMP" >> "$LOG" 2>/dev/null || true
fi
# The command output path above can create the file before logline runs; apply
# the evidence-log mode after both write paths, too.
chmod 0644 "$LOG" 2>/dev/null || true
logline "[$(ts)] END job=$JOB exit=$RC duration=${DURATION}s"

# Pass the command's output + exit code through, so a cron MAILTO (if the
# operator set one) still delivers failures.
cat "$TMP"
rm -f "$TMP"
exit "$RC"
