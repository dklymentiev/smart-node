#!/usr/bin/env python3
"""Internal messaging as an MCP server (stdio) -- the transport a narrow agent
was missing.

The node already ships the messaging BACKEND (core-api: POST /api/messages/send,
/reply, /dismiss and GET /api/messages/inbox) and the bus behind it. What was
missing is a way for an agent to CALL it: a business agent has no bash and the
CLI runtime only sees tools listed in its .mcp.json, so
an api-kind catalog row is a contract with no way to execute. This server closes
that gap by wrapping the four existing endpoints -- no new endpoint, no new
state, no external dependency.

Identity is NOT a parameter. The sender is taken from SN_AGENT_SLUG, which
the gateway sets from the agent's own folder name (the same identity the planner
tools use), falling back to the working directory name (`claude` runs with cwd =
/srv/agents/<slug>). The model can therefore neither forget nor spoof who it is
writing as. core-api then confirms it independently: this server reaches it over
the unix socket, where the kernel names the calling process and the slug we send
is only attribution, not the credential.

Transport: stdio, spawned per agent session by the CLI runtime -- the same shape
as the google-calendar / google-drive connectors. Registered once per node by
companions/dispatcher.sh under MCP-server slug `messaging`; an agent gets it by
being granted `messaging`, which renders it into that agent's .mcp.json.

Note: brin's live-chat loop dispatches MCP over SSE only, so this stdio server
serves the CLI/wake path (cron wakes, task work). Live chat reaches colleagues
through the dispatcher as before.

Usage:
    python mcp_messaging.py        # stdio (the only transport)
"""

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

# The MCP SDK renamed its high-level server between majors and nodes are not all
# on the same one: 2.x ships mcp.server.MCPServer, 1.x shipped
# mcp.server.fastmcp.FastMCP. Both expose the same .tool() / .run() surface we
# use, so bind whichever this node has instead of pinning a version.
try:  # SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

# WHY THE SOCKET AND NOT THE ADDRESS. core-api answers on both doors, and
# on the TCP door an agent identity is refused outright: a `Remote-User` there is
# an assertion anything on the node could make, so core-api answers 403 and names
# the socket in the detail. This server used to call the address, which meant
# every messaging tool call on every node returned 403 -- an agent could neither
# read its own inbox nor answer -- and the model reported it as having nothing to
# do. On the socket SO_PEERCRED names the caller, which the caller never sends and
# therefore cannot forge. The MCP is spawned per agent session as that agent's OS
# user (by the gateway), and that user is in `smart-node-agents`, which
# is exactly who the socket admits.
CORE_API_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
# For a test bed with no socket, and nothing else. Deliberately NOT a fallback:
# retrying over TCP would answer 403 and read to the model as an empty inbox,
# which is precisely how this stayed invisible for a whole release cycle.
CORE_API_URL = os.environ.get("SN_CORE_API_URL", "")
TIMEOUT = float(os.environ.get("SN_MESSAGING_TIMEOUT", "30"))

mcp = _McpServer("smart-node-messaging")


def _me() -> str:
    """The calling agent's slug, from the runtime -- never from the model."""
    slug = (os.environ.get("SN_AGENT_SLUG") or "").strip()
    if not slug:
        slug = Path.cwd().name
    return slug


def _send_payload(to: str, body: str, channel: Optional[str] = None,
                  subject: Optional[str] = None,
                  cc: Optional[list[str]] = None,
                  needs_reply: bool = True) -> dict:
    """Build a send request, including the person-facing origin when inherited.

    Claude owns the opaque tool loop, so Brin cannot decorate this call as it
    does for HTTP models. The gateway gives a desk CLI its trusted conversation
    in the environment and this stdio child inherits it. Only a NEW colleague
    message (no explicit channel) receives the origin. A relay necessarily has
    a channel and must not create another relay obligation.
    """
    payload: dict = {"to": to, "body": body}
    if subject is not None:
        payload["subject"] = subject
    if cc is not None:
        payload["cc"] = cc
    # Only a colleague can be held to an answer; a person, a chat or a relay cannot.
    if needs_reply and not channel and "@" not in to and not to.startswith("desk:"):
        payload["needs_reply"] = True
    if channel:
        payload["channel"] = channel
        return payload

    origin_channel = (os.environ.get("SN_MESSAGE_ORIGIN_CHANNEL") or "").strip()
    origin_conversation = (
        os.environ.get("SN_MESSAGE_ORIGIN_CONVERSATION") or ""
    ).strip()
    if origin_channel and origin_conversation:
        payload["origin"] = {
            "channel": origin_channel,
            "conversation": origin_conversation,
        }
    return payload


async def _call(method: str, path: str, body: Optional[dict] = None) -> str:
    # The socket FIRST, and the reason is not preference. SN_CORE_API_URL is set
    # node-wide in /etc/smart-node/core-api.env and the gateway passes its whole
    # environment to the agents it launches, so on a real node the variable is
    # always present. Asking for it first therefore sent every agent over TCP,
    # where core-api refuses an agent identity outright: the kernel cannot prove
    # a caller there. The comments in this file already said the variable is for
    # a test bed with no socket and is "deliberately NOT a fallback" -- this is
    # the code finally saying the same. mcp_secrets.py has always had it this way.
    if os.path.exists(CORE_API_SOCKET):
        transport, base = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET), "http://localhost"
    elif CORE_API_URL:
        transport, base = None, CORE_API_URL.rstrip("/")
    else:
        # Name the missing door. The alternative -- falling through to the address
        # -- produces a 403 that the model reads as an empty inbox.
        return json.dumps(
            {"error": "no core-api socket",
             "detail": f"{CORE_API_SOCKET} does not exist, and an agent identity "
                       f"is accepted only there. SN_CORE_API_URL is for a test "
                       f"bed with no socket."},
            ensure_ascii=False,
        )
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.request(
            method,
            f"{base}/api/messages{path}",
            headers={"Remote-User": _me(), "Content-Type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            return json.dumps(
                {"error": f"HTTP {r.status_code}", "detail": r.text[:400]},
                ensure_ascii=False,
            )
        return json.dumps(r.json() if r.content else {}, ensure_ascii=False)


@mcp.tool()
async def send_message(to: str, body: str, channel: Optional[str] = None,
                       subject: Optional[str] = None,
                       cc: Optional[list[str]] = None,
                       needs_reply: bool = True) -> str:
    """Start a new colleague message or email, or relay an answer to your requester.

    Email is queued for outbound delivery; every To and Cc address must be a
    granted contact for this agent on email. A missing grant blocks delivery.

    Args:
        to: a colleague's agent slug, or an email address containing '@'. For a
            relay listed in to_relay, use that item's origin conversation (your
            own chat), never another agent's chat. To answer an existing message
            or email thread, use inbox_reply instead.
        body: what you are telling or asking them. Asking for something, say what
            result you expect and what to do with it: who gets it and how.
        channel: for a relay only -- the origin channel (e.g. 'telegram').
                 Omit it for a colleague; email is inferred from an '@' address.
        subject: optional subject for a new email thread.
        cc: optional list of email copy addresses; each needs a contact grant.
        needs_reply: colleague messages only, on by default: the colleague has to
            answer. The node keeps your message open until they reply or dismiss it,
            reminds them, and tells you if they stay silent. After sending, do not
            wait or poll for the answer: end your turn. The reply, or the node's
            notice that there was none, arrives in your inbox and wakes you. Turn
            it off (false) only for information that needs no answer.
    """
    return await _call("POST", "/send",
                       _send_payload(to, body, channel, subject, cc, needs_reply))


@mcp.tool()
async def inbox_read(thread_id: Optional[str] = None, limit: int = 10) -> str:
    """Check what is waiting for you: new messages and answers to requests you sent.

    Without arguments it returns ONLY the messages still waiting for you, each with
    its thread_id, and any answers you must relay. Older, already handled mail is
    not included. When you need the earlier conversation of one message, call it
    again with that message's thread_id to get that one thread's recent history.
    Never narrate its contents to the person you are serving.

    Args:
        thread_id: read the history of this one conversation instead.
        limit: with thread_id, how many of its latest messages (at most 50).
    """
    # WHY NOT THE WHOLE WINDOW. This used to return the last 20 messages
    # in every state, bodies and metadata included. Everything a tool returns
    # stays in the session and is re-read by every later request: on example-node this
    # one call was 38% of all the text agents read in a day, and 19 of a typical
    # 20 rows were mail already handled. History is asked for, not handed over.
    if thread_id:
        query = urlencode({"view": "thread", "thread_id": thread_id,
                           "limit": max(1, min(int(limit or 10), 50))})
    else:
        query = urlencode({"view": "new", "limit": 50})
    return await _call("GET", "/inbox?" + query)


@mcp.tool()
async def inbox_reply(to_message_id: int, body: str,
                      cc: Optional[list[str]] = None,
                      only_sender: bool = False) -> str:
    """Answer one message, including email, in its existing thread and close it.

    Email replies keep the thread headers and the original audience by default.
    Every external recipient, including Cc, must be a granted contact. An
    ungranted address is blocked by the outbound contact gate.

    Args:
        to_message_id: id of the inbound message you are answering.
        body: your reply.
        cc: optional explicit email copy list, replacing the default audience.
            If supplied, this takes precedence over only_sender.
        only_sender: for email, reply privately to the sender when cc is omitted.
    """
    payload = {"to_message_id": int(to_message_id), "body": body}
    if cc is not None:
        payload["cc"] = cc
    if only_sender:
        payload["only_sender"] = True
    return await _call(
        "POST", "/reply", payload
    )


@mcp.tool()
async def inbox_dismiss(message_id: int) -> str:
    """Close an internal message WITHOUT replying (a thanks or acknowledgment).

    Args:
        message_id: id of the message to close.
    """
    return await _call("POST", "/dismiss", {"message_id": int(message_id)})


@mcp.tool()
async def chat_history(message_id: int, limit: int = 20,
                       before: Optional[int] = None) -> str:
    """Read the Studio chat a mention came from.

    Use it when a message in your inbox says a person mentioned you in a Studio
    chat and you need the conversation that led to the question. Pass that
    message's id. It returns the chat's latest turns, oldest first, each with an
    index: role "user" is the person, role "assistant" is the chat's own agent
    or a colleague's answer ("Answer from <name>"). Start with the latest turns;
    ask for older ones (before=<the smallest index you have>) only when you need
    them. It works for chat mentions addressed to you, and for nothing else.

    Args:
        message_id: id of the mention message in your inbox.
        limit: how many turns (at most 100).
        before: return the turns before this index (older ones).
    """
    query = {"limit": max(1, min(int(limit or 20), 100))}
    if before is not None:
        query["before"] = int(before)
    return await _call("GET", "/%d/chat-history?%s" % (int(message_id), urlencode(query)))


@mcp.tool()
async def message_search(q: str, identity: Optional[str] = None,
                         channel: Optional[str] = None, since: Optional[str] = None,
                         until: Optional[str] = None, limit: int = 20) -> str:
    """Find what was said: search the text of your conversations by words.

    Use it when you remember a message, a decision or a promise by its words and
    not by where it was said, and BEFORE you tell anyone you cannot find it or
    that it was never said. To search more than conversations, use recall.

    Type 2 to 5 distinct words you expect in the message, in any order and in any
    form. One or two words must all occur; a longer query needs about three in five
    of them, and the messages holding the most words come first. Do NOT add since or
    until unless the person gave you a date: a guessed window silently hides the
    answer. Without `identity` it searches every conversation you are part of
    (email, telegram, web chat, voice, internal mail); with `identity` (an email, a
    handle or a name) only that person's. Each row carries its channel, sender,
    recipient, time and text.

    Args:
        q: the words to find, for example "contacts document created".
        identity: limit to one person: their email, handle or name.
        channel: limit to one channel: telegram, email, web-chat, voice, internal.
        since: only messages from this ISO date or datetime on; leave empty unless given a date.
        until: only messages up to this ISO date or datetime; leave empty unless given a date.
        limit: how many to return, best match first (at most 100).
    """
    payload = {"q": q, "limit": max(1, min(int(limit or 20), 100))}
    for key, val in (("identity", identity), ("channel", channel),
                     ("since", since), ("until", until)):
        if val:
            payload[key] = val
    return await _call("POST", "/search", payload)


@mcp.tool()
async def recall(q: str, limit: int = 5) -> str:
    """Look something up across everything you know, in ONE call: your tasks, your
    conversations (email, telegram, chat, voice, internal mail) and your own memory.

    Use it INSTEAD OF guessing where to look. Typical questions it answers: "where
    is the list / document / table of X", "did we ever discuss X", "what did I
    send to whom about X", "who decided X", "what was promised about X". Call it
    BEFORE mesh_search, planner_search, listing folders by guessed names, and before
    you tell anyone you cannot find something or that it does not exist.

    It returns the candidates from every source side by side, so you see all the
    places a thing appears and not only the first one you thought of. If more than
    one thing could be what was meant (two lists, two documents, two people), say
    so to the person and ask which; never answer with the first plausible one. Type
    3 to 6 distinct words in any order and in any form ("contact" finds
    "contacts"); no punctuation, no sentences. The time window starts at three
    days and widens by itself; the answer names it. Each source says whether it was
    read, and "nothing found" is only true when every searched source was read.
    Documents (Doci) are not searched by this call: if the thing may be a document,
    list the likely folder as well.

    Args:
        q: the words you remember, for example "contacts list outreach".
        limit: hits per source (at most 20).
    """
    return await _call("POST", "/recall", {"q": q, "limit": max(1, min(int(limit or 5), 20))})


if __name__ == "__main__":
    mcp.run()
