"""A file mailed to an agent is announced in the one shape the dispatcher opens.

The email adapter appends one line per attachment to the message body. brin
(core/attachment_delivery.py) rewrites the trailing lines that match its
composer marker into the exact command that fetches the file for the turn; a
line in any other shape reaches the model as plain text. The adapter wrote
"[attachment: report.csv (32 KB, text/csv)]", which matches nothing, so an agent
was told a file had arrived and never how to open it.

These tests hold the two sides together: the line the ingest writes is parsed
by the dispatcher's own marker, back into the same name, type, size and id.
"""
import importlib.util
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

DELIVERY = COMPONENT.parent / "brin" / "core" / "attachment_delivery.py"


def _delivery():
    spec = importlib.util.spec_from_file_location("brin_attachment_delivery", DELIVERY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod     # dataclasses resolves annotations through it
    spec.loader.exec_module(mod)
    return mod


def _mailed(filename, content_type, data):
    from common import attachments
    return attachments.prepare_attachment(filename, content_type, data)


def test_the_summary_line_is_the_dispatcher_marker():
    from common.attachments import attachment_summary_line
    att = _mailed("Accounts Sold.csv", "text/csv", b"a,b\n1,2\n")
    line = attachment_summary_line(att)
    body, found = _delivery().split_trailing_attachments("September report attached\n\n" + line)
    assert body.strip() == "September report attached"
    assert len(found) == 1
    got = found[0]
    assert (got.filename, got.content_type, got.size_bytes, got.attachment_id) == \
        ("Accounts Sold.csv", "text/csv", 8, att["id"])


def test_mime_parameters_do_not_break_the_marker():
    att = _mailed("notes.txt", 'text/plain; charset="utf-8"; name="notes.txt"', b"hello")
    from common.attachments import attachment_summary_line
    _, found = _delivery().split_trailing_attachments(attachment_summary_line(att))
    assert [a.content_type for a in found] == ["text/plain"]


def test_every_attachment_of_a_mail_is_opened():
    from common.attachments import attachment_summary_line
    atts = [_mailed("a.pdf", "application/pdf", b"%PDF-1.4"),
            _mailed("b.png", "image/png", b"\x89PNG\r\n\x1a\n")]
    text = "two files\n\n" + "\n".join(attachment_summary_line(a) for a in atts)
    _, found = _delivery().split_trailing_attachments(text)
    assert [a.attachment_id for a in found] == [a["id"] for a in atts]
