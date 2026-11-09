"""Email transport -- IMAP poll for inbound, SMTP send for outbound.

Stdlib only (imaplib / smtplib / email) -- no external dependency.
Synchronous: the email adapter is cron-invoked, one pass per tick, so
there is nothing to gain from async here. IMAP/SMTP mechanics and the
header/body parsing are harvested from brin-client-email's EmailService.
"""
import email
import email.header
import email.utils
import html
import imaplib
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from common.config import get, require


def _ssl_context():
    """TLS context for the mail server. The Smart Node mail companion
    serves a self-signed cert on the internal VPN; EMAIL_TLS_VERIFY=0
    (the value the communication installer writes for that case) drops
    cert + hostname verification. It defaults to a verifying context, so
    a public mailbox with a real cert needs no extra setting."""
    ctx = ssl.create_default_context()
    if get("EMAIL_TLS_VERIFY", "1") == "0":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _decode_header(raw):
    """Decode an RFC 2047 encoded header to a plain string."""
    if not raw:
        return ""
    out = []
    for data, charset in email.header.decode_header(raw):
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(data)
    return " ".join(out)


def _extract_address(header):
    """Pull the bare email address out of a From/To header."""
    match = re.search(r"<([^>]+)>", header or "")
    if match:
        return match.group(1).lower()
    addr = (header or "").strip().lower()
    return addr if "@" in addr else ""


def _extract_name(header):
    """Pull the display name out of a From/To header."""
    decoded = _decode_header(header)
    match = re.match(r'^"?([^"<]+)"?\s*<', decoded)
    return match.group(1).strip() if match else ""


def _arrived_at(envelope, msg):
    """When this message actually landed in the mailbox, as an aware datetime.

    Two sources, in this order, and the order is the point:

      1. IMAP INTERNALDATE -- the SERVER's record of when it accepted the
         message. The sender cannot influence it, and it survives being read
         months later, which is exactly the case this exists for: telling a
         mailbox's accumulated backlog apart from mail that arrived a minute
         ago. `imaplib.Internaldate2tuple` parses it out of the FETCH envelope
         line and converts to local time, so mktime returns the right epoch.
      2. The `Date` header -- sender-controlled, therefore second. It is
         nonetheless better than nothing: a server that omits INTERNALDATE
         would otherwise leave every message looking brand new.

    Returns None when neither is readable. The caller decides what that means;
    it must not silently become "now", because "now" is the one answer that
    makes a backlog look live.
    """
    if envelope:
        try:
            tt = imaplib.Internaldate2tuple(
                envelope if isinstance(envelope, bytes) else str(envelope).encode())
            if tt:
                return datetime.fromtimestamp(time.mktime(tt), tz=timezone.utc)
        except Exception:
            pass
    raw_date = msg.get("Date")
    if raw_date:
        try:
            parsed = email.utils.parsedate_to_datetime(raw_date)
            if parsed is not None:
                # A Date header may carry no zone at all (-0000 or none). Read
                # it as UTC rather than as node-local: guessing the node's zone
                # would shift the message by hours in either direction.
                return (parsed if parsed.tzinfo
                        else parsed.replace(tzinfo=timezone.utc))
        except Exception:
            pass
    return None


def _get_body(msg):
    """Extract a plain-text body, flattening HTML when that is all there is."""
    def _strip_html(raw):
        raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
        raw = re.sub(r"<p[^>]*>", "\n", raw, flags=re.IGNORECASE)
        raw = re.sub(r"<[^>]+>", "", raw)
        return re.sub(r"\n{3,}", "\n\n", html.unescape(raw)).strip()

    def _clean(text):
        """Remove NUL bytes. Postgres refuses a string containing 0x00, and the
        insert failure aborted the whole poll pass for that mailbox -- one DMARC
        report with a zip attachment left a live mailbox unread for a day, which
        looks exactly like "the poller is fine, nobody wrote"."""
        return text.replace("\x00", "")

    def _decode(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return _clean(payload.decode(charset, errors="replace"))

    if msg.is_multipart():
        text_body = None
        html_body = ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp or not ctype.startswith("text/"):
                # Attachment parts are extracted separately by _extract_attachments
                # and stored via the attachment store. Skip them here so _get_body
                # returns only the human-readable prose.
                continue
            if ctype == "text/plain" and text_body is None:
                text = _decode(part)
                if text.strip():
                    text_body = text.strip()
            elif ctype == "text/html" and not html_body:
                html_body = _strip_html(_decode(part))
        return text_body if text_body is not None else html_body

    ctype = msg.get_content_type()
    # A single-part message that is not text at all (Google sends its DMARC
    # aggregate reports as a bare application/zip) decodes to binary. Describe
    # it instead: the row is still evidence that something arrived, and nothing
    # downstream has to handle a zip pretending to be a sentence.
    if not ctype.startswith("text/"):
        return "[non-text message: %s]" % ctype
    text = _decode(msg)
    if ctype == "text/html":
        return _strip_html(text)
    return text.strip()


def _extract_attachments(msg):
    """Return a list of raw attachment dicts from a MIME message.

    Each dict has: filename, content_type, data (bytes).
    Only genuine attachment parts are returned -- multipart/* container nodes
    and the inline text/plain / text/html body parts are excluded. Parts with
    no payload are skipped.

    The caller (adapter) applies the size and count caps, writes bytes to disk,
    and inserts the metadata rows -- this function only parses the MIME tree.
    """
    if not msg.is_multipart():
        # A single-part non-text message (e.g. a bare application/zip) is
        # itself the attachment.
        ctype = msg.get_content_type()
        if not ctype.startswith("text/") and not ctype.startswith("multipart/"):
            data = msg.get_payload(decode=True)
            if data:
                return [{
                    "filename": msg.get_filename() or "",
                    "content_type": ctype,
                    "data": data,
                }]
        return []

    parts = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("multipart/"):
            continue  # container node; walk() descends into it
        disp = str(part.get("Content-Disposition", ""))
        is_attachment = "attachment" in disp or not ctype.startswith("text/")
        if not is_attachment:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        parts.append({
            "filename": part.get_filename() or "",
            "content_type": ctype,
            "data": data,
        })
    return parts


class EmailTransport:
    """One mailbox: IMAP poll + SMTP send. Config from the environment."""

    def __init__(self):
        self.imap_host = require("IMAP_HOST")
        self.imap_port = int(get("IMAP_PORT", "993"))
        self.imap_user = require("IMAP_USER")
        self.imap_password = require("IMAP_PASSWORD")
        self.imap_folder = get("IMAP_FOLDER", "INBOX")
        self.smtp_host = require("SMTP_HOST")
        self.smtp_port = int(get("SMTP_PORT", "587"))
        self.smtp_user = get("SMTP_USER", "")
        self.smtp_password = get("SMTP_PASSWORD", "")
        self.smtp_from = require("SMTP_FROM")
        self.ssl_ctx = _ssl_context()

    def _imap_connect(self):
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port,
                                 ssl_context=self.ssl_ctx)
        conn.login(self.imap_user, self.imap_password)
        conn.select(self.imap_folder)
        return conn

    def poll_unseen(self):
        """Fetch every UNSEEN message. Returns a list of plain dicts.
        BODY.PEEK[] is used so fetching does not flip the Seen flag --
        mark_seen() is an explicit step after a message is recorded.

        INTERNALDATE is fetched alongside the body and is the message's real
        arrival time -- see _arrived_at. Without it the only timestamp anything
        downstream has is "when we happened to poll", which makes a mailbox's
        whole backlog indistinguishable from mail that arrived this minute.
        """
        conn = self._imap_connect()
        try:
            _, data = conn.search(None, "UNSEEN")
            if not data or not data[0]:
                return []
            messages = []
            for uid_bytes in data[0].split():
                uid = uid_bytes.decode()
                _, fetched = conn.fetch(uid_bytes, "(INTERNALDATE BODY.PEEK[])")
                if not fetched or not fetched[0]:
                    continue
                # A FETCH response is (envelope_line, raw_bytes); INTERNALDATE
                # rides on the envelope line, the body in the second element.
                envelope = fetched[0][0]
                messages.append(self._parse(uid, fetched[0][1], envelope))
            return messages
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _parse(uid, raw_bytes, envelope=None):
        msg = email.message_from_bytes(raw_bytes)
        return {
            "uid": uid,
            "arrived_at": _arrived_at(envelope, msg),
            "message_id": (msg.get("Message-ID") or "").strip(),
            "from_addr": _extract_address(_decode_header(msg.get("From", ""))),
            "from_name": _extract_name(msg.get("From", "")),
            "to_addr": _extract_address(_decode_header(msg.get("To", ""))),
            "subject": _decode_header(msg.get("Subject", "")),
            "body": _get_body(msg),
            # Raw attachment parts extracted from the MIME tree. The adapter
            # applies caps, writes bytes to disk, and records metadata rows.
            # Each entry: {filename, content_type, data (bytes)}.
            "attachments": _extract_attachments(msg),
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "references": (msg.get("References") or "").strip(),
            # Who else was on this message. A reply that keeps only the sender
            # quietly drops everyone who was copied, and the person who put
            # them there is the last to learn that their colleague was cut out
            # of the thread. Parsed here because this is the only place that
            # still has the raw headers.
            "to_addrs": [a for _n, a in
                         email.utils.getaddresses(msg.get_all("To") or []) if a],
            "cc_addrs": [a for _n, a in
                         email.utils.getaddresses(msg.get_all("Cc") or []) if a],
            # Signals that this message came from machinery rather than a person.
            # Carried into the row's metadata so the reply decision can be made
            # by whichever path picks the row up later.
            "auto_submitted": (msg.get("Auto-Submitted") or "").strip(),
            "precedence": (msg.get("Precedence") or "").strip(),
            "return_path": (msg.get("Return-Path") or "").strip(),
            "x_failed_recipients": (msg.get("X-Failed-Recipients") or "").strip(),
            "list_id": (msg.get("List-Id") or "").strip(),
        }

    def mark_seen(self, uid):
        """Flag a UID as Seen -- called only after it is safely recorded."""
        conn = self._imap_connect()
        try:
            conn.store(uid.encode(), "+FLAGS", "\\Seen")
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    def send(self, to, subject, body, in_reply_to=None, references=None,
             message_id=None, html=None, from_name=None, bcc=None, cc=None):
        """Send a plain-text email. Returns the generated Message-ID.
        from_name sets the From display name (e.g. the business name) so the
        recipient sees the business, not the bare node mailbox address.
        bcc, if set, is added to the SMTP envelope only (no Bcc header), so the
        owner silently receives a copy without the recipient seeing the address.
        cc, a list, is the opposite: it goes in the envelope AND in a Cc header,
        because the point of a copy is that everyone can see who has it."""
        msg = MIMEMultipart("alternative")
        msg["From"] = email.utils.formataddr((from_name, self.smtp_from)) if from_name else self.smtp_from
        msg["To"] = to
        _cc = [a for a in (cc or []) if a and a != to]
        if _cc:
            msg["Cc"] = ", ".join(_cc)
        msg["Subject"] = subject
        message_id = message_id or email.utils.make_msgid(
            domain=self.smtp_from.split("@")[-1])
        msg["Message-ID"] = message_id
        msg["Date"] = email.utils.formatdate(localtime=True)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as server:
            server.starttls(context=self.ssl_ctx)
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            envelope = [to] + _cc + ([bcc] if (bcc and bcc != to and bcc not in _cc) else [])
            server.sendmail(self.smtp_from, envelope, msg.as_string())
        return message_id
