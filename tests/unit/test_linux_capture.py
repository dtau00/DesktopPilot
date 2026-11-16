"""Screen copies on X11: a top-level window is shot as its X frame, every
copy comes from the root window, and a screenshot is that copy saved."""

from __future__ import annotations

import sys

import pytest
from PIL import Image

from desktoppilot.core import capture, ui

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="X11 capture")


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Windowed:
    def __init__(self, rect, handle=0):
        self.BoundingRectangle = Rect(*rect)
        self.NativeWindowHandle = handle

    def GetParentControl(self):
        return None


@pytest.fixture
def x11(monkeypatch):
    grabs = []
    monkeypatch.setattr(ui.win32gui, "GetAncestor", lambda handle, flag: 1)
    monkeypatch.setattr(capture.x11, "frame_rect", lambda w: (95, 170, 1105, 1010))

    def grab(left, top, width, height):
        grabs.append((left, top, width, height))
        return Image.new("RGB", (width, height), "red")

    monkeypatch.setattr(capture.x11, "grab", grab)
    return grabs


def test_a_top_level_window_is_shot_as_its_x_frame(x11):
    assert ui.capture_origin(Windowed((100, 200, 1100, 1000), handle=1)) == (95, 170, 1010, 840)


def test_a_child_control_is_shot_as_its_bounding_rectangle(x11):
    assert ui.capture_origin(Windowed((300, 400, 350, 420), handle=0)) == (300, 400, 50, 20)


def test_a_screenshot_is_the_root_windows_copy_of_its_rect(x11, tmp_path):
    path = ui.screenshot(Windowed((300, 400, 350, 420)), tmp_path / "shot.png")
    assert x11 == [(300, 400, 50, 20)]
    assert Image.open(path).size == (50, 20)


def test_a_rect_is_grabbed_from_the_root_window(x11):
    assert ui.capture_rect(10, 20, 30, 40).size == (30, 40)
    assert x11 == [(10, 20, 30, 40)]
