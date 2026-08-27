#!/usr/bin/env python3
"""Turn the nano-banana source sheets into the sprites the viewer preloads.

Sources (committed, `scripts/art/source/`, rendered with
`playbooks/art-nanobanana.md` / `gemini-2.5-flash-image`):

    hulls_sheet.png      four Softmax-cog-crewed barges, one per seat colour
    yards_sheet.png      the four matching docks
    halite_crystals.png  a density ramp of salt-crystal clusters
    seabed_tile.png      a tiling dried-salt-flat floor

Outputs (committed, `data/art/`, preloaded into the wasm bundle by
`--preload-file` in `replay-viewer/config.nims`):

    hull_{alpha,bravo,charlie,delta}.png    64 px, transparent
    yard_{alpha,bravo,charlie,delta}.png    64 px, transparent
    halite_crystals.png                     6 frames x 64 px (frame 0 is empty)
    seabed.png                              512 px tiling

Gemini does not return alpha and the "pure green" it returns is *some* green
with a tinted edge, so the backdrop is keyed by flood-filling from the border
with the border's median colour as the key. The sheet's layout is not assumed:
components are found by connected pixels and assigned to seats by **hue match**
against the seat palette, so a model that answers a "row of four" prompt with a
2 x 2 grid still splits correctly.

    python3 scripts/art/split_art_sheets.py

Needs Pillow only: `python3 -m pip install --user pillow`.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "scripts" / "art" / "source"
OUT = REPO / "data" / "art"

#: Seat colours, in seat order (design note §Seats, aliases, quadrants).
SEATS = (
    ("alpha", (0xE8, 0xA3, 0x3D)),
    ("bravo", (0x3F, 0xB6, 0xB0)),
    ("charlie", (0xC6, 0x5F, 0xA8)),
    ("delta", (0x8F, 0xBF, 0x3F)),
)

SPRITE_PX = 64
CRYSTAL_FRAMES = 6
SEABED_PX = 512
#: Chroma tolerance, squared RGB distance from the keyed backdrop colour.
KEY_TOLERANCE = 62**2
#: Ignore specks: a component must cover at least this fraction of the sheet.
MIN_AREA_FRACTION = 0.002


def _median_border(image: Image.Image) -> tuple[int, int, int]:
    w, h = image.size
    px = image.load()
    samples = []
    for x in range(0, w, max(1, w // 64)):
        samples.append(px[x, 0])
        samples.append(px[x, h - 1])
    for y in range(0, h, max(1, h // 64)):
        samples.append(px[0, y])
        samples.append(px[w - 1, y])
    channels = [sorted(s[i] for s in samples) for i in range(3)]
    return tuple(c[len(c) // 2] for c in channels)  # type: ignore[return-value]


def key_out(path: Path) -> Image.Image:
    """Flood-fill the backdrop from the border; keep interior greens."""
    image = Image.open(path).convert("RGB")
    w, h = image.size
    px = image.load()
    key = _median_border(image)
    background = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()

    def close(p) -> bool:
        return sum((p[i] - key[i]) ** 2 for i in range(3)) <= KEY_TOLERANCE

    for x in range(w):
        for y in (0, h - 1):
            if not background[y * w + x] and close(px[x, y]):
                background[y * w + x] = 1
                queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not background[y * w + x] and close(px[x, y]):
                background[y * w + x] = 1
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not background[ny * w + nx]:
                if close(px[nx, ny]):
                    background[ny * w + nx] = 1
                    queue.append((nx, ny))

    out = image.convert("RGBA")
    opx = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if background[row + x]:
                opx[x, y] = (0, 0, 0, 0)
    return out


def components(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of opaque connected components, largest first."""
    w, h = image.size
    alpha = image.split()[3].load()
    seen = bytearray(w * h)
    boxes: list[tuple[int, int, int, int, int]] = []
    for sy in range(h):
        for sx in range(w):
            if seen[sy * w + sx] or alpha[sx, sy] < 24:
                continue
            queue = deque([(sx, sy)])
            seen[sy * w + sx] = 1
            x0 = x1 = sx
            y0 = y1 = sy
            area = 0
            while queue:
                x, y = queue.popleft()
                area += 1
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                        if alpha[nx, ny] >= 24:
                            seen[ny * w + nx] = 1
                            queue.append((nx, ny))
            boxes.append((area, x0, y0, x1 + 1, y1 + 1))
    floor = MIN_AREA_FRACTION * w * h
    boxes = [b for b in boxes if b[0] >= floor]
    boxes.sort(key=lambda b: -b[0])
    return [(b[1], b[2], b[3], b[4]) for b in boxes]


def square(image: Image.Image, box: tuple[int, int, int, int], size: int) -> Image.Image:
    part = image.crop(box)
    side = max(part.size)
    pad = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    pad.paste(part, ((side - part.width) // 2, (side - part.height) // 2))
    return pad.resize((size, size), Image.LANCZOS)


def dominant(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, float, float]:
    """Mean opaque colour of a component (used to match it to a seat)."""
    part = image.crop(box)
    px = part.load()
    total = [0.0, 0.0, 0.0]
    count = 0
    for y in range(part.height):
        for x in range(part.width):
            r, g, b, a = px[x, y]
            if a < 200:
                continue
            # Skip near-white/near-black: outlines and crew art are shared.
            if max(r, g, b) - min(r, g, b) < 24:
                continue
            total[0] += r
            total[1] += g
            total[2] += b
            count += 1
    if not count:
        return (0.0, 0.0, 0.0)
    return (total[0] / count, total[1] / count, total[2] / count)


def assign_seats(image: Image.Image, boxes: list[tuple[int, int, int, int]]) -> dict[str, tuple[int, int, int, int]]:
    """Greedy best-match of components to seat colours (layout-agnostic)."""
    if len(boxes) < len(SEATS):
        raise SystemExit(
            f"expected >= {len(SEATS)} components, found {len(boxes)} — re-render the sheet"
        )
    boxes = boxes[: len(SEATS)]
    means = {box: dominant(image, box) for box in boxes}
    pairs = sorted(
        (
            (sum((means[box][i] - colour[i]) ** 2 for i in range(3)), name, box)
            for name, colour in SEATS
            for box in boxes
        ),
        key=lambda p: p[0],
    )
    taken: dict[str, tuple[int, int, int, int]] = {}
    used: set[tuple[int, int, int, int]] = set()
    for _score, name, box in pairs:
        if name in taken or box in used:
            continue
        taken[name] = box
        used.add(box)
    if len(taken) != len(SEATS):
        raise SystemExit(f"could not assign every seat: {sorted(taken)}")
    return taken


def build_kit(source: str, prefix: str) -> None:
    image = key_out(SOURCE / source)
    boxes = components(image)
    for name, box in assign_seats(image, boxes).items():
        target = OUT / f"{prefix}_{name}.png"
        square(image, box, SPRITE_PX).save(target)
        print(f"{target.relative_to(REPO)}  from {box}")


def build_crystals() -> None:
    image = key_out(SOURCE / "halite_crystals.png")
    boxes = components(image)
    if len(boxes) < CRYSTAL_FRAMES - 1:
        raise SystemExit(
            f"halite_crystals.png has {len(boxes)} clusters, need "
            f"{CRYSTAL_FRAMES - 1} — re-render the sheet"
        )
    # Density ramp: smallest footprint first. Frame 0 stays EMPTY (a cell with
    # no halite draws nothing), frames 1..5 are the five drawn steps.
    ramp = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    picks = [ramp[round(i * (len(ramp) - 1) / (CRYSTAL_FRAMES - 2))] for i in range(CRYSTAL_FRAMES - 1)]
    sheet = Image.new("RGBA", (SPRITE_PX * CRYSTAL_FRAMES, SPRITE_PX), (0, 0, 0, 0))
    for index, box in enumerate(picks, start=1):
        sheet.paste(square(image, box, SPRITE_PX), (index * SPRITE_PX, 0))
    sheet.save(OUT / "halite_crystals.png")
    print(f"data/art/halite_crystals.png  {CRYSTAL_FRAMES} frames x {SPRITE_PX}px")


def build_seabed() -> None:
    image = Image.open(SOURCE / "seabed_tile.png").convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    tile = image.crop((left, top, left + side, top + side)).resize(
        (SEABED_PX, SEABED_PX), Image.LANCZOS
    )
    tile.convert("RGBA").save(OUT / "seabed.png")
    print(f"data/art/seabed.png  {SEABED_PX}x{SEABED_PX}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    build_kit("hulls_sheet.png", "hull")
    build_kit("yards_sheet.png", "yard")
    build_crystals()
    build_seabed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
