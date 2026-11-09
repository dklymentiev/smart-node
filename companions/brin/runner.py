"""smart-node-runner: the centralized launcher = the run.sh analog.

Polls core-api for FREE agents that have pending work and launches each, fire-and-forget,
bounded by RUNNER_MAX_CONCURRENCY (K). The Gateway EXECUTES the agent; this service only decides
WHEN/WHETHER to start it. claim is FAIL-CLOSED (only an explicit claimed=true launches), so it
coexists safely with any other launcher -- the per-agent claim guard prevents a double launch.

RUNNER_DRY_RUN=1 -> log would-launch only, do not act (shadow mode). Set 0 to actually launch.
Separate process (own systemd unit) for fault isolation; reuses the dispatcher venv + .env.
"""
import asyncio
import logging
import os
import sys
import time as _time

import httpx

from core.owner_proof import owner as _owner, owner_headers as _owner_headers
from core.turn_budget import refresh_turn_ceiling, runner_call_timeout
from core.service_proof import service_headers as _service_headers

# Both names, because the tree is split on them: the installer writes the node
# contract under SN_CORE_API_URL and much of brin still passes the bare name.
# The empty default that used to sit here was not a default -- it turned every
# request into a relative URL, so the loop died once every five seconds with
# "Request URL is missing an 'http://' protocol" while the node looked healthy
# and no inbound message woke anybody.
CORE = (os.environ.get("SN_CORE_API_URL") or os.environ.get("CORE_API_URL") or "").rstrip("/")
if not CORE:
    raise RuntimeError(
        "SN_CORE_API_URL is not set. "
        "The node installer should write this to the service environment."
    )
BRIN = os.environ.get("BRIN_SELF_URL", "http://127.0.0.1:8101").rstrip("/")
API_KEY = os.environ.get("BRIN_API_KEY", "")
ACTOR = os.environ.get("RUNNER_ACTOR", "runner")
K = int(os.environ.get("RUNNER_MAX_CONCURRENCY", "6"))
# reserve person-facing slots. The background ceiling mirrors the
# gateway's own two-pool cap so the runner never pre-commits all CLI slots to
# background work and leaves none for a person-facing turn.
_SN_CLI_MAX_PROCS = int(os.environ.get("SN_CLI_MAX_PROCS", "6"))
_SN_CLI_PERSON_SLOTS = int(os.environ.get("SN_CLI_PERSON_SLOTS", "1"))
BACKGROUND_CEILING = max(1, _SN_CLI_MAX_PROCS - _SN_CLI_PERSON_SLOTS)
POLL = int(os.environ.get("RUNNER_POLL_INTERVAL", "5"))
# Default OFF. It used to default ON, which was survivable only because
# the per-agent cron woke agents regardless: a node missing the variable merely
# ran late. With the cron retired the Runner is the only waker, so the same
# omission would give a node that logs what it would have done and does nothing,
# silently, with every service reporting healthy.
DRY_RUN = os.environ.get("RUNNER_DRY_RUN", "0") == "1"
WAKE = (
    "You have been woken because there is work for you. Start with your inbox: "
    "if any waiting internal message names a task (by its number, in any "
    "language, or by a /tasks/N link), work on THAT task and leave the rest for "
    "another wake. Otherwise call planner_next_task and work on what it returns "
    "(one task by default). Close messages that need no answer. Be concise."
)

# No words of any other language in this English text: a turn with no
# conversation in front of it took its language from the one Russian example
# that stood here (the Russian for "task #N") and told an English-speaking owner
# in Russian it was time to stand up (2026-10-30).
# The wake prompt is English; the conversation often is not. Without this the
# agent answers a Russian chat in the language of its own instructions -- the
# owner got his RAM figures back in English, mid-conversation. Said here rather
# than in each agent's prompt because the wake is what introduced the English.
WAKE += (" Answer in the language of the conversation you are answering, not"
         " the language of this instruction.")

# A wake for chat mentions only: the person is waiting in the chat, and the
# messages are handed over instead of "start with your inbox". With the general
# wake the agent read its whole inbox and the chat before answering, five model
# calls and about 90 s to say hello (2026-10-29). Other wakes keep WAKE.
CHAT_WAKE = (
    "You have been woken for a Studio chat: a person waiting there, or a reminder "
    "you set for it coming due. The messages are below. Answer each with inbox_reply "
    "on its id: the node posts your answer into that chat. A person's message shows who is in "
    "the chat and its latest turns; call chat_history only when you need older ones. Nothing else "
    "is needed for these messages. Answer in the language of the chat."
)


# What a chat wake goes without: the inbox. With inbox_read the agent answered
# other chats in the same turn, and inbox_dismiss followed every reply although
# the reply had closed the message (2026-10-29). Everything else it keeps: a
# researcher asked "@otto find ..." needs its search tools.
CHAT_WITHHOLD = ["mcp__messaging__inbox_read", "mcp__messaging__inbox_dismiss"]


def _chat_batch(a):
    """The messages of the one chat this wake is for, or None for a general wake.
    core-api decides it (with-pending-work next_wake): the chat waiting longest
    goes first, before mail and tasks, one chat per wake."""
    nw = a.get("next_wake") or {}
    return (nw.get("messages") or None) if nw.get("reason") == "chat" else None


# Work passed down from a person's chat, between agents. Not a person waiting for
# one line: the chat wake's "the node posts your answer" made a strategist thank the
# colleague who sent the draft and stop there (2026-10-31).
WORK_WAKE = (
    "You have been woken for work that started in a person's chat, {desk}, the chat of "
    "{agent}. The messages are below. A request: do it, and answer with inbox_reply on "
    "its id. The answer to something you asked for: finish the work and pass the whole "
    "result on to whoever asked you, or to {agent} if you do not know who, with "
    "send_message. {agent} shows it to the person with send_message to {desk}. "
    "\"Got it, passing it on\" is not passing it on. Once the result has reached the person, "
    "stop: do not improve it or start the next step until they decide. A message that "
    "asks nothing of you needs no answer: do not answer it, end your turn. "
    "Answer in the language of the chat."
)


def _for_a_person(batch):
    """The messages are a person's or the clock's, not a colleague's."""
    return all(m.get("direct", True) for m in batch)


def _wake_text(batch, nw=None):
    if not batch:
        return WAKE
    head = CHAT_WAKE if _for_a_person(batch) else WORK_WAKE.format(
        desk="desk:%s" % (nw or {}).get("desk"), agent=(nw or {}).get("chat_agent") or "the chat's agent")
    return head + "".join("\n\n#%s from %s:\n%s" % (m["id"], m.get("sender") or "", m.get("content") or "")
                               for m in batch)


logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s runner %(message)s")
log = logging.getLogger("runner")

_inflight = set()      # agents THIS runner is currently launching
_last_flag = {}        # agent -> wake_requested_at we last woke it for (same rule, task side)
_seq = 0               # monotonic wake counter -> a UNIQUE conversation id per wake (fresh session)

# A wake that FAILED is not the same thing as an agent that is BROKEN, and
# the Runner used to treat them as one: any non-200 from brin wrote status=broken.
# brin raises httpx.ReadTimeout on its own gateway call at 300s and answers 500,
# so an agent whose model simply ran long was marked broken -- and a broken agent
# never appears in /api/agents/with-pending-work again, so nothing ever woke it
# back. That was survivable only while the per-agent cron woke agents regardless
# of runtime status. With the cron retired the Runner is the sole waker
# and the same timeout costs the agent permanently: webmaster sat broken from
# 2026-06-28 to 2026-08-13, forty-six days, with 'wake 500' as its whole epitaph.
# the canonical turn ceiling, read through the one helper so the runner,
# brin and the gateway cannot disagree about it (#7). The wake timeout now derives
# from it and sits ABOVE it: the gateway ends the turn, because it is the only
# layer that knows why it ended and can say so. The old literal 310 meant the
# runner gave up first, at five minutes, on a node whose ceiling was ten.
WAKE_RETRY_BACKOFF = int(os.environ.get("RUNNER_WAKE_RETRY_BACKOFF", "60"))
# A failed wake is retried with a doubling pause up to this ceiling and never ends in
# `broken`: a provider limit (a subscription cap resets in hours) is the same failure
# as a timeout, and a broken agent has no way back.
WAKE_RETRY_MAX_BACKOFF = int(os.environ.get("RUNNER_WAKE_RETRY_MAX_BACKOFF", "300"))
_wake_failures = {}    # agent -> CONSECUTIVE failed wakes (any success clears it)
_retry_after = {}      # agent -> monotonic deadline before which we do not retry it

# ONE shared HTTP client for the whole process. Every helper used to open its own
# `httpx.AsyncClient` per call -- ~20 setups/teardowns per tick with no connection
# reuse, the dominant CPU cost of the loop. A single keep-alive client reuses
# connections to core-api/planner/brin. Per-call timeout overrides are passed
# explicitly where they differ from the default.
_client = None       # httpx.AsyncClient | None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )
    return _client


async def _get(client, path):
    r = await client.get(CORE + path, headers=_service_headers())
    r.raise_for_status()
    return r.json()


async def _pending(client):
    try:
        return (await _get(client, "/api/agents/with-pending-work")).get("agents", [])
    except Exception as e:
        log.warning("pending fetch failed: %s", e)
        return []


async def _busy_count(client):
    try:
        return (await _get(client, "/api/agents/state")).get("by_status", {}).get("busy", 0)
    except Exception as e:
        log.warning("state fetch failed: %s", e)
        return 0


async def _runtime(client, agent, action, ref=None, error=None, lease_seconds=None,
                   handed=None, scope=None):
    try:
        payload = {"action": action, "ref": ref, "error": error}
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        if handed:
            payload["handed"] = handed
        if scope:
            payload["scope"] = scope
        r = await client.post("%s/api/agents/%s/runtime" % (CORE, agent),
                              headers=_service_headers(),
                              json=payload)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning("runtime %s for %s failed: %s", action, agent, e)
    return {}


#: How long a claim survives without a release. The API default is 300s, which is
#: shorter than an ordinary coding turn, so the lease expired mid-turn and the agent
#: was relaunched on top of itself. This is the recovery path for a run that
#: died without releasing, so it must outlive a real turn and no more.
LEASE_SECONDS = int(os.environ.get("RUNNER_LEASE_SECONDS", "1800"))


async def _claim(client, agent, ref, handed=None, scope=None):
    # handed: the messages this wake is given; core-api marks them seen when the
    # claim succeeds. scope: for a wake for one chat, core-api then refuses the
    # agent an answer to any other message while this claim stands.
    d = await _runtime(client, agent, "claim", ref=ref, lease_seconds=LEASE_SECONDS,
                       handed=handed, scope=scope)
    return d.get("claimed") is True  # FAIL-CLOSED


# --- A held claim ends with the turn, not with the lease ---
#
# A wake whose turn outlives the gateway's synchronous window comes back 202 and
# the Runner keeps the claim on purpose. Until this, nothing ever told it
# the turn had ENDED, so the claim stood for the whole lease: half an hour of an
# idle agent that is absent from with-pending-work and deaf to everything that
# arrives. Measured on the field node: a front desk finished at 12:23, a letter
# came at 12:26, and it was woken for it at 12:48, when the lease ran out.

#: The gateway the agents' turns run in. Written into the service environment by
#: companions/dispatcher.sh since its first version and checked by its --verify,
#: so a missing value is a broken install and is said at startup, like CORE.
GATEWAY = os.environ.get("AI_GATEWAY_URL", "").rstrip("/")
if not GATEWAY:
    raise RuntimeError(
        "AI_GATEWAY_URL is not set. companions/dispatcher.sh writes it to the "
        "service environment; re-run it.")
#: brin looks the same sessions up under tenant 1 (core/executor.py). A wrong
#: tenant finds no session, which reads as "cannot tell" and holds the claim.
GATEWAY_TENANT = os.environ.get("AI_GATEWAY_TENANT_ID", "1")
#: Claims held for a background turn: agent -> (monotonic start of the hold, the
#: wake's ref, consecutive sweeps on which the gateway said the turn was over).
_held: dict = {}
#: Sweeps in a row that must see the turn over before the claim is released. The
#: gateway's answer is its own bookkeeping and normally enough; the second sweep
#: costs one poll interval and covers a turn the CLI backend restarts in place.
HELD_MISSES = int(os.environ.get("RUNNER_HELD_MISSES", "2"))


async def _turn_running(client, agent, ref):
    """Is the gateway turn behind this wake still running? True, False or None.

    brin opens the wake's session under the conversation "<agent>:<ref>"
    (core/executor.py, convo_key; the Runner hands ref over as user_id), and ref
    is unique per wake, so it names this wake's session and no other. The
    gateway answers in_progress from its own turn bookkeeping (_TURN_STARTED),
    which it clears when the turn ends whether or not the request that started
    it is still waiting (gateway 2.6.6).

    NOT the process table, which is what the field PR read. Our gateway keeps
    the claude process alive after its turn on purpose, so --resume can restore
    the conversation, and reaps it only after SN_CLI_IDLE_TIMEOUT (900s). A live
    process says nothing about a live turn: that check would have released a
    quarter of an hour late.

    None means "cannot tell" and is never read as "over": no session found, a
    gateway that does not answer, an answer without in_progress. Every one of
    those holds, because releasing a live turn relaunches the agent on top of
    itself while holding costs one late wake at most.
    """
    try:
        r = await client.get("%s/api/v1/sessions/v2" % GATEWAY,
                             params={"tenant_id": GATEWAY_TENANT,
                                     "conversation": "%s:%s" % (agent, ref),
                                     "limit": 5})
        if r.status_code != 200:
            return None
        sessions = r.json().get("sessions") or []
        if not sessions:
            return None
        for s in sessions:
            a = await client.get("%s/api/v1/sessions/%s/activity" % (GATEWAY, s["session_id"]))
            if a.status_code != 200:
                return None
            flag = a.json().get("in_progress")
            if flag is None:
                return None
            if flag:
                return True
        return False
    except Exception:
        return None


def _hold_verdict(elapsed, running, misses):
    """What to do with one held claim this sweep. Returns (verdict, misses). Pure.

      expired     the lease has run out, so the claim is no longer this wake's to
                  release and may already be somebody else's. Forget it.
      unknowable  the gateway could not say. Hold; the lease still recovers it.
      running     the turn is alive. Hold, and start the count again.
      settling    the turn looked over on fewer than HELD_MISSES sweeps. Hold.
      release     over on HELD_MISSES sweeps in a row. Let go.
    """
    if elapsed >= LEASE_SECONDS:
        return "expired", misses
    if running is None:
        return "unknowable", misses
    if running:
        return "running", 0
    misses += 1
    if misses < HELD_MISSES:
        return "settling", misses
    return "release", misses


async def _holder_ref(client, agent):
    """current_ref of the agent's claim right now, or None if it cannot be read."""
    try:
        for a in (await _get(client, "/api/agents/state")).get("agents", []):
            if a.get("slug") == agent:
                return a.get("current_ref")
    except Exception:
        pass
    return None


async def _sweep_held_claims(client):
    """Release the claim of every background turn that has ended.

    Never raises: it runs ahead of tick() inside the same try, and a failure here
    must not cost the node its wakes.
    """
    if not _held:
        return
    try:
        now = _time.monotonic()
        for agent, (since, ref, misses) in list(_held.items()):
            running = await _turn_running(client, agent, ref)
            verdict, misses = _hold_verdict(now - since, running, misses)
            if verdict in ("unknowable", "running", "settling"):
                _held[agent] = (since, ref, misses)
                continue
            _held.pop(agent, None)
            # This is where a turn that ran on in the background ends, so the
            # wait before its next reminder starts here too.
            _turn_over(agent)
            if verdict == "expired":
                continue
            # sn_core.release_agent(slug) frees whoever holds the agent, not a
            # named claim. Let go only of the claim THIS wake took: if current_ref
            # has moved on, somebody else holds the agent and it is not ours.
            if await _holder_ref(client, agent) != ref:
                log.info("held claim on %s has moved on -- nothing to release", agent)
                continue
            await _runtime(client, agent, "release")
            await _emit(client, "agent.turn_settled", agent,
                        {"held_seconds": round(now - since), "ref": ref, "via": "runner"})
            log.info("released %s -- background turn ended, claim held %ds instead of the lease",
                     agent, round(now - since))
    except Exception as e:
        log.warning("held-claim sweep failed: %s", e)


async def _adopt_claims(client):
    """After a restart, take back the claims a previous Runner left behind.

    _held lives in memory. A Runner restarted while one of its wakes was running
    forgot that claim, and nothing released it until the lease ran out: on the
    trials stand the agent stayed busy, absent from with-pending-work and deaf
    to new messages, for up to LEASE_SECONDS (30 min) after a turn that had
    long ended. Every busy claim whose ref is a Runner wake ref is put back
    under the held-claim sweep, which releases it as soon as the gateway says
    that wake's turn is over -- or holds it, exactly as before, while it runs or
    while the gateway cannot tell. Claims other components took (fast-dispatch
    takes "dispatch:<id>") are theirs and left alone.

    Returns how many it adopted. Never raises: startup must not fail on it.
    """
    try:
        agents = (await _get(client, "/api/agents/state")).get("agents", [])
    except Exception as e:
        log.warning("could not read claims to adopt after restart: %s", e)
        return 0
    now = _time.monotonic()
    adopted = []
    for a in agents:
        ref = a.get("current_ref") or ""
        if a.get("status") == "busy" and ref.startswith("runner-") and a["slug"] not in _held:
            _held[a["slug"]] = (now, ref, 0)
            adopted.append("%s (%s)" % (a["slug"], ref))
    if adopted:
        log.info("adopted %d claim(s) left by a previous Runner: %s",
                 len(adopted), ", ".join(adopted))
    return len(adopted)


async def _emit(client, event_type, agent, payload):
    """Best-effort audit event. Never raises: an unrecorded event must not change
    what the Runner does, and a failed POST here used to be the only thing between
    a working wake and an exception handler that marked the agent broken."""
    try:
        await client.post("%s/api/events" % CORE, headers=_service_headers(),
                          json={"actor": "component:runner", "event_type": event_type,
                                "entity_type": "agent", "entity_id": agent,
                                "payload": payload})
    except Exception:
        pass


async def _emit_woken(client, agent, ref):
    """Audit: the Runner is now the waker -> record agent.woken (keeps the chain auditable)."""
    await _emit(client, "agent.woken", agent, {"via": "runner", "ref": ref})


# A message is UNFINISHED in two states, not one. 'routed' means brin handed it to
# the agent and the agent never finished it -- on reference-node the whole backlog flipped
# new -> routed within 18 seconds of the wake, so a closure that only looked at
# 'new' would have watched eleven messages move from one invisible bucket to
# another and reported success. /dismiss accepts both for exactly this reason.
OPEN_STATES = ("new", "routed")


#: How an older planner's one-word name maps onto the three statuses of
#: planner migration 0010, for a task row that carries no `state`.
_LEGACY_PLACE = {
    "open": ("queued", None), "awaiting_approval": ("queued", "approval"),
    "pending_review": ("queued", "review"), "escalated": ("queued", "owner"),
    "blocked": ("queued", "person"), "assessing": ("in_progress", None),
    "in_progress": ("in_progress", None), "incident": ("in_progress", None),
    "done": ("closed", None), "rejected": ("closed", None),
}


def _place(task):
    """Where a task stands: (status, wait_reason), the status one of queued,
    in_progress or closed and wait_reason why a queued task waits (None: nobody
    has taken it yet). The Runner asks the planner for its three statuses, and
    every planner since migration 0010 carries them as `state` and
    `wait_reason` whatever a reader asked; an older planner only has the one
    word, mapped as 0010 maps it."""
    task = task or {}
    state = task.get("state")
    if state:
        return state, (task.get("wait_reason") if state == "queued" else None)
    return _LEGACY_PLACE.get(task.get("status") or "", (task.get("status"), None))


def _drivable(task):
    """A workflow task the Runner may start or step: new work nobody has taken
    (queued, no reason), work under way, or a run in flight whose agent handed
    the task to review on its own. Queued for approval, for a person, for a
    date, or closed: never."""
    return _place(task) in (("queued", None), ("in_progress", None), ("queued", "review"))
# The default inbox window is 20 messages, which is the last hour on a busy node
# and cannot see a row from six weeks ago. Closure has to reach what actually woke
# the agent, so ask for a window that covers it.
INBOX_LIMIT = int(os.environ.get("RUNNER_INBOX_LIMIT", "100"))


async def _inbox_state(client, agent):
    """(ids that are NEW, ids that are still UNFINISHED) for this agent's inbox.

    Read AS the agent, with the service proof: core-api refuses a bare Remote-User
    naming an agent, and a refused read looked like an empty inbox.
    """
    try:
        r = await client.get("%s/api/messages/inbox" % CORE,
                             params={"limit": INBOX_LIMIT},
                             headers=_agent_delegation_headers(agent), timeout=15.0)
        if r.status_code == 200:
            d = r.json()
            new_ids = [int(i) for i in (d.get("new_message_ids") or [])]
            open_ids = {int(row["id"]) for row in (d.get("conversation") or [])
                        if row.get("direction") == "inbound"
                        and row.get("status") in OPEN_STATES}
            return new_ids, open_ids
    except Exception as e:
        log.warning("inbox read for %s failed: %s", agent, e)
    return [], set()


async def _waking_sender(client, agent):
    """Who the newest unanswered inbound message is from, or None.

    The wake itself carries no person: the runner tells the agent "there is
    work" and the agent goes and reads it. That is why a telegram or email turn
    never got the earlier change cross-channel recall -- brin folds a person's history in
    only when it is handed an identity, and only desk turns handed one over.
    The sender of the message that caused this wake IS that identity, and
    by-contact resolves a raw channel handle by design.

    Internal and system traffic is skipped: there is no person behind it.
    Failure is None, never an exception: a turn that cannot name the person is
    the behaviour we already had, and must not become a turn that does not run.
    """
    try:
        r = await client.get("%s/api/messages/inbox" % CORE,
                             params={"limit": INBOX_LIMIT},
                             headers=_agent_delegation_headers(agent), timeout=15.0)
        if r.status_code != 200:
            return None
        rows = [row for row in (r.json().get("conversation") or [])
                if row.get("direction") == "inbound"
                and row.get("status") in OPEN_STATES
                and (row.get("channel_type") or "") not in ("internal", "system")]
        if not rows:
            return None
        newest = max(rows, key=lambda row: int(row.get("id") or 0))
        return (newest.get("sender") or "").strip() or None
    except Exception as e:
        log.warning("waking-sender lookup for %s failed: %s", agent, e)
        return None


async def _dismiss(client, agent, mid, auto=False):
    # auto marks the Runner's own mechanical closure. core-api holds a message
    # that asked for a reply open on it instead of closing it, which a dismiss
    # the agent chose to make still does.
    try:
        r = await client.post("%s/api/messages/dismiss" % CORE,
                              headers=_agent_delegation_headers(agent),
                              json={"message_id": mid, "auto": auto}, timeout=10.0)
        return r.status_code == 200
    except Exception as e:
        log.warning("dismiss %s for %s failed: %s", mid, agent, e)
        return False


async def _owed_answers(client, agent):
    """Ids of the messages still owed an answer by this agent: what asked for a
    reply, a person's mention in a chat, or work a colleague passed on from a
    person's chat (origin): that person is waiting. A reply is owed nothing, a thanks
    above all, or two agents would never stop. awaiting_reply is one already seen and
    unanswered, the reason the node woke it again."""
    r = await client.get("%s/api/messages/inbox" % CORE, params={"limit": INBOX_LIMIT},
                         headers=_agent_delegation_headers(agent), timeout=15.0)
    rows = r.json().get("conversation") or [] if r.status_code == 200 else []
    return {int(row["id"]) for row in rows
            if row.get("direction") == "inbound"
            and row.get("status") in OPEN_STATES + ("awaiting_reply",)
            and ((row.get("metadata") or {}).get("needs_reply") or (row.get("metadata") or {}).get("kind") == "chat"
                 or (str(((row.get("metadata") or {}).get("origin") or {}).get("conversation") or "").startswith("desk:")
                     and not row["metadata"].get("reply_to_message_id")))}


async def _answer_with_final_text(client, agent, owed, answer):
    """A wake's final text is its answer to what was owed one when the turn began,
    the same rule a chat or email turn has (dispatch.py); silence is said with
    NO_REPLY. It used to be thrown away: an agent that reported in plain text left
    the person with nothing, also when it closed the message itself and wrote
    "Delivered" (2026-10-30, 2026-10-31). Whether it replied with its tool instead,
    which makes this text a summary, core-api decides.

    Only what is owed (_owed_answers). Other mail keeps its old treatment: a final
    "noted" is not an answer anyone is waiting for, and two agents answering each
    other's summaries would never stop."""
    text = (answer or "").strip()
    if not text or not owed:
        return
    try:
        if text.strip("[]().").strip().upper() in ("NO_REPLY", "NO WORK"):
            # chosen silence is the agent's own dismiss, which closes even a request
            for mid in sorted(owed):
                await _dismiss(client, agent, mid)
            return
        r = await client.post("%s/api/messages/final-text" % CORE, headers=_agent_delegation_headers(agent),
                              json={"message_ids": sorted(owed), "body": text}, timeout=15.0)
        if r.status_code != 200:
            log.warning("final text of %s as an answer: %s", agent, r.status_code)
    except Exception as e:
        log.warning("final text of %s not delivered: %s", agent, e)


def _woke_it(before, scope):
    """What a wake answers for: a chat wake only the messages it was handed. The
    rest of the inbox it never saw, another chat's reminder among them."""
    return [m for m in before if m in scope] if scope else before


async def _close_what_woke_it(client, agent, before, answer):
    """A wake that ran to completion CLOSES what it was woken for.

    Ported from run.sh (b0bdc6b), which is the path the Runner migration retired. Without it the
    wake condition is not merely true, it is PERMANENTLY true: 'new' is lifted
    only by read_thread or dismiss, both of which the model must CHOOSE to call.
    An agent that reads a message, judges it needs no answer and calls neither
    leaves a row that the watermark then hides forever. Measured on reference-node the
    morning after the cron was retired: eleven rows with nobody coming for them,
    17 agents free, nothing busy, nothing broken, the oldest row from 2026-06-29.

    Deliberately NOT conditioned on the model answering "no work". The contract
    asks for that exact phrase and agents answered in prose for eight days, so
    every wording-based rule would have kept the loop alive. Closure is
    mechanical or it does not hold."""
    if not before:
        return
    # Ask the inbox again rather than trusting the dismiss reply: /dismiss answers
    # ok whether or not it changed a row, so trusting it reports closures that
    # never happened and marks a well-behaved agent as faulty on every wake.
    _, still = await _inbox_state(client, agent)
    closed = []
    for mid in before:
        # Only what the model actually LEFT UNFINISHED. A message it answered or
        # read is already processed, so it is absent from `still` and is never
        # touched. And only what was there BEFORE the run, so anything that
        # arrived while the model was working survives to the next wake.
        if mid not in still:
            continue
        if await _dismiss(client, agent, mid, auto=True):
            closed.append(mid)
    if not closed:
        return
    log.info("closed internal message(s) %s that woke %s", closed, agent)
    # An agent that neither answered nor closed a message IS a real fault. It is
    # just not one worth waking the model over for the rest of the node's life.
    # Record it so the fix makes the fault visible instead of hiding it.
    if answer.strip().lower().strip(".!") != "no work":
        await _emit(client, "agent.inbox_autoclosed", agent,
                    {"message_ids": closed,
                     "reason": "wake ended without answering or closing"})


def _rollback(agent, prev_flag):
    """Undo tick()'s optimistic flag advance for a wake that did not happen.

    This is the half that is easy to leave out and silently fatal: tick() advances
    _last_flag BEFORE launching, so a "retry" that does not put it back retries
    nothing at all -- the task is simply skipped as already-woken forever. The
    messages are not undone here: a lost claim never marked them seen, and a failed
    wake gives them back in _wake_failed."""
    if prev_flag is None:
        _last_flag.pop(agent, None)
    else:
        _last_flag[agent] = prev_flag


async def _wake_failed(client, agent, why, prev_flag, ref):
    """A wake that did not complete is a FAILED ATTEMPT, never a verdict on the agent.

    Give its messages back, release the claim so the agent is claimable again, count
    the failure and retry later with a doubling pause (WAKE_RETRY_BACKOFF up to
    WAKE_RETRY_MAX_BACKOFF). The agent is NOT marked broken here: a broken agent
    drops out of with-pending-work and nothing wakes it again, so a provider limit
    that resets in an hour would leave an agent deaf to its inbox for good. Only a
    request the gateway refuses outright (4xx, see _launch) condemns an agent.

    An agent.wake_failed event is always emitted so the failure is visible in the
    event journal, not only in runner.log."""
    # The agent saw nothing, so the messages it was handed are not used-up reminders.
    await _runtime(client, agent, "unhand", ref=ref)
    n = _wake_failures.get(agent, 0) + 1
    _wake_failures[agent] = n
    delay = min(WAKE_RETRY_BACKOFF * (2 ** (n - 1)), WAKE_RETRY_MAX_BACKOFF)
    _rollback(agent, prev_flag)
    _retry_after[agent] = _time.monotonic() + delay
    await _runtime(client, agent, "release")
    log.warning("wake failed for %s (%s) -- consecutive failure %d, retry in %ds",
                agent, why, n, delay)
    await _emit(client, "agent.wake_failed", agent, {
        "reason": why,
        "attempt": n,
        "retry_in_seconds": delay,
    })


async def _brin_notify(text: str) -> None:
    """Best-effort: send a Telegram notification to the owner via brin /api/notify.
    Used for budget alerts. Never raises."""
    try:
        c = _http()
        await c.post("%s/api/notify" % BRIN, headers={"X-API-Key": API_KEY},
                     json={"text": text, "channel": "telegram"}, timeout=10.0)
    except Exception as e:
        log.warning("brin notify failed (non-fatal): %s", e)


async def _handle_budget_exhausted(client, agent, body, prev_flag) -> None:
    """Budget rules 1-3: handle a BUDGET_EXHAUSTED wake outcome.

    The node has hit a hard spending limit. Do NOT mark the agent broken, do NOT
    increment the failure counter. Suspend the agent until the budget resets,
    and send the owner one notification per budget period.
    """
    reset_at = body.get("reset_at")
    limit = body.get("limit")
    spent = body.get("spent")
    period = body.get("period") or "period"
    log.warning("budget exhausted for %s (period=%s reset_at=%s) -- suspending",
                agent, period, reset_at)

    # _runtime budget_suspend: sets budget_suspended_until on agent_runtime AND
    # atomically claims notified_at on the exhausted budget row. Returns
    # notification_claimed=True if this is the first process to claim it.
    result = await _runtime(client, agent, "budget_suspend", ref=reset_at)

    if result.get("notification_claimed"):
        # This runner is the designated notifier for this period. Send once.
        notify_limit = result.get("limit") or limit
        notify_spent = result.get("spent") or spent
        notify_period = result.get("period") or period
        notify_reset = result.get("reset_at") or reset_at or "the next reset time"
        limit_str = ("$%.2f" % notify_limit) if notify_limit is not None else "?"
        spent_str = ("$%.2f" % notify_spent) if notify_spent is not None else "?"
        msg = (
            f"Budget exhausted: the {notify_period} limit of {limit_str} "
            f"has been reached (spent {spent_str}). "
            f"No agent turns will be processed until {notify_reset}. "
            f"Raise the limit in the node panel to resume sooner."
        )
        await _brin_notify(msg)
        await _emit(client, "budget.exhausted", agent, {"text": msg, "period": notify_period})

    # Rollback the watermark so this agent does not appear already-woken.
    # It is suspended now; the _fresh() check below will also gate on that.
    _rollback(agent, prev_flag)
    # Clear failure counters -- this is not a failure, it is a deliberate limit.
    _wake_failures.pop(agent, None)
    _retry_after.pop(agent, None)


async def _check_budget_warnings(client) -> None:
    """after a successful wake, check if any pre-warning thresholds
    were newly crossed. Atomically claims warned_at; sends one Telegram message per
    newly crossed threshold. Best-effort, never raises."""
    try:
        r = await client.get("%s/api/budgets/check-warnings" % CORE,
                             headers=_service_headers(), timeout=10.0)
        if r.status_code != 200:
            return
        warnings = r.json().get("warnings") or []
        for w in warnings:
            pct = w.get("warn_at_pct", 90)
            period = w.get("period", "period")
            limit = w.get("limit_usd", 0)
            spent = w.get("current_spend_usd", 0)
            reset_at = w.get("reset_at") or "the next reset time"
            msg = (
                f"Budget pre-warning: the {period} limit is ${limit:.2f} "
                f"and spend has reached ${spent:.2f} ({pct}% threshold crossed). "
                f"Limit resets at {reset_at}. No action is required yet."
            )
            await _brin_notify(msg)
            await _emit(client, "budget.warning", "node", {"text": msg, "period": period})
            log.info("budget pre-warning sent for period=%s spend=%.4f limit=%.2f",
                     period, spent, limit)
    except Exception as e:
        log.warning("check_budget_warnings failed (non-fatal): %s", e)


async def _launch(agent, ref, prev_flag=None, wake=WAKE, withhold=None, handed=None, scope=None,
                  desk=None, person=None, ends=None):
    """claim -> wake (gateway) -> release / retry / broken. Background task (non-blocking).
    ref is a UNIQUE per-wake conversation id -> the gateway opens a fresh session each wake (no
    cross-wake context accumulation/degradation; mirrors the old MessageWatcher dispatch-N-seq).
    prev_flag is the task flag mark as it stood BEFORE tick() advanced it, so a wake
    that never happened can be undone rather than counted as done. handed: the
    messages this wake is given (they become seen on a successful claim); scope:
    for a chat wake, the only messages it may answer. desk: the chat a chat wake
    works for, so that chat can show the colleague's work live and stop it."""
    c = _http()
    try:
        if not await _claim(c, agent, ref, handed, scope):
            log.info("claim lost for %s (another launcher won or busy) -- skip", agent)
            # The wake did not happen, so the flag mark must not say it did.
            _rollback(agent, prev_flag)
            return
        log.info("launching %s", agent)
        await _emit_woken(c, agent, ref)
        # Snapshot what woke us BEFORE the model runs.
        before, _ = await _inbox_state(c, agent)
        try:
            owed_before = await _owed_answers(c, agent)
        except Exception as e:
            log.warning("owed answers of %s not read: %s", agent, e)
            owed_before = set()
        try:
            # The wake is the long call: the gateway runs the agent inline, so this
            # waits the turn ceiling plus the runner's margin (#7).
            # Hand over WHO is waiting, when a person is. One inbox read
            # on a path that is about to spend a model call, and it turns "a
            # stranger wrote to me" into "this is the person I was emailing
            # yesterday".
            await refresh_turn_ceiling()
            # A chat wake names whose chat it is: their history and their clock.
            _person = person or await _waking_sender(c, agent)
            _wake_ctx = {"user_id": ref}
            if withhold:
                _wake_ctx["withhold_tools"] = withhold
            if ends:
                _wake_ctx["answer_ids"] = ends    # the turn ends once these are answered
            if desk:
                _wake_ctx["wake_desk"] = desk     # the chat this wake works for
            if _person:
                _wake_ctx["person_identity"] = _person
            r = await c.post("%s/api/message" % BRIN, headers={"X-API-Key": API_KEY},
                             json={"agent": agent, "message": wake, "channel": "system",
                                   "context": _wake_ctx}, timeout=runner_call_timeout())
        except Exception as e:
            # Timeout, connection reset, DNS -- the agent said nothing about itself
            # here, only the transport did. Retry rather than condemn.
            log.warning("wake transport error for %s: %s", agent, e)
            await _wake_failed(c, agent, "wake exception %s" % type(e).__name__,
                               prev_flag, ref)
            return
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        if r.status_code == 200 and body.get("error_type") == "budget_exhausted":
            # Budget exhaustion: BUDGET_EXHAUSTED is a named outcome. Do not count
            # as a failed wake, do not mark broken. Suspend the agent until reset.
            await _handle_budget_exhausted(c, agent, body, prev_flag)
        elif r.status_code == 200 and not body.get("failed"):
            _wake_failures.pop(agent, None)
            _retry_after.pop(agent, None)
            await _answer_with_final_text(c, agent, owed_before & set(scope) if scope else owed_before,
                                          body.get("final_answer") or "")
            await _close_what_woke_it(c, agent, _woke_it(before, scope), body.get("content") or "")
            await _runtime(c, agent, "release")
            log.info("done %s (200)", agent)
            # check spend pre-warning thresholds after a successful wake.
            await _check_budget_warnings(c)
        elif r.status_code == 200:
            # 200 carrying failed=true: brin answered, the gateway behind it did
            # not. The wake did not run, so it is an attempt, not a verdict.
            await _wake_failed(c, agent, "wake upstream failed", prev_flag, ref)
        elif 500 <= r.status_code < 600:
            await _wake_failed(c, agent, "wake %s" % r.status_code, prev_flag, ref)
        elif r.status_code == 202:
            # 202 is GatewayWorkingError propagated from brin: the turn outlived the
            # synchronous response window (SN_SYNC_RESPONSE_TIMEOUT) and is running
            # as a background task in the gateway. This is a NORMAL outcome for a
            # long turn, not a failure. Clear the failure counter, KEEP the claim,
            # and leave the watermark advanced (the agent WAS woken and IS working).
            # Do NOT close what woke it -- the turn is not done yet.
            _wake_failures.pop(agent, None)
            _retry_after.pop(agent, None)
            # Do NOT release here. Releasing marks the agent free while its
            # turn is still running, and because the wake signal deliberately stays
            # standing, the next poll five seconds later launches the SAME agent on
            # the SAME task again. On 2026-09-07 that produced 23 developer and 26
            # architect sessions in under three hours, three of each alive at once,
            # two of them writing the same task log. The claim is kept and the lease
            # (LEASE_SECONDS) is what recovers a turn that really did die.
            #
            # What was missing is the other end: nothing told
            # the Runner when such a turn FINISHED, so the claim stood for the whole
            # lease and the agent, idle, was absent from with-pending-work and deaf
            # to everything arriving. _sweep_held_claims asks the gateway instead.
            _held[agent] = (_time.monotonic(), ref, 0)
            log.info("done %s (202 -- turn still in flight, claim held until the gateway says it ended)", agent)
        elif r.status_code in (400, 401, 403, 404, 422):
            # The request was refused: a credential not yet set, a policy that stops the
            # agent, a route that is not up. Those are fixed on the node while the agent
            # waits, and a broken agent has no way back, so this is a failed wake too:
            # retried with the same growing pause, and every try is an event that names
            # the status.
            await _wake_failed(c, agent, "wake %s (refused)" % r.status_code, prev_flag, ref)
        else:
            # Unknown status: do not condemn silently. Route through _wake_failed so
            # the failure accumulates in the counter and appears in logs with context.
            await _wake_failed(c, agent, "wake %s (unexpected)" % r.status_code,
                               prev_flag, ref)
    finally:
        _turn_over(agent)


# --- a message seen and not answered: reminded, then reported ---
# core-api keeps it, on the message (message_state.handed_at, handed_count) and
# answers it in with-pending-work: a message no wake was handed yet is due; one
# handed and still open is due again WAKE_RETRY_SECONDS after the agent's last
# turn ended, at most WAKE_REMINDERS more times; after that it is listed as
# exhausted and reported once here (/unhandled tags it, so it is not listed
# again). Without reminders a message the agent did not get to was never
# delivered again: on 2026-10-10 an agent got two messages a second apart,
# answered one, and the other sat unread for seven and a half minutes. The
# Runner counted this in its own memory, which a restart lost (2026-10-29).


def _turn_over(agent):
    """The agent's turn has ended, however it ended. It is free again, so look
    for its next work now: waiting for the poll left 5 s between one chat's
    answer and the next chat's wake (2026-10-29)."""
    _inflight.discard(agent)
    _work.set()


async def _report_exhausted(a):
    for mid in a.get("exhausted_ids") or []:
        log.warning("agent %s: message %s is still open after every reminder; not waking for it again",
                    a["slug"], mid)
        try:
            await _http().post("%s/api/events" % CORE, headers=_service_headers(),
                               json={"actor": "component:runner", "event_type": "agent.wake_exhausted",
                                     "entity_type": "agent", "entity_id": a["slug"],
                                     "payload": {"agent": a["slug"], "message_id": mid,
                                                 "final_reason": "left_unread",
                                                 "pending_messages": a.get("pending_messages")}})
        except Exception:
            pass
        # The event is for the operator; whoever wrote the message is waiting for
        # an answer that is not coming and was never told. core-api says it once
        # and to whom it can (an agent sender gets a note).
        try:
            await _http().post("%s/api/messages/%s/unhandled" % (CORE, mid),
                               headers=_service_headers(),
                               json={"reason": "every wake ended with it unread"})
        except Exception as e:
            log.warning("could not report message %s as unhandled: %s", mid, e)


async def tick():
    c = _http()
    agents = await _pending(c)
    if not agents:
        return
    busy = await _busy_count(c)
    # What is due is core-api's answer (due_ids: messages no wake was handed
    # yet, and seen ones whose reminder is due), kept on the messages, so it
    # holds across a restart and a wake for one chat leaves the rest due. The
    # task side is the flag: an agent is fresh if a wake flag was stamped later
    # than the one we last woke it for; the mark stops the flag, once raised,
    # from relaunching the agent every tick until something clears it.
    def _fresh(a):
        slug = a["slug"]
        if slug in _inflight:
            return False
        # A wake that just failed is retried, but not instantly: without this the
        # 5s poll would relaunch a timing-out agent immediately and burn three
        # five-minute model runs back to back.
        if _retry_after.get(slug, 0) > _time.monotonic():
            return False
        if a.get("due_ids"):
            return True
        flag = a.get("wake_requested_at")
        return bool(flag) and flag != _last_flag.get(slug)
    fresh = [a for a in agents if _fresh(a)]
    for a in agents:
        await _report_exhausted(a)
    # cap background launches at BACKGROUND_CEILING, which is
    # SN_CLI_MAX_PROCS - SN_CLI_PERSON_SLOTS. K may be lower (RUNNER_MAX_CONCURRENCY
    # is a separate tuning knob); take the smaller of the two.
    _bg_cap = min(K, BACKGROUND_CEILING)
    slots = max(0, _bg_cap - busy)
    cands = fresh[:slots]
    if DRY_RUN:
        log.info("DRY-RUN pending=%s busy=%d K=%d bg_ceiling=%d slots=%d would_launch=%s",
                 [a["slug"] for a in agents], busy, K, BACKGROUND_CEILING, slots,
                 [a["slug"] for a in cands])
        return
    global _seq
    for a in cands:
        slug = a["slug"]
        # Captured before the advance so a launch that never happens can put it back.
        prev_flag = _last_flag.get(slug)
        # core-api decides what the wake is for and which messages it is given
        # (next_wake); they become seen when the claim succeeds.
        nw = a.get("next_wake") or {}
        batch = _chat_batch(a)
        # The task's flag is not taken by a chat wake, so the next wake is the task's.
        if a.get("wake_requested_at") and not batch:
            _last_flag[slug] = a["wake_requested_at"]
        _inflight.add(slug)
        # A new launch means the old hold is over one way or another (released,
        # or its lease ran out): forget it, so its sweep cannot touch this wake.
        _held.pop(slug, None)
        _seq += 1
        ref = "runner-%s-%d-%d" % (slug, a.get("max_id", 0), _seq)
        asyncio.create_task(_launch(slug, ref, prev_flag, _wake_text(batch, nw),
                                    CHAT_WITHHOLD if batch else None,
                                    nw.get("ids") or [], [m["id"] for m in batch] if batch else None,
                                    nw.get("desk") if batch else None,
                                    nw.get("person") if batch else None,
                                    [m["id"] for m in batch] if batch and _for_a_person(batch) else None))




# --- flow execution via rein --step (Runner-driven) ---
import json as _json
import sqlite3 as _sqlite

# The planner (tracking companion) is optional: without it the runner still
# launches agents that have work (tick), and only the task loops (flows,
# approvals, waits, gates) are off. Said once at start, in the journal, so the
# off state is read there and not mistaken for a silent no-op.
PLANNER = os.environ.get("PLANNER_API_URL", "").rstrip("/")
# The engine lives where its companion installs it. These used to name
# /opt/smart-node/rein, an install no stage has ever created: the workflow path
# therefore worked on exactly one node, where the tree had been assembled by
# hand. Same Rein either way, so this points at the copy that
# exists rather than shipping a second one.
REIN_PY = os.environ.get("SN_REIN_PY", "/opt/smart-node/workflows/venv/bin/python")
REIN_CWD = os.environ.get("SN_REIN_CWD", "/opt/smart-node/workflows")
RUNNER_AGENTS = os.environ.get("SN_RUNNER_AGENTS", "/opt/smart-node/flows/runner-agents")
# A node's own flows live outside /opt, in the node's state: /opt is what an
# update replaces, and a flow the node made is not ours to replace. The
# installer creates this second agents directory once and never writes inside
# its flows/; core-api's import door publishes there. The engine resolves a
# flow only as <agents-dir>/flows/<name>/<name>.yaml, so the Runner hands it
# whichever of the two directories holds the flow.
AUTHORED_AGENTS = os.environ.get("SN_AUTHORED_AGENTS", "/var/lib/smart-node/workflows")
_REIN_ENV = {**os.environ,
             "AI_GATEWAY_URL": os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000"),
             "AI_GATEWAY_TENANT_ID": os.environ.get("AI_GATEWAY_TENANT_ID", "1")}
_flow_inflight = set()

import re as _re
_approval_inflight = set()                              # gate tasks being released this tick
# The owner whose approval comment releases a held reply: the node's configured
# owner, the same name core-api accepts from this service. No default.
APPROVAL_OWNER = _owner()
if not APPROVAL_OWNER:
    log.error("SN_OPERATOR_ACTOR is not set: owner approvals cannot be recognised "
              "and owner alerts cannot be sent (re-run the core-api and dispatcher "
              "companions)")

# Circuit breaker (convergence backstop, kernel-level -- applies to every flow, no per-flow code).
# Terminality (a resolved marker + the status guard) is the PRIMARY defense; this is the safety net
# for any future non-converging case of the wake-storm class. If approval_tick acts on the SAME gate
# task more than this many times, it pauses the task (tag blocked:stuck, excluded from the candidate
# scan) and alerts the owner exactly ONCE. A correctly resolved task leaves the candidate set after
# one action, so under normal operation the counter never approaches this.
APPROVAL_MAX_ATTEMPTS = int(os.environ.get("APPROVAL_MAX_ATTEMPTS", "3"))
_approval_attempts = {}                                 # task_id -> times acted on this process


async def _task_tags(tid):
    try:
        r = await _planner("GET", "/api/v1/tasks/%s/tags" % tid)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


async def _add_tag(tid, tag):
    try:
        await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": tag})
    except Exception as e:
        log.warning("tag add %s on task %s failed: %s", tag, tid, e)


async def _notify_owner(tid, title, summary, event):
    """One owner alert over their enabled channels, reusing the same push path the planner uses.
    Sent AS the owner (APPROVAL_OWNER): /api/notify is tenant-scoped and rejects the 'runner'
    actor with 403; the owner is the super-admin principal, like the reply/release calls above."""
    try:
        r = await _http().post("%s/api/notify" % CORE, headers=_owner_headers(),
                               json={"assignee": APPROVAL_OWNER, "task_id": tid, "event": event,
                                     "title": title, "summary": summary})
        if r.status_code != 200:
            # An alert the owner never receives must not look like one sent.
            log.error("notify owner for task %s answered %s: %s",
                      tid, r.status_code, r.text[:200])
    except Exception as e:
        log.error("notify owner failed for task %s: %s", tid, e)


async def _emit_task_event(tid, event_type, payload):
    try:
        await _http().post("%s/api/events" % CORE, headers=_service_headers(),
                           json={"actor": "component:runner", "event_type": event_type,
                                 "entity_type": "task", "entity_id": str(tid), "payload": payload})
    except Exception:
        pass

# Flow grounding: a flow declares the live documents it needs in its YAML
# (grounding: {input_name: doc-guid}); the Runner fetches them fresh from Doci at
# launch and injects them as inputs. Declarative -- no flow specifics are hardcoded
# in the Runner. A rein specialist block is a single prompt->completion (no tool
# loop), so live data must arrive as input, not be read by the block.
# flow name -> the path the catalog holds for it (sn_core.workflows.definition_path),
# filled with the id -> name map below.
_wf_paths = {}
DOCI = os.environ.get("DOCI_URL", "").rstrip("/")
import yaml as _yaml


_wf_cache = {}         # workflow_id -> name (catalog is near-static)
_wf_status = {}        # workflow_id -> status; "off" starts no new run
_wf_off_noted = set()  # task ids already logged as waiting on a flow that is off
_wf_cache_at = 0.0     # monotonic ts of last full fetch
_WF_TTL = 300.0


async def _wf_name(workflow_id):
    """Resolve a catalog workflow_id (int) to its flow name via core-api.

    The catalog is near-static; previously this re-fetched the FULL /api/workflows
    list on EVERY call -> once per flow-task per tick (an N+1 burst). Cache the whole
    id->name map and refresh only when stale or on a genuine miss."""
    global _wf_cache, _wf_cache_at
    now = _time.monotonic()
    if workflow_id in _wf_cache and (now - _wf_cache_at) < _WF_TTL:
        return _wf_cache[workflow_id]
    try:
        r = await _http().get(CORE + "/api/workflows", headers=_service_headers())
        rows = r.json().get("workflows") or []
        wf = {w.get("id"): w.get("name") for w in rows}
        if wf:
            _wf_cache = wf
            _wf_cache_at = now
            _wf_paths.clear()
            _wf_paths.update({w.get("name"): w.get("definition_path") for w in rows if w.get("definition_path")})
            _wf_status.clear()
            _wf_status.update({w.get("id"): w.get("status") for w in rows})
    except Exception as e:
        log.warning("wf_name resolve failed for %s: %s", workflow_id, e)
    return _wf_cache.get(workflow_id)


async def _flow_grounding(flow):
    """Read the flow YAML grounding map {input: doc-guid} and fetch each doc's live
    content from Doci. Returns {input: content}."""
    out = {}
    try:
        doc = _yaml.safe_load(open(_flow_file(flow))) or {}
    except Exception as e:
        log.warning("flow %s yaml unreadable: %s", flow, e)
        return out
    grounding = doc.get("grounding") or {}
    if not grounding:
        return out
    c = _http()
    for key, guid in grounding.items():
        try:
            r = await c.get("%s/api/documents.php?guid=%s&content=1" % (DOCI, guid),
                            headers={"Remote-User": ACTOR}, timeout=15.0)
            out[key] = r.json()["document"]["content"]
        except Exception as e:
            log.warning("flow %s grounding %s (%s) fetch failed: %s", flow, key, guid, e)
            out[key] = ""
    return out



def _planner_headers():
    """Who we are to the planner, and proof we may talk to it at all.

    From planner 0.4.0 the port requires a credential. This key is NOT
    delegating: it is bound to the `runner` actor, so everything the runner
    writes is authored `runner` whatever headers accompany it. That is the right
    shape for a service that only ever speaks for itself -- only core-api, which
    resolves other people's identities, is allowed to name somebody else.

    Read per call so a rotated key survives a restart without a stale copy in a
    module global.
    """
    # The planner's three statuses (queued / in_progress / closed) on every
    # answer, whatever the node default.
    h = {"Remote-User": "runner", "X-Status-Vocabulary": "new"}
    key = os.environ.get("PLANNER_API_KEY", "").strip()
    if key:
        h["X-API-Key"] = key
    return h


async def _planner(method, path, **kw):
    return await _http().request(method, PLANNER + path, headers=_planner_headers(), **kw)


async def _close_done(tid, why):
    """Close a task as completed. The planner requires a comment on that move (it
    answers 400 without one) and this used to send none and not look at the answer,
    so a finished flow or a released approval left its task in_progress for good."""
    r = await _planner("PATCH", "/api/v1/tasks/%s" % tid,
                       json={"status": "closed", "close_outcome": "completed", "comment": why})
    if r.status_code not in (200, 204):
        log.warning("task %s: closing as completed refused: %s %s", tid, r.status_code, r.text[:200])
    return r


def _flow_task_start(task, tags):
    """What the Runner does with a task that carries a workflow_id: "start" a
    run, "step" the run in flight, or None (leave it alone). The Runner reads
    the planner here and writes nothing: the flow owns the task's status.

    | status       | wait_reason      | reindir tag | approved_at | does  |
    |--------------|------------------|-------------|-------------|-------|
    | queued       | none             | -           | -           | start |
    | queued       | none, review     | set         | -           | step  |
    | in_progress  |                  | set         | -           | step  |
    | in_progress  |                  | none        | set         | start |
    | in_progress  |                  | none        | empty       | -     |
    | queued       | review           | none        | -           | -     |
    | queued       | any other reason | -           | -           | -     |
    | closed       |                  | -           | -           | -     |
    | any, tagged blocked:/flow-done:/flow-review: |  |           | -     |

    The fourth row is the owner's approval. A task filed by an agent or a
    service is queued for approval, and the approval moves it to in_progress,
    never back to queued; the planner stamps approved_at then. So an approved
    task with no run is a start, and a task the owner sent back (modify:
    in_progress, no approved_at) is not. Until 2026-10 this row needed a tag,
    flow:start, put on by hand, which some agents knew and some did not; on one
    node four approved content tasks waited for days. The tag means nothing now.
    """
    if not _drivable(task):
        return None
    if any(x.startswith(("blocked:", "flow-done:", "flow-review:")) for x in tags):
        return None
    if any(x.startswith("reindir:") for x in tags):
        return "step"
    state, wait = _place(task)
    if state == "queued" and wait is None:
        return "start"
    if state == "in_progress" and (task or {}).get("approved_at"):
        return "start"
    return None


async def _drivable_tasks(limit):
    """Tasks a flow can be driven on: in_progress, and queued with nobody on
    them or for review. Two reads, each by status (the planner query param is
    `status_filter`, not `status`: an unknown param is ignored and returns ALL
    tasks, and the old `status=` once re-drove closed tasks that still carried
    a `reindir:` tag every tick, a workflow.completed storm). The queued read
    also returns tasks waiting for approval or a person; callers keep only
    the drivable ones (_drivable)."""
    tasks = []
    for st in ("in_progress", "queued"):
        r = await _planner("GET", "/api/v1/tasks?status_filter=%s&limit=%d"
                           % (st, limit if st == "in_progress" else max(limit, 500)))
        if r.status_code == 200:
            tasks += r.json()
    return [t for t in tasks if _drivable(t)]


async def _flow_tasks():
    """Tasks linked to a workflow (workflow_id set), not blocked:*, in a drivable
    status. The Runner drives these block by block. A flow's own business logic
    (create a task, message someone, wait for a reply) lives in its blocks; the
    Runner has no special gate concept."""
    out = []
    try:
        # Queued for review is here for a run already in flight. An agent that
        # did its block's work may hand the task to review on its own: that is
        # what an agent does with finished work everywhere outside a flow, and
        # an order saying "do not change the status" does not always stop it.
        # Seen on a stand on 2026-10-03: the developer answered the order, moved
        # the task, and the flow stood for good, because the Runner did not look
        # at a task in review at all. The engine owns the status of a flow task,
        # so it keeps driving and writes the next status itself.
        tasks = await _drivable_tasks(50)
        for t in tasks:
            wid = t.get("workflow_id")
            if not wid:
                continue
            # Only a fresh trigger (queued, nobody on it) or an in-flight instance
            # is drivable. Closed tasks and tasks queued for a person's call
            # (approval, a person, a date ...) are never re-driven even if they
            # still carry a reindir tag -- the storm guard, independent of the
            # query filter in _drivable_tasks.
            if not _drivable(t):
                continue
            tid = t.get("id")
            rt = await _planner("GET", "/api/v1/tasks/%s/tags" % tid)
            tags = rt.json() if rt.status_code == 200 else []
            what = _flow_task_start(t, tags)
            if not what:
                continue
            name = await _wf_name(wid)
            if not name:
                continue
            reindir = next((x.split(":", 1)[1] for x in tags if x.startswith("reindir:")), None)
            # A flow the owner switched off starts no new run; one already in
            # flight (reindir set) finishes. The task waits, and starts when
            # the flow is on again.
            if not reindir and _wf_status.get(wid, "available") != "available":
                if tid not in _wf_off_noted:
                    _wf_off_noted.add(tid)
                    log.info("task %s: workflow %s is off, not started", tid, name)
                continue
            out.append({"id": tid, "topic": t.get("title") or "", "flow": name, "reindir": reindir,
                        "status": _place(t)[0], "assignee": t.get("assignee")})
    except Exception as e:
        log.warning("flow_tasks fetch failed: %s", e)
    return out


async def _rein(args):
    p = await asyncio.create_subprocess_exec(
        REIN_PY, "-m", "rein", *args, cwd=REIN_CWD, env=_REIN_ENV,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return out.decode("utf-8", "replace")


def _rein_state(task_dir):
    status = None
    try:
        status = open(task_dir + "/state/status").read().strip()
    except Exception:
        pass
    blocks = {}
    try:
        c = _sqlite.connect(task_dir + "/state/rein.db")
        blocks = {r[0]: r[1] for r in c.execute("select name,status from processes").fetchall()}
        c.close()
    except Exception:
        pass
    return status, blocks


async def _emit_workflow(tid, flow, etype, blocks=None):
    """Emit a workflow lifecycle event so the event log records Runner-driven flow runs.
    WHY: until now the task-driven flow path (flow_tick -> _drive_flow) wrote NOTHING to
    sn_core.events, so a flow run was visible only in planner task state. That leaves an audit
    gap: the event log is meant to be the verification substrate, and the flow test catalog
    asserts from it. Entity = the flow name; payload carries the trigger task + the rein block
    snapshot. Best-effort, like _emit_woken: a failed emit never affects the drive."""
    try:
        payload = {"flow": flow, "task_id": tid}
        if blocks is not None:
            payload["blocks"] = blocks
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post("%s/api/events" % CORE, headers=_service_headers(),
                         json={"actor": "component:runner", "event_type": etype,
                               "entity_type": "workflow", "entity_id": str(flow),
                               "payload": payload})
    except Exception:
        pass


# ------------------------------------------------------------- where a flow is
# What a block's verdict does to the planner task is the flow's own logic: the
# flow declares it in planner.on_block and writes it in its post hook,
# logic/planner_effect.py. The Runner launches and does not write a task's
# status or assignee on a flow's behalf. From 2026-09-25 to 2026-10-10 it did,
# with its own record of what it had applied; that record is how one hand-off
# came to be written every tick for three hours.


def _flow_home(flow):
    """The agents directory that holds `flow`, or None.

    The shipped tree is looked at first, then the node's own. When the catalog
    holds a path for the flow, the file found has to be that file: a flow that
    was registered from one place and is found in another is not run.
    """
    for home in (RUNNER_AGENTS, AUTHORED_AGENTS):
        path = os.path.join(home, "flows", flow, flow + ".yaml")
        if not os.path.isfile(path):
            continue
        registered = _wf_paths.get(flow)
        if registered and os.path.realpath(registered) != os.path.realpath(path):
            log.warning("flow %s: found at %s, the catalog says %s; not run", flow, path, registered)
            return None
        return home
    return None


def _flow_file(flow):
    return os.path.join(_flow_home(flow) or RUNNER_AGENTS, "flows", flow, flow + ".yaml")


async def _finish_flow(tid, flow, blocks=None):
    """What the Runner records when a run is over. It does not move the task.

    How a flow ends is the flow's to say: its last block hands the task to a
    person, and a block that fails is the flow's on_error. The Runner
    used to close the task here, or hand it to its reviewer, whatever the flow
    had meant, so a rejected or a stopped run was reported done.

    What is left is the Runner's own bookkeeping: a tag that stops the task
    being driven again, and the event. A task the flow left neither ended nor
    with a person gets one comment saying so, for the flow's author to read.
    """
    status = None
    r = await _planner("GET", "/api/v1/tasks/%s" % tid)
    if r.status_code == 200:
        status, wait = _place(r.json() or {})
        if status == "queued" and wait:
            status = "queued for %s" % wait
        elif status == "queued":
            status = "queued with nobody on it"
    if status == "queued for review":
        await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": "flow-review:" + flow})
        log.info("flow %s FINISHED task %s, which waits for review", flow, tid)
        await _emit_workflow(tid, flow, "workflow.awaiting_review", blocks)
        return "review"
    if status in ("queued with nobody on it", "in_progress"):
        await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                       json={"content": "Flow %s finished and left this task %s: no block of the flow "
                                        "said how it ends. Nothing was changed here; the end has to be "
                                        "declared in the flow (planner.on_block of its last block)."
                                        % (flow, status)})
        log.warning("flow %s finished and left task %s %s", flow, tid, status)
    await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": "flow-done:" + flow})
    log.info("flow %s COMPLETED task %s (%s)", flow, tid, status)
    await _emit_workflow(tid, flow, "workflow.completed", blocks)
    return "done"


async def _drive_flow(t):
    tid, flow, reindir = t["id"], t["flow"], t["reindir"]
    try:
        home = _flow_home(flow)
        if home is None:
            await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": "blocked:error"})
            log.warning("flow %s task %s: the flow is in neither tree, or not where the catalog says; "
                        "parked blocked:error", flow, tid)
            return
        if not reindir:
            rt = await _planner("GET", "/api/v1/tasks/%s/tags" % tid)
            if rt.status_code == 200:
                reindir = next((x.split(":", 1)[1] for x in rt.json() if x.startswith("reindir:")), None)
        if not reindir:
            grounding = await _flow_grounding(flow)
            inp = _json.dumps({"task_id": str(tid), "topic": t["topic"], **grounding})
            out = await _rein(["--flow", flow, "--step", "1", "--agents-dir", home,
                               "--no-ui", "--input", inp])
            path = None
            for line in out.splitlines():
                if "Directory:" in line and "/tasks/" in line:
                    path = line.split("Directory:", 1)[1].strip()
                    break
            if not path:
                log.warning("flow %s create: no task dir (task %s); tail=%s", flow, tid, out[-300:])
                return
            base = os.path.basename(path.rstrip("/"))
            await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": "reindir:" + base})
            # The task's status is the flow's to set: its first block takes the
            # task. The Runner only ties the task to its run.
            full = home + "/tasks/" + base
            log.info("flow %s started task %s -> %s", flow, tid, base)
            await _emit_workflow(tid, flow, "workflow.started")
        else:
            full = home + "/tasks/" + reindir
            if not os.path.isdir(full):
                await _planner("POST", "/api/v1/tasks/%s/tags" % tid, json={"tag": "blocked:error"})
                log.warning("flow %s task %s: instance dir %s missing, parked blocked:error", flow, tid, reindir)
                return
            await _rein(["--step", "1", "--agents-dir", home, "--task-dir", full, "--no-ui"])
        status, blocks = _rein_state(full)
        # The hand-off a block's verdict calls for is written by the flow itself,
        # in its post hook (logic/planner_effect.py). The Runner launches: it steps
        # the engine and writes neither status nor assignee here.
        if status == "completed":
            await _finish_flow(tid, flow, blocks)
        else:
            log.info("flow %s stepped task %s blocks=%s", flow, tid, blocks)
            await _emit_workflow(tid, flow, "workflow.step", blocks)
    except Exception as e:
        log.exception("drive_flow error task %s: %s", tid, e)
    finally:
        _flow_inflight.discard(tid)


async def flow_tick():
    for t in await _flow_tasks():
        if t["id"] in _flow_inflight or len(_flow_inflight) >= K:
            continue
        _flow_inflight.add(t["id"])
        asyncio.create_task(_drive_flow(t))


# --- Approve-by-comment: release a held reply when the owner approves ---
# The owner approves on the gate task by posting a comment (author=APPROVAL_OWNER,
# comment_type='approval', content approve|reject). The gate task carries the source
# inbound link (a comment 'message_id=<id> dir=in') and the drafted reply (a comment
# comment_type='draft'). On approve, release the draft into the outbox via core-api
# /api/messages/reply (the delivery service then sends it); on reject, reject the task.
# A 'sent' marker comment makes it idempotent. Self-contained: its errors never touch
# tick()/flow_tick(). No new tables, no wake, no rein -- pure comments-as-control.

def _parse_gate(comments):
    inbound_id = draft = decision = subject = None
    sent = False
    recipients = []
    booking = None
    for c in (comments or []):
        txt = c.get("content") or ""
        typ = c.get("comment_type") or ""
        author = c.get("author") or ""
        if typ in ("sent", "resolved"):
            # terminal markers: 'sent' (something went out), 'resolved' (closed without sending,
            # e.g. a reject). Either one means the gate is done -> the scan must never re-act.
            sent = True
        elif typ == "draft":
            draft = txt.strip()
        elif typ == "booking":
            # machine booking intent (summary/start/end/...). Present -> approve creates a
            # calendar event before confirming the guest (the booking gate).
            try:
                booking = _json.loads(txt)
            except Exception:
                booking = None
        elif typ == "recipients":
            recipients = _re.findall(r"[\w.+-]+@[\w.-]+\.\w+", txt)
            ms = _re.search(r"SUBJECT:\s*(.+)", txt)
            if ms:
                subject = ms.group(1).strip()
        if "message_id=" in txt and "dir=in" in txt:
            m = _re.search(r"message_id=(\d+)", txt)
            if m:
                inbound_id = int(m.group(1))
        if typ == "approval" and author == APPROVAL_OWNER:
            low = txt.strip().lower()
            if low.startswith("approve"):
                decision = "approve"
            elif low.startswith("reject"):
                decision = "reject"
    return inbound_id, draft, decision, sent, recipients, subject, booking


# Sender for broadcast invitations: an agent that has a real mailbox, so the
# guest sees the business's address. Set BROADCAST_SENDER to choose one; empty
# means the node has not been told, and the broadcast path must refuse rather
# than send as an agent that may not exist here. No slug is named in this file.
BROADCAST_SENDER = os.environ.get("BROADCAST_SENDER", "")


def _agent_delegation_headers(agent):
    secret = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
    if not secret or not agent:
        raise RuntimeError("brin agent delegation proof is not configured")
    return {"Remote-User": agent, "X-SN-Service": "brin",
            "X-SN-Service-Auth": secret, "X-SN-On-Behalf-Of": agent}


async def _release_reply(inbound_id, body):
    r = await _http().post(CORE + "/api/messages/reply",
                           headers=_owner_headers(),
                           json={"to_message_id": inbound_id, "body": body}, timeout=15.0)
    r.raise_for_status()
    return r.json()


async def _send_originate(to, body, subject):
    if not BROADCAST_SENDER:
        raise RuntimeError("BROADCAST_SENDER is not set -- no agent to originate this as")
    r = await _http().post(CORE + "/api/messages/send",
                           headers=_agent_delegation_headers(BROADCAST_SENDER),
                           json={"to": to, "body": body, "subject": subject or "An invitation",
                                 "channel": "email"}, timeout=15.0)
    r.raise_for_status()
    return r.json()


async def _create_calendar_event(intent):
    """Create the calendar event for an approved booking, on the owner's connected
    calendar via core-api (the same loopback endpoint the booking flow's check block
    uses). Sent AS the owner (the super-admin principal)."""
    r = await _http().post(CORE + "/api/integrations/google/calendar/events",
                           headers=_owner_headers(),
                           json={"summary": intent.get("summary", "Booking"),
                                 "start": intent["start"], "end": intent["end"],
                                 "calendar_id": intent.get("calendar_id", "primary"),
                                 "description": intent.get("description", ""),
                                 "location": intent.get("location", "")}, timeout=20.0)
    r.raise_for_status()
    return r.json()


async def approval_tick():
    try:
        # NB: the planner list filter param is `status_filter`, not `status` (an unknown query
        # param is silently ignored -> the whole table comes back). We pass the correct one AND
        # guard client-side below, so a resolved task can never re-enter the candidate set.
        r = await _planner("GET", "/api/v1/tasks?status_filter=queued&wait_reason=approval&limit=50")
        tasks = r.json() if r.status_code == 200 else []
    except Exception as e:
        log.warning("approval_tick fetch failed: %s", e)
        return
    for t in tasks:
        tid = t.get("id")
        # Client-side convergence guard: only act on a still-awaiting task (do not trust the
        # server filter). A resolved/rejected/done task is terminal -> skip it.
        if _place(t) != ("queued", "approval"):
            continue
        if tid in _approval_inflight:
            continue
        # Paused task (incl. a circuit-tripped blocked:stuck) -> the Runner does not touch it.
        if any(x.startswith("blocked:") for x in await _task_tags(tid)):
            continue
        try:
            rc = await _planner("GET", "/api/v1/tasks/%s/comments" % tid)
            comments = rc.json() if rc.status_code == 200 else []
        except Exception:
            continue
        inbound_id, draft, decision, sent, recipients, subject, booking = _parse_gate(comments)
        if sent or decision is None:
            continue
        # Circuit breaker: a correctly resolved task drops out of the scan after one action, so this
        # counter only climbs if the action keeps failing to make the task terminal (the loop class).
        n = _approval_attempts.get(tid, 0) + 1
        _approval_attempts[tid] = n
        if n > APPROVAL_MAX_ATTEMPTS:
            await _add_tag(tid, "blocked:stuck")
            await _emit_task_event(tid, "runner_circuit_tripped",
                                   {"pass": "approval", "attempts": n, "decision": decision})
            await _notify_owner(tid, "A task was auto-paused (stuck in approval)",
                                "The system tried to resolve this task %d times without it closing, "
                                "so it was paused for you to check. Nothing was sent." % n,
                                "circuit_tripped")
            log.warning("approval: task %s tripped circuit breaker after %d attempts -> blocked:stuck",
                        tid, n)
            continue
        _approval_inflight.add(tid)
        try:
            if decision == "reject":
                await _planner("PATCH", "/api/v1/tasks/%s" % tid,
                               json={"status": "closed", "close_outcome": "rejected",
                                     "comment": "Closed: the owner rejected the draft."})
                # 'resolved' is a TERMINAL marker (the parser treats it like 'sent'): the gate is
                # closed even if the status PATCH above is ever lost. This is what the reject path
                # was missing -- approve had two guards (status=done + a marker), reject had none.
                await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                               json={"content": "Owner rejected; draft discarded, nothing sent.",
                                     "comment_type": "resolved"})
                log.info("approval: task %s rejected", tid)
            else:  # approve
                if not draft:
                    log.warning("approval: task %s approved but no draft", tid)
                    continue
                if booking:
                    # booking gate: create the calendar event FIRST, then confirm the guest.
                    # If the create fails, leave the task awaiting (no terminal marker) so it
                    # is retried; the circuit breaker bounds the retries.
                    try:
                        ev = await _create_calendar_event(booking)
                    except Exception as e:
                        log.warning("approval: task %s calendar create failed: %s", tid, e)
                        continue
                    link = ev.get("htmlLink") or ev.get("id") or "created"
                    note = "Booking approved: calendar event created (%s)." % link
                    if inbound_id:
                        # confirm the guest in their thread (telegram/email on the bus).
                        try:
                            res = await _release_reply(inbound_id, draft)
                            note += (" Confirmation released to outbox -> outbound #%s to %s."
                                     % (res.get("outbound_id"), res.get("to")))
                        except Exception as e:
                            log.warning("approval: task %s booking reply release failed: %s", tid, e)
                    await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                                   json={"content": note, "comment_type": "sent"})
                    await _close_done(tid, "Approved by the owner; the runner released it.")
                    log.info("approval: task %s booking confirmed -> %s", tid, link)
                elif inbound_id:
                    # customer-reply gate: release the drafted reply into the outbox.
                    res = await _release_reply(inbound_id, draft)
                    await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                                   json={"content": "Released to outbox -> outbound #%s to %s; delivery will send."
                                         % (res.get("outbound_id"), res.get("to")), "comment_type": "sent"})
                    await _close_done(tid, "Approved by the owner; the runner released it.")
                    log.info("approval: task %s released -> outbound #%s to %s",
                             tid, res.get("outbound_id"), res.get("to"))
                elif recipients:
                    # broadcast gate (e.g. an event invitation): send the draft to each guest.
                    ok = []
                    for to in recipients:
                        try:
                            await _send_originate(to, draft, subject)
                            ok.append(to)
                        except Exception as e:
                            log.warning("approval: broadcast to %s failed: %s", to, e)
                    await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                                   json={"content": "Sent the invitation to %d of %d guest(s): %s"
                                         % (len(ok), len(recipients), ", ".join(ok)), "comment_type": "sent"})
                    await _close_done(tid, "Approved by the owner; the runner released it.")
                    log.info("approval: task %s broadcast to %d/%d guests", tid, len(ok), len(recipients))
                else:
                    # review-only gate (e.g. a compliance report): there is no recipient.
                    # Approval just marks the reviewed draft done; nothing is sent.
                    await _planner("POST", "/api/v1/tasks/%s/comments" % tid,
                                   json={"content": "Reviewed and approved by the owner. Nothing is sent (review-only).",
                                         "comment_type": "sent"})
                    await _close_done(tid, "Approved by the owner; the runner released it.")
                    log.info("approval: task %s reviewed (review-only, no send)", tid)
        except Exception as e:
            log.exception("approval_tick act failed task %s: %s", tid, e)
        finally:
            _approval_inflight.discard(tid)


# --- agent work wait ---
# A flow block that hands work to a real agent posts a work order comment
# ("[work order] <flow>/<block>"), tags the task blocked:agent-work and reports
# WAITING; the engine assigns the agent, which wakes it. While the tag is there
# the Runner does not drive the task. This lifts the tag the moment the task's
# assignee has answered the open order with a comment carrying a VERDICT: line,
# so the flow resumes on the next tick and the block reads that verdict. It only
# lifts a hold: the block, not this, decides what the verdict means.
AGENT_WAIT_TAG = "blocked:agent-work"
_AGENT_WAIT_MACHINE = {"", "unknown-peer", "system", "runner", "component:runner"}


async def agent_wait_tick():
    try:
        tasks = await _drivable_tasks(100)
        for t in tasks:
            if not t.get("workflow_id"):
                continue
            tid = t["id"]
            if AGENT_WAIT_TAG not in await _task_tags(tid):
                continue
            rc = await _planner("GET", "/api/v1/tasks/%s/comments" % tid)
            comments = rc.json() if rc.status_code == 200 else []
            if isinstance(comments, dict):
                comments = comments.get("comments", [])
            idx = None
            for i, c in enumerate(comments):
                first = ((c.get("content") or "").strip().split("\n", 1)[0]).strip()
                if first.startswith("[work order] "):
                    idx = i
                elif first.startswith("[work order closed] "):
                    idx = None
            if idx is None:
                continue
            who = (t.get("assignee") or "").strip()
            for c in comments[idx + 1:]:
                author = (c.get("author") or "").strip()
                if author in _AGENT_WAIT_MACHINE or (who and author != who):
                    continue
                if any(l.strip().upper().startswith("VERDICT:") for l in (c.get("content") or "").splitlines()):
                    r = await _planner("DELETE", "/api/v1/tasks/%s/tags/%s" % (tid, AGENT_WAIT_TAG))
                    log.info("task %s: %s answered the work order, hold lifted (%s)", tid, author, r.status_code)
                    await _emit_task_event(tid, "workflow.agent_answered", {"task_id": tid, "agent": author})
                    break
    except Exception:
        log.exception("agent_wait_tick error")


# --- owner decision lifts a flow gate hold ---
# A flow gate (task-complex) holds its task with blocked:approval while the
# status stays in_progress, so the planner's decision doors (awaiting_approval
# only) cannot answer it. The owner answers with a comment whose first line is
# "DECISION: approve|modify|reject" after the gate's request; this lifts the
# hold so the gate runs again and reads that decision. It never decides.
#
# The request is recognised by its first line, which ends with "approval gate."
# in every flow: "Plan-approval gate.", "Material-approval gate.". This used to
# be the one string "Plan-approval gate.", so a flow whose gate approved
# anything but a plan asked the owner and then never heard the answer.
GATE_REQUEST_ENDS = "approval gate."


def _is_gate_request(text):
    lines = (text or "").strip().splitlines()
    return bool(lines) and lines[0].strip().endswith(GATE_REQUEST_ENDS)


async def gate_decision_tick():
    try:
        tasks = []
        r = await _planner("GET", "/api/v1/tasks?status_filter=in_progress&limit=100")
        if r.status_code == 200:
            tasks = r.json()
        for t in tasks:
            if not t.get("workflow_id"):
                continue
            tid = t["id"]
            if "blocked:approval" not in await _task_tags(tid):
                continue
            rc = await _planner("GET", "/api/v1/tasks/%s/comments" % tid)
            comments = rc.json() if rc.status_code == 200 else []
            if isinstance(comments, dict):
                comments = comments.get("comments", [])
            at = None
            for i, c in enumerate(comments):
                if _is_gate_request(c.get("content")):
                    at = i
            if at is None:
                continue
            for c in comments[at + 1:]:
                if (c.get("author") or "") != APPROVAL_OWNER:
                    continue
                if (c.get("content") or "").strip().upper().startswith("DECISION:"):
                    r = await _planner("DELETE", "/api/v1/tasks/%s/tags/blocked:approval" % tid)
                    log.info("task %s: owner decision recorded, gate hold lifted (%s)", tid, r.status_code)
                    break
    except Exception:
        log.exception("gate_decision_tick error")


# Set when core-api says something that may be work has arrived (a message, a
# wake request, a reminder): the loop looks at once instead of after POLL, which
# made every wake wait 2.5 s on average (2026-10-29). POLL stays as the fallback
# for a dropped stream and for work that sends no signal.
_work = asyncio.Event()


async def _listen_wakes():
    while True:
        try:
            async with _http().stream("GET", CORE + "/api/events/wakes", headers=_service_headers(),
                                      timeout=httpx.Timeout(10.0, read=60.0)) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        _work.set()
        except Exception as e:
            log.warning("wake stream dropped (%s); polling every %ds until it is back", e, POLL)
        await asyncio.sleep(3)


async def main():
    log.info("Runner started (K=%d poll=%ds dry_run=%s core=%s)", K, POLL, DRY_RUN, CORE)
    if not PLANNER:
        log.warning("PLANNER_API_URL is not set: tracking is not installed, "
                    "flows, approvals, waits and gates are off; agents still launch")
    await _adopt_claims(_http())
    asyncio.create_task(_listen_wakes())
    while True:
        _work.clear()   # a signal from now on means another look
        try:
            # Ahead of tick() so an agent whose background turn has just ended
            # is free, and therefore in with-pending-work, on this very poll.
            await _sweep_held_claims(_http())
            await tick()
            if PLANNER:
                await flow_tick()
                await approval_tick()
                await agent_wait_tick()
                await gate_decision_tick()
        except Exception:
            log.exception("tick error")
        try:
            await asyncio.wait_for(_work.wait(), timeout=POLL)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
