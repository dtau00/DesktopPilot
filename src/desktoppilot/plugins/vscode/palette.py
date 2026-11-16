"""Running a VS Code command through the command palette (Ctrl+Shift+P).

Keystrokes go to whatever has focus, and in the user's live editor that is
often another extension's chat composer. So nothing is typed until focus is
proven to be inside the palette, focus is proven again before every further
key, and Enter is pressed only when the highlighted row is exactly the one
asked for.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

from desktoppilot.core import selectors, ui
from desktoppilot.core.errors import PilotError
from desktoppilot.core.native import auto, win32gui
from desktoppilot.core.util import collapse_whitespace, wait_until

from .common import ancestors, read, send_keys_if

QUICK_INPUT = "quick-input-widget"
MAX_STEPS = 8  # {Down} presses before giving up on a row
STEP_WAIT = 0.15  # after a {Down}, before the focused row is read again
SETTLE = 0.4  # a filtered list has settled once its top row holds this long
_EDIT = selectors.parse("app//Edit").steps[0]


class NoRow(PilotError):
    """The list has no row that matches; it was closed."""


class NotOffered(PilotError):
    """The palette has no row for the title; it was closed, nothing ran."""


def _in_palette(control) -> bool:
    return any(QUICK_INPUT in read(c, "ClassName") for c in ancestors(control, 8))


def _focused_row() -> str | None:
    try:
        focused = auto.GetFocusedControl()
    except Exception:  # noqa: BLE001 - COMError while focus moves between windows
        return None
    if focused is None or not _in_palette(focused):
        return None
    try:
        return collapse_whitespace(focused.Name)
    except Exception:  # noqa: BLE001 - row re-rendered under us
        return None


FRONT_TIMEOUT = 3.0  # for the window manager to make the window active


def bring_to_front(window) -> None:
    """SetActive only: SetFocus on the frame could move focus inside VS Code.
    Raises unless the window then is the foreground one: every key after
    this goes to whichever window that is."""
    handle = window.NativeWindowHandle
    if win32gui.GetForegroundWindow() == handle:
        return
    window.SetActive(waitTime=0.1)
    try:
        wait_until(
            lambda: win32gui.GetForegroundWindow() == handle,
            timeout=FRONT_TIMEOUT,
            interval=0.05,
            what="the VS Code window to come to the front",
        )
    except PilotError as exc:
        raise PilotError(
            f"could not bring the VS Code window to the front; sent nothing ({exc})"
        ) from exc


def _is_row(row: str | None, title: str) -> bool:
    return row is not None and (row == title or row.startswith(title + ","))


def prompt() -> str | None:
    """The quick input box's name (a picker's prompt, e.g. "Choose a
    reference ..."), read from the widget that holds focus; None if focus is
    not in the palette. Focus sits on a row, not on the box."""
    try:
        focused = auto.GetFocusedControl()
    except Exception:  # noqa: BLE001 - COMError while focus moves between windows
        return None
    widget = next((c for c in ancestors(focused, 12) if QUICK_INPUT in read(c, "ClassName")), None)
    if widget is None:
        return None
    boxes = ui.search(widget, _EDIT, max_depth=10, limit=1)
    return collapse_whitespace(read(boxes[0], "Name")) if boxes else None


def _has_focus() -> bool:
    return _focused_row() is not None


def _key(keys: str, what: str) -> None:
    send_keys_if(
        _has_focus,
        keys,
        f"focus left the palette while looking for {what}; sent nothing more",
        waitTime=STEP_WAIT,
    )


def _close() -> None:
    """Esc, if focus is still in the palette (else Esc would go elsewhere)."""
    if _focused_row() is not None:
        auto.SendKeys("{Esc}", waitTime=0.1)


def _settled(match: Callable[[str], bool], timeout: float) -> str | None:
    """The focused row once it matches, or once it has held still for SETTLE
    (the list is done filtering); None if focus is not in the palette. A
    hold-still timer, so not a wait_until."""
    deadline = time.monotonic() + timeout
    last, since = _focused_row(), time.monotonic()
    while time.monotonic() < deadline:
        if last is not None and (match(last) or time.monotonic() - since >= SETTLE):
            return last
        time.sleep(0.1)
        row = _focused_row()
        if row != last:
            last, since = row, time.monotonic()
    return last


def _step_to(
    match: Callable[[str], bool], what: str, *, max_steps: int, seen: list[str] | None
) -> str | None:
    """Step {Down} from the focused row until one matches. Returns it, or
    None when max_steps ran out or the list wrapped round to its first row.
    Raises if focus leaves the palette."""
    rows = seen if seen is not None else []
    first = None
    for step in range(max_steps + 1):
        row = _focused_row()
        if row is None:
            raise PilotError(f"focus left the palette while looking for {what}; sent nothing more")
        if step and row == first:
            return None  # wrapped: every row has been seen
        first = first if step else row
        rows.append(row)
        if match(row):
            return row
        if step < max_steps:
            _key("{Down}", what)
            # Focus is briefly on no row while it moves; a one-row list never
            # changes, and the next read ends the walk.
            with contextlib.suppress(PilotError):
                wait_until(
                    lambda prev=row: _focused_row() not in (None, prev),
                    timeout=1.0,
                    interval=0.05,
                )
    return None


def _wait_for_palette(what: str, timeout: float, *, typed: bool = False) -> None:
    try:
        wait_until(lambda: _focused_row() is not None, timeout=timeout, what=what)
    except PilotError as exc:
        done = "sent nothing more" if typed else "nothing was typed"
        raise PilotError(f"{what} did not take focus; {done} ({exc})") from exc


def wait_row(match: Callable[[str], bool], what: str, *, timeout: float = 10.0) -> str:
    """The focused palette row, once one satisfies `match` - e.g. once a
    command's own quick pick has replaced the palette. Sends nothing."""
    try:
        return wait_until(
            lambda: (row := _focused_row()) is not None and match(row) and row,
            timeout=timeout,
            what=what,
        )
    except PilotError as exc:
        _close()
        raise PilotError(f"{what} did not show (focused row {_focused_row()!r}) ({exc})") from exc


def type_filter(text: str) -> None:
    """Type `text` into the palette's input; refused unless focus is in it."""
    if "{" in text or "}" in text:
        raise PilotError(f"cannot type {text!r}: braces are SendKeys syntax")
    send_keys_if(
        _has_focus,
        text,
        f"focus is not in the palette; did not type {text!r}",
        interval=0.01,
        waitTime=0.2,
    )


def pick(
    match: Callable[[str], bool],
    what: str,
    *,
    timeout: float = 10.0,
    max_steps: int = MAX_STEPS,
    seen: list[str] | None = None,
) -> str:
    """In the quick pick that has (or is about to get) focus, step {Down}
    until the focused row satisfies `match`, then press Enter. Returns the
    row. On failure the pick is closed (Esc) and PilotError names the last
    row seen; `seen` collects every row read."""
    _wait_for_palette(what, timeout, typed=True)
    rows = seen if seen is not None else []
    _settled(match, timeout)
    row = _step_to(match, what, max_steps=max_steps, seen=rows)
    if row is None:
        _close()
        last = rows[-1] if rows else None
        raise NoRow(f"no row for {what} (last row {last!r}); closed the list, ran nothing")
    _key("{Enter}", what)
    return row


def run(
    window, titles: str | tuple[str, ...], *, timeout: float = 10.0, max_steps: int = MAX_STEPS
) -> dict:
    """Open the palette on `window`, type a title, and run it once the
    highlighted row reads exactly that title (e.g. "Codex: New Codex Agent"),
    stepping {Down} to it when a longer title ranks above it. Each of
    `titles` (its accepted texts, one per UI language) is tried in turn."""
    last = None
    for title in (titles,) if isinstance(titles, str) else titles:
        try:
            return _run_one(window, title, timeout=timeout, max_steps=max_steps)
        except NotOffered as exc:
            last = exc
    raise last


def _run_one(window, title: str, *, timeout: float, max_steps: int) -> dict:
    bring_to_front(window)
    auto.SendKeys("{Ctrl}{Shift}p", waitTime=0.2)
    _wait_for_palette("the command palette", 5.0)
    type_filter(title)
    rows: list[str] = []
    try:
        pick(
            lambda row: _is_row(row, title),
            f"palette row {title!r}",
            timeout=timeout,
            max_steps=max_steps,
            seen=rows,
        )
    except NoRow as exc:
        top = rows[0] if rows else None
        raise NotOffered(
            f"the palette offers {top!r}, not {title!r}; closed it without running anything "
            "(is the extension installed and enabled?)"
        ) from exc
    return {"ran": title}
