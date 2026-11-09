#!/usr/bin/env python3
"""Operator commands for a deployment: register callers, issue enrolment links."""

import base64
import json
import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hitl.lib import HttpError, b64u, event, iso_at, now, origin, q, rows, token_hash, transaction, unb64u  # noqa: E402

USAGE = """Usage:
  admin.py add-caller <caller_id> <display_name> <display_domain> <pubkey_b64url>
  admin.py set-caller-theme <caller_id> <colors.json> [logo.png|logo.jpg|logo.webp]
  admin.py enrol-link <recipient_id> <display_name> [hours=24]
  admin.py reset-recipient <recipient_id>        recovery when no enrolled device survives
  admin.py list
"""
ID = re.compile(r"[a-z0-9._-]{1,64}")
COLORS = {"background": "#fafaf8", "surface": "#ffffff", "ink": "#101114",
          "muted": "#76767a", "border": "#e7e6e1", "accent": "#101114", "on_accent": "#ffffff"}


def luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def contrast(a: str, b: str) -> float:
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def theme_from_files(colors_path: str, logo_path: str | None) -> dict:
    try:
        with open(colors_path, encoding="utf-8") as f:
            theme = json.load(f)
    except (OSError, ValueError) as exc:
        fail(f"cannot read theme JSON: {exc}")
    if not isinstance(theme, dict) or set(theme) - set(COLORS):
        fail("theme must contain only supported color names")
    for key, value in theme.items():
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            fail(f"{key} must be a six-digit hex color")
    palette = {**COLORS, **theme}
    if min(contrast(palette["ink"], palette["surface"]),
           contrast(palette["ink"], palette["background"]),
           contrast(palette["on_accent"], palette["accent"])) < 4.5:
        fail("text and button colors need a contrast ratio of at least 4.5")
    if logo_path:
        try:
            with open(logo_path, "rb") as f:
                data = f.read(65537)
        except OSError as exc:
            fail(f"cannot read logo: {exc}")
        if not data or len(data) > 65536:
            fail("logo must be 1-65536 bytes")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            fail("logo must be PNG, JPEG or WebP")
        theme["logo"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return theme


def usage():
    sys.stderr.write(USAGE)
    sys.exit(64)


def fail(msg: str):
    sys.stderr.write(msg + "\n")
    sys.exit(65)


def main(args: list[str]) -> None:
    cmd = args.pop(0) if args else ""
    if cmd == "add-caller":
        if len(args) != 4:
            usage()
        id_, name, domain, pub = args
        try:
            ok = ID.fullmatch(id_) and len(unb64u(pub)) == 32
        except HttpError:
            ok = False
        if not ok:
            fail("invalid caller id or public key")
        q("""INSERT INTO callers (id, display_name, display_domain, pubkey, verified_since) VALUES (?, ?, ?, ?, ?)
             ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name, display_domain = excluded.display_domain,
             pubkey = excluded.pubkey""", (id_, name, domain, pub, now()))
        print(f"caller {id_} registered")

    elif cmd == "set-caller-theme":
        if len(args) not in (2, 3) or not ID.fullmatch(args[0]):
            usage()
        id_ = args[0]
        theme = theme_from_files(args[1], args[2] if len(args) == 3 else None)
        if q("UPDATE callers SET theme = ? WHERE id = ?", (json.dumps(theme, separators=(",", ":")), id_)).rowcount != 1:
            fail(f"unknown caller {id_}")
        print(f"caller {id_} theme updated")

    elif cmd == "enrol-link":
        if len(args) < 2:
            usage()
        id_, name = args[0], args[1]
        m = re.match(r"^\s*([+-]?\d+)", args[2]) if len(args) > 2 else None
        hours = int(m.group(1)) if m else (24 if len(args) <= 2 else 0)
        if not ID.fullmatch(id_) or hours < 1 or hours > 168:
            fail("invalid recipient id or hours")
        q("""INSERT INTO recipients (id, display_name, created_at) VALUES (?, ?, ?)
             ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name""", (id_, name, now()))
        token = b64u(secrets.token_bytes(24))
        q("INSERT INTO enrol_tokens (token_hash, recipient_id, expires_at) VALUES (?, ?, ?)",
          (token_hash(token), id_, iso_at(time.time() + hours * 3600)))
        event("hitl.recipient.enrol_started", None, None, {"recipient_id": id_, "via": "operator-link"})
        print(f"{origin()}/enroll/{token}")

    elif cmd == "reset-recipient":
        # Recovery with no surviving device (spec, Recipient keys): retire every credential and
        # drop the encryption key, so the person enrols again from scratch. Requests encrypted to
        # the old key stay unreadable. The new enrolment starts a new epoch with its own chain from
        # sequence 1; callers never follow a change of epoch on their own and must pin again, out of
        # band. Enrolment links issued before the reset die with it.
        if len(args) != 1:
            usage()
        id_ = args[0]
        if not rows("SELECT 1 FROM recipients WHERE id = ?", (id_,)):
            fail(f"unknown recipient {id_}")
        with transaction():
            n = q("UPDATE credentials SET retired_at = ? WHERE recipient_id = ? AND retired_at IS NULL", (now(), id_)).rowcount
            q("UPDATE recipients SET enc_pubkey = NULL, epoch = epoch + 1 WHERE id = ?", (id_,))
            q("DELETE FROM enrol_tokens WHERE recipient_id = ?", (id_,))
            event("hitl.recipient.reset", None, None, {"recipient_id": id_, "retired_credentials": n})
        print(f"recipient {id_} reset: {n} credential(s) retired; enrol again, then callers re-pin")

    elif cmd == "list":
        for c in rows("SELECT id, display_name, display_domain FROM callers ORDER BY id"):
            print(f"caller     {c['id']}  {c['display_name']} ({c['display_domain']})")
        for r in rows("""SELECT r.id, r.display_name, COUNT(c.id) AS n FROM recipients r
                         LEFT JOIN credentials c ON c.recipient_id = r.id AND c.retired_at IS NULL
                         GROUP BY r.id ORDER BY r.id"""):
            print(f"recipient  {r['id']}  {r['display_name']}  credentials={r['n']}")

    else:
        usage()


if __name__ == "__main__":
    main(sys.argv[1:])
