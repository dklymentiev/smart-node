"""Shared classification sets for the node-ops event tools.

Both the tick-side monitor (event-monitor.py) and the daily digest
(event-digest.py) decide what counts as a signal event and what is noise.
Keeping that decision in one place guarantees the two agree: a change to
either list in isolation (new signal type, new noise entry, actor rename)
reaches both callers in one edit.

Why a plain module and not a config file: the sets are code the two tools
also compile into SQL and Python filters. A YAML or env knob would need
a loader on both sides and still break the moment either side forgets to
refresh. One Python module imported by both is the smallest shape that
cannot drift.

NO BEHAVIOUR beyond the data and a tiny is_signal() predicate. Anything
richer (dedup window, cooldowns, cursor state) stays local to the tool
that owns the semantics.
"""
from __future__ import annotations

# -----------------------------------------------------------------------------
# Actor label the node-ops event tools write on their own emitted events.
# Shared on purpose: the monitor and the digest both file their fallback
# observability (event_monitor.fallback_sender, event_monitor.alert_failed)
# with THE SAME actor, so a reader looking for "the alerting path is
# degraded" finds one event stream, not two. Origin (digest vs monitor) is
# carried in the event payload, not in the actor or the type.
# -----------------------------------------------------------------------------

MONITOR_ACTOR = "component:event-monitor"


# -----------------------------------------------------------------------------
# What always matters.
# -----------------------------------------------------------------------------

SIGNAL_EXACT: frozenset[str] = frozenset({
    "agent.wake_failed",
    "agent.broken",
    "mcp.server_down",
    "service.unavailable",
    "circuit.open",
    "secret.access_denied",
    "runner.panic",
})

# Suffix match covers the family. Any event_type ending in one of these
# counts as signal, unless it is explicitly in NOISE_EXACT below.
SIGNAL_SUFFIX: tuple[str, ...] = (".error", ".crash", ".panic", ".failed")


# -----------------------------------------------------------------------------
# What we never report, even if the suffix matches.
# -----------------------------------------------------------------------------

NOISE_EXACT: frozenset[str] = frozenset({
    "auth.permission_denied",
    "auth.permission_granted",
    "auth.human_tcp_assertion_denied",
    # Our own observability events. The monitor and digest announce their
    # degraded modes through sn_core.events; they must never alert on them.
    # That would be the self-loop with extra steps.
    "event_monitor.fallback_sender",
    "event_monitor.alert_failed",
})


def is_signal(event_type: str) -> bool:
    """True when event_type is in SIGNAL_EXACT, or ends with one of
    SIGNAL_SUFFIX, and is not in NOISE_EXACT. The exclusion wins.
    """
    if not event_type:
        return False
    if event_type in NOISE_EXACT:
        return False
    if event_type in SIGNAL_EXACT:
        return True
    for suf in SIGNAL_SUFFIX:
        if event_type.endswith(suf):
            return True
    return False
