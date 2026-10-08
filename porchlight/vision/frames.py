"""Cheap, CPU-only checks that run before any vision model is called.

Ring's computer vision guidance for Appstore apps is to spend compute in
proportion to the chance that an event matters: filter cheaply first and keep
the expensive model for what survives. These are Porchlight's cheap stages:

* quality(): is the frame usable at all (not black, not blown out, not blank)?
* fingerprint() / distance(): has the porch visibly changed since the last
  frame a model or a person actually read? If not, that reading still stands.

Ring overlays its logo, the device id and a running timestamp on every frame
it serves, so both checks ignore the top and bottom bands where those sit.
"""

from __future__ import annotations

import io

from PIL import Image, ImageStat

SIG_W, SIG_H = 48, 27
CELL = 3
# Watermark bands as a fraction of frame height: logo and device/app/time text on top, labels at the bottom.
TOP_BAND = 0.16
BOTTOM_BAND = 0.10
# Largest brightness difference (0-255) allowed in any one cell for two frames to count as the same scene.
# Compression and small exposure shifts stay well under this; a parcel or a person does not.
UNCHANGED_MAX_CELL_DIFF = 10.0


def _scene(image_bytes: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("L")
    except Exception:
        return None
    w, h = img.size
    if w < 32 or h < 32:
        return None
    return img.crop((0, int(h * TOP_BAND), w, int(h * (1 - BOTTOM_BAND))))


def quality(image_bytes: bytes) -> str | None:
    """None if the frame is usable, otherwise a short reason it is not."""
    scene = _scene(image_bytes)
    if scene is None:
        return "not a readable image"
    stat = ImageStat.Stat(scene.resize((96, 54)))
    mean, spread = stat.mean[0], stat.stddev[0]
    if mean < 14:
        return "too dark"
    if mean > 246:
        return "blown out"
    if spread < 4:
        return "blank"
    return None


def fingerprint(image_bytes: bytes) -> str | None:
    """A small greyscale thumbnail of the scene (48x27), as hex. Compared cell by cell, not as a whole,
    so a small new object in one corner is not averaged away."""
    scene = _scene(image_bytes)
    if scene is None:
        return None
    return scene.resize((SIG_W, SIG_H), Image.Resampling.BOX).tobytes().hex()


def distance(a: str | None, b: str | None) -> float:
    """The largest per-cell brightness difference between two fingerprints, after removing any
    overall exposure shift. Missing fingerprints count as completely different."""
    if not a or not b or len(a) != len(b) or len(a) != SIG_W * SIG_H * 2:
        return 255.0
    pa, pb = bytes.fromhex(a), bytes.fromhex(b)
    shift = (sum(pa) - sum(pb)) / len(pa)
    worst = 0.0
    for cy in range(0, SIG_H, CELL):
        for cx in range(0, SIG_W, CELL):
            total = 0.0
            for y in range(cy, cy + CELL):
                row = y * SIG_W
                for x in range(cx, cx + CELL):
                    total += pa[row + x] - pb[row + x] - shift
            worst = max(worst, abs(total) / (CELL * CELL))
    return worst


def unchanged(a: str | None, b: str | None) -> bool:
    return distance(a, b) <= UNCHANGED_MAX_CELL_DIFF
