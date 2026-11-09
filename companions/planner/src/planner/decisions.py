#!/usr/bin/env python3
"""How a human decision is written onto a task, so a machine can read it back.

An approval gate used to be prose. The owner wrote a comment, and whatever was
downstream searched it for words: a flow's gate script asked whether the text
contained "reject", then whether it contained "revise". A comment saying "do not
reject this, just fix the wording" therefore rejected it, and a comment from
anyone at all counted, not only from the person being asked.

A decision is a fact about the task, not a description of one, so it is written
in a fixed shape and read by that shape:

    DECISION: modify

    move the third paragraph up and drop the price line

and the same decision is mirrored as a tag, `decision:modify`, so a reader that
only has the tag list can route without parsing text at all. The note is for the
person doing the work; the first line is for the machine.

One writer (the decision endpoints), one format, two readers (tags for routing,
the note for the work). Anything that needs to understand a decision imports
this, rather than inventing a third convention.
"""

APPROVE = "approve"
MODIFY = "modify"
REJECT = "reject"
VALID = (APPROVE, MODIFY, REJECT)

PREFIX = "DECISION:"
TAG_PREFIX = "decision:"


def tag_for(decision: str) -> str:
    """The tag that carries this decision."""
    return TAG_PREFIX + decision


def compose(decision: str, note: str | None = None) -> str:
    """The comment body for a decision. The first line is the machine's."""
    if decision not in VALID:
        raise ValueError("unknown decision: %r" % decision)
    body = "%s %s" % (PREFIX, decision)
    if note and note.strip():
        body += "\n\n" + note.strip()
    return body


def parse(comment: str | None) -> tuple[str | None, str | None]:
    """Read a decision back out of a comment body.

    Returns (decision, note), or (None, None) when the comment is not one. Only
    the first line is inspected: a note that happens to mention another decision
    is a note, not a second decision.
    """
    if not comment:
        return None, None
    head, _, rest = comment.strip().partition("\n")
    head = head.strip()
    if not head.upper().startswith(PREFIX):
        return None, None
    decision = head[len(PREFIX):].strip().lower()
    if decision not in VALID:
        return None, None
    return decision, rest.strip() or None
