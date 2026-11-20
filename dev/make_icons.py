"""Draw Platewright's icons (a build plate with a 3 x 3 grid of parts) as PNGs, standard library only.

    uv run python -m dev.make_icons
writes platewright/web/static/icon-{32,180,192,512}.png and icon-maskable-512.png.
"""

import struct
import zlib
from pathlib import Path

GREEN = (0x00, 0xAE, 0x42)
DARK = (0x00, 0x7A, 0x2E)
WHITE = (0xFF, 0xFF, 0xFF)
OUT = Path(__file__).resolve().parent.parent / "platewright" / "web" / "static"


def inside_rounded(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def draw(size: int, maskable: bool = False) -> bytes:
    s = size
    pad = 0.0 if maskable else s * 0.04  # maskable icons fill the square; the OS rounds them
    plate = (s * 0.18, s * 0.18, s * 0.82, s * 0.82) if maskable else (s * 0.16, s * 0.16, s * 0.84, s * 0.84)
    px0, py0, px1, py1 = plate
    cell = (px1 - px0) / 3
    gap = cell * 0.18
    rows = []
    for j in range(s):
        row = bytearray([0])  # filter: none
        for i in range(s):
            x, y = i + 0.5, j + 0.5
            if maskable or inside_rounded(x, y, pad, pad, s - pad, s - pad, s * 0.22):
                color, alpha = GREEN, 255
                if inside_rounded(x, y, px0, py0, px1, py1, s * 0.06):
                    color = DARK
                    cx = int((x - px0) // cell)
                    cy = int((y - py0) // cell)
                    if 0 <= cx < 3 and 0 <= cy < 3:
                        bx0, by0 = px0 + cx * cell + gap, py0 + cy * cell + gap
                        if inside_rounded(x, y, bx0, by0, bx0 + cell - 2 * gap, by0 + cell - 2 * gap, cell * 0.16):
                            color = WHITE
            else:
                color, alpha = (0, 0, 0), 0
            row += bytes((*color, alpha))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    for n in (32, 180, 192, 512):
        (OUT / f"icon-{n}.png").write_bytes(draw(n, maskable=(n == 180)))  # iOS rounds its own corners
    (OUT / "icon-maskable-512.png").write_bytes(draw(512, maskable=True))
    print("written to", OUT)
