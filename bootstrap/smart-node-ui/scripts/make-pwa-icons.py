#!/usr/bin/env python3
"""Writes the admin app's icons into ../src/icons/. The mark is the two offset squares of assets/mark.svg, dark on a
light ground: the Studio app's icon is the same mark light on dark, and the two sit side by side on a home screen.
Plain PNGs from the standard library, so nothing has to be installed to remake them:  python3 make-pwa-icons.py"""
import pathlib, struct, zlib

BG, INK = (242, 242, 240), (10, 10, 12)
OUT = pathlib.Path(__file__).resolve().parent.parent / "src" / "icons"

def png(size, mark_share):
    unit = size * mark_share / 18.0                  # the mark is 18 units wide (1..19 of a 20 unit box)
    off = (size - 18 * unit) / 2.0
    def inside(x, y):
        u, v = (x - off) / unit, (y - off) / unit      # 0..18 within the mark
        a = 0 <= u < 10 and 0 <= v < 10
        b = 8 <= u < 18 and 8 <= v < 18
        return a or b
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row += bytes(INK if inside(x + 0.5, y + 0.5) else BG)
        rows.append(bytes(row))
    raw = b"".join(rows)
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

OUT.mkdir(parents=True, exist_ok=True)
for name, size, share in (("icon-192.png", 192, 0.56), ("icon-512.png", 512, 0.56),
                          ("icon-maskable-512.png", 512, 0.46), ("apple-touch-icon.png", 180, 0.56)):
    (OUT / name).write_bytes(png(size, share))
    print("wrote", OUT / name)
