"""Slack channel adapter (Socket Mode) -- Smart Node communication.

Mirror of the telegram adapter, inbound half. Connects OUT to Slack over
Socket Mode (no public URL needed), and turns every inbound channel
message into one immutable `messages` row (direction inbound). The B02
AFTER INSERT trigger seeds `message_state` (status new); the Runner /
dispatch routes it onward to the bound agent. Transport/identity only --
no reasoning, no LLM. Outbound delivery is send_outbound._send_slack
(chat.postMessage), not here.

Needs (env / .env):
  SLACK_BOT_TOKEN   xoxb-...  scope channels:history (read the channel)
  SLACK_APP_TOKEN   xapp-...  scope connections:write (open the socket)
  SLACK_CHANNEL_ID  C....     the one channel this node listens on
plus: Socket Mode enabled, the app subscribed to message.channels, and
the bot a member of SLACK_CHANNEL_ID.

Run as a daemon:  python -m adapters.slack.adapter
"""
import time

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

from common import db
from common.config import LOG, require

CHANNEL_TYPE = "slack"


def _channel_id():
    """The channel this node listens on. Also the inbound `recipient`, which
    agent_channels.instance_ref maps to the bound agent (mirrors the role of
    TG_CHANNEL_ID for telegram)."""
    return require("SLACK_CHANNEL_ID")


def _ingest(event):
    """Turn one Slack message event into an inbound messages row.

    Skips anything that is not a plain user message on our channel: subtypes
    (joins, edits, bot_message), bot posts (our own agents -- avoids an echo
    loop), and messages on other channels."""
    if event.get("type") != "message":
        return
    if event.get("subtype"):
        return
    if event.get("bot_id"):
        return
    channel = event.get("channel")
    if channel != _channel_id():
        return
    ts = event.get("ts")
    if not ts:
        return
    text = event.get("text") or ""
    user = event.get("user") or "unknown"
    external_id = "%s:%s" % (channel, ts)
    thread_ts = event.get("thread_ts") or ts
    row_id = db.record_inbound(
        channel_type=CHANNEL_TYPE,
        channel_id=_channel_id(),
        external_id=external_id,
        thread_id=thread_ts,
        sender=user,
        recipient=_channel_id(),
        content=text,
        metadata={"channel": channel, "ts": ts, "user": user},
    )
    if row_id:
        LOG(CHANNEL_TYPE, "inbound -> messages id=%s ext=%s: %r"
            % (row_id, external_id, text[:60]))


def _handler(client, req):
    """Socket Mode listener: ACK first (Slack requires it within 3s), then
    ingest. Only Events API envelopes carry channel messages."""
    if req.type != "events_api":
        return
    client.send_socket_mode_response(
        SocketModeResponse(envelope_id=req.envelope_id))
    try:
        _ingest((req.payload or {}).get("event") or {})
    except Exception as exc:  # one bad event must not kill the socket
        LOG(CHANNEL_TYPE, "ERROR ingest %r" % exc)


def main():
    LOG(CHANNEL_TYPE, "adapter up, channel=%s" % _channel_id())
    client = SocketModeClient(
        app_token=require("SLACK_APP_TOKEN"),
        web_client=WebClient(token=require("SLACK_BOT_TOKEN")),
    )
    client.socket_mode_request_listeners.append(_handler)
    client.connect()                 # SDK keeps it alive + auto-reconnects
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
