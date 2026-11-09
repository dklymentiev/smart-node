"""Telegram channel adapter -- Smart Node communication component.

Long-polls Telegram; every inbound text message becomes one immutable
`messages` row (direction inbound). The B02 AFTER INSERT trigger seeds
`message_state` (status 'new'); a trigger -- brin or the secretary --
takes it from there.

Transport only -- no reasoning, no LLM. Outbound delivery is `deliver()`,
called by the dispatch layer (the secretary / brin), which also records
the outbound row.

Run as a daemon:  python -m adapters.telegram.adapter
"""
import os
import time

from common import db
from common.config import LOG, STATE_DIR, require
from adapters.telegram import transport

CHANNEL_TYPE = "telegram"
_OFFSET_FILE = os.path.join(STATE_DIR, "telegram_offset")


def _channel_id():
    """The bot's own identity -- the `channel_id` for every row this
    adapter writes (a logged shake-out bug was writing the literal
    'telegram' here instead)."""
    return require("TG_CHANNEL_ID")


def _load_offset():
    """Last saved getUpdates offset, or None on a first-ever run."""
    try:
        return int(open(_OFFSET_FILE).read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_offset(offset):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_OFFSET_FILE, "w") as fh:
        fh.write(str(offset))


def _drain():
    """First run: skip any backlog so the node only ingests fresh traffic."""
    updates = transport.get_updates(-1, timeout=0)
    offset = updates[-1]["update_id"] + 1 if updates else 0
    _save_offset(offset)
    LOG(CHANNEL_TYPE, "drained %d backlog update(s), offset=%d"
        % (len(updates), offset))
    return offset


def _ingest(update):
    """Turn one Telegram update into an inbound messages row."""
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    tg_message_id = msg["message_id"]
    external_id = "%s:%s" % (chat_id, tg_message_id)
    row_id = db.record_inbound(
        channel_type=CHANNEL_TYPE,
        channel_id=_channel_id(),
        external_id=external_id,
        thread_id=str(chat_id),
        sender=str(msg["from"]["id"]),
        recipient=_channel_id(),
        content=msg["text"],
        metadata={"chat_id": chat_id, "tg_message_id": tg_message_id},
    )
    if row_id:
        LOG(CHANNEL_TYPE, "inbound -> messages id=%s ext=%s: %r"
            % (row_id, external_id, msg["text"][:60]))


def deliver(chat_id, text, thread_id=None, agent=None):
    """Send an outbound message and record it in the registry. Returns the
    Telegram message_id.

    Note the fast-dispatch layer does NOT use this: it queues the reply as an
    outbound row for send-outbound to transmit. This is the direct-send path,
    which transmits first and therefore settles the row itself."""
    tg_message_id = transport.send(chat_id, text)
    external_id = "%s:%s" % (chat_id, tg_message_id)
    row_id = db.record_outbound(
        channel_type=CHANNEL_TYPE,
        channel_id=_channel_id(),
        external_id=external_id,
        thread_id=thread_id or str(chat_id),
        sender=_channel_id(),
        recipient=str(chat_id),
        content=text,
        metadata={"chat_id": chat_id, "tg_message_id": tg_message_id},
        agent=agent,
    )
    if row_id:
        db.mark_transmitted(CHANNEL_TYPE, row_id, str(chat_id),
                            actor=db.adapter_actor(CHANNEL_TYPE))
    return tg_message_id


def main():
    """Long-poll loop -- ingest forever."""
    LOG(CHANNEL_TYPE, "adapter up, channel=%s" % _channel_id())
    offset = _load_offset()
    if offset is None:
        offset = _drain()
    while True:
        try:
            for update in transport.get_updates(offset):
                _ingest(update)
                offset = update["update_id"] + 1
                _save_offset(offset)
        except Exception as exc:  # transport hiccup -- log, back off, retry
            LOG(CHANNEL_TYPE, "ERROR %r" % exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
