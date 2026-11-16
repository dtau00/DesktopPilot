"""Crop, zoom and grid for `shot`: pure image arithmetic, no UIA, no win32.

A shot is a copy of the screen, so every pixel of it has a screen coordinate.
This module keeps that mapping exact through a crop and a zoom, and draws a
grid labelled in screen coordinates, so the agent reads `ui click --at X,Y`
straight off the picture:

    screen = origin + floor(image / scale)
    image  = (screen - origin) * scale

Everything is physical pixels. The virtual screen is a parameter, not a
lookup, so the clamping is testable without a display.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

from .errors import PilotError

Rect = tuple[int, int, int, int]  # left, top, width, height

MAX_ZOOM = 16
# `auto` targets the size at which Claude vision starts to resize an image:
# anything larger is shrunk on the way in, so upscaling past it buys nothing.
AUTO_LONG_EDGE = 1568
AUTO_MAX_PIXELS = 1_150_000
# An explicit --zoom is honoured, within what a PNG viewer and memory survive.
MAX_EDGE = 16384
MAX_PIXELS = 64_000_000
MIN_GRID = 2
# Grid labels are sized in OUTPUT pixels: legible at any scale.
LABEL_SIZE = 12
LABEL_PAD = 2  # text to box edge
LABEL_OFFSET = 2  # crossing to box corner, so the crossing itself stays visible
LABEL_GAP = 4  # the least room between two neighbouring boxes
LINE_RGBA = (255, 0, 128, 110)
LABEL_FILL = (255, 255, 210)
LABEL_TEXT = (0, 0, 0)
LABEL_EDGE = (255, 0, 128)
# floor() of a float quotient that should be whole (6 * 0.1 / 0.1) must not
# land one pixel short.
EPSILON = 1e-9
# PNG text chunk holding a shot's frame: {"origin": [x, y], "scale": s}.
FRAME_CHUNK = "desktoppilot"


@dataclass(frozen=True)
class Plan:
    """What to capture and how to scale it. `origin` and `size` are the screen
    rect of the source pixels; the image is `size * scale`."""

    origin: tuple[int, int]
    size: tuple[int, int]
    scale: float
    resample: str  # none | nearest | lanczos
    clipped: bool

    @property
    def image_size(self) -> tuple[int, int]:
        w, h = self.size
        if self.scale >= 1:
            k = int(self.scale)
            return w * k, h * k
        return max(1, math.floor(w * self.scale)), max(1, math.floor(h * self.scale))


# ------------------------------------------------------------------ parsing


def _ints(value: str, count: int, what: str, shape: str) -> list[int]:
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError:
        parts = []
    if len(parts) != count:
        raise PilotError(f"{what} wants '{shape}' in whole pixels; got {value!r}")
    return parts


def parse_point(value: str) -> tuple[int, int]:
    """'X,Y' in screen pixels, for ui click --at."""
    x, y = _ints(value, 2, "--at", "X,Y")
    return x, y


def parse_region(value: str | None) -> Rect | None:
    """'X,Y,W,H' in pixels of the plain shot of the same selector."""
    if value is None:
        return None
    x, y, w, h = _ints(value, 4, "--region", "X,Y,W,H")
    if w <= 0 or h <= 0:
        raise PilotError(f"--region wants a positive width and height; got {value!r}")
    return x, y, w, h


def parse_zoom(value: str | None) -> int | str | None:
    """An integer 1..MAX_ZOOM, 'auto', or None for the default."""
    if value is None:
        return None
    text = value.strip().lower()
    if text == "auto":
        return "auto"
    try:
        zoom = int(text)
    except ValueError:
        zoom = 0
    if not 1 <= zoom <= MAX_ZOOM:
        raise PilotError(f"--zoom wants an integer 1-{MAX_ZOOM} or 'auto'; got {value!r}")
    return zoom


def parse_grid(value: str | None) -> int | None:
    """A grid step in source (screen) pixels."""
    if value is None:
        return None
    try:
        step = int(value.strip())
    except ValueError:
        step = 0
    if step < MIN_GRID:
        raise PilotError(f"--grid wants a step of at least {MIN_GRID} pixels; got {value!r}")
    return step


# --------------------------------------------------------------------- plan


def _intersect(a: Rect, b: Rect) -> Rect:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    return left, top, max(0, right - left), max(0, bottom - top)


def auto_scale(size: tuple[int, int]) -> float:
    """The largest integer factor inside both caps; below 1 only when the
    source is already over them."""
    w, h = size
    fit = min(AUTO_LONG_EDGE / max(w, h), math.sqrt(AUTO_MAX_PIXELS / (w * h)))
    if fit >= 1:
        return float(min(math.floor(fit + EPSILON), MAX_ZOOM))
    return fit


def plan(
    base: Rect,
    region: Rect | None,
    zoom: int | str | None,
    screen: Rect,
) -> Plan:
    """`base` is the capture rect of the selector (screen px), `region` a box
    in pixels of that plain shot, `screen` the virtual screen.

    A region is clamped to the base and to the screen - `clipped` says so; a
    plain shot keeps its whole rect, off-screen parts and all, so it stays the
    picture it always was."""
    if region is None:
        left, top, w, h = base
        clipped = False
    else:
        want = (base[0] + region[0], base[1] + region[1], region[2], region[3])
        left, top, w, h = _intersect(_intersect(want, base), screen)
        if w <= 0 or h <= 0:
            raise PilotError(
                f"--region {','.join(map(str, region))} is empty inside the "
                f"{base[2]}x{base[3]} shot of this selector (and the screen)"
            )
        clipped = (left, top, w, h) != want
    if w <= 0 or h <= 0:
        raise PilotError(f"nothing to capture: the element is {w}x{h}")

    if zoom is None:
        zoom = "auto" if region is not None else 1
    if zoom == "auto":
        scale = auto_scale((w, h))
    else:
        scale = float(zoom)
        if max(w, h) * zoom > MAX_EDGE or w * h * zoom * zoom > MAX_PIXELS:
            raise PilotError(
                f"--zoom {zoom} makes {w * zoom}x{h * zoom}; the most is {MAX_EDGE} px an "
                f"edge and {MAX_PIXELS // 1_000_000} MP - crop with --region or use --zoom auto"
            )
    if scale == 1:
        resample = "none"
    elif scale > 1:
        resample = "nearest"
    else:
        resample = "lanczos"
    return Plan(origin=(left, top), size=(w, h), scale=scale, resample=resample, clipped=clipped)


def image_to_screen(p: Plan, ix: float, iy: float) -> tuple[int, int]:
    return (
        p.origin[0] + math.floor(ix / p.scale + EPSILON),
        p.origin[1] + math.floor(iy / p.scale + EPSILON),
    )


def screen_to_image(p: Plan, x: int, y: int) -> tuple[int, int]:
    """The image pixel where screen pixel (x, y) starts: its left/top edge."""
    return (
        math.floor((x - p.origin[0]) * p.scale + EPSILON),
        math.floor((y - p.origin[1]) * p.scale + EPSILON),
    )


# ------------------------------------------------------------------- render


def render(img: Image.Image, p: Plan, grid: int | None = None) -> tuple[Image.Image, dict | None]:
    """Scale the captured source (`p.size`) and draw the grid on the result.
    Upscaling is NEAREST by an integer factor - crisp glyph edges, no
    invented detail, an exact mapping; only a source over the auto cap is
    shrunk, with LANCZOS."""
    if img.size != p.size:
        raise PilotError(f"capture is {img.size}, the plan wanted {p.size}")
    out = img.convert("RGB")
    if p.resample == "nearest":
        out = out.resize(p.image_size, Image.Resampling.NEAREST)
    elif p.resample == "lanczos":
        out = out.resize(p.image_size, Image.Resampling.LANCZOS)
    if grid is None:
        return out, None
    return _draw_grid(out, p, grid)


def _lines(origin: int, extent: int, step: int) -> list[int]:
    """Screen coordinates that are multiples of `step` inside [origin, origin+extent)."""
    first = -(-origin // step) * step
    return list(range(first, origin + extent, step))


def _draw_grid(out: Image.Image, p: Plan, step: int) -> tuple[Image.Image, dict]:
    spacing = step * p.scale
    if spacing < 2:
        raise PilotError(
            f"--grid {step} is {spacing:.2f} image px apart at scale {p.scale:.3g}; raise the step"
        )
    xs = _lines(p.origin[0], p.size[0], step)
    ys = _lines(p.origin[1], p.size[1], step)
    iw, ih = out.size

    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for x in xs:
        col = screen_to_image(p, x, p.origin[1])[0]
        draw.line([(col, 0), (col, ih - 1)], fill=LINE_RGBA, width=1)
    for y in ys:
        row = screen_to_image(p, p.origin[0], y)[1]
        draw.line([(0, row), (iw - 1, row)], fill=LINE_RGBA, width=1)
    out = Image.alpha_composite(out.convert("RGBA"), overlay).convert("RGB")

    font = ImageFont.load_default(size=LABEL_SIZE)
    every = 1
    if xs and ys:
        # The widest label bounds them all; one thinning factor for both axes,
        # so the labelled lines stay a regular sub-grid.
        widest = max((f"{x},{y}" for x in (xs[0], xs[-1]) for y in (ys[0], ys[-1])), key=len)
        box_w, box_h = _box_size(font, widest)
        every = max(
            1,
            math.ceil((box_w + LABEL_OFFSET + LABEL_GAP) / spacing),
            math.ceil((box_h + LABEL_OFFSET + LABEL_GAP) / spacing),
        )
        _draw_labels(out, p, font, xs, ys, step * every)
    return out, {"step": step, "label_every": every}


def _box_size(font, text: str) -> tuple[int, int]:
    left, top, right, bottom = font.getbbox(text)
    return right - left + 2 * LABEL_PAD, bottom - top + 2 * LABEL_PAD


def _draw_labels(out: Image.Image, p: Plan, font, xs, ys, every_px: int) -> None:
    """A box at each labelled crossing, below-right of it; flipped left or up
    at the image edge, and dropped rather than drawn over a neighbour."""
    draw = ImageDraw.Draw(out)
    placed: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for gy, y in enumerate(v for v in ys if v % every_px == 0):
        for gx, x in enumerate(v for v in xs if v % every_px == 0):
            text = f"{x},{y}"
            neighbours = [placed.get((gx + dx, gy + dy)) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            box = _place(screen_to_image(p, x, y), _box_size(font, text), out.size, neighbours)
            if box is None:
                continue
            placed[(gx, gy)] = box
            draw.rectangle(
                [box[0], box[1], box[2] - 1, box[3] - 1], fill=LABEL_FILL, outline=LABEL_EDGE
            )
            ox, oy = font.getbbox(text)[:2]
            draw.text(
                (box[0] + LABEL_PAD - ox, box[1] + LABEL_PAD - oy), text, font=font, fill=LABEL_TEXT
            )


def _place(crossing, box_size, image_size, neighbours):
    """The first free spot for a box: below-right, then flipped left and/or up."""
    (cx, cy), (bw, bh), (iw, ih) = crossing, box_size, image_size
    for left in (cx + LABEL_OFFSET, cx - LABEL_OFFSET - bw):
        for top in (cy + LABEL_OFFSET, cy - LABEL_OFFSET - bh):
            box = (left, top, left + bw, top + bh)
            inside = left >= 0 and top >= 0 and box[2] <= iw and box[3] <= ih
            if inside and not any(_overlap(box, other) for other in neighbours):
                return box
    return None


def _overlap(a, b) -> bool:
    if b is None:
        return False
    return (
        a[0] < b[2] + LABEL_GAP
        and b[0] < a[2] + LABEL_GAP
        and a[1] < b[3] + LABEL_GAP
        and b[1] < a[3] + LABEL_GAP
    )


def save_image(img: Image.Image, path: str | Path, *, frame: dict | None = None) -> Path:
    """PNG, or JPEG for a .jpg/.jpeg path. A PNG carries `frame` (origin,
    scale) in a `desktoppilot` text chunk, so `image annotate` can map
    selectors onto it with no extra arguments; a JPEG has nowhere to keep it."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() in (".jpg", ".jpeg"):
        img.convert("RGB").save(target, "JPEG", quality=92)
        return target
    info = None
    if frame is not None:
        info = PngInfo()
        info.add_text(FRAME_CHUNK, json.dumps(frame))
    img.save(target, "PNG", pnginfo=info)
    return target
