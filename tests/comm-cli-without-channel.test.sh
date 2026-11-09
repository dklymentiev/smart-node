#!/usr/bin/env bash
#
# A node without a channel still gets the agent CLI comm.sh and its tool
# contract.
#
# WHAT WENT WRONG. install-on ran the communication companion only when a
# channel was given (Telegram or mail). Every agent's instructions cite
# /srv/tools/comm/comm.sh, and agents message each other through it, so on a
# channel-less node the agents were pointed at a file that did not exist. The
# e2e agent-comms check on the 1.12.46 stand: "env: /srv/tools/comm/comm.sh:
# No such file or directory".
#
# THE PROPERTIES:
#   1. communication.sh has an agent-cli mode that installs the CLI, registers
#      the comm tool and checks both, and wires no channel, service or stage
#   2. install-on, when no channel is given and the companion is not installed,
#      runs communication in that mode instead of skipping it
#   3. run_companion passes a forced mode through, skips the full-companion
#      --verify after it, and does not record it as an installed companion
#
# Source-level: the live proof is a clean channel-less install where comm.sh
# exists and tests/e2e/agent-comms.sh passes.
#
#   tests/comm-cli-without-channel.test.sh                  # tree, PASS
#   tests/comm-cli-without-channel.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "comm CLI without a channel (git:$BASELINE)"
else echo "comm CLI without a channel (working tree)"; fi

COMM="$(read_src companions/communication.sh)"
INST="$(read_src orchestrator/install-on.sh)"
[[ -n "$COMM" && -n "$INST" ]] || { bad "sources are readable"; echo; echo "FAIL"; exit 1; }

# The body of a shell function: from "name() {" to the first line that is "}".
fn_body() { awk -v f="$2() {" '$0 == f {on=1; next} on && $0 == "}" {exit} on' <<<"$1"; }

# --- 1 -------------------------------------------------------------------
if grep -qE '^[[:space:]]*--agent-cli\)[[:space:]]+MODE="agent-cli"' <<<"$COMM" \
   && grep -qE '^[[:space:]]*agent-cli\)[[:space:]]+run_agent_cli' <<<"$COMM"; then
    ok "communication.sh accepts --agent-cli and dispatches it"
else
    bad "communication.sh has no --agent-cli mode"
fi
BODY="$(fn_body "$COMM" run_agent_cli)"
if [[ -z "$BODY" ]]; then
    bad "run_agent_cli exists"
else
    for need in install_agent_cli register_comm_tool tool_registered_check; do
        grep -qw "$need" <<<"$BODY" && ok "agent-cli mode runs $need" \
            || bad "agent-cli mode does not run $need"
    done
    grep -q 'CLI_PATH' <<<"$BODY" && ok "agent-cli mode checks the CLI is on disk" \
        || bad "agent-cli mode does not check the CLI is on disk"
    for never in require_a_channel install_services write_env build_venv stage_mark setup_db_role; do
        grep -qw "$never" <<<"$BODY" && bad "agent-cli mode must not run $never" \
            || ok "agent-cli mode does not run $never"
    done
fi

# --- 2 -------------------------------------------------------------------
# The branch that logs "communication companion not installed" must run the
# companion in agent-cli mode.
BRANCH="$(awk '/communication companion not installed -- no channel given/ {on=1} on {print} on && /^        fi$/ {exit}' <<<"$INST")"
if grep -qE 'run_companion communication .* agent-cli' <<<"$BRANCH"; then
    ok "install-on installs the agent CLI when no channel is given"
else
    bad "install-on skips communication entirely when no channel is given"
fi

# --- 3 -------------------------------------------------------------------
RC="$(fn_body "$INST" run_companion)"
if grep -q 'forced_mode="${4:-}"' <<<"$RC" && grep -q 'run_suffix=" --$forced_mode"' <<<"$RC"; then
    ok "run_companion passes a forced mode through"
else
    bad "run_companion has no forced mode"
fi
if grep -qE 'if \[\[ -z "\$forced_mode" \]\]; then' <<<"$RC"; then
    ok "run_companion skips the full-companion --verify after a forced mode"
else
    bad "run_companion would run the full-companion --verify after a forced mode"
fi

# A forced mode must not be recorded as an installed companion: validate
# requires a done stage for every companion in COMPANIONS_DONE, and agent-cli
# writes none. Missed on the first pass; the clean install failed validate.
FORCED_LINE=$(grep -n 'if \[\[ -n "\$forced_mode" \]\]; then' <<<"$RC" | tail -1 | cut -d: -f1)
DONE_LINE=$(grep -n 'COMPANIONS_DONE+=' <<<"$RC" | tail -1 | cut -d: -f1)
if [[ -n "$FORCED_LINE" && -n "$DONE_LINE" && "$FORCED_LINE" -lt "$DONE_LINE" ]] \
   && sed -n "${FORCED_LINE},${DONE_LINE}p" <<<"$RC" | grep -q 'return 0'; then
    ok "a forced-mode run is not recorded in COMPANIONS_DONE"
else
    bad "a forced-mode run is recorded in COMPANIONS_DONE, so validate demands its stage"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; exit 0; else echo "FAIL ($FAILED)"; exit 1; fi
