"""ui.type_text's keystrokes: each character as the layout's virtual key plus
its modifiers, the way a keyboard sends it, not as a Unicode packet.

No SendInput: these read the event lists _keystrokes builds. VkKeyScanW and
MapVirtualKeyW are real, so the layout has to be en-US.
"""

from __future__ import annotations

import ctypes

import pytest

from desktoppilot.core import ui

SHIFT_SCAN = 0x2A
UP = ui.KEYEVENTF_KEYUP

pytestmark = pytest.mark.skipif(
    ctypes.windll.user32.GetKeyboardLayout(0) & 0xFFFF != 0x0409,
    reason="the expected virtual keys and scan codes are the en-US layout's",
)


def events(text: str) -> list[list[tuple[int, int, int]]]:
    """(vk, scan, flags) per event, one list per character."""
    out = []
    for inputs in ui._keystrokes(text):
        assert all(i.type == ui.INPUT_KEYBOARD for i in inputs)
        out.append([(i.u.ki.wVk, i.u.ki.wScan, i.u.ki.dwFlags) for i in inputs])
    return out


@pytest.mark.parametrize(
    ("ch", "vk", "scan"),
    [("a", 0x41, 0x1E), ("1", 0x31, 0x02), (" ", 0x20, 0x39), (".", 0xBE, 0x34)],
)
def test_unshifted_character_is_its_key_alone(ch, vk, scan):
    assert events(ch) == [[(vk, scan, 0), (vk, scan, UP)]]


@pytest.mark.parametrize(("ch", "vk", "scan"), [("A", 0x41, 0x1E), ("!", 0x31, 0x02)])
def test_shifted_character_holds_shift_around_its_key(ch, vk, scan):
    assert events(ch) == [
        [
            (ui.VK_SHIFT, SHIFT_SCAN, 0),
            (vk, scan, 0),
            (vk, scan, UP),
            (ui.VK_SHIFT, SHIFT_SCAN, UP),
        ]
    ]


@pytest.mark.parametrize(
    ("ch", "vk"), [("\n", ui.VK_RETURN), ("\r", ui.VK_RETURN), ("\t", ui.VK_TAB)]
)
def test_newline_and_tab_are_enter_and_tab(ch, vk):
    [[down, up]] = events(ch)
    assert (down[0], down[2]) == (vk, 0)
    assert (up[0], up[2]) == (vk, UP)
    assert down[1] == up[1] != 0


def test_crlf_is_one_enter():
    assert [e[0][0] for e in events("a\r\nb")] == [0x41, ui.VK_RETURN, 0x42]


@pytest.mark.parametrize("ch", ["é", "日"])
def test_character_off_the_layout_is_a_unicode_packet(ch):
    unicode = ui.KEYEVENTF_UNICODE
    assert events(ch) == [[(0, ord(ch), unicode), (0, ord(ch), unicode | UP)]]


def test_character_outside_the_bmp_is_a_surrogate_pair():
    unicode = ui.KEYEVENTF_UNICODE
    assert events("😀") == [
        [
            (0, 0xD83D, unicode),
            (0, 0xD83D, unicode | UP),
            (0, 0xDE00, unicode),
            (0, 0xDE00, unicode | UP),
        ]
    ]


def test_braces_type_as_braces_not_a_token():
    assert len(events("{Ctrl}")) == 6
    assert ui.VK_CONTROL not in {vk for e in events("{Ctrl}") for vk, _, _ in e}
