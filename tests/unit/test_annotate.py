"""Annotation ops and the `image` group: no display, synthetic images."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from desktoppilot.cli import build
from desktoppilot.core import annotate, imaging
from desktoppilot.core.annotate import Frame
from desktoppilot.core.errors import PilotError

WHITE = (255, 255, 255)
RED = (230, 0, 0)


def canvas(size=(200, 120), color=WHITE):
    return Image.new("RGB", size, color)


def apply(ops, img=None, frame=None, resolve=None):
    return annotate.apply(img or canvas(), ops, frame, resolve)


def ink(img, box, background=WHITE) -> int:
    left, top, w, h = box
    colors = img.crop((left, top, left + w, top + h)).getcolors(w * h)
    return sum(count for count, color in colors if color != background)


# -------------------------------------------------------------------- frame


def test_a_screen_rect_maps_onto_a_cropped_zoomed_shot():
    assert Frame((110, 70), 15).to_image((120, 80, 10, 5)) == (150, 150, 150, 75)


def test_a_selector_box_stays_right_after_crop_pad_and_resize():
    rect = (120, 80, 10, 5)
    ops = [
        {"type": "crop", "at": "100,100,400,300"},
        {"type": "pad", "margin": 20},
        {"type": "resize", "scale": 2},
        {"type": "box", "at": "target"},
    ]
    img = canvas((800, 600))
    _, frame, results = apply(ops, img, Frame((110, 70), 15), lambda sel: rect)
    # (150,150) -> crop -(100,100) -> pad +20 -> x2
    assert results[-1]["box"] == [140, 140, 300, 150]
    assert frame.to_image(rect) == (140, 140, 300, 150)


def test_a_selector_resolved_by_the_callback_lands_on_the_mapped_box():
    seen = []

    def resolve(sel):
        seen.append(sel)
        return (20, 10, 30, 20)

    out, _, results = apply(
        [{"type": "box", "at": "app//Button"}], frame=Frame((10, 5), 2), resolve=resolve
    )
    assert seen == ["app//Button"] and results[0]["box"] == [20, 10, 60, 40]
    assert out.getpixel((20, 30)) == RED


def test_a_selector_without_a_frame_asks_for_origin_and_scale():
    with pytest.raises(PilotError, match="--origin"):
        apply([{"type": "box", "at": "ribbon"}])


# ------------------------------------------------------------------- shapes


def test_a_box_outlines_its_edge_and_leaves_the_centre():
    out, _, results = apply([{"type": "box", "at": "10,10,50,30"}])
    assert out.getpixel((10, 20)) == RED and out.getpixel((59, 20)) == RED
    assert out.getpixel((35, 25)) == WHITE
    assert results == [{"type": "box", "box": [10, 10, 50, 30]}]


def test_a_filled_box_paints_its_centre():
    out, _, _ = apply([{"type": "box", "at": [10, 10, 50, 30], "fill": "#0000FF"}])
    assert out.getpixel((35, 25)) == (0, 0, 255) and out.getpixel((10, 20)) == RED


def test_an_ellipse_touches_its_sides_but_not_its_corners():
    out, _, _ = apply([{"type": "ellipse", "at": "10,10,60,40"}])
    assert out.getpixel((11, 30)) == RED and out.getpixel((11, 11)) == WHITE


def test_a_line_runs_between_its_points():
    out, _, results = apply([{"type": "line", "from": "10,50", "to": "150,50"}])
    assert out.getpixel((10, 50)) == RED and out.getpixel((150, 50)) == RED
    assert out.getpixel((80, 60)) == WHITE
    assert results[0]["from"] == [10, 50] and results[0]["to"] == [150, 50]


def test_an_arrow_to_a_box_ends_on_its_nearest_edge():
    out, _, results = apply([{"type": "arrow", "from": "10,50", "to": "100,40,40,20"}])
    assert results[0]["to"] == [100, 50]
    assert out.getpixel((99, 50)) == RED  # the head's tip
    assert out.getpixel((120, 50)) == WHITE  # not the box centre


def test_half_opacity_blends_with_the_image():
    out, _, _ = apply([{"type": "box", "at": "10,10,50,30", "fill": True, "opacity": 0.5}])
    r, g, b = out.getpixel((35, 25))
    assert abs(r - 242) <= 1 and abs(g - 127) <= 1


def test_a_box_wholly_outside_the_image_names_the_op():
    with pytest.raises(PilotError, match=r"ops\[1\] \(box\).*200x120"):
        apply([{"type": "box", "at": "1,1,5,5"}, {"type": "box", "at": "300,10,20,20"}])


def test_a_box_partly_outside_is_drawn_and_reported_clipped():
    _, _, results = apply([{"type": "box", "at": "180,10,50,30"}])
    assert results[0]["clipped"] is True


# --------------------------------------------------------------------- text


def test_text_draws_inside_its_box_on_a_dark_label():
    out, _, results = apply([{"type": "text", "at": "20,10,50,20", "text": "Hello"}])
    box = results[0]["box"]
    assert box[:2] == [20, 36]  # below, 6 px gap
    corner = out.getpixel((box[0], box[1]))
    assert corner == (65, 65, 65)  # #202020 at 0.85 over white
    assert ink(out, box, corner) > 0


def test_a_badge_is_a_red_circle_with_a_white_digit():
    out, _, results = apply([{"type": "badge", "at": "50,50", "n": 3}])
    left, top, w, h = results[0]["box"]
    assert (left + w // 2, top + h // 2) == (50, 50)
    assert out.getpixel((left + 3, top + h // 2)) == RED
    inner = (left + 8, top + 8, w - 16, h - 16)
    assert ink(out, inner, RED) > 0 and out.getpixel((5, 5)) == WHITE


def test_a_callout_goes_above_when_below_would_leave_the_image():
    out, _, results = apply([{"type": "callout", "at": "20,80,40,30", "text": "Pick"}])
    result = results[0]
    label = result["label"]
    assert result["anchor"] == "above" and label[1] + label[3] <= 80 - 24
    assert out.getpixel((40, 70)) == RED  # the leader line between label and box


def test_font_renders_japanese_with_ink():
    if not any(Path(p).exists() for p in annotate.FONTS):
        pytest.skip("no candidate font installed")
    font = annotate.font(16)
    assert hasattr(font, "path")
    out, _, results = apply(
        [{"type": "text", "at": "5,5", "text": "日本語", "bg": False, "color": "#000000"}]
    )
    assert ink(out, results[0]["box"]) > 20


# ------------------------------------------------------------ highlight etc.


def test_highlight_dim_keeps_the_rect_and_darkens_outside():
    out, _, _ = apply([{"type": "highlight", "at": "50,50,40,40"}])
    assert out.getpixel((60, 60)) == WHITE
    assert out.getpixel((5, 5))[0] < 130


def test_highlight_fill_tints_the_rect_only():
    out, _, _ = apply([{"type": "highlight", "at": "50,50,40,40", "mode": "fill"}])
    r, g, b = out.getpixel((60, 60))
    assert r == 255 and b < 200 and out.getpixel((5, 5)) == WHITE


def stripes():
    img = canvas()
    for x in range(0, 200, 2):
        img.paste((0, 0, 0), (x, 0, x + 1, 120))
    return img


@pytest.mark.parametrize("op", [{"type": "blur"}, {"type": "pixelate", "block": 8}])
def test_blur_and_pixelate_change_inside_not_outside(op):
    src = stripes()
    out, _, _ = apply([{**op, "at": "40,40,48,48"}], src.copy())
    assert out.crop((40, 40, 88, 88)).tobytes() != src.crop((40, 40, 88, 88)).tobytes()
    assert out.crop((0, 0, 30, 30)).tobytes() == src.crop((0, 0, 30, 30)).tobytes()


# -------------------------------------------------------------------- edits


def test_crop_resize_and_pad_set_the_size_and_pad_draws_a_red_border():
    ops = [
        {"type": "crop", "at": "10,10,50,30", "margin": 5},
        {"type": "resize", "scale": 2},
        {"type": "pad", "margin": 10, "border": 2},
    ]
    out, frame, results = apply(ops)
    assert [r["image_size"] for r in results] == [[60, 40], [120, 80], [140, 100]]
    assert out.getpixel((0, 0)) == RED and out.getpixel((5, 5)) == WHITE
    assert frame is None


def test_resize_to_a_width_keeps_the_aspect():
    out, _, _ = apply([{"type": "resize", "width": 100}])
    assert out.size == (100, 60)


def test_join_sizes():
    a, b = canvas((10, 20)), canvas((30, 5))
    assert annotate.join([a, b], gap=4).size == (44, 20)
    assert annotate.join([a, b], vertical=True).size == (30, 25)


# --------------------------------------------------------------------- save


def test_a_png_keeps_the_frame_and_a_jpeg_has_none(tmp_path):
    frame = {"origin": [110, 70], "scale": 15.0}
    png = imaging.save_image(canvas(), tmp_path / "a.png", frame=frame)
    jpg = imaging.save_image(canvas(), tmp_path / "a.jpg", frame=frame)
    assert annotate.read_frame(Image.open(png)) == Frame((110, 70), 15.0)
    assert annotate.read_frame(Image.open(jpg)) is None
    assert Image.open(jpg).format == "JPEG"


# ---------------------------------------------------------------------- CLI


def pilot(*args):
    result = CliRunner().invoke(build([]), ["desktop", "image", *args])
    return result.exit_code, json.loads(result.stdout)


def test_annotate_writes_beside_src_and_never_over_it(tmp_path):
    src = tmp_path / "s.png"
    imaging.save_image(canvas(), src, frame={"origin": [0, 0], "scale": 1.0})
    before = src.read_bytes()
    op = json.dumps({"type": "box", "at": "10,10,50,30"})

    code, out = pilot("annotate", str(src), "--op", op)
    assert code == 0 and out["path"] == str(tmp_path / "s-annotated.png")
    assert out["annotations"][0]["box"] == [10, 10, 50, 30] and out["frame"]["scale"] == 1.0
    assert src.read_bytes() == before

    code, out = pilot("annotate", str(src), "-o", str(src), "--op", op)
    assert code == 1 and "--in-place" in out["message"]

    code, out = pilot("annotate", str(src), "--in-place", "--op", op)
    assert code == 0 and out["path"] == str(src) and src.read_bytes() != before


def test_join_cli_sizes(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    canvas((10, 20)).save(a)
    canvas((30, 5)).save(b)
    code, out = pilot("join", str(a), str(b), "-o", str(tmp_path / "j.png"), "--vertical")
    assert code == 0 and out["image_size"] == [30, 25]
