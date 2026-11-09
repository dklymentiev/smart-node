#!/usr/bin/env bash
# claude-auth-capture.sh -- install-time Claude auth capture.
#
# Primary auth path: during the interactive install TTY, run
# `claude setup-token` so the node is BORN AUTHED -- the sysadmin agent and the
# full CLI roster work from first boot without a cloud-first constraint. The
# token lands in the node-level CLAUDE_HOME (/etc/smart-node/claude) that the
# credential provider distributes to agent homes.
#
# Fallbacks stay intact and are NOT replaced here:
#   - Studio "AI Gateway -> Connect" modal (core-api cli_login_start/complete ->
#     admin-runner pty_login) for re-auth / rotation / deferred setup.
#   - Chat-based login stays REJECTED (OAuth code must never transit chat).
#
# Design: idempotent (skip if already authed), non-fatal (a skipped/failed
# capture leaves the node deferred, not broken -- the modal covers it), and
# TTY-gated (no interactive terminal -> defer, never block a headless install).
#
# Exit: 0 always (install must not hard-fail on optional auth). The outcome is
# reported on stdout and as the last word: CAPTURED | ALREADY | DEFERRED | FAILED.
set -uo pipefail

CLAUDE_HOME="${SN_ADMIN_CLAUDE_HOME:-/etc/smart-node/claude}"
CRED="$CLAUDE_HOME/.claude/.credentials.json"
CLAUDE_BIN="${SN_CLAUDE_BIN:-claude}"

_say() { printf '[claude-auth] %s\n' "$*"; }
_outcome() { printf '[claude-auth] outcome=%s\n' "$1"; echo "$1"; }

# 1. already authed? (idempotent -- re-running install must not re-prompt)
if [[ -s "$CRED" ]]; then
    _say "credential already present at $CRED -- node already authed."
    _outcome ALREADY
    exit 0
fi

# 2. interactive TTY required for the OAuth paste step
if [[ ! -t 0 || ! -t 1 ]]; then
    _say "no interactive TTY; skipping install-time capture."
    _say "complete later via Studio -> AI Gateway -> Connect (admin-runner login)."
    _outcome DEFERRED
    exit 0
fi

# 3. claude CLI must be installed (cli_providers claude-cli is seeded by B02, but
#    the binary is what runs setup-token)
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    _say "claude CLI ('$CLAUDE_BIN') not found on PATH; cannot capture now."
    _say "install the CLI, then re-run this stage or use the Connect modal."
    _outcome FAILED
    exit 0
fi

# 4. run the interactive setup-token in the node-level HOME. `claude setup-token`
#    prints an OAuth URL, waits for the pasted code, then writes .credentials.json.
mkdir -p "$CLAUDE_HOME/.claude"
chmod 700 "$CLAUDE_HOME" "$CLAUDE_HOME/.claude" 2>/dev/null || true
_say "launching 'claude setup-token' (HOME=$CLAUDE_HOME) -- authorize in your browser, paste the code."
HOME="$CLAUDE_HOME" "$CLAUDE_BIN" setup-token </dev/tty >/dev/tty 2>&1 || true

# 5. verify the credential landed (never print the token itself)
if [[ -s "$CRED" ]]; then
    chmod 600 "$CRED" 2>/dev/null || true
    _say "credential captured at $CRED -- node is born authed."
    _outcome CAPTURED
    exit 0
fi

_say "setup-token did not produce a credential; node remains unauthed."
_say "retry now, or complete later via Studio -> AI Gateway -> Connect."
_outcome FAILED
exit 0
