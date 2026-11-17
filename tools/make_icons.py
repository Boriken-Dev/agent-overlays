#!/usr/bin/env python3
"""Draw each overlay's icon: a white glyph on a coloured rounded square, 256 by 256 PNG.

Standard library only, and deterministic: the same script writes the same bytes.

    python tools/make_icons.py            # write overlays/<name>/icon.png
    python tools/make_icons.py --check    # exit 1 when a committed icon differs
    python tools/make_icons.py --sheet out.png   # all icons side by side, for a look
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 256
SAMPLES = 3  # per axis, for smooth edges

ROOT = Path(__file__).resolve().parent.parent


# --- shapes, in unit coordinates with y down -------------------------------------------------


def dist_segment(x, y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return math.hypot(x - ax, y - ay)
    t = ((x - ax) * dx + (y - ay) * dy) / length
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy))


def stroke(points, width):
    """A polyline drawn `width` wide with round ends and joins."""
    half = width / 2
    pairs = list(zip(points, points[1:]))

    def inside(x, y):
        return any(dist_segment(x, y, a[0], a[1], b[0], b[1]) <= half for a, b in pairs)

    return inside


def disc(cx, cy, r):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def ring(cx, cy, r_in, r_out):
    return lambda x, y: r_in * r_in <= (x - cx) ** 2 + (y - cy) ** 2 <= r_out * r_out


def box(x0, y0, x1, y1, radius=0.0):
    def inside(x, y):
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        if radius <= 0:
            return True
        cx = min(max(x, x0 + radius), x1 - radius)
        cy = min(max(y, y0 + radius), y1 - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius

    return inside


def polygon(points):
    def inside(x, y):
        hit = False
        j = len(points) - 1
        for i, (xi, yi) in enumerate(points):
            xj, yj = points[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
        return hit

    return inside


def union(*shapes):
    return lambda x, y: any(s(x, y) for s in shapes)


def minus(shape, *cuts):
    return lambda x, y: shape(x, y) and not any(c(x, y) for c in cuts)


def arc_points(cx, cy, r, start, end, steps=48):
    """Points along a circle from `start` to `end` degrees (y down: clockwise grows)."""
    return [
        (
            cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
            cy + r * math.sin(math.radians(start + (end - start) * i / steps)),
        )
        for i in range(steps + 1)
    ]


# --- glyphs ---------------------------------------------------------------------------------


def cog():
    def tooth(x, y):
        dx, dy = x - 0.5, y - 0.5
        r = math.hypot(dx, dy)
        if r == 0 or r > 0.40:
            return False
        step = math.pi / 4
        a = math.atan2(dy, dx)
        d = (a + step / 2) % step - step / 2
        return r * math.cos(d) > 0 and abs(r * math.sin(d)) <= 0.055 and r >= 0.25

    return minus(union(disc(0.5, 0.5, 0.30), tooth), disc(0.5, 0.5, 0.14))


def network():
    nodes = [(0.5, 0.27), (0.27, 0.68), (0.73, 0.68)]
    lines = [stroke([nodes[i], nodes[(i + 1) % 3]], 0.04) for i in range(3)]
    return union(*lines, *[disc(x, y, 0.10) for x, y in nodes])


def code():
    left = stroke([(0.38, 0.30), (0.20, 0.50), (0.38, 0.70)], 0.07)
    right = stroke([(0.62, 0.30), (0.80, 0.50), (0.62, 0.70)], 0.07)
    slash = stroke([(0.55, 0.26), (0.45, 0.74)], 0.05)
    return union(left, right, slash)


def serpent():
    upper = arc_points(0.5, 0.34, 0.15, -30, -270)
    lower = arc_points(0.5, 0.64, 0.15, -90, 150)
    body = stroke(upper + lower, 0.085)
    eye = disc(0.64, 0.255, 0.022)
    return minus(body, eye)


def crate():
    corners = [
        (0.5 + 0.30 * math.cos(math.radians(-90 + 60 * k)), 0.5 + 0.30 * math.sin(math.radians(-90 + 60 * k)))
        for k in range(6)
    ]
    outline = stroke(corners + [corners[0]], 0.055)
    inner = [stroke([(0.5, 0.5), corners[k]], 0.05) for k in (0, 2, 4)]
    return union(outline, *inner)


def padlock():
    body = box(0.28, 0.46, 0.72, 0.80, 0.06)
    shackle = stroke(arc_points(0.5, 0.46, 0.15, 180, 360), 0.075)
    keyhole = union(disc(0.5, 0.60, 0.05), box(0.482, 0.60, 0.518, 0.70))
    return minus(union(body, shackle, box(0.35, 0.46, 0.65, 0.50)), keyhole)


def rewind():
    cx = cy = 0.5
    r_in, r_out = 0.17, 0.29
    gap = (280.0, 340.0)

    def band(x, y):
        dx, dy = x - cx, y - cy
        rr = dx * dx + dy * dy
        if not r_in * r_in <= rr <= r_out * r_out:
            return False
        a = math.degrees(math.atan2(dy, dx)) % 360
        return not gap[0] < a < gap[1]

    rm = (r_in + r_out) / 2
    ang = math.radians(gap[0])
    px, py = cx + rm * math.cos(ang), cy + rm * math.sin(ang)
    tx, ty = -math.sin(ang), math.cos(ang)  # direction of travel
    nx, ny = math.cos(ang), math.sin(ang)  # outward
    head = polygon(
        [
            (px + tx * 0.13, py + ty * 0.13),
            (px + nx * 0.12, py + ny * 0.12),
            (px - nx * 0.12, py - ny * 0.12),
        ]
    )
    return union(band, head)


def tag():
    label = polygon([(0.24, 0.30), (0.58, 0.30), (0.80, 0.50), (0.58, 0.70), (0.24, 0.70)])
    return minus(label, disc(0.37, 0.50, 0.055))


# name: (background colour, glyph)
ICONS = {
    "engineering": ((0x2F, 0x4F, 0x73), cog),
    "net": ((0x1B, 0x7F, 0x86), network),
    "node": ((0x3F, 0x7A, 0x52), code),
    "private-sync": ((0x63, 0x4A, 0x96), padlock),
    "python": ((0x36, 0x68, 0xA0), serpent),
    "recovery": ((0xB0, 0x3A, 0x32), rewind),
    "release": ((0xC2, 0x73, 0x10), tag),
    "rust": ((0x9A, 0x4B, 0x28), crate),
}


# --- raster ---------------------------------------------------------------------------------


def render(colour, glyph_factory):
    glyph = glyph_factory()
    plate = box(0.0, 0.0, 1.0, 1.0, 0.22)
    rows = []
    for py in range(SIZE):
        row = bytearray([0])  # PNG filter: none
        for px in range(SIZE):
            cover = glyph_n = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / SIZE
                    y = (py + (sy + 0.5) / SAMPLES) / SIZE
                    if plate(x, y):
                        cover += 1
                        if glyph(x, y):
                            glyph_n += 1
            n = SAMPLES * SAMPLES
            if cover == 0:
                row += bytes((0, 0, 0, 0))
                continue
            lift = 1.0 + 0.10 * (1 - py / SIZE)  # a little lighter at the top
            base = [min(255, round(c * lift)) for c in colour]
            white = glyph_n / cover
            rgb = [round(b + (255 - b) * white) for b in base]
            row += bytes((*rgb, round(255 * cover / n)))
        rows.append(bytes(row))
    return b"".join(rows)


def png(raw, width=SIZE, height=SIZE):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def sheet(rendered):
    names = sorted(rendered)
    width = SIZE * len(names)
    rows = []
    for y in range(SIZE):
        row = bytearray([0])
        for name in names:
            line = rendered[name][y * (SIZE * 4 + 1) + 1 : (y + 1) * (SIZE * 4 + 1)]
            # lay each icon on a light page so the corners show
            for i in range(0, len(line), 4):
                r, g, b, a = line[i : i + 4]
                row += bytes(round((c * a + 238 * (255 - a)) / 255) for c in (r, g, b)) + b"\xff"
        rows.append(bytes(row))
    return png(b"".join(rows), width, SIZE)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 when a committed icon differs")
    parser.add_argument("--sheet", metavar="PNG", help="also write every icon side by side to this file")
    args = parser.parse_args()

    rendered = {name: render(colour, glyph) for name, (colour, glyph) in ICONS.items()}
    stale = []
    for name, raw in rendered.items():
        path = ROOT / "overlays" / name / "icon.png"
        data = png(raw)
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(name)
        else:
            path.write_bytes(data)
            print(f"wrote {path.relative_to(ROOT).as_posix()} ({len(data)} bytes)")
    if args.sheet:
        Path(args.sheet).write_bytes(sheet(rendered))
    if stale:
        print("icons out of date: " + ", ".join(sorted(stale)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
