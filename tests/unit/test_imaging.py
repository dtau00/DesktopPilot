"""Crop, zoom and grid arithmetic for `shot`: no display, synthetic images."""

from __future__ import annotations

import pytest
from PIL import Image

from desktoppilot.core import imaging
from desktoppilot.core.errors import PilotError

SCREEN = (0, 0, 2560, 1600)
WINDOW = (100, 50, 1000, 800)


def plan(region=None, zoom=None, base=WINDOW, screen=SCREEN):
    return imaging.plan(base, region, zoom, screen)


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize("value", ["1,2,3", "a,b,c,d", "1,2,3,4,5", "1,2,0,4", "1,2,3,-4", ""])
def test_a_bad_region_is_refused(value):
    with pytest.raises(PilotError, match="--region"):
        imaging.parse_region(value)


def test_a_region_parses_with_spaces_and_a_negative_origin():
    assert imaging.parse_region(" -5, 10 ,20,30") == (-5, 10, 20, 30)


@pytest.mark.parametrize("value", ["0", "17", "x", "2.5", "-1"])
def test_a_zoom_outside_1_to_16_is_refused(value):
    with pytest.raises(PilotError, match="--zoom"):
        imaging.parse_zoom(value)


def test_zoom_parses_auto_and_integers():
    assert imaging.parse_zoom("AUTO") == "auto"
    assert imaging.parse_zoom("16") == 16
    assert imaging.parse_zoom(None) is None


@pytest.mark.parametrize("value", ["1", "0", "x"])
def test_a_grid_under_2_px_is_refused(value):
    with pytest.raises(PilotError, match="--grid"):
        imaging.parse_grid(value)


# --------------------------------------------------------------------- plan


def test_a_plain_shot_is_the_whole_rect_at_scale_1():
    p = plan()
    assert p.origin == (100, 50) and p.size == (1000, 800)
    assert p.scale == 1.0 and p.resample == "none" and p.clipped is False


def test_a_region_is_relative_to_the_capture_origin_and_defaults_to_auto():
    p = plan(region=(10, 20, 100, 50))
    assert p.origin == (110, 70) and p.size == (100, 50)
    # 1568 / 100 -> 15; 100*50*15^2 = 1.125 MP <= 1.15 MP
    assert p.scale == 15.0 and p.resample == "nearest"


def test_auto_picks_the_largest_factor_inside_both_caps():
    # long edge: 1568 / 200 = 7.84 -> 7; area: sqrt(1.15e6 / 40000) = 5.36 -> 5
    assert plan(region=(0, 0, 200, 200), zoom="auto").scale == 5.0
    # long edge binds: 1568 / 500 = 3.1 -> 3; area would allow 4.8
    assert plan(region=(0, 0, 500, 100), zoom="auto").scale == 3.0


def test_auto_never_passes_the_zoom_limit():
    assert plan(region=(0, 0, 10, 10), zoom="auto").scale == imaging.MAX_ZOOM


def test_an_oversized_source_is_shrunk_with_lanczos():
    p = plan(base=(0, 0, 2560, 1600), zoom="auto")
    assert p.scale < 1 and p.resample == "lanczos"
    w, h = p.image_size
    assert max(w, h) <= imaging.AUTO_LONG_EDGE and w * h <= imaging.AUTO_MAX_PIXELS


def test_a_region_is_clamped_to_the_base_rect():
    p = plan(region=(900, 700, 300, 300))
    assert p.origin == (1000, 750) and p.size == (100, 100) and p.clipped is True


def test_a_region_is_clamped_to_the_virtual_screen():
    p = plan(base=(-50, 0, 400, 300), region=(0, 0, 100, 100), screen=(0, 0, 1920, 1080))
    assert p.origin == (0, 0) and p.size == (50, 100) and p.clipped is True


def test_a_negative_virtual_screen_origin_is_a_real_place():
    screen = (-1920, -200, 4480, 1800)
    p = plan(base=(-1800, -100, 800, 600), region=(10, 20, 30, 40), screen=screen)
    assert p.origin == (-1790, -80) and p.clipped is False
    assert imaging.image_to_screen(p, 0, 0) == (-1790, -80)


def test_a_region_that_is_empty_after_the_clamp_is_refused():
    with pytest.raises(PilotError, match="empty"):
        plan(region=(2000, 0, 50, 50))


@pytest.mark.parametrize(
    ("size", "zoom"),
    [((1100, 10), 16), ((1000, 1000), 9)],  # 17600 px edge; 81 MP
)
def test_an_explicit_zoom_past_the_output_limits_is_refused(size, zoom):
    with pytest.raises(PilotError, match="the most is"):
        plan(base=(0, 0, *size), zoom=zoom)


def test_an_explicit_zoom_is_honoured_past_the_auto_cap():
    p = plan(zoom=4)
    assert p.scale == 4.0 and p.image_size == (4000, 3200)


# ------------------------------------------------------------------ mapping


@pytest.mark.parametrize("zoom", [1, 3, 16])
def test_screen_to_image_and_back_is_exact(zoom):
    p = plan(region=(5, 5, 60, 40), zoom=zoom)
    for point in [(105, 55), (130, 70), (164, 94)]:
        assert imaging.image_to_screen(p, *imaging.screen_to_image(p, *point)) == point


def test_every_image_pixel_of_a_zoomed_block_maps_to_its_source_pixel():
    p = plan(region=(0, 0, 10, 10), zoom=3)
    assert {imaging.image_to_screen(p, ix, 0)[0] for ix in (3, 4, 5)} == {101}


def test_a_shrunk_image_maps_within_one_source_pixel():
    p = plan(base=(0, 0, 2560, 1600), zoom="auto")
    for ix in (0, 1, 777, p.image_size[0] - 1):
        x, _ = imaging.image_to_screen(p, ix, 0)
        assert 0 <= ix / p.scale - x < 1


# ------------------------------------------------------------------- render


def checkerboard(w, h):
    img = Image.new("RGB", (w, h))
    img.putdata([(255, 255, 255) if (x + y) % 2 else (0, 0, 0) for y in range(h) for x in range(w)])
    return img


def test_render_gives_the_planned_image_size():
    p = plan(region=(0, 0, 40, 30), zoom=5)
    out, grid = imaging.render(Image.new("RGB", (40, 30)), p)
    assert out.size == p.image_size == (200, 150) and grid is None


def test_nearest_keeps_a_one_pixel_checkerboard_exact():
    src = checkerboard(8, 6)
    p = plan(region=(0, 0, 8, 6), zoom=4)
    out, _ = imaging.render(src, p)
    for ix in range(out.width):
        for iy in range(out.height):
            assert out.getpixel((ix, iy)) == src.getpixel((ix // 4, iy // 4))


def test_grid_lines_sit_on_multiples_of_the_step_in_screen_coordinates():
    # origin (103, 57), 30x20: lines at x = 110, 120, 130 and y = 60, 70; scale 2
    p = plan(base=(103, 57, 30, 20), zoom=2)
    white = Image.new("RGB", (30, 20), (255, 255, 255))
    out, grid = imaging.render(white, p, grid=10)
    assert grid["step"] == 10
    changed_cols = {
        ix for ix in range(out.width) if out.getpixel((ix, out.height - 1)) != (255, 255, 255)
    }
    changed_rows = {
        iy for iy in range(out.height) if out.getpixel((out.width - 1, iy)) != (255, 255, 255)
    }
    assert changed_cols == {(x - 103) * 2 for x in (110, 120, 130)}
    assert changed_rows == {(60 - 57) * 2, (70 - 57) * 2}


def test_a_grid_on_an_unzoomed_shot_changes_the_pixels_on_the_lines():
    p = plan(base=(0, 0, 50, 50))
    out, grid = imaging.render(Image.new("RGB", (50, 50), (255, 255, 255)), p, grid=25)
    assert out.getpixel((25, 49)) != (255, 255, 255) and out.getpixel((24, 49)) == (255, 255, 255)
    assert grid["step"] == 25


def test_dense_crossings_label_every_mth_line():
    p = plan(base=(0, 0, 400, 400))
    _, grid = imaging.render(Image.new("RGB", (400, 400), (255, 255, 255)), p, grid=10)
    assert grid["label_every"] > 1


def test_sparse_crossings_label_every_line():
    p = plan(region=(0, 0, 100, 100), zoom=10)
    _, grid = imaging.render(Image.new("RGB", (100, 100), (255, 255, 255)), p, grid=20)
    assert grid["label_every"] == 1


def test_a_grid_denser_than_the_image_can_draw_is_refused():
    p = plan(base=(0, 0, 2560, 1600), zoom="auto")
    with pytest.raises(PilotError, match="raise the step"):
        imaging.render(Image.new("RGB", (2560, 1600)), p, grid=2)
