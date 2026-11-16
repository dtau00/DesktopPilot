"""The X11 backend on a real desktop: windows, input and the clipboard,
against a Mousepad this test starts (and closes).

Input goes only to that window: before every burst it is activated and
`_NET_ACTIVE_WINDOW` is checked to be it, else the test stops. A test that
sends input or activates the window runs only with PILOT_TEST_ACTIVATE set.
The clipboard's text is put back at the end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the X11 backend"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs an X display"),
    pytest.mark.skipif(not shutil.which("mousepad"), reason="needs mousepad"),
]

if sys.platform.startswith("linux"):
    from desktoppilot.core import clip, ui
    from desktoppilot.core.util import wait_until
    from desktoppilot.linux import input as xinput
    from desktoppilot.linux import procs, win32clipboard, win32con, win32gui, win32process, x11

# Input and activation take the desktop: only when nobody else drives it.
activates = pytest.mark.skipif(
    not os.environ.get("PILOT_TEST_ACTIVATE"),
    reason="activates its window (set PILOT_TEST_ACTIVATE=1 when no other agent drives the desktop)",
)


def clean_env(home) -> dict:
    """The VS Code snap's leaks out (`procs.app_env`, as `app launch` does),
    and settings and autosaves kept to this test, so Mousepad neither asks
    to restore a killed session nor leaves one."""
    env = procs.app_env(dict(os.environ))
    env |= {"GSETTINGS_BACKEND": "memory", "XDG_DATA_HOME": str(home)}
    return env


@pytest.fixture(scope="module", autouse=True)
def keep_clipboard():
    saved = clip.read()
    yield
    if saved:
        clip.write(saved)


@pytest.fixture
def editor(tmp_path):
    """A Mousepad on its own file: (hwnd, pid). Closed afterwards."""
    path = tmp_path / "pilot-x11-live.txt"
    path.write_text("")
    proc = subprocess.Popen(
        ["mousepad", "--disable-server", str(path)],
        env=clean_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def window():
        found = []

        def collect(hwnd, _):
            mine = win32process.GetWindowThreadProcessId(hwnd)[1] == proc.pid
            if mine and "pilot-x11-live" in win32gui.GetWindowText(hwnd):
                found.append(hwnd)
            return True

        win32gui.EnumWindows(collect, None)
        return found[0] if found else None

    try:
        hwnd = wait_until(window, timeout=15, interval=0.1, what="mousepad's window")
        yield hwnd, proc.pid
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def focus(hwnd: int) -> None:
    """Activate our window and refuse to go on unless it is the active one."""
    x11.activate(hwnd)
    if x11.active_window() != hwnd or x11.focus_window() != hwnd:
        pytest.fail(f"window {hwnd:#x} is not active; sent no input")


def typed_text(hwnd: int) -> str:
    focus(hwnd)
    before = win32clipboard.GetClipboardSequenceNumber()
    clip.clear()
    focus(hwnd)
    xinput.send_keys("{Ctrl}a{Ctrl}c", 0)
    return wait_until(
        lambda: win32clipboard.GetClipboardSequenceNumber() != before and clip.read(),
        timeout=5,
        interval=0.05,
        what="the copied text",
    )


def test_the_window_list_knows_the_editor(editor):
    hwnd, pid = editor
    assert win32gui.IsWindowVisible(hwnd) and win32gui.IsWindow(hwnd)
    assert win32process.GetWindowThreadProcessId(hwnd) == (0, pid)
    assert win32gui.GetClassName(hwnd) == "Org.xfce.mousepad"
    assert "pilot-x11-live.txt - Mousepad" in win32gui.GetWindowText(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    cl, ct, cr, cb = x11.client_rect(hwnd)
    assert left <= cl < cr <= right and top < ct < cb <= bottom  # a title bar above
    assert win32gui.GetAncestor(hwnd, win32con.GA_ROOT) == hwnd


@activates
def test_activate_makes_it_the_foreground(editor):
    hwnd, _ = editor
    assert x11.activate(hwnd)
    assert win32gui.GetForegroundWindow() == hwnd


@activates
def test_typing_lands_exactly(editor):
    hwnd, _ = editor
    text = "Hello, Wörld! <>&\t2 ß€ → 漢"
    focus(hwnd)
    for events in ui._keystrokes(text):
        ui._send_input(events)
    assert typed_text(hwnd) == text


@activates
def test_crlf_is_one_line_break(editor):
    hwnd, _ = editor
    focus(hwnd)
    for events in ui._keystrokes("a\r\nb"):
        ui._send_input(events)
    assert typed_text(hwnd) == "a\nb"


@activates
def test_a_click_puts_the_caret_where_it_lands(editor):
    hwnd, _ = editor
    focus(hwnd)
    for events in ui._keystrokes("first\nsecond"):
        ui._send_input(events)
    left, top, right, bottom = x11.client_rect(hwnd)
    # Line 1 of the text, near its left: the click moves the caret there.
    focus(hwnd)
    assert ui.click_point(left + 40, top + 80) == "mouse"
    assert x11.active_window() == hwnd
    xinput.send_keys("{Home}X", 0)
    text = typed_text(hwnd)
    assert text in ("Xfirst\nsecond", "first\nXsecond")


def test_minimize_restore_and_close(editor):
    hwnd, _ = editor
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    assert win32gui.IsIconic(hwnd) and win32gui.IsWindowVisible(hwnd)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    assert not win32gui.IsIconic(hwnd) and x11.is_mapped(hwnd)
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    assert wait_until(lambda: not win32gui.IsWindow(hwnd), timeout=5, what="close")


def test_move_resize_sets_the_frame(editor):
    hwnd, _ = editor
    win32gui.MoveWindow(hwnd, 200, 150, 700, 500, True)
    assert win32gui.GetWindowRect(hwnd) == (200, 150, 900, 650)
    assert win32gui.WindowFromPoint((550, 400)) == hwnd


@activates
def test_grab_holds_the_window(editor):
    hwnd, _ = editor
    focus(hwnd)
    left, top, right, bottom = x11.client_rect(hwnd)
    image = x11.grab(left, top, right - left, bottom - top)
    assert image.size == (right - left, bottom - top) and image.mode == "RGB"
    # A text view is mostly one background colour.
    colours = image.getcolors(maxcolors=1 << 20)
    assert max(colours)[0] > 0.3 * image.width * image.height


def test_the_clipboard_round_trips_unicode():
    text = "Ünïcödé → 漢字 🙂\nline 2"
    before = win32clipboard.GetClipboardSequenceNumber()
    assert clip.write(text) == len(text)
    assert clip.read() == text
    assert win32clipboard.GetClipboardSequenceNumber() != before
    assert win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
    clip.clear()
    assert clip.read() == "" and win32clipboard.CountClipboardFormats() == 0
