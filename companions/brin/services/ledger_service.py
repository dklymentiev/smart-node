"""Ledger service: render the durable agent ledger into prompt sections.

the ledger portion of the "one assembly contract": every turn the agent's prompt carries
two pre-fetched, durable sections sourced from stores that already exist --

  [internal mail]  <- the bus
  [tool calls]     <- the event log

WHY this module exists: today the agent must CALL a tool to read its inbox, and its
tool-call history is ephemeral (per-run). Rendering both as durable snapshot sections
means the agent SEES pending colleague mail and in-progress/finished tool results
without spending a model turn to fetch them -- and async results survive disconnect
because they live in the event log, not process memory.

Pure plumbing: zero LLM. Non-fatal by design (like memory enrichment in
context_enricher): any fetch error yields an empty section, never blocks the turn.
"""

import logging
import os
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

import httpx
from core.service_proof import service_headers as _service_headers

logger = logging.getLogger("brin.ledger")

# Same default as core/executor.py and core/catalog_tools.py so all of brin
# talks to core-api over the same docker0 address.
CORE_API_URL = os.environ.get("CORE_API_URL", "")

# Reading an agent's own ledger means asking core-api AS that agent, and core-api
# refuses an agent name asserted over TCP -- correctly, since a header is
# forgeable. auth.py:get_principal raises 403 on it before it reaches any route.
# brin is a node service and not the agent's process, so it proves itself the way
# voice already does: the shared secret says WHICH service is calling, the
# on-behalf-of header names the agent, and core-api checks that pair against
# SN_SERVICE_AGENTS.
#
# Without it every fetch below returns 403 and every caller reads that as "no
# rows" -- silently, and by design, because a ledger failure must never break a
# turn. Measured on this node before the change: GET /api/events?actor=agent:alex
# with Remote-User: alex -> 403, the same call as admin -> 200 with rows. So the
# internal-mail and tool-call sections were empty on every turn of every agent,
# and nothing showed it from the outside: the agent behaved as though nothing had
# happened, because as far as its context went, nothing had.
SERVICE_SECRET = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
SERVICE_NAME = "brin"


def _agent_headers(agent_slug: str) -> dict:
    """Headers for a core-api read made on `agent_slug`'s behalf.

    Remote-User is still sent, so a node where the secret was never configured
    fails exactly as it does today rather than in some new way.
    """
    h = {"Remote-User": agent_slug}
    if SERVICE_SECRET:
        h["X-SN-Service-Auth"] = SERVICE_SECRET
        h["X-SN-Service"] = SERVICE_NAME
        h["X-SN-On-Behalf-Of"] = agent_slug
    return h

# How far back the tool-call ledger looks, and how many rows to pull. The event
# log is per-agent and high-signal, so a short window keeps the section about
# "what just happened / is happening" rather than ancient history.
_TOOL_WINDOW_MIN = 30
_TOOL_LIMIT = 40
_INBOX_LIMIT = 12
_RESULT_SNIPPET = 140
# How many recent cross-channel messages with the person to surface on a desk
# turn. Bounded so the block stays context, not a whole
# transcript -- but wide enough that a chatty channel (a voice session can run to
# hundreds of turns) does not crowd every other channel out of the window. At 12
# the owner's telegram history was pushed out by recent voice; 30 keeps several
# turns of each channel in view.
_PERSON_LIMIT = 30
# Lines older than this are left out of the person block: a channel that has been
# silent for weeks otherwise puts its last twelve lines next to today's. When the
# bound would leave almost nothing (a person returning after a long gap) the
# window is widened ONCE, to _PERSON_WIDE_AGE_DAYS, and never to "no limit": a
# month-old call listed next to this morning reads as part of the present, which
# is how an agent answered from lines 25 and 30 days old. The header of the block
# names the window it covers, so the reader knows what it is and is not seeing.
_PERSON_MAX_AGE_DAYS = 3
_PERSON_WIDE_AGE_DAYS = 14
_PERSON_MIN_LINES = 4
# A line of the block is cut short, except the agent's own latest replies: the
# next session needs what it actually said ("tests passed, 38 of 38"), and that
# sentence sits past the first 160 characters. Only the last few are long, so the
# block stays small.
_PERSON_LINE_CAP = 160
_OWN_REPLY_CAP = 1500
_OWN_REPLIES_FULL = 3


def _ago(ts_iso: str) -> str:
    """Human 'N<unit> ago' from an ISO timestamp, scaling seconds->minutes->hours->
    days so a gap reads naturally whether it is a second or two days. '?' when
    unparseable (never raises)."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return "?"
    if secs < 90:
        return "%ds ago" % int(secs)
    if secs < 5400:           # < 90 min
        return "%dm ago" % int(secs / 60)
    if secs < 172800:         # < 48 h
        return "%dh ago" % int(secs / 3600)
    return "%dd ago" % int(secs / 86400)


def _now_str(tz: str | None = None) -> str:
    """Current wall-clock time on the person's clock when their zone is known, else
    the node's, with explicit offset so it is unambiguous."""
    now = datetime.now(timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz) if tz else None).strftime("%Y-%m-%d %H:%M %z (%a)")
    except Exception:
        return now.astimezone().strftime("%Y-%m-%d %H:%M %z (%a)")


def _snippet(text: str, n: int = _RESULT_SNIPPET) -> str:
    """Collapse whitespace and clip -- a section line must stay one short line."""
    s = " ".join(str(text or "").split())
    return (s[:n] + "...") if len(s) > n else s


async def emit_event(actor: str, event_type: str, entity_id: str, payload: dict) -> None:
    """Append one event to the durable log (sn_core.events via core-api). The write
    side of the ledger -- used to record async tool invoked/succeeded/failed terminals
    that the read side (fetch_tool_calls) later surfaces. Best-effort: never raises,
    so telemetry can never break the operation it is recording."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            await c.post(
                f"{CORE_API_URL}/api/events",
                headers=_service_headers(),
                json={"actor": actor, "event_type": event_type,
                      "entity_type": "tool", "entity_id": entity_id, "payload": payload},
            )
    except Exception as e:
        logger.warning("event emit failed (%s/%s): %s", event_type, entity_id, e)


async def fetch_tool_calls(agent_slug: str) -> list[dict]:
    """Latest state per tool for this agent from the event log.

    Pairing invoked<->terminal: events carry no call_id, so we collapse to the
    MOST RECENT event per entity_id (tool name). If that latest event is a
    tool.invoked with no later terminal, the tool is still IN PROGRESS; otherwise
    it shows its DONE/FAILED result. This is honest (it never claims done for a
    dangling invoke) and avoids duplicate noise from repeated calls of one tool.
    Returns [] on any error.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=_TOOL_WINDOW_MIN)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(
                f"{CORE_API_URL}/api/events",
                params={"actor": f"agent:{agent_slug}", "since": since, "limit": _TOOL_LIMIT},
                # The agent reads its OWN event log: Remote-User = the agent slug
                # gives correct tenant scoping. ("brin" is not a registered
                # principal -- the events route rejects it with 'no active user'.)
                headers=_agent_headers(agent_slug),
            )
        rows = (r.json() or {}).get("events", []) if r.status_code == 200 else []
    except Exception as e:
        logger.warning("tool-call ledger fetch failed for %s: %s", agent_slug, e)
        return []

    # Rows arrive newest-first; the first event seen per call is its current state.
    # Correlation key: call_id when present (an async launch tags invoked+terminal
    # with the same call_id, so one invocation is one logical call even if the same
    # tool ran many times). Falls back to the tool name for legacy/sync events that
    # carry no call_id. IN PROGRESS = an invoked whose call_id has no terminal yet.
    latest: dict[str, dict] = {}
    for ev in rows:
        et = ev.get("event_type") or ""
        if not et.startswith("tool."):
            continue
        payload = ev.get("payload") or {}
        name = ev.get("entity_id") or payload.get("name") or "?"
        key = payload.get("call_id") or name
        if key in latest:
            continue
        if et == "tool.invoked":
            state, detail = "IN PROGRESS", ""
        elif et == "tool.succeeded":
            state, detail = "DONE", _snippet(payload.get("result"))
        elif et == "tool.failed":
            state, detail = "FAILED", _snippet(payload.get("error") or payload.get("result"))
        else:
            continue
        latest[key] = {"name": name, "ts": ev.get("ts"), "state": state, "detail": detail}
    return list(latest.values())


def render_tool_calls(calls: list[dict]) -> str:
    """Format tool-call ledger as the [tool calls] section. '' when empty."""
    if not calls:
        return ""
    lines = ["[tool calls]"]
    for c in calls:
        arrow = (" -> " + c["detail"]) if c.get("detail") else ""
        lines.append("- %s  %s  %s%s" % (c["name"], _ago(c.get("ts")), c["state"], arrow))
    lines.append("[end tool calls]")
    return "\n".join(lines)


_ACTIVITY_WINDOW_MIN = 15
_ACTIVITY_LIMIT = 12
# tool.* has its own section below; agent.woken / turn_completed / claimed /
# released are the runner talking about this turn, not anything the agent did.
# auth.*: a permission check is the node's bookkeeping, not something the agent
# did; ten auth.permission_granted per turn filled the limit and pushed out the
# actions this section exists to show (seen 2026-10-29).
_ACTIVITY_SKIP_PREFIXES = ("tool.", "auth.")
_ACTIVITY_SKIP = {"agent.woken", "agent.turn_completed", "agent.claimed", "agent.released"}


async def fetch_recent_activity(agent_slug: str) -> list[dict]:
    """What this agent did in the last few minutes, tool calls excluded.

    WHY THIS EXISTS. A turn is not the only thing happening to an agent. A wake
    and a chat turn can be minutes apart on the same conversation, and until the
    agent lock covered both paths they could overlap outright. Either way the
    second turn used to start with no idea that the first had just answered the
    same person -- which is how one question got answered twice, the second time
    contradicting the first.

    Context and not a tool, deliberately. A tool has to be called, and a model
    calls one when it suspects it is missing something. This is exactly the case
    where it does not suspect: a turn that already answered has no reason to ask.
    The event log knows; the turn is told.

    Returns [] on any error -- a turn must never fail because its context was
    incomplete.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=_ACTIVITY_WINDOW_MIN)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(
                f"{CORE_API_URL}/api/events",
                params={"actor": f"agent:{agent_slug}", "since": since,
                        "limit": _ACTIVITY_LIMIT * 3},
                headers=_agent_headers(agent_slug),
            )
        rows = (r.json() or {}).get("events", []) if r.status_code == 200 else []
    except Exception as e:
        logger.warning("activity ledger fetch failed for %s: %s", agent_slug, e)
        return []

    out = []
    for ev in rows:
        et = ev.get("event_type") or ""
        if et in _ACTIVITY_SKIP or et.startswith(_ACTIVITY_SKIP_PREFIXES):
            continue
        payload = ev.get("payload") or {}
        who = payload.get("to") or payload.get("from") or payload.get("recipient") or ""
        where = payload.get("channel") or ""
        preview = _snippet(payload.get("preview") or payload.get("subject") or "")
        out.append({"ts": ev.get("ts"), "what": et, "who": who,
                    "where": where, "preview": preview})
        if len(out) >= _ACTIVITY_LIMIT:
            break
    return out


def render_recent_activity(rows: list[dict]) -> str:
    """The [recent activity] section: what this agent has already done, newest
    last so it reads as a sequence. '' when there is nothing."""
    if not rows:
        return ""
    lines = ["[recent activity]",
             "What you have already done in the last few minutes. If one of these",
             "answers what you are being woken for, do not answer it a second time."]
    for r in reversed(rows):
        where = (" on %s" % r["where"]) if r.get("where") else ""
        who = (" to %s" % r["who"]) if r.get("who") else ""
        tail = (" -- %s" % r["preview"]) if r.get("preview") else ""
        lines.append("- %s  %s%s%s%s" % (_ago(r.get("ts")), r["what"], who, where, tail))
    lines.append("[end recent activity]")
    return "\n".join(lines)


async def fetch_internal_mail(agent_slug: str) -> dict | None:
    """This agent's bus inbox: recent conversation + new ids + relay
    duties. Identified by Remote-User.

    Returns the inbox dict on success (a real empty inbox is still a dict with empty
    lists), or None when the fetch FAILED (non-200 / unresolved principal / network).
    The None-vs-dict distinction matters for honesty: a caller must not report
    "no messages" when it actually could not read the mailbox at all.
    """
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(
                f"{CORE_API_URL}/api/messages/inbox",
                params={"limit": _INBOX_LIMIT},
                headers=_agent_headers(agent_slug),
            )
        if r.status_code != 200:
            logger.warning("internal-mail fetch HTTP %s for %s", r.status_code, agent_slug)
            return None
        return r.json()
    except Exception as e:
        logger.warning("internal-mail fetch failed for %s: %s", agent_slug, e)
        return None


def render_internal_mail(inbox: dict) -> str:
    """Format the bus inbox as the [internal mail] section. '' when nothing the
    agent must act on (no new inbound and nothing to relay) -- a quiet inbox adds
    no noise to the prompt.

    Surfaces only what needs action: new inbound messages, and explicit relay
    duties the bus already computed (a colleague answered something this agent
    asked on behalf of an outside caller -- the message ledger).
    """
    if not inbox:
        return ""
    convo = inbox.get("conversation") or []
    new_ids = set(inbox.get("new_message_ids") or [])
    to_relay = inbox.get("to_relay") or []
    if not new_ids and not to_relay:
        return ""

    lines = ["[internal mail]"]
    for m in convo:
        if m.get("id") not in new_ids:
            continue
        lines.append("- from %s (new, #%s): \"%s\""
                     % (m.get("sender") or "?", m.get("id"), _snippet(m.get("content"), 120)))
    for r in to_relay:
        # A colleague's answer waiting to be relayed back to the original asker.
        lines.append("- RELAY: %s answered \"%s\" -> send to %s, then dismiss #%s"
                     % (r.get("from") or "?", _snippet(r.get("answer"), 100),
                        (r.get("origin") or {}).get("conversation") or "?",
                        r.get("message_id")))
    lines.append("[end internal mail]")
    return "\n".join(lines)


async def fetch_last_activity(agent_slug: str) -> str | None:
    """Timestamp of the agent's most recent event of ANY type (no time window), i.e.
    when it was last active. Source for the temporal-awareness hint. None on error
    or if the agent has no events yet."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(
                f"{CORE_API_URL}/api/events",
                params={"actor": f"agent:{agent_slug}", "limit": 1},
                headers=_agent_headers(agent_slug),
            )
        rows = (r.json() or {}).get("events", []) if r.status_code == 200 else []
        return rows[0].get("ts") if rows else None
    except Exception as e:
        logger.warning("last-activity fetch failed for %s: %s", agent_slug, e)
        return None


def render_time(last_ts: str | None, tz: str | None = None) -> str:
    """The [time] block: current wall-clock time, plus the gap since the agent was
    last active. Gives the agent a sense of elapsed time between turns/sessions (a
    second ago vs two days ago). Always rendered -- knowing 'now' is always useful."""
    lines = ["[time]", "- now: " + _now_str(tz)]
    if last_ts:
        lines.append("- since your last activity: " + _ago(last_ts))
    lines.append("[end time]")
    return "\n".join(lines)


async def fetch_person_history(identity: str, agent_slug: str,
                               thread: str = None,
                               thread_channel: str = None) -> dict | None:
    """The person's recent conversation across every channel,
    as far as `agent_slug` was part of it.

    Resolves `identity` (a handle or account) to the ONE contact and returns that
    contact's history over all channels -- a query over the contact, not over a
    channel string. Asked AS the agent: by-contact used to answer any
    caller about anyone, so every agent was handed the owner's conversations with
    its colleagues too. None on failure, distinct from a real empty result."""
    ident = (identity or "").strip()
    if not ident:
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            _params = {"identity": ident, "limit": _PERSON_LIMIT, "skip_notices": 1}
            if thread and thread_channel:
                # Same channel, other conversation, must not arrive here.
                _params["thread"] = thread
                _params["thread_channel"] = thread_channel
            r = await c.get(
                f"{CORE_API_URL}/api/messages/by-contact",
                params={**_params, "max_age_days": _PERSON_MAX_AGE_DAYS},
                headers=_agent_headers(agent_slug),
            )
            window = _PERSON_MAX_AGE_DAYS
            if r.status_code == 200 and len((r.json() or {}).get("messages") or []) < _PERSON_MIN_LINES:
                # Almost nothing recent: look back further, once, and no further.
                window = _PERSON_WIDE_AGE_DAYS
                r = await c.get(
                    f"{CORE_API_URL}/api/messages/by-contact",
                    params={**_params, "max_age_days": window},
                    headers=_agent_headers(agent_slug),
                )
        if r.status_code != 200:
            logger.warning("person-history fetch HTTP %s for %s", r.status_code, ident)
            return None
        data = r.json()
        if isinstance(data, dict):
            data["window_days"] = window
        return data
    except Exception as e:
        logger.warning("person-history fetch failed for %s: %s", ident, e)
        return None


# The "your day" block: what this agent did, was handed and has not started over
# the last day, read from the system's records and not from the agent's memory.
# A session that starts cold answers "what did you do today" from whatever the
# prompt happens to hold; this puts the record there instead.
_DAY_WINDOW_HOURS = 24


async def fetch_day_card(agent_slug: str) -> dict | None:
    """The agent's own day from core-api. None on any failure: the block is
    context, and a turn must never fail because it could not be built."""
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(
                f"{CORE_API_URL}/api/project-state/agent-day",
                params={"hours": _DAY_WINDOW_HOURS},
                headers=_agent_headers(agent_slug),
            )
        if r.status_code != 200:
            logger.warning("day card fetch HTTP %s for %s", r.status_code, agent_slug)
            return None
        return r.json()
    except Exception as e:
        logger.warning("day card fetch failed for %s: %s", agent_slug, e)
        return None


def _local_when(ts_iso: str) -> str:
    """HH:MM in the node's local zone, with the date when it is not today."""
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError, AttributeError):
        return "?"
    today = datetime.now(timezone.utc).astimezone().date()
    return ts.strftime("%H:%M") if ts.date() == today else ts.strftime("%m-%d %H:%M")


def render_day_card(card: dict) -> str:
    """The [your day] section. '' when there is no card (the fetch failed)."""
    if not card:
        return ""
    done = card.get("done") or []
    stuck = card.get("not_started") or []
    waiting = card.get("open_assigned") or []
    mail = card.get("mail") or []
    lines = ["[your day -- the last %d hours, from the system's records and not from memory]"
             % _DAY_WINDOW_HOURS,
             "Read this before you answer what you did, what you were asked or what is open.",
             "Check a line against the task before you repeat it as fact."]
    if not (done or stuck or waiting or mail):
        lines.append("Nothing is recorded for you in this window.")
    if done:
        lines.append("Worked on:")
        for t in done:
            by = ""
            if t.get("ordered_by"):
                by = " Handed over by %s at %s." % (t["ordered_by"].get("sender"),
                                                    _local_when(t["ordered_by"].get("ts")))
            lines.append("- #%s %s [%s]: %s comments, %s status moves, %s to %s, woken %sx.%s"
                         % (t.get("id"), _snippet(t.get("title"), 70), t.get("status") or "?",
                            t.get("comments", 0), t.get("moves", 0),
                            _local_when(t.get("first")), _local_when(t.get("last")),
                            t.get("woken", 0), by))
            if t.get("last_comment"):
                lines.append("    last comment: \"%s\"" % t["last_comment"])
        # The rest of the day is not cut, it is grouped by what was done, one short
        # entry per task. A batch (nine approvals in a row) reads as a batch here.
        labels = {"updated": "Status changed", "closed": "Closed", "commented": "Comment only"}
        others = card.get("others") or []
        if others:
            lines.append("Also touched in the window, in short:")
            for g in others:
                lines.append("%s: %s" % (
                    labels.get(g.get("kind"), g.get("kind")),
                    "; ".join("#%s %s%s" % (t.get("id"), t.get("title") or "?",
                                            "" if t.get("assignee") else " (nobody assigned)"
                                            if g.get("kind") == "updated" else "")
                              for t in g.get("tasks") or [])))
            more = (card.get("others_total") or 0) - sum(len(g.get("tasks") or []) for g in others)
            if more > 0:
                lines.append("and %d more older ones" % more)
        else:
            extra = (card.get("done_total") or len(done)) - len(done)
            if extra > 0:
                lines.append("- and %d more tasks you touched" % extra)
    if stuck:
        lines.append("Name you as the doer and not started (a name match, so check it):")
        for t in stuck:
            who = t.get("assignee") or "nobody assigned"
            lines.append("- #%s %s [%s, %s, woken %sx]"
                         % (t.get("id"), _snippet(t.get("title"), 70), t.get("status") or "?",
                            who, t.get("woken", 0)))
    if waiting:
        lines.append("Open and assigned to you, not touched in this window:")
        for t in waiting:
            lines.append("- #%s %s (updated %s)"
                         % (t.get("id"), _snippet(t.get("title"), 60), _local_when(t.get("updated_at"))))
    if mail:
        lines.append("Messages between you and colleagues:")
        for m in mail:
            lines.append("- %s %s -> %s: \"%s\""
                         % (_local_when(m.get("ts")), m.get("sender"), m.get("recipient"), m.get("preview")))
    for n in card.get("notes") or []:
        lines.append("Note: %s." % n)
    lines.append("[end your day]")
    return "\n".join(lines)


def render_person_history(data: dict) -> str:
    """The [conversation with <person>] section: the recent back-and-forth with
    this person ACROSS channels, so the agent recalls them by identity and does
    not treat someone returning on a different channel as a stranger. '' when
    there is nothing or the person is unknown."""
    if not data:
        return ""
    msgs = data.get("messages") or []
    if not msgs:
        return ""
    name = data.get("display_name") or "this person"
    window = data.get("window_days")
    span = ", the last %d days" % window if window else ""
    lines = ["[conversation with %s -- across all channels%s, most recent last]" % (name, span)]
    # Messages arrive oldest first; the agent's own latest replies are the last
    # few "you" lines of the list.
    own = [i for i, m in enumerate(msgs) if m.get("direction") != "inbound"]
    full = set(own[-_OWN_REPLIES_FULL:])
    for i, m in enumerate(msgs):
        who = "them" if m.get("direction") == "inbound" else "you"
        cap = _OWN_REPLY_CAP if i in full else _PERSON_LINE_CAP
        lines.append("- %s [%s] %s: \"%s\""
                     % (_ago(m.get("received_at")), m.get("channel_type") or "?",
                        who, _snippet(m.get("content"), cap)))
    lines.append("[end conversation]")
    return "\n".join(lines)


async def build_ledger_sections(agent_slug: str, person_identity: str = None,
                                thread: str = None,
                                thread_channel: str = None,
                                with_mail: bool = True) -> str:
    """Assembly snapshot for an agent, ready to prepend to the turn message: the
    [time] block (always), then [conversation with <person>] when the turn is with
    a known person (a desk turn passes their identity), then [internal mail]
    and [tool calls] when they have content. '' only when agent_slug is falsy.
    Never raises -- a ledger problem must never block a conversation turn (the ledger contract
    assembly is best-effort, mirroring memory enrichment).

    with_mail=False leaves [internal mail] out: a wake for one chat must not
    list the other chats waiting, or the agent works them in the same context.
    """
    if not agent_slug:
        return ""
    try:
        last_ts = await fetch_last_activity(agent_slug)
        calls = await fetch_tool_calls(agent_slug)
        inbox = await fetch_internal_mail(agent_slug) if with_mail else None
        activity = await fetch_recent_activity(agent_slug)
        day = await fetch_day_card(agent_slug)
        person = (await fetch_person_history(person_identity, agent_slug,
                                             thread=thread,
                                             thread_channel=thread_channel)
                  if person_identity else None)
    except Exception as e:
        logger.warning("ledger assembly failed for %s: %s", agent_slug, e)
        return ""
    # [time] is always present; the rest only when they carry something.
    blocks = [render_time(last_ts, (person or {}).get("timezone"))]
    blocks += [b for b in (render_person_history(person),
                           render_internal_mail(inbox),
                           render_day_card(day),
                           # Before the tool ledger on purpose: what the agent
                           # has already SAID matters more to a second turn than
                           # what it called, and the last block is the one a
                           # model reads closest.
                           render_recent_activity(activity),
                           render_tool_calls(calls)) if b]
    return "\n\n".join(blocks) + "\n\n"
