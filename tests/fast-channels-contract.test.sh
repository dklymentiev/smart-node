#!/usr/bin/env bash
#
# The fast-channel list reaches core-api, and both ends name the same file.
#
# WHAT WENT WRONG. A channel message is answered by exactly one of two paths:
# the communication dispatcher answers it itself (fast, for a person waiting in
# a chat), or the Runner wakes the agent to read its own inbox and reply with
# its own tools. /api/agents/with-pending-work keeps them apart by subtracting
# FAST_CHANNELS from the wake criterion.
#
# core-api never received FAST_CHANNELS. The value was written into the
# communication companion's .env and passed nowhere, so core-api read an empty
# list, subtracted nothing, and BOTH paths answered every message. Measured on
# a node in the field: two turns on one agent per email, the
# dispatcher posting the turn text as the reply and a separately woken agent
# answering with the internal messaging tools -- which is why replies to mail
# arrived as internal notes while the letter carried the leftover narration.
#
# WHY A FILE AND NOT A UNIT LINE. core-api installs BEFORE communication does,
# so a value baked into the unit at render time is empty on a fresh node and
# only becomes right on some later run. The contract file is written by the
# companion that owns the value and read through EnvironmentFile=-, so the
# order stops mattering. This mirrors the planner key exactly.
#
# THE PROPERTIES:
#
#   1. core-api declares the contract path and its unit reads it
#   2. communication declares the SAME path and writes it
#   3. write_env calls the publisher -- the value cannot stay in the 0600 .env
#   4. with-pending-work still reads FAST_CHANNELS, so the contract has a consumer
#   5. the file is 0644 and NOT the companion's own .env, which is 0600 and
#      carries the telegram token
#   6. behaviour: the publisher writes the value, restarts core-api when it
#      CHANGED, and does not restart when it did not
#
# Property 2 against property 1 is the point. Two ends naming the value
# separately is how it came to be missing in the first place.
#
#   tests/fast-channels-contract.test.sh                  # working tree, PASS
#   tests/fast-channels-contract.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() { # PATH -> stdout
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then
    echo "fast-channel contract (git:$BASELINE)"
else
    echo "fast-channel contract (working tree)"
fi

TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
CA="$TMPD/core-api.sh"; COMM="$TMPD/communication.sh"; AG="$TMPD/agents.py"
read_src companions/core-api.sh            > "$CA"
read_src companions/communication.sh       > "$COMM"
read_src companions/core-api/routes/agents.py > "$AG"

for f in "$CA" "$COMM" "$AG"; do
    [[ -s "$f" ]] || { bad "sources are readable ($(basename "$f"))"; echo; echo "FAIL"; exit 1; }
done

# ------------------------------------------------------------------ 1 and 2
ca_path=$(sed -n 's/^FAST_CHANNELS_ENV="\(.*\)"$/\1/p' "$CA" | head -1)
comm_path=$(sed -n 's/^FAST_CHANNELS_ENV="\(.*\)"$/\1/p' "$COMM" | head -1)

if [[ -n "$ca_path" ]]; then ok "core-api declares the contract path ($ca_path)"
else bad "core-api declares the contract path"; fi

if [[ -n "$comm_path" ]]; then ok "communication declares the contract path ($comm_path)"
else bad "communication declares the contract path"; fi

if [[ -n "$ca_path" && "$ca_path" == "$comm_path" ]]; then
    ok "both ends name the same file"
else
    bad "both ends name the same file (core-api '$ca_path', communication '$comm_path')"
fi

if grep -q 'EnvironmentFile=-\$FAST_CHANNELS_ENV' "$CA"; then
    ok "the core-api unit reads it, and tolerates its absence"
else
    bad "the core-api unit reads it, and tolerates its absence"
fi

# ------------------------------------------------------------------------ 3
n=$(grep -c '^ *publish_fast_channels$' "$COMM")
if [[ "$n" == "2" ]]; then
    ok "every install path publishes the value ($n call sites, one per write_env)"
else
    bad "every install path publishes the value ($n call sites, want 2)"
fi
# It must NOT be called from inside write_env. This companion runs under
# `set -e`, and a publish that fails there killed the run that writes the .env:
# a file that is only an optimisation for another service must not be able to
# take down the install of this one.
if sed -n '/^write_env()/,/^}/p' "$COMM" | grep -q publish_fast_channels; then
    bad "the publisher is called from inside write_env, where a failure is fatal"
else
    ok "the publisher is outside write_env, where its failure cannot be fatal"
fi

# ------------------------------------------------------------------------ 4
if grep -q 'FAST_CHANNELS' "$AG"; then
    ok "with-pending-work still reads FAST_CHANNELS"
else
    bad "with-pending-work still reads FAST_CHANNELS -- the contract has no consumer"
fi

# ------------------------------------------------------------------------ 5
if grep -qE 'install -m 0644 [^|]*"\$FAST_CHANNELS_ENV"' "$COMM"; then
    ok "the contract file is 0644, not the 0600 env that carries the token"
else
    bad "the contract file is 0644, not the 0600 env that carries the token"
fi

# ------------------------------------------------------------------ 6 behaviour
FN=$(sed -n '/^publish_fast_channels()/,/^}/p' "$COMM")
if [[ -z "$FN" ]]; then
    bad "publish_fast_channels is defined"
else
    OUT="$TMPD/restarts.log"; : > "$OUT"
    (
        FAST_CHANNELS_ENV="$TMPD/fast-channels.env"
        _envfile_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1; }
        manifest_record() { :; }
        log() { :; }
        # install(1) here would need root for -o/-g; keep the copy, drop the
        # ownership, so the test asserts the CONTENT it writes.
        install() { local a=("$@"); cp "${a[-2]}" "${a[-1]}"; }
        systemctl() {
            case "$1" in
                is-active) return 0 ;;
                restart)   echo "restart $2" >> "$OUT" ;;
            esac
            return 0
        }
        ENV_FILE="$TMPD/comm.env"
        eval "$FN"
        echo "FAST_CHANNELS=email,telegram" > "$ENV_FILE"; publish_fast_channels
        echo "FAST_CHANNELS=email,telegram" > "$ENV_FILE"; publish_fast_channels
        echo "FAST_CHANNELS=telegram"       > "$ENV_FILE"; publish_fast_channels
    )
    if grep -qxF "FAST_CHANNELS=email,telegram" "$TMPD/fast-channels.env" 2>/dev/null \
       || grep -qxF "FAST_CHANNELS=telegram" "$TMPD/fast-channels.env" 2>/dev/null; then
        ok "the publisher writes FAST_CHANNELS into the contract file"
    else
        bad "the publisher writes FAST_CHANNELS into the contract file"
    fi
    if grep -qxF "FAST_CHANNELS=telegram" "$TMPD/fast-channels.env" 2>/dev/null; then
        ok "the last value written is the one that stands"
    else
        bad "the last value written is the one that stands"
    fi
    n=$(grep -c 'restart' "$OUT" 2>/dev/null || echo 0)
    if [[ "$n" == "2" ]]; then
        ok "core-api is restarted on a change and only on a change (2 of 3 calls)"
    else
        bad "core-api is restarted on a change and only on a change (got $n restarts, want 2)"
    fi
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
