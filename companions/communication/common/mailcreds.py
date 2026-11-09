"""Per-mailbox credentials -- Smart Node communication component.

A node holds several agent mailboxes on one mail server, and every send must
authenticate AS the mailbox it claims to be from: the server refuses a From it
is not logged in as, and an identity that does not match the box the customer
wrote to is a support incident even when the server accepts it.

The passwords live in the generate-once store (AGENT_ACCOUNTS_FILE), keyed
PW_<address with @ and . replaced by _>. EmailTransport reads its identity from
the environment at construction, and every caller builds a fresh transport, so
setting the environment per mailbox immediately before the send is all that is
needed to reuse the single-mailbox transport for many boxes.

This module exists because that swap was implemented three times (the poller,
the outbound sender, and nowhere at all in the dispatch path -- which is how a
node answered a customer as the wrong mailbox entirely).
"""
import os

from common.config import get

DEFAULT_CREDS_FILE = "/opt/smart-node/mail/agent-accounts.env"


def cred_key(addr):
    """Address -> the key it is stored under (PW_alex_smart-node_app)."""
    return "PW_" + (addr or "").replace("@", "_").replace(".", "_")


def creds_file():
    """The configured store, or the installer's default location."""
    return get("AGENT_ACCOUNTS_FILE", DEFAULT_CREDS_FILE)


def load_passwords(path=None):
    """Parse the generate-once creds file into {PW_key: password}.

    A missing file is not fatal here: the caller decides, because a node with a
    single mailbox configured inline is a valid (if legacy) shape.
    """
    out = {}
    try:
        with open(path or creds_file()) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def apply_mailbox_env(addr, passwords=None, path=None):
    """Point the environment at ONE mailbox, so the next EmailTransport()
    authenticates and signs as `addr`.

    Raises when the mailbox has no password: sending from a different identity
    than the one addressed is worse than not sending, because it fails silently
    (the server accepts it) and the customer sees a stranger's address.
    """
    if not addr or "@" not in addr:
        raise RuntimeError("refusing to send: no mailbox identity (%r)" % addr)
    passwords = passwords if passwords is not None else load_passwords(path)
    password = passwords.get(cred_key(addr))
    if not password:
        raise RuntimeError(
            "no password for mailbox %s in %s -- refusing to send as a "
            "different identity" % (addr, path or creds_file()))
    os.environ["IMAP_USER"] = addr
    os.environ["IMAP_PASSWORD"] = password
    os.environ["SMTP_USER"] = addr
    os.environ["SMTP_PASSWORD"] = password
    os.environ["SMTP_FROM"] = addr
    os.environ["EMAIL_CHANNEL_ID"] = addr
    return addr
