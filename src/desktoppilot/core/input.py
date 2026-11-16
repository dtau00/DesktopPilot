"""Real input through SendInput: pointer clicks at screen coordinates and
keystrokes as the active keyboard layout makes them.

Windows only; it imports anywhere. On Linux `ui` exports the same entry
points from `desktoppilot.linux.input` (XTest) instead."""

from __future__ import annotations

import ctypes
import time

from . import ui
from .errors import PilotError
from .native import WINDOWS, win32api, win32con

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
_AT_POINT = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_TAB, VK_RETURN = 0x09, 0x0D
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
# VkKeyScanW's high byte: the modifiers a character needs, held in this order.
_SHIFT_STATE = ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU))
MAPVK_VK_TO_VSC = 0

# A private handle, so these prototypes do not leak into ctypes.windll.user32.
if WINDOWS:
    _user32 = ctypes.WinDLL("user32")
    _user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    _user32.VkKeyScanW.restype = ctypes.c_short
    _user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
    _user32.MapVirtualKeyW.restype = ctypes.c_uint
else:
    _user32 = None


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    # MOUSEINPUT is the largest member, so INPUT has its real size without
    # HARDWAREINPUT.
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


def normalise_point(x: int, y: int, screen: tuple[int, int, int, int]) -> tuple[int, int]:
    """(x, y) in physical virtual-screen px as SendInput's 0-65535 absolute
    coordinates over the whole virtual screen (MOUSEEVENTF_VIRTUALDESK), so a
    point on any monitor - left of or above the primary included - is reached.

    Aimed at the centre of the pixel: Windows maps back with n * size / 65536,
    rounded down, so every pixel comes back exact (the 65535 / (size - 1)
    formula misses by one on some pixels, measured on a 2560 px screen).
    A point outside the virtual screen is refused, not clamped to its edge."""
    left, top, width, height = screen
    if not (left <= x < left + width and top <= y < top + height):
        raise PilotError(
            f"{x},{y} is outside the virtual screen "
            f"({width}x{height} at {left},{top}); no monitor shows that point"
        )
    return (
        (2 * (x - left) + 1) * 32768 // width,
        (2 * (y - top) + 1) * 32768 // height,
    )


def _send_input(inputs: list[_Input]) -> None:
    array = (_Input * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
    if sent != len(inputs):
        raise PilotError(
            f"the input was blocked ({sent} of {len(inputs)} events sent); "
            "a window that runs elevated refuses input from a normal process"
        )


def _mouse(flags: int, nx: int, ny: int) -> _Input:
    return _Input(type=INPUT_MOUSE, u=_InputUnion(mi=_MouseInput(dx=nx, dy=ny, dwFlags=flags)))


def _key(vk: int, scan: int, flags: int) -> _Input:
    return _Input(
        type=INPUT_KEYBOARD, u=_InputUnion(ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags))
    )


def _press(vk: int) -> list[_Input]:
    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    return [_key(vk, scan, 0), _key(vk, scan, KEYEVENTF_KEYUP)]


def _unicode_packets(ch: str) -> list[_Input]:
    """A character the layout cannot produce, as KEYEVENTF_UNICODE packets -
    one down/up per UTF-16 unit, so a character outside the BMP goes as its
    surrogate pair. A hook sees VK_PACKET, not a key; there is no key to show."""
    units = ch.encode("utf-16-le")
    inputs = []
    for i in range(0, len(units), 2):
        unit = int.from_bytes(units[i : i + 2], "little")
        inputs += [
            _key(0, unit, KEYEVENTF_UNICODE),
            _key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ]
    return inputs


def _keystroke(ch: str) -> list[_Input]:
    """The events a person makes to type `ch` on the active layout: the
    modifiers VkKeyScanW names go down, the key is pressed, the modifiers come
    back up in reverse. Each event carries the key's real scan code, as a
    keyboard's does."""
    if ch in ("\r", "\n"):
        return _press(VK_RETURN)
    if ch == "\t":
        return _press(VK_TAB)
    res = _user32.VkKeyScanW(ch) if ord(ch) <= 0xFFFF else -1
    if res == -1:
        return _unicode_packets(ch)
    vk, state = res & 0xFF, (res >> 8) & 0xFF
    held = [_press(mod) for bit, mod in _SHIFT_STATE if state & bit]
    return [down for down, _ in held] + _press(vk) + [up for _, up in reversed(held)]


def _keystrokes(text: str) -> list[list[_Input]]:
    """`text` as one event list per character, each sent whole so a context
    switch never separates Shift from its key. CRLF is one Enter, not two."""
    return [
        _keystroke(ch)
        for i, ch in enumerate(text)
        if not (ch == "\r" and text[i + 1 : i + 2] == "\n")
    ]


def click_point(x: int, y: int, *, button: str = "left", double: bool = False) -> str:
    """Click screen coordinates. The last resort: nothing about the UIA tree is
    checked, so the agent owns the claim that (x, y) is the right place.

    SendInput over the virtual screen, not uiautomation's Click: that one
    scales to the primary monitor, so a point on another monitor lands on
    the primary one. Every event carries the point, so a pointer moved
    between the press and the release does not drag."""
    if button not in _BUTTONS:
        raise PilotError("button must be left or right; got " + repr(button))
    nx, ny = normalise_point(x, y, ui.virtual_screen_rect())
    down, up = _BUTTONS[button]
    ui._send_input([_mouse(_AT_POINT, nx, ny)])
    for press in range(2 if double else 1):
        if press:  # inside the double-click time, so the two read as one gesture
            time.sleep(ctypes.windll.user32.GetDoubleClickTime() / 4000)
        ui._send_input([_mouse(_AT_POINT | down, nx, ny)])
        time.sleep(0.05)
        ui._send_input([_mouse(_AT_POINT | up, nx, ny)])
    time.sleep(0.05)
    return "mouse"


def move_point(x: int, y: int) -> str:
    """Move the pointer to screen coordinates and leave it there: one
    absolute move over the virtual screen, as click_point's first event.
    Returns "mouse"."""
    nx, ny = normalise_point(x, y, ui.virtual_screen_rect())
    ui._send_input([_mouse(_AT_POINT, nx, ny)])
    return "mouse"


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """The union of every monitor; left or top is negative when a monitor sits
    left of or above the primary."""
    metric = win32api.GetSystemMetrics
    return (
        metric(win32con.SM_XVIRTUALSCREEN),
        metric(win32con.SM_YVIRTUALSCREEN),
        metric(win32con.SM_CXVIRTUALSCREEN),
        metric(win32con.SM_CYVIRTUALSCREEN),
    )
