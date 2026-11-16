"""Real input through XTest: pointer clicks at screen coordinates and
keystrokes as the active keyboard layout makes them.

The Linux side of `core/input.py`: `core/ui.py` exports these names in place
of the SendInput ones, and the core calls them through `ui` (`ui._send_input`,
`ui._keystrokes`, `ui.click_point`, `ui.move_point`), so a test patches the
same names on both platforms. An event is one `Event`: an `Xlib.ext.xtest.fake_input` call, or a
REMAP that binds a keysym to a spare keycode before the key that needs it.

Pacing is the X server's: XTest delays an event by its `delay` ms, so a
press and its release are apart in time without this process sleeping.
Nothing grabs the keyboard.
"""

from __future__ import annotations

import atexit
import contextlib
import time
from typing import NamedTuple

from ..core.errors import PilotError
from ..core.keysyntax import tokens as _tokens
from . import x11

KEY_DOWN, KEY_UP, BUTTON_DOWN, BUTTON_UP, MOTION = 2, 3, 4, 5, 6  # X event types
REMAP = "remap"

_BUTTONS = {"left": 1, "middle": 2, "right": 3}
WHEEL_UP, WHEEL_DOWN = 4, 5
HOLD_MS = 50  # a click's press to release, as core/input's
DOUBLE_GAP_MS = 60  # well inside any double-click time (GTK: 400)
KEY_GAP_MS = 10  # between send_keys events, as uiautomation's hotkey interval

RETURN, TAB = 0xFF0D, 0xFF09
SHIFT, LEVEL3 = 0xFFE1, 0xFE03
# Keymap columns python-xlib reports: group 1 levels 1-4, the modifiers each
# needs. Columns 2 and 3 are group 2, which no keystroke here switches to.
_LEVELS = {0: (), 1: (SHIFT,), 4: (LEVEL3,), 5: (SHIFT, LEVEL3)}

# uiautomation's SendKeys names (upper case) -> X keysyms.
HOLD_KEYS = {
    "SHIFT": 0xFFE1, "LSHIFT": 0xFFE1, "RSHIFT": 0xFFE2,
    "CTRL": 0xFFE3, "CONTROL": 0xFFE3, "LCTRL": 0xFFE3, "LCONTROL": 0xFFE3,
    "RCTRL": 0xFFE4, "RCONTROL": 0xFFE4,
    "ALT": 0xFFE9, "LALT": 0xFFE9, "RALT": 0xFFEA,
    "WIN": 0xFFEB, "LWIN": 0xFFEB, "RWIN": 0xFFEC,
}  # fmt: skip
SPECIAL_KEYS = {
    "BACK": 0xFF08, "TAB": 0xFF09, "CLEAR": 0xFF0B, "RETURN": 0xFF0D, "ENTER": 0xFF0D,
    "PAUSE": 0xFF13, "CAPITAL": 0xFFE5, "ESC": 0xFF1B, "ESCAPE": 0xFF1B, "SPACE": 0x20,
    "PRIOR": 0xFF55, "PAGEUP": 0xFF55, "NEXT": 0xFF56, "PAGEDOWN": 0xFF56,
    "END": 0xFF57, "HOME": 0xFF50, "LEFT": 0xFF51, "UP": 0xFF52, "RIGHT": 0xFF53,
    "DOWN": 0xFF54, "SELECT": 0xFF60, "PRINT": 0xFF61, "EXECUTE": 0xFF62,
    "SNAPSHOT": 0xFF61, "PRINTSCREEN": 0xFF61, "INSERT": 0xFF63, "INS": 0xFF63,
    "DELETE": 0xFFFF, "DEL": 0xFFFF, "HELP": 0xFF6A, "APPS": 0xFF67,
    "MULTIPLY": 0xFFAA, "ADD": 0xFFAB, "SEPARATOR": 0xFFAC, "SUBTRACT": 0xFFAD,
    "DECIMAL": 0xFFAE, "DIVIDE": 0xFFAF, "NUMLOCK": 0xFF7F, "SCROLL": 0xFF14,
    "SLEEP": 0x1008FF2F, "BROWSER_BACK": 0x1008FF26, "BROWSER_FORWARD": 0x1008FF27,
    "BROWSER_REFRESH": 0x1008FF29, "BROWSER_STOP": 0x1008FF28,
    "BROWSER_SEARCH": 0x1008FF1B, "BROWSER_FAVORITES": 0x1008FF30,
    "BROWSER_HOME": 0x1008FF18, "VOLUME_MUTE": 0x1008FF12, "VOLUME_DOWN": 0x1008FF11,
    "VOLUME_UP": 0x1008FF13, "MEDIA_NEXT_TRACK": 0x1008FF17,
    "MEDIA_PREV_TRACK": 0x1008FF16, "MEDIA_STOP": 0x1008FF15,
    "MEDIA_PLAY_PAUSE": 0x1008FF14, "LAUNCH_MAIL": 0x1008FF19,
    "OEM_1": 0x3B, "OEM_PLUS": 0x3D, "OEM_COMMA": 0x2C, "OEM_MINUS": 0x2D,
    "OEM_PERIOD": 0x2E, "OEM_2": 0x2F, "OEM_3": 0x60, "OEM_4": 0x5B, "OEM_5": 0x5C,
    "OEM_6": 0x5D, "OEM_7": 0x27, "OEM_102": 0x3C,
    **{f"NUMPAD{n}": 0xFFB0 + n for n in range(10)},
    **{f"F{n}": 0xFFBE + n - 1 for n in range(1, 25)},
}  # fmt: skip

SPARE_POOL = 32  # spare keycodes taken in turn, so a binding outlives its key


class Event(NamedTuple):
    kind: int | str
    detail: int = 0  # keycode or button
    x: int = 0
    y: int = 0
    delay: int = 0  # ms the server waits before it
    keysym: int = 0  # REMAP: what `detail` types from now on


class Keymap:
    """The keyboard mapping as read once: keysym -> (keycode, modifiers) with
    the fewest modifiers, and the keycodes no key uses."""

    def __init__(self, first: int, rows: list) -> None:
        self.codes: dict[int, tuple[int, tuple[int, ...]]] = {}
        self.empty: list[int] = []
        for keycode, row in enumerate(rows, first):
            if keycode in _bound or not any(row):
                self.empty.append(keycode)
                continue
            for column, mods in _LEVELS.items():
                keysym = row[column] if column < len(row) else 0
                known = self.codes.get(keysym)
                if keysym and (known is None or len(mods) < len(known[1])):
                    self.codes[keysym] = (keycode, mods)

    @classmethod
    def read(cls) -> Keymap:
        with x11.lock():
            d = x11.display()
            first, last = d.display.info.min_keycode, d.display.info.max_keycode
            return cls(first, d.get_keyboard_mapping(first, last - first + 1))


# Keycodes this process bound to a keysym, restored at exit; the pool and
# the next one to take are fixed by the first keymap read.
_bound: dict[int, int] = {}
_spares: list[int] = []
_next_spare = [0]


def _spare(keymap: Keymap) -> int:
    if not _spares:
        _spares.extend(sorted(keymap.empty, reverse=True)[:SPARE_POOL])
        if not _spares:
            raise PilotError("the keymap has no free keycode to type this character with")
    keycode = _spares[_next_spare[0] % len(_spares)]
    _next_spare[0] += 1
    return keycode


def _restore_spares() -> None:
    """Give the spare keycodes back their empty mapping."""
    if not _bound:
        return
    try:
        with x11.lock():
            d = x11.display()
            for keycode in list(_bound):
                d.change_keyboard_mapping(keycode, [(0,)])
            d.sync()
        _bound.clear()
    except Exception:  # noqa: BLE001 - at exit the display may be gone already
        pass


atexit.register(_restore_spares)


def char_keysym(ch: str) -> int:
    """Latin-1 characters are their own keysym; the rest are Unicode keysyms."""
    code = ord(ch)
    return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 | code


def _press(keymap: Keymap, keysym: int, *, levels: bool = True, delay: int = 0) -> list[Event]:
    """Press and release the key for `keysym`, with the modifiers its level
    needs (`levels`); a keysym no key makes is bound to a spare keycode."""
    found = keymap.codes.get(keysym)
    if found is None:
        keycode = _spare(keymap)
        _bound[keycode] = keysym
        return [
            Event(REMAP, keycode, keysym=keysym),
            Event(KEY_DOWN, keycode, delay=delay),
            Event(KEY_UP, keycode),
        ]
    keycode, mods = found
    held = [keymap.codes[m][0] for m in mods if m in keymap.codes] if levels else []
    if levels and len(held) < len(mods):  # a level with no modifier key to reach it
        return _press(_without(keymap, keysym), keysym, delay=delay)
    downs = [Event(KEY_DOWN, k) for k in held]
    ups = [Event(KEY_UP, k) for k in reversed(held)]
    key = [Event(KEY_DOWN, keycode), Event(KEY_UP, keycode)]
    events = downs + key + ups
    return [events[0]._replace(delay=delay), *events[1:]]


def _without(keymap: Keymap, keysym: int) -> Keymap:
    """The keymap with `keysym` unknown, so it goes to a spare keycode."""
    copy = Keymap.__new__(Keymap)
    copy.codes = {k: v for k, v in keymap.codes.items() if k != keysym}
    copy.empty = keymap.empty
    return copy


def _keystroke(ch: str, keymap: Keymap | None = None) -> list[Event]:
    """The events a person makes to type `ch`: the keysym's keycode with the
    modifiers its keymap level needs (Shift, ISO_Level3_Shift). Newline and
    tab are Return and Tab. A character no key produces is typed by binding
    its keysym to a spare keycode first (as xdotool does); the bindings go
    back at exit."""
    keymap = keymap or Keymap.read()
    if ch in ("\r", "\n"):
        return _press(keymap, RETURN)
    if ch == "\t":
        return _press(keymap, TAB)
    return _press(keymap, char_keysym(ch))


def _keystrokes(text: str) -> list[list[Event]]:
    """`text` as one event list per character; CRLF is one Return."""
    keymap = Keymap.read()
    return [
        _keystroke(ch, keymap)
        for i, ch in enumerate(text)
        if not (ch == "\r" and text[i + 1 : i + 2] == "\n")
    ]


def _fake(d, root, event: Event) -> None:
    from Xlib.ext import xtest

    if event.kind == MOTION:
        xtest.fake_input(d, MOTION, 0, time=event.delay, root=root, x=event.x, y=event.y)
    else:
        xtest.fake_input(d, event.kind, event.detail, time=event.delay)


_RELEASE = {KEY_DOWN: KEY_UP, BUTTON_DOWN: BUTTON_UP}


def _send_input(events: list[Event]) -> None:
    """Send one batch of XTest events, then sync, so the batch reaches the
    server whole (a modifier is never separated from its key). A key or
    button the batch pressed is released even when it fails half-way."""
    held: list[Event] = []
    with x11.lock():
        d = x11.display()
        root = d.screen().root
        try:
            for event in events:
                if event.kind == REMAP:
                    d.change_keyboard_mapping(event.detail, [(event.keysym, event.keysym)])
                    continue
                _fake(d, root, event)
                if event.kind in _RELEASE:
                    held.append(Event(_RELEASE[event.kind], event.detail))
                elif Event(event.kind, event.detail) in held:
                    held.remove(Event(event.kind, event.detail))
            d.sync()
        except BaseException:
            for event in reversed(held):
                with contextlib.suppress(Exception):  # releasing what it can
                    _fake(d, root, event)
            with contextlib.suppress(Exception):
                d.sync()
            raise


def normalise_point(x: int, y: int, screen: tuple[int, int, int, int]) -> tuple[int, int]:
    """(x, y) checked against the screen and returned as is: XTest moves in
    root-window pixels, with no 0-65535 scale to map to. A point outside the
    screen is refused, not clamped to its edge."""
    left, top, width, height = screen
    if not (left <= x < left + width and top <= y < top + height):
        raise PilotError(
            f"{x},{y} is outside the screen ({width}x{height} at {left},{top}); "
            "no monitor shows that point"
        )
    return x, y


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the root window, which spans every monitor."""
    return x11.screen_rect()


def click_point(x: int, y: int, *, button: str = "left", double: bool = False) -> str:
    """Click screen coordinates through XTest (`ui._send_input`): move, press,
    release, at the point; a double click inside the double-click time.
    Returns "mouse", as on Windows."""
    from ..core import ui

    if button not in ("left", "right"):
        raise PilotError("button must be left or right; got " + repr(button))
    x, y = ui.normalise_point(x, y, ui.virtual_screen_rect())
    number = _BUTTONS[button]
    ui._send_input([Event(MOTION, 0, x, y)])
    for press in range(2 if double else 1):
        ui._send_input(
            [
                Event(BUTTON_DOWN, number, delay=DOUBLE_GAP_MS if press else 0),
                Event(BUTTON_UP, number, delay=HOLD_MS),
            ]
        )
    return "mouse"


def move_to(x: int, y: int) -> None:
    """Move the pointer to screen coordinates (one MotionNotify)."""
    from ..core import ui

    x, y = ui.normalise_point(x, y, ui.virtual_screen_rect())
    ui._send_input([Event(MOTION, 0, x, y)])


def move_point(x: int, y: int) -> str:
    """Move the pointer to screen coordinates and leave it there, as
    core/input's move_point. Returns "mouse"."""
    move_to(x, y)
    return "mouse"


def wheel(clicks: int) -> None:
    """Scroll at the pointer: buttons 4 (up, clicks > 0) and 5 (down)."""
    from ..core import ui

    number = WHEEL_UP if clicks > 0 else WHEEL_DOWN
    events = []
    for _ in range(abs(clicks)):
        events += [Event(BUTTON_DOWN, number, delay=KEY_GAP_MS), Event(BUTTON_UP, number)]
    if events:
        ui._send_input(events)


def key_events(keys: str, keymap: Keymap | None = None) -> list[Event]:
    """uiautomation's SendKeys syntax as one event list. A hold key ({Ctrl},
    {Shift}, {Alt}, {Win}) is held for the next key, or for a parenthesised
    group after it; hold keys nest ({Ctrl}{Shift}p). A character under a
    hold presses its key without the level's Shift ({Ctrl}A is Ctrl+a);
    other text is typed. {Key N} presses it N times. An unknown {Name}
    raises instead of being typed."""
    keymap = keymap or Keymap.read()
    out: list[Event] = []
    held: list[int] = []
    group = False

    def key_down(keysym: int) -> None:
        found = keymap.codes.get(keysym)
        if found is None:
            raise PilotError(f"no key on this keymap makes keysym {keysym:#x}")
        out.append(Event(KEY_DOWN, found[0]))
        held.append(found[0])

    def release() -> None:
        out.extend(Event(KEY_UP, k) for k in reversed(held))
        held.clear()

    def under_hold(ch: str) -> None:
        out.extend(_press(keymap, char_keysym(ch.lower()), levels=False))
        if not group:
            release()

    for token in _tokens(keys):
        if token[0] == "open":
            group = bool(held)
            if not held:
                out.extend(_keystroke("(", keymap))
        elif token[0] == "close":
            if held:
                group = False
                release()
            else:
                out.extend(_keystroke(")", keymap))
        elif token[0] == "char":
            if held:
                under_hold(token[1])
            else:
                out.extend(_keystroke(token[1], keymap))
        else:
            _, name, count = token
            upper = name.upper()
            for _ in range(count):
                if upper in HOLD_KEYS:
                    key_down(HOLD_KEYS[upper])
                elif upper in SPECIAL_KEYS:
                    out.extend(_press(keymap, SPECIAL_KEYS[upper]))
                    if held and not group:
                        release()
                elif len(name) == 1:
                    if held:
                        under_hold(name)
                    else:
                        out.extend(_keystroke(name, keymap))
                else:
                    release()
                    raise PilotError(f"{{{name}}} is not a key name uiautomation knows")
    release()
    return [
        e._replace(delay=KEY_GAP_MS) if i and e.kind == KEY_DOWN else e for i, e in enumerate(out)
    ]


def send_keys(keys: str, wait_time: float = 0.05) -> None:
    """uiautomation's key syntax ('{Ctrl}s', '{Enter}', '{Ctrl}{Shift}p',
    '{Delete 3}') through XTest, as one batch, then waits `wait_time` - the
    caller's settle time, as uiautomation's waitTime."""
    from ..core import ui

    events = key_events(keys)
    if events:
        ui._send_input(events)
    if wait_time > 0:
        time.sleep(wait_time)
