"""Draw on a screenshot: boxes, arrows, labels, badges, highlights, blur, and
crop/resize/pad. Pure Pillow - no UIA, no win32.

A list of ops is applied in order. Every target is image pixels ('X,Y,W,H',
'X,Y') or a selector; a selector is resolved by a callback the CLI supplies
(screen px) and mapped through a `Frame`, the screen-to-image rule of the shot
the image came from. Crop, pad and resize move the frame along, so a selector
still lands right after them:

    image = (screen - origin) * scale
"""

from __future__ import annotations

import functools
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from .errors import PilotError
from .imaging import FRAME_CHUNK

Rect = tuple[int, int, int, int]  # left, top, width, height
Point = tuple[int, int]

# House style: every value an op may override.
COLOR = "#E60000"
WIDTH = 3
TEXT_SIZE = 16
TEXT_COLOR = "#FFFFFF"
TEXT_BG = "#202020"
TEXT_BG_OPACITY = 0.85
TEXT_PAD = 4
GAP = 6  # target to label or badge
CALLOUT_GAP = 24
CALLOUT_WIDTH = 2
RADIUS = 8
DIM_OPACITY = 0.55
FILL_COLOR = "#FFE600"
FILL_OPACITY = 0.35
BLUR_RADIUS = 8
BLOCK = 12
PAD_COLOR = "#FFFFFF"
# Yu Gothic first: it has Japanese and Latin glyphs, so no empty boxes for CJK.
FONTS = (
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
)
EPSILON = 1e-9


@dataclass
class Frame:
    """Where an image sits on screen: screen px of image pixel (0, 0), and
    image px per screen px."""

    origin: tuple[float, float]
    scale: float

    def to_image(self, rect: Rect) -> Rect:
        left, top, w, h = rect
        x0, y0 = self._point(left, top)
        x1, y1 = self._point(left + w, top + h)
        return x0, y0, x1 - x0, y1 - y0

    def _point(self, x: float, y: float) -> Point:
        return (
            math.floor((x - self.origin[0]) * self.scale + EPSILON),
            math.floor((y - self.origin[1]) * self.scale + EPSILON),
        )

    def as_dict(self) -> dict:
        return {"origin": list(self.origin), "scale": self.scale}


def read_frame(img: Image.Image) -> Frame | None:
    """The frame `shot` wrote into the PNG, if any."""
    raw = getattr(img, "text", {}).get(FRAME_CHUNK)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Frame(origin=tuple(data["origin"]), scale=float(data["scale"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise PilotError(f"the {FRAME_CHUNK!r} PNG chunk is not a frame: {raw!r}") from exc


@functools.lru_cache(maxsize=32)
def font(size: int, path: str | None = None):
    for candidate in (path,) if path else FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            if path:
                raise PilotError(f"cannot load font {path!r}") from None
    return ImageFont.load_default(size=size)


# ----------------------------------------------------------------- targets


class _Op:
    """One op being applied: its dict, its index, and target parsing."""

    def __init__(self, index: int, spec: dict, frame: Frame | None, resolve):
        self.index, self.spec, self.frame, self.resolve = index, spec, frame, resolve
        self.type = spec.get("type")
        self.clipped = False

    def __str__(self) -> str:
        return f"ops[{self.index}] ({self.type})"

    def get(self, key, default=None):
        return self.spec.get(key, default)

    def need(self, key):
        if key not in self.spec:
            raise PilotError(f"{self} needs {key!r}")
        return self.spec[key]

    def target(self, key: str) -> Rect | Point:
        """A 4-tuple rect or a 2-tuple point, in image px."""
        value = self.need(key)
        if isinstance(value, list | tuple):
            numbers = list(value)
        else:
            try:
                numbers = [float(part) for part in str(value).split(",")]
            except ValueError:
                numbers = None
        if numbers is not None:
            if len(numbers) not in (2, 4) or not all(isinstance(n, int | float) for n in numbers):
                raise PilotError(
                    f"{self} {key!r} wants 'X,Y,W,H', 'X,Y' or a selector; got {value!r}"
                )
            return tuple(round(n) for n in numbers)
        if self.frame is None:
            raise PilotError(
                f"{self} {key!r} is a selector, but there is no origin/scale: this PNG was not "
                "made by `pilot shot`; pass --origin X,Y --scale S"
            )
        if self.resolve is None:
            raise PilotError(f"{self} {key!r} is a selector, and there is no app to resolve it in")
        return self.frame.to_image(self.resolve(str(value)))

    def rect(self, key: str = "at") -> Rect:
        value = self.target(key)
        if len(value) != 4:
            raise PilotError(f"{self} {key!r} wants a box 'X,Y,W,H' or a selector; got a point")
        return value

    def check(self, box: Rect, size: tuple[int, int]) -> None:
        """No overlap is an error; a partial one is drawn and reported."""
        left, top, w, h = box
        if left >= size[0] or top >= size[1] or left + w <= 0 or top + h <= 0 or w <= 0 or h <= 0:
            raise PilotError(f"{self}: box {list(box)} is outside the {size[0]}x{size[1]} image")
        if left < 0 or top < 0 or left + w > size[0] or top + h > size[1]:
            self.clipped = True

    def color(self, key: str = "color", default: str = COLOR) -> tuple[int, int, int]:
        value = self.get(key, default)
        try:
            return ImageColor.getrgb(value)[:3]
        except (ValueError, AttributeError) as exc:
            raise PilotError(f"{self} {key!r} is not a colour: {value!r}") from exc

    def font(self, size: int):
        return font(size, self.get("font"))


def _center(rect: Rect) -> Point:
    return rect[0] + rect[2] // 2, rect[1] + rect[3] // 2


def _point(value: Rect | Point) -> Point:
    return _center(value) if len(value) == 4 else value


def _edge_toward(rect: Rect, toward: Point) -> Point:
    """The midpoint of the edge of `rect` that faces `toward`."""
    left, top, w, h = rect
    cx, cy = _center(rect)
    dx = (toward[0] - cx) / max(w / 2, 1)
    dy = (toward[1] - cy) / max(h / 2, 1)
    if abs(dx) >= abs(dy):
        return (left if dx < 0 else left + w - 1), cy
    return cx, (top if dy < 0 else top + h - 1)


def _clamp(p: Point, rect: Rect) -> Point:
    left, top, w, h = rect
    return min(max(p[0], left), left + w - 1), min(max(p[1], top), top + h - 1)


def _corners(rect: Rect) -> list[int]:
    return [rect[0], rect[1], rect[0] + rect[2] - 1, rect[1] + rect[3] - 1]


def _span(a: Point, b: Point, pad: int = 0) -> Rect:
    left, top = min(a[0], b[0]) - pad, min(a[1], b[1]) - pad
    return left, top, abs(a[0] - b[0]) + 1 + 2 * pad, abs(a[1] - b[1]) + 1 + 2 * pad


# ---------------------------------------------------------------- painting


def _paint(img: Image.Image, rgb, opacity: float, paint: Callable) -> Image.Image:
    """Draw a mask with `paint`, then lay one colour through it at `opacity`:
    antialiased edges blend, and opacity is the same for every op."""
    mask = Image.new("L", img.size, 0)
    paint(ImageDraw.Draw(mask))
    layer = Image.new("RGBA", img.size, (*rgb, 0))
    layer.putalpha(mask.point(lambda a: round(a * opacity)))
    return Image.alpha_composite(img, layer)


def _opacity(op: _Op, default: float = 1.0) -> float:
    value = float(op.get("opacity", default))
    if not 0 <= value <= 1:
        raise PilotError(f"{op} 'opacity' wants 0-1; got {value}")
    return value


def _shape(img, op: _Op, draw_one):
    box = op.rect()
    op.check(box, img.size)
    rgb, opacity, width = op.color(), _opacity(op), int(op.get("width", WIDTH))
    fill = op.get("fill", False)
    if fill:
        fill_rgb = rgb if fill is True else op.color("fill")
        img = _paint(img, fill_rgb, opacity, lambda d: draw_one(d, _corners(box), 255, None, 0))
    img = _paint(img, rgb, opacity, lambda d: draw_one(d, _corners(box), None, 255, width))
    return img, {"box": list(box)}


def _box(img, op):
    return _shape(
        img,
        op,
        lambda d, xy, fill, outline, w: d.rectangle(xy, fill=fill, outline=outline, width=w),
    )


def _rounded(img, op):
    radius = int(op.get("radius", RADIUS))
    return _shape(
        img,
        op,
        lambda d, xy, fill, outline, w: d.rounded_rectangle(
            xy, radius, fill=fill, outline=outline, width=w
        ),
    )


def _ellipse(img, op):
    return _shape(
        img, op, lambda d, xy, fill, outline, w: d.ellipse(xy, fill=fill, outline=outline, width=w)
    )


def _ends(op: _Op) -> tuple[Point, Point]:
    start_target, end_target = op.target("from"), op.target("to")
    start = _point(start_target)
    end = _point(end_target)
    if op.type == "arrow" and len(end_target) == 4:
        end = _edge_toward(end_target, start)
    return start, end


def _line(img, op):
    start, end = _ends(op)
    width = int(op.get("width", WIDTH))
    op.check(_span(start, end, width // 2), img.size)
    img = _paint(img, op.color(), _opacity(op), lambda d: d.line([start, end], 255, width))
    return img, {"box": list(_span(start, end, width // 2)), "from": list(start), "to": list(end)}


def _arrow(img, op):
    start, end = _ends(op)
    width = int(op.get("width", WIDTH))
    head = 4 * width
    op.check(_span(start, end, head // 2), img.size)
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy) or 1
    ux, uy = dx / length, dy / length
    back = min(head, length)
    base = (end[0] - ux * back, end[1] - uy * back)
    half = back / 2
    wing_a = (base[0] - uy * half, base[1] + ux * half)
    wing_b = (base[0] + uy * half, base[1] - ux * half)

    def paint(d):
        if length > back:
            d.line([start, base], 255, width)
        d.polygon([end, wing_a, wing_b], fill=255)

    img = _paint(img, op.color(), _opacity(op), paint)
    return img, {"box": list(_span(start, end, head // 2)), "from": list(start), "to": list(end)}


def _label_size(fnt, text: str, pad: int) -> tuple[tuple[int, int, int, int], int, int]:
    bbox = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=fnt)
    return bbox, bbox[2] - bbox[0] + 2 * pad, bbox[3] - bbox[1] + 2 * pad


def _label_at(rect: Rect, size: tuple[int, int], anchor: str, gap: int) -> Point:
    left, top, w, h = rect
    bw, bh = size
    spots = {
        "below": (left, top + h + gap),
        "above": (left, top - gap - bh),
        "right": (left + w + gap, top),
        "left": (left - gap - bw, top),
        "inside": (left + gap, top + gap),
    }
    if anchor not in spots:
        raise PilotError(f"anchor wants one of {', '.join(spots)}; got {anchor!r}")
    return spots[anchor]


def _draw_label(img, op: _Op, text: str, at: Point, text_rgb) -> tuple[Image.Image, Rect]:
    size, pad = int(op.get("size", TEXT_SIZE)), int(op.get("pad", TEXT_PAD))
    fnt = op.font(size)
    bbox, bw, bh = _label_size(fnt, text, pad)
    box = (at[0], at[1], bw, bh)
    op.check(box, img.size)
    opacity = _opacity(op)
    bg = op.get("bg", TEXT_BG)
    if bg is not False and bg is not None:
        bg_opacity = float(op.get("bg_opacity", TEXT_BG_OPACITY)) * opacity
        img = _paint(
            img, op.color("bg", TEXT_BG), bg_opacity, lambda d: d.rectangle(_corners(box), fill=255)
        )
    origin = (at[0] + pad - bbox[0], at[1] + pad - bbox[1])
    img = _paint(img, text_rgb, opacity, lambda d: d.text(origin, text, font=fnt, fill=255))
    return img, box


def _text(img, op):
    text = str(op.need("text"))
    target = op.target("at")
    if len(target) == 4:
        fnt = op.font(int(op.get("size", TEXT_SIZE)))
        _, bw, bh = _label_size(fnt, text, int(op.get("pad", TEXT_PAD)))
        at = _label_at(target, (bw, bh), op.get("anchor", "below"), GAP)
    else:
        at = target
    img, box = _draw_label(img, op, text, at, op.color("color", TEXT_COLOR))
    return img, {"box": list(box)}


def _badge(img, op):
    text = str(op.get("n", op.get("text", "1")))
    size = int(op.get("size", TEXT_SIZE))
    radius = round(2.2 * size) // 2
    target = op.target("at")
    if len(target) == 4:
        left, top, w, h = target
        right, bottom = left + w - 1, top + h - 1
        cx, cy = _center(target)
        out = GAP + radius
        spots = {
            "tl": (left, top),
            "tr": (right, top),
            "bl": (left, bottom),
            "br": (right, bottom),
            "left": (left - out, cy),
            "right": (right + out, cy),
            "above": (cx, top - out),
            "below": (cx, bottom + out),
        }
        anchor = op.get("anchor", "tl")
        if anchor not in spots:
            raise PilotError(f"{op} anchor wants one of {', '.join(spots)}; got {anchor!r}")
        center = spots[anchor]
    else:
        center = target
    box = (center[0] - radius, center[1] - radius, 2 * radius + 1, 2 * radius + 1)
    op.check(box, img.size)
    opacity = _opacity(op)
    img = _paint(img, op.color(), opacity, lambda d: d.ellipse(_corners(box), fill=255))
    fnt = op.font(size)
    x0, y0, x1, y1 = fnt.getbbox(text)
    origin = (center[0] - (x0 + x1) / 2, center[1] - (y0 + y1) / 2)
    stroke = max(1, size // 16)  # bold-ish

    def paint(d):
        d.text(origin, text, font=fnt, fill=255, stroke_width=stroke, stroke_fill=255)

    img = _paint(img, op.color("text_color", TEXT_COLOR), opacity, paint)
    return img, {"box": list(box)}


def _callout(img, op):
    text = str(op.need("text"))
    target = op.rect()
    op.check(target, img.size)
    fnt = op.font(int(op.get("size", TEXT_SIZE)))
    _, bw, bh = _label_size(fnt, text, int(op.get("pad", TEXT_PAD)))
    iw, ih = img.size
    anchors = [op.get("anchor")] if op.get("anchor") else ["below", "above", "right", "left"]
    for anchor in anchors:
        at = _label_at(target, (bw, bh), anchor, CALLOUT_GAP)
        if at[0] >= 0 and at[1] >= 0 and at[0] + bw <= iw and at[1] + bh <= ih:
            break
    else:
        anchor = anchors[0]
        at = _label_at(target, (bw, bh), anchor, CALLOUT_GAP)
        at = (min(max(at[0], 0), max(iw - bw, 0)), min(max(at[1], 0), max(ih - bh, 0)))
    label = (at[0], at[1], bw, bh)
    near_label = _clamp(_center(target), label)
    near_target = _clamp(near_label, target)
    width = int(op.get("line_width", CALLOUT_WIDTH))
    rgb, opacity = op.color(), _opacity(op)
    if near_label != near_target:
        img = _paint(img, rgb, opacity, lambda d: d.line([near_label, near_target], 255, width))
    img, _ = _draw_label(img, op, text, at, op.color("text_color", TEXT_COLOR))
    return img, {"box": list(target), "label": list(label), "anchor": anchor}


def _highlight(img, op):
    box = op.rect()
    op.check(box, img.size)
    mode = op.get("mode", "dim")
    if mode == "dim":

        def paint(d):
            d.rectangle([0, 0, img.width, img.height], fill=255)
            d.rectangle(_corners(box), fill=0)

        img = _paint(img, op.color("color", "#000000"), _opacity(op, DIM_OPACITY), paint)
    elif mode == "fill":
        img = _paint(
            img,
            op.color("color", FILL_COLOR),
            _opacity(op, FILL_OPACITY),
            lambda d: d.rectangle(_corners(box), fill=255),
        )
    else:
        raise PilotError(f"{op} mode wants dim or fill; got {mode!r}")
    return img, {"box": list(box), "mode": mode}


def _inside(img, op) -> Rect:
    left, top, w, h = op.rect()
    op.check((left, top, w, h), img.size)
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + w, img.width), min(top + h, img.height)
    return x0, y0, x1 - x0, y1 - y0


def _blur(img, op):
    left, top, w, h = box = _inside(img, op)
    part = img.crop((left, top, left + w, top + h))
    img.paste(part.filter(ImageFilter.GaussianBlur(float(op.get("radius", BLUR_RADIUS)))), box[:2])
    return img, {"box": list(box)}


def _pixelate(img, op):
    left, top, w, h = box = _inside(img, op)
    block = max(1, int(op.get("block", BLOCK)))
    part = img.crop((left, top, left + w, top + h))
    # BOX (the block's average) going down, so no single stray pixel survives.
    small = part.resize((max(1, w // block), max(1, h // block)), Image.Resampling.BOX)
    img.paste(small.resize((w, h), Image.Resampling.NEAREST), box[:2])
    return img, {"box": list(box)}


# ------------------------------------------------------------------- edits


def _crop(img, op, frame):
    left, top, w, h = op.rect()
    margin = int(op.get("margin", 0))
    grown = (left - margin, top - margin, w + 2 * margin, h + 2 * margin)
    op.check(grown, img.size)
    x0, y0 = max(grown[0], 0), max(grown[1], 0)
    x1, y1 = min(grown[0] + grown[2], img.width), min(grown[1] + grown[3], img.height)
    img = img.crop((x0, y0, x1, y1))
    if frame is not None:
        ox, oy = frame.origin
        frame = Frame((ox + x0 / frame.scale, oy + y0 / frame.scale), frame.scale)
    return img, frame, {"box": [x0, y0, x1 - x0, y1 - y0], "image_size": list(img.size)}


def _resize(img, op, frame):
    if "scale" in op.spec:
        factor = float(op.spec["scale"])
    elif "width" in op.spec:
        factor = int(op.spec["width"]) / img.width
    elif "height" in op.spec:
        factor = int(op.spec["height"]) / img.height
    else:
        raise PilotError(f"{op} needs 'scale', 'width' or 'height'")
    if factor <= 0:
        raise PilotError(f"{op} wants a positive size; got factor {factor}")
    size = (max(1, round(img.width * factor)), max(1, round(img.height * factor)))
    exact = factor >= 1 and abs(factor - round(factor)) < EPSILON
    resample = Image.Resampling.NEAREST if exact else Image.Resampling.LANCZOS
    img = img.resize(size, resample)
    if frame is not None:
        frame = Frame(frame.origin, frame.scale * factor)
    return img, frame, {"box": [0, 0, *size], "image_size": list(size)}


def _pad(img, op, frame):
    margin, border = int(op.get("margin", 0)), int(op.get("border", 0))
    size = (img.width + 2 * margin, img.height + 2 * margin)
    out = Image.new("RGBA", size, (*op.color("color", PAD_COLOR), 255))
    out.paste(img, (margin, margin))
    if border > 0:
        ring = [0, 0, size[0] - 1, size[1] - 1]
        out = _paint(
            out,
            op.color("border_color", COLOR),
            1.0,
            lambda d: d.rectangle(ring, outline=255, width=border),
        )
    if frame is not None:
        ox, oy = frame.origin
        frame = Frame((ox - margin / frame.scale, oy - margin / frame.scale), frame.scale)
    return out, frame, {"box": [0, 0, *size], "image_size": list(size)}


DRAW = {
    "box": _box,
    "rounded": _rounded,
    "ellipse": _ellipse,
    "line": _line,
    "arrow": _arrow,
    "text": _text,
    "badge": _badge,
    "callout": _callout,
    "highlight": _highlight,
    "blur": _blur,
    "pixelate": _pixelate,
}
EDIT = {"crop": _crop, "resize": _resize, "pad": _pad}
TYPES = (*DRAW, *EDIT)


def apply(
    img: Image.Image,
    ops: list[dict],
    frame: Frame | None = None,
    resolve: Callable[[str], Rect] | None = None,
) -> tuple[Image.Image, Frame | None, list[dict]]:
    """Apply `ops` in order. `resolve(selector)` gives a screen rect.
    Returns the RGB image, the frame after any crop/pad/resize, and one
    result per op (its final box in image px)."""
    out = img.convert("RGBA")
    results = []
    for index, spec in enumerate(ops):
        if not isinstance(spec, dict):
            raise PilotError(f"ops[{index}] is not an object: {spec!r}")
        op = _Op(index, spec, frame, resolve)
        if op.type in DRAW:
            out, result = DRAW[op.type](out, op)
        elif op.type in EDIT:
            out, frame, result = EDIT[op.type](out, op, frame)
        else:
            raise PilotError(f"ops[{index}] type {op.type!r} is not one of {', '.join(TYPES)}")
        result = {"type": op.type, **result}
        if op.clipped:
            result["clipped"] = True
        results.append(result)
    return out.convert("RGB"), frame, results


def join(images: list[Image.Image], *, vertical=False, gap=0, color=PAD_COLOR) -> Image.Image:
    """Side by side (or stacked), aligned top (or left), `gap` px of `color`
    between them."""
    sizes = [i.size for i in images]
    spacing = gap * (len(images) - 1)
    if vertical:
        size = (max(w for w, _ in sizes), sum(h for _, h in sizes) + spacing)
    else:
        size = (sum(w for w, _ in sizes) + spacing, max(h for _, h in sizes))
    try:
        out = Image.new("RGB", size, ImageColor.getrgb(color)[:3])
    except ValueError as exc:
        raise PilotError(f"--color is not a colour: {color!r}") from exc
    at = 0
    for img in images:
        out.paste(img.convert("RGB"), (0, at) if vertical else (at, 0))
        at += (img.height if vertical else img.width) + gap
    return out


def load(path: str | Path) -> Image.Image:
    try:
        img = Image.open(Path(path).expanduser())
        img.load()
    except (OSError, ValueError) as exc:
        raise PilotError(f"cannot read image {str(path)!r}: {exc}") from exc
    return img
