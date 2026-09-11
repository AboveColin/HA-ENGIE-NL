#!/usr/bin/env python3
"""Draw a neutral brand glyph as PNG without any imaging library.

HACS integrations bundle their own icon since HA 2026.3. This one is a plain
lightning bolt in a rounded square; deliberately not ENGIE's trademarked logo.
Pure Python: rasterises polygons with an even-odd test and writes the PNG by
hand with zlib, so it runs anywhere.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

BG = (13, 71, 161)     # deep blue
FG = (255, 213, 79)    # warm yellow


def _inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    hit = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                hit = not hit
    return hit


def _rounded_square(x: float, y: float, size: float, radius: float) -> bool:
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def render(size: int) -> bytes:
    s = size
    bolt = [(0.56, 0.12), (0.30, 0.55), (0.47, 0.55), (0.40, 0.88), (0.70, 0.42), (0.53, 0.42)]
    poly = [(x * s, y * s) for x, y in bolt]
    rows = []
    for j in range(s):
        row = bytearray([0])
        for i in range(s):
            px, py = i + 0.5, j + 0.5
            if not _rounded_square(px, py, s, s * 0.2):
                row += bytes((0, 0, 0, 0))
            elif _inside(px, py, poly):
                row += bytes((*FG, 255))
            else:
                row += bytes((*BG, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("custom_components/engie_nl/brand")
    out.mkdir(parents=True, exist_ok=True)
    # The brand is one square glyph with no wordmark, so the logo is the icon
    # at the same two sizes. Home Assistant asks for all four names and falls
    # back to nothing, not to the icon, when the logo pair is missing.
    for size, suffix in ((256, ""), (512, "@2x")):
        png = render(size)
        (out / f"icon{suffix}.png").write_bytes(png)
        (out / f"logo{suffix}.png").write_bytes(png)
    print(f"wrote icon/logo .png (256) and @2x.png (512) to {out}")


if __name__ == "__main__":
    main()
