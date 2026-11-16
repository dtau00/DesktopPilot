"""XTest input without a desktop: the events a keystroke, a SendKeys string
and a click expand to, over a small US-like keymap."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the X11 backend")

if sys.platform.startswith("linux"):
    from desktoppilot.core import ui
    from desktoppilot.core.errors import PilotError
    from desktoppilot.linux import input as xi
    from desktoppilot.linux.input import BUTTON_DOWN, BUTTON_UP, KEY_DOWN, KEY_UP, MOTION, REMAP

A, ONE, Q, RET, TAB, SHIFT, CTRL, ALT, L3, P, DEL, SPARE_HI, SPARE_LO = (
    38, 10, 24, 36, 23, 50, 37, 64, 108, 33, 119, 255, 254,
)  # fmt: skip
ROWS = {
    A: [0x61, 0x41, 0x61, 0x41],
    ONE: [0x31, 0x21, 0x31, 0x21],
    Q: [0x71, 0x51, 0x71, 0x51, 0x40],  # '@' on level 3, as some layouts have
    P: [0x70, 0x50],
    RET: [0xFF0D],
    TAB: [0xFF09],
    SHIFT: [0xFFE1],
    CTRL: [0xFFE3],
    ALT: [0xFFE9],
    L3: [0xFE03],
    DEL: [0xFFFF],
}


@pytest.fixture
def keymap(monkeypatch):
    monkeypatch.setattr(xi, "_bound", {})
    monkeypatch.setattr(xi, "_spares", [])
    monkeypatch.setattr(xi, "_next_spare", [0])
    rows = [ROWS.get(code, [0, 0]) for code in range(8, 256)]
    km = xi.Keymap(8, rows)
    monkeypatch.setattr(xi.Keymap, "read", classmethod(lambda cls: km))
    return km


def keys(events):
    return [(e.kind, e.detail) for e in events]


def tap(code):
    return [(KEY_DOWN, code), (KEY_UP, code)]


def test_a_plain_character_is_its_key(keymap):
    assert keys(xi._keystroke("a")) == tap(A)


def test_a_shifted_character_holds_shift_around_its_key(keymap):
    assert keys(xi._keystroke("!")) == [(KEY_DOWN, SHIFT), *tap(ONE), (KEY_UP, SHIFT)]


def test_a_level_three_character_holds_iso_level3(keymap):
    assert keys(xi._keystroke("@")) == [(KEY_DOWN, L3), *tap(Q), (KEY_UP, L3)]


def test_a_character_no_key_makes_is_bound_to_a_spare_keycode(keymap):
    events = xi._keystroke("ö")
    assert events[0] == xi.Event(REMAP, SPARE_HI, keysym=0xF6)
    assert keys(events[1:]) == tap(SPARE_HI)
    beyond_latin1 = xi._keystroke("漢")
    assert beyond_latin1[0] == xi.Event(REMAP, SPARE_LO, keysym=0x01000000 | ord("漢"))
    assert xi._bound == {SPARE_HI: 0xF6, SPARE_LO: 0x01000000 | ord("漢")}


def test_spare_keycodes_are_taken_in_turn(keymap, monkeypatch):
    monkeypatch.setattr(xi, "SPARE_POOL", 2)
    taken = [xi._keystroke(ch)[0].detail for ch in "äöüä"]
    assert taken == [SPARE_HI, SPARE_LO, SPARE_HI, SPARE_LO]


def test_newline_and_tab_are_return_and_tab(keymap):
    assert keys(xi._keystroke("\n")) == tap(RET)
    assert keys(xi._keystroke("\t")) == tap(TAB)


def test_crlf_is_one_return(keymap):
    assert [keys(e) for e in xi._keystrokes("a\r\nq\ra")] == [
        tap(A),
        tap(RET),
        tap(Q),
        tap(RET),
        tap(A),
    ]


def test_ctrl_holds_for_the_next_key_only(keymap):
    events = xi.key_events("{Ctrl}aq")
    assert keys(events) == [(KEY_DOWN, CTRL), *tap(A), (KEY_UP, CTRL), *tap(Q)]


def test_a_character_under_a_hold_is_its_key_without_shift(keymap):
    assert keys(xi.key_events("{Ctrl}A")) == [(KEY_DOWN, CTRL), *tap(A), (KEY_UP, CTRL)]


def test_hold_keys_nest(keymap):
    assert keys(xi.key_events("{Ctrl}{Shift}p")) == [
        (KEY_DOWN, CTRL),
        (KEY_DOWN, SHIFT),
        *tap(P),
        (KEY_UP, SHIFT),
        (KEY_UP, CTRL),
    ]


def test_a_group_holds_until_its_parenthesis_closes(keymap):
    assert keys(xi.key_events("{Ctrl}(aq)a")) == [
        (KEY_DOWN, CTRL),
        *tap(A),
        *tap(Q),
        (KEY_UP, CTRL),
        *tap(A),
    ]


def test_a_parenthesis_without_a_hold_is_typed(keymap):
    assert [e.keysym for e in xi.key_events("()") if e.kind == REMAP] == [0x28, 0x29]


def test_a_count_repeats_and_only_the_first_is_held(keymap):
    assert keys(xi.key_events("{Delete 2}")) == tap(DEL) * 2
    assert keys(xi.key_events("{Ctrl}{a 2}")) == [
        (KEY_DOWN, CTRL),
        *tap(A),
        (KEY_UP, CTRL),
        *tap(A),
    ]


def test_a_special_key_and_text(keymap):
    assert keys(xi.key_events("a{Enter}")) == tap(A) + tap(RET)


def test_braces_type_braces(keymap):
    assert [e.keysym for e in xi.key_events("{{}{}}") if e.kind == REMAP] == [0x7B, 0x7D]


def test_keys_after_the_first_wait_a_little(keymap):
    downs = [e.delay for e in xi.key_events("{Ctrl}a") if e.kind == KEY_DOWN]
    assert downs == [0, xi.KEY_GAP_MS]


@pytest.mark.parametrize("bad", ["{Entr}", "{Ctrl", "{a b}", "{}"])
def test_a_bad_key_raises_rather_than_typing(keymap, bad):
    with pytest.raises(PilotError):
        xi.key_events(bad)


def test_click_moves_then_presses_and_releases(monkeypatch):
    sent = []
    monkeypatch.setattr(ui, "_send_input", sent.append)
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: (0, 0, 100, 100))
    assert ui.click_point(10, 20, button="right", double=True) == "mouse"
    assert [keys(batch) for batch in sent] == [
        [(MOTION, 0)],
        [(BUTTON_DOWN, 3), (BUTTON_UP, 3)],
        [(BUTTON_DOWN, 3), (BUTTON_UP, 3)],
    ]
    assert (sent[0][0].x, sent[0][0].y) == (10, 20)
    assert sent[2][0].delay == xi.DOUBLE_GAP_MS and sent[1][1].delay == xi.HOLD_MS


def test_a_point_off_the_screen_is_refused(monkeypatch):
    monkeypatch.setattr(ui, "_send_input", lambda batch: pytest.fail("sent input"))
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: (0, 0, 100, 100))
    with pytest.raises(PilotError, match="outside the screen"):
        ui.click_point(100, 5)


def test_the_wheel_uses_buttons_four_and_five(monkeypatch):
    sent = []
    monkeypatch.setattr(ui, "_send_input", sent.append)
    xi.wheel(-2)
    xi.wheel(1)
    assert [keys(batch) for batch in sent] == [
        [(BUTTON_DOWN, 5), (BUTTON_UP, 5)] * 2,
        [(BUTTON_DOWN, 4), (BUTTON_UP, 4)],
    ]


class FakeDisplay:
    def __init__(self):
        self.calls = []

    def screen(self):
        return type("S", (), {"root": "root"})()

    def change_keyboard_mapping(self, keycode, syms):
        self.calls.append(("map", keycode, syms))

    def sync(self):
        self.calls.append(("sync",))


def test_send_input_releases_what_it_pressed_when_it_fails(monkeypatch):
    display = FakeDisplay()
    faked = []

    def fake(d, root, event):
        if event.detail == A and event.kind == KEY_DOWN:
            raise RuntimeError("boom")
        faked.append((event.kind, event.detail))

    monkeypatch.setattr(xi.x11, "display", lambda: display)
    monkeypatch.setattr(xi, "_fake", fake)
    events = [xi.Event(KEY_DOWN, CTRL), xi.Event(KEY_DOWN, A), xi.Event(KEY_UP, A)]
    with pytest.raises(RuntimeError):
        xi._send_input(events)
    assert faked == [(KEY_DOWN, CTRL), (KEY_UP, CTRL)]


def test_send_input_remaps_before_the_key(monkeypatch):
    display = FakeDisplay()
    monkeypatch.setattr(xi.x11, "display", lambda: display)
    monkeypatch.setattr(xi, "_fake", lambda d, root, e: display.calls.append((e.kind, e.detail)))
    xi._send_input([xi.Event(REMAP, SPARE_HI, keysym=0xF6), *(xi.Event(*k) for k in tap(SPARE_HI))])
    assert display.calls == [
        ("map", SPARE_HI, [(0xF6, 0xF6)]),
        (KEY_DOWN, SPARE_HI),
        (KEY_UP, SPARE_HI),
        ("sync",),
    ]


def test_move_point_moves_and_leaves_the_pointer(monkeypatch):
    sent = []
    monkeypatch.setattr(ui, "_send_input", sent.append)
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: (0, 0, 100, 100))
    assert ui.move_point(10, 20) == "mouse"
    assert [keys(batch) for batch in sent] == [[(MOTION, 0)]]
    assert (sent[0][0].x, sent[0][0].y) == (10, 20)
    with pytest.raises(PilotError, match="outside the screen"):
        ui.move_point(100, 5)
    assert len(sent) == 1


def test_get_cursor_pos_is_the_root_pointer(monkeypatch):
    from desktoppilot.linux import win32api, x11

    class Root:
        def query_pointer(self):
            class Reply:
                root_x, root_y = 31, 42

            return Reply()

    monkeypatch.setattr(x11, "_root", Root)
    assert x11.pointer() == (31, 42)
    assert win32api.GetCursorPos() == (31, 42)
