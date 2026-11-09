#!/usr/bin/env python3
"""Multi-mailbox inbound poller -- Smart Node communication component.

One pass: read every enabled email mailbox from sn_config.agent_channels,
poll each over IMAP, and record every UNSEEN message into the core
registry (sn_core.messages) as an immutable inbound row -- agent-agnostic
(recipient = the To address; no agent binding). Topology: real per-agent
mailbox + ONE poller iterating the mailbox list from the DB, so a new
agent mailbox is picked up automatically with no new systemd unit.

Per-mailbox IMAP/SMTP credentials come from the mail companion's
generate-once store (AGENT_ACCOUNTS_FILE). Shared server settings
(host/port/TLS, DB DSN) come from the environment (systemd
EnvironmentFile / sibling .env).

Cron-invoked, one pass per tick. Run from this directory so `common`
and `adapters` resolve as packages:  python poll_mailboxes.py
"""
import os

from common import db
from common.config import LOG, get

CREDS_FILE = get("AGENT_ACCOUNTS_FILE",
                 "/opt/smart-node/mail/agent-accounts.env")


def _cred_key(addr):
    """The PW_ key the mail companion stored for an address (@ and . -> _)."""
    return "PW_" + addr.replace("@", "_").replace(".", "_")


def _load_passwords(path):
    """Parse the generate-once creds file into {PW_key: password}."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                out[key.strip()] = val.strip()
    except FileNotFoundError:
        LOG("email-multi", "creds file not found: %s" % path)
    return out


def _mailboxes():
    """Every enabled email mailbox address from agent_channels."""
    rows = db.query(
        """SELECT ac.instance_ref
             FROM sn_config.agent_channels ac
             JOIN sn_config.channels c ON c.id = ac.channel_id
            WHERE c.slug = 'email' AND ac.enabled AND c.enabled
              AND ac.instance_ref IS NOT NULL
            ORDER BY ac.instance_ref""",
        fetch=True,
    )
    return [r[0] for r in rows]


def _poll_one(addr, password):
    """Set this mailbox's per-account env, then run one adapter pass.

    EmailTransport reads IMAP_USER/PASSWORD/SMTP_FROM at construction and
    run_once() builds a fresh transport each call, so re-setting these
    per mailbox is all that is needed to reuse the single-mailbox adapter.
    """
    os.environ["IMAP_USER"] = addr
    os.environ["IMAP_PASSWORD"] = password
    os.environ["SMTP_USER"] = addr
    os.environ["SMTP_PASSWORD"] = password
    os.environ["SMTP_FROM"] = addr
    os.environ["EMAIL_CHANNEL_ID"] = addr
    from adapters.email import adapter
    return adapter.run_once()


def main():
    passwords = _load_passwords(CREDS_FILE)
    mailboxes = _mailboxes()
    LOG("email-multi", "poll pass start -- %d mailbox(es)" % len(mailboxes))
    total = 0
    for addr in mailboxes:
        pw = passwords.get(_cred_key(addr))
        if not pw:
            LOG("email-multi", "SKIP %s -- no password in %s" % (addr, CREDS_FILE))
            continue
        try:
            recorded = _poll_one(addr, pw)
            total += recorded
            LOG("email-multi", "%s -- %d new" % (addr, recorded))
        except Exception as exc:  # one bad mailbox must not stop the rest
            LOG("email-multi", "ERROR %s -- %s" % (addr, exc))
    LOG("email-multi", "poll pass done -- %d new message(s) recorded" % total)


if __name__ == "__main__":
    main()
