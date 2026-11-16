"""The pointer click: a screen point as SendInput's absolute coordinates over
the whole virtual screen, so a point on any monitor lands where it was read.

SendInput is stubbed, so none of this moves the real pointer."""

from __future__ import annotations

import pytest

from desktoppilot.core import ui
from desktoppilot.core.errors import PilotError

PRIMARY = (0, 0, 2560, 1600)
LEFT_OF_PRIMARY = (-1920, 0, 4480, 1600)  # 1920 px monitor left of a 2560 px primary
ABOVE_PRIMARY = (0, -1080, 2560, 2680)  # 1080 px monitor above the primary


def windows_pixel(normalised: int, origin: int, size: int) -> int:
    """How Windows maps an absolute coordinate back to a pixel (measured)."""
    return origin + normalised * size // 65536


@pytest.mark.parametrize("screen", [PRIMARY, LEFT_OF_PRIMARY, ABOVE_PRIMARY])
def test_every_pixel_comes_back_exact(screen):
    left, top, width, height = screen
    for x in range(left, left + width):
        nx, _ = ui.normalise_point(x, top, screen)
        assert windows_pixel(nx, left, width) == x
    for y in range(top, top + height):
        _, ny = ui.normalise_point(left, y, screen)
        assert windows_pixel(ny, top, height) == y


def test_the_primary_origin_is_mid_screen_when_a_monitor_is_left_of_it():
    nx, ny = ui.normalise_point(0, 0, LEFT_OF_PRIMARY)
    assert nx == pytest.approx(65536 * 1920 / 4480, abs=8)
    assert ny < 32


def test_the_primary_origin_is_mid_screen_when_a_monitor_is_above_it():
    nx, ny = ui.normalise_point(0, 0, ABOVE_PRIMARY)
    assert nx < 32
    assert ny == pytest.approx(65536 * 1080 / 2680, abs=16)


@pytest.mark.parametrize("screen", [PRIMARY, LEFT_OF_PRIMARY, ABOVE_PRIMARY])
def test_the_far_corners_stay_inside_0_65535(screen):
    left, top, width, height = screen
    first = ui.normalise_point(left, top, screen)
    last = ui.normalise_point(left + width - 1, top + height - 1, screen)
    assert all(0 <= n < 32 for n in first)
    assert all(65504 < n <= 65535 for n in last)


@pytest.mark.parametrize(
    "point",
    [(-1, 0), (0, -1), (2560, 0), (0, 1600), (-1921, 5), (99999, 99999)],
)
def test_a_point_outside_the_virtual_screen_is_refused(point):
    screen = LEFT_OF_PRIMARY if point == (-1921, 5) else PRIMARY
    with pytest.raises(PilotError, match="outside the virtual screen"):
        ui.normalise_point(*point, screen)


# ------------------------------------------------------------- click_point


@pytest.fixture
def sent(monkeypatch) -> list[tuple[int, int, int]]:
    """Every event click_point sends, as (flags, dx, dy)."""
    events: list[tuple[int, int, int]] = []

    def record(inputs):
        for item in inputs:
            assert item.type == ui.INPUT_MOUSE
            events.append((item.u.mi.dwFlags, item.u.mi.dx, item.u.mi.dy))

    monkeypatch.setattr(ui, "_send_input", record)
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: LEFT_OF_PRIMARY)
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)
    return events


AT = ui.MOUSEEVENTF_MOVE | ui.MOUSEEVENTF_ABSOLUTE | ui.MOUSEEVENTF_VIRTUALDESK


def test_a_left_click_moves_presses_and_releases_at_the_point(sent):
    nx, ny = ui.normalise_point(-500, 300, LEFT_OF_PRIMARY)
    assert ui.click_point(-500, 300) == "mouse"
    assert sent == [
        (AT, nx, ny),
        (AT | ui.MOUSEEVENTF_LEFTDOWN, nx, ny),
        (AT | ui.MOUSEEVENTF_LEFTUP, nx, ny),
    ]


def test_a_right_click_uses_the_right_button(sent):
    nx, ny = ui.normalise_point(10, 20, LEFT_OF_PRIMARY)
    assert ui.click_point(10, 20, button="right") == "mouse"
    assert sent == [
        (AT, nx, ny),
        (AT | ui.MOUSEEVENTF_RIGHTDOWN, nx, ny),
        (AT | ui.MOUSEEVENTF_RIGHTUP, nx, ny),
    ]


def test_a_double_click_presses_twice_inside_the_double_click_time(sent, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(ui.time, "sleep", waits.append)
    monkeypatch.setattr(ui.ctypes.windll.user32, "GetDoubleClickTime", lambda: 500)
    nx, ny = ui.normalise_point(10, 20, LEFT_OF_PRIMARY)

    assert ui.click_point(10, 20, double=True) == "mouse"

    down, up = AT | ui.MOUSEEVENTF_LEFTDOWN, AT | ui.MOUSEEVENTF_LEFTUP
    assert sent == [(AT, nx, ny)] + [(down, nx, ny), (up, nx, ny)] * 2
    first_up_to_second_down = waits[1]
    assert first_up_to_second_down == 0.125
    assert sum(waits[:3]) < 0.5


def test_a_point_off_every_monitor_sends_nothing(sent):
    with pytest.raises(PilotError, match="outside the virtual screen"):
        ui.click_point(-1921, 0)
    assert sent == []


def test_an_unknown_button_is_refused(sent):
    with pytest.raises(PilotError, match="button must be left or right"):
        ui.click_point(0, 0, button="middle")
    assert sent == []


def test_a_blocked_input_is_an_error(monkeypatch):
    monkeypatch.setattr(ui.ctypes.windll.user32, "SendInput", lambda count, array, size: 0)
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: PRIMARY)
    with pytest.raises(PilotError, match="blocked"):
        ui.click_point(5, 5)


# ------------------------------------------------------------- move_point


def test_move_point_sends_one_absolute_move(sent):
    nx, ny = ui.normalise_point(-500, 300, LEFT_OF_PRIMARY)
    assert ui.move_point(-500, 300) == "mouse"
    assert sent == [(AT, nx, ny)]


def test_move_point_off_every_monitor_sends_nothing(sent):
    with pytest.raises(PilotError, match="outside the virtual screen"):
        ui.move_point(-1921, 0)
    assert sent == []
