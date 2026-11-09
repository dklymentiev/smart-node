"""Turn Studio attachment handles into explicit agent retrieval instructions.

Claude Code's stream-json input is text-only. The durable Studio transcript is
also intentionally textual, with trailing ``[attachment: ...]`` handles. Before
an agent turn, replace those handles in the model input with the exact installed
retrieval command. The command runs as the agent, reads bytes through core-api's
Unix socket (kernel identity + approved/access gate), materializes a correctly
named file in the gateway's private /tmp, and prints the path for built-in Read.

The raw message is stored separately by the gateway, so the browser keeps the
stable handle lines it needs to render chips after reload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


ATTACHMENT_READ = "/srv/tools/attachments/attachment-read.py"
_MARKER = re.compile(
    r"^\[attachment: (.+), ([\w.+/-]+), (\d+) bytes, id ([0-9a-fA-F-]{36})\]$"
)


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    size_bytes: int
    attachment_id: str


def split_trailing_attachments(message: str) -> tuple[str, list[Attachment]]:
    """Return body and the consecutive composer markers at its end."""
    lines = message.splitlines()
    found: list[Attachment] = []
    while lines:
        match = _MARKER.fullmatch(lines[-1])
        if not match:
            break
        found.append(Attachment(
            filename=match.group(1),
            content_type=match.group(2).lower(),
            size_bytes=int(match.group(3)),
            attachment_id=match.group(4).lower(),
        ))
        lines.pop()
    found.reverse()
    if not found:
        return message, []
    return "\n".join(lines).rstrip(), found


def _safe_label(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value or "")
    return value.replace("`", "'").strip()[:200] or "attachment"


async def content_for_agent(message: str) -> str:
    """Replace handles with deterministic Read instructions for this turn."""
    body, attachments = split_trailing_attachments(message)
    if not attachments:
        return message

    instructions = []
    for att in attachments:
        name = _safe_label(att.filename)
        if att.content_type.startswith(("audio/", "video/")):
            instructions.append(
                f"[Attachment unavailable: {name} ({att.content_type}, {att.size_bytes} bytes). "
                "This node cannot inspect audio or video attachments yet. Tell the person "
                "plainly that you cannot read it; do not invent or infer its contents.]"
            )
            continue
        instructions.append(
            f"[Attachment to inspect before answering: {name} ({att.content_type}, "
            f"{att.size_bytes} bytes, id {att.attachment_id}). Run "
            f"`{ATTACHMENT_READ} {att.attachment_id}` exactly once, then use the built-in "
            "Read tool on the absolute path it prints. The retrieval command enforces this "
            "agent's access and approved status. If it fails, say the file could not be read; "
            "do not guess.]"
        )
    return body + ("\n\n" if body else "") + "\n".join(instructions)
