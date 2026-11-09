"""Telegram transport -- the Bot API calls.

Long-poll for inbound updates, send for outbound. Nothing else lives
here: transport is wire mechanics, no registry, no reasoning.
"""
import requests

from common.config import require

_API = "https://api.telegram.org/bot" + require("TG_TOKEN")


def get_updates(offset, timeout=25):
    """Long-poll getUpdates from `offset`. Returns the update list."""
    resp = requests.get(
        _API + "/getUpdates",
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_chat_action(chat_id, action="typing"):
    """Show the channel's own activity hint (the "typing..." line).

    Telegram clears it after about five seconds, so a caller that wants it held
    open for a long turn has to repeat it -- see dispatch._activity().

    Best-effort BY CONTRACT: the hint is a courtesy and must never be able to
    fail a turn, so this returns False instead of raising. Everything else in
    this module raises for status on purpose; this is the one call whose
    failure means nothing.
    """
    try:
        resp = requests.post(
            _API + "/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


def send(chat_id, text):
    """Send a text message. Returns the Telegram message_id."""
    resp = requests.post(
        _API + "/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["result"]["message_id"]
