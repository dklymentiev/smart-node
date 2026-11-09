"""Slack transport.

Two delivery modes, chosen by what is configured:
- bot token (chat.postMessage): honors a per-message username + icon, so each
  agent shows its OWN name/avatar in the shared channel. Needs SLACK_BOT_TOKEN
  (scopes chat:write + chat:write.customize) and SLACK_CHANNEL_ID, with the bot
  invited to that channel.
- incoming webhook (fallback): one fixed channel, the app identity only -- an
  app webhook ignores username overrides.

Transport is wire mechanics only.
"""
import requests

from common.config import get, require

_POST = "https://slack.com/api/chat.postMessage"


def send(text, username=None, icon_emoji=None):
    """Deliver one message. Uses the bot token (per-message identity) when
    SLACK_BOT_TOKEN is set, otherwise the incoming webhook."""
    token = get("SLACK_BOT_TOKEN")
    if token:
        payload = {"channel": require("SLACK_CHANNEL_ID"), "text": text}
        if username:
            payload["username"] = username
        if icon_emoji:
            payload["icon_emoji"] = icon_emoji
        resp = requests.post(
            _POST,
            headers={"Authorization": "Bearer " + token},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError("slack chat.postMessage failed: %s"
                               % body.get("error"))
        return body
    # Fallback: incoming webhook -- one fixed channel, app identity.
    resp = requests.post(require("SLACK_WEBHOOK_URL"),
                         json={"text": text}, timeout=20)
    resp.raise_for_status()
    return resp.text
