# gateway-ops -- node-level operational sidecars for the gateway

Deliberately a SIBLING of `companions/gateway/`, not a directory inside it.
`companions/gateway/` is the vendored snapshot of the upstream `smart-node-gateway`
repository and `install_snapshot` syncs it with `rsync -a --delete`. Anything
added inside that tree is destroyed by the next re-vendor, silently. Everything
here is ours, so it lives outside it.

## claude-agent-wrapper

Installed by `gateway.sh` as the command sudo runs for each agent. It reads the
model credential after the privilege drop, so the credential is absent from
sudo's logged environment.

The gateway sends a system prompt as an eight-byte big-endian UTF-8 byte length
followed by exactly that many bytes on stdin, before stream-json messages. The
wrapper consumes this prefix using unbuffered reads and exposes the prompt as
an inherited anonymous memory file with mode 0600 through `--system-prompt-file`.
The CLI receives the original prompt; sudo sees only the transport flag. No
named prompt file is created, and the memory file disappears when its last
descriptor closes. Launches without a system prompt use stdin directly.

This requires Linux `memfd_create` and a Claude CLI supporting
`--system-prompt-file` (verified with Claude Code 2.1.143). Sudo's command and
environment audit remains enabled. The gateway and wrapper are installed
together by the gateway companion update.

## session-reaper.py

Kills idle or orphaned `claude --print` agent processes by
`sn_core.ai_sessions.last_activity_at`, never by age, and records each kill as a
`session.reaped` event. Read the module docstring before changing the threshold:
it explains what "idle" means here and the one case in which it could take a live
session.

Installed to `/opt/smart-node/ops/session-reaper.py` (a node-level path, not a
gateway-private one -- it reaps processes the gateway spawned but does not own,
and it runs as root while the gateway does not). Scheduled by
`smart-node-session-reaper.timer`, every five minutes.

Extracted from reference-node on 2026-08-13, where it had run since June while
existing in no repository. **It is the sweep, not the fix**: the leak is the gateway owning no durable lifecycle
itself, and a reaper that runs every five minutes hides an unbounded leak rather
than closing it.
