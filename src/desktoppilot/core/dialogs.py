"""Find and answer an app's modal dialogs.

A missed modal is the usual reason a click "did nothing", so dialogs are part
of every state dump. Core never guesses an answer: with `dialogs = "explicit"`
a prompt is reported and left alone, because guessing on someone's real editor
is worse than stopping. `dialogs = "auto"` is for a plugin that knows its app's
prompts and supplies a policy. Closing what is open (`close_all`) only happens
when asked, or in an app this tool launched.
"""

from __future__ import annotations

import contextlib

from ..linux import dialogs as linux_dialogs
from .errors import Timeout
from .native import WINDOWS, auto, win32con, win32gui, win32process
from .util import wait_until

# Plain Win32 message boxes. Apps with custom dialog classes add their own
# through the profile's dialog_classes. On Linux a dialog is known by its
# window type (see linux/dialogs.py), and dialog_classes name WM_CLASS
# values to count as well.
BASE_DIALOG_CLASSES = ("#32770",) if WINDOWS else ()

# Win32 dialog button IDs: the same on every UI language, unlike the labels.
IDCANCEL, IDYES, IDNO = 2, 6, 7

# The least destructive button first.
CLOSE_ORDER = ("Cancel", "Close", "No", "OK")
# How long `remaining` waits for an answered dialog's window to go: a click
# or a close request returns before the app has destroyed it.
CLOSE_SETTLE = 2.0


def dialog_classes(profile=None) -> tuple[str, ...]:
    extra = tuple(profile.dialog_classes) if profile else ()
    return BASE_DIALOG_CLASSES + extra


def dialog_windows(
    pids: set[int] | list[int] | None,
    *,
    classes: tuple[str, ...] = BASE_DIALOG_CLASSES,
) -> list[dict]:
    """Dialogs, wherever Windows puts them. pids=None means any process.

    Some are top-level; others are child windows of the app frame, which
    EnumWindows never reports. Missing those is how a modal blocks the UI while
    the tool insists nothing is open. On Linux, see linux/dialogs.py.
    """
    owned = set(pids) if pids is not None else None
    if not WINDOWS:
        return linux_dialogs.dialog_windows(owned, classes)
    found: list[dict] = []
    seen: set[int] = set()

    def record(hwnd: int) -> None:
        if hwnd in seen or not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) not in classes:
            return
        seen.add(hwnd)
        found.append(
            {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "text": dialog_text(hwnd)}
        )

    def walk_children(parent: int) -> None:
        def collect(child, _):
            record(child)
            return True

        with contextlib.suppress(Exception):  # window closed mid-walk
            win32gui.EnumChildWindows(parent, collect, None)

    def collect_top(hwnd, _):
        _, owner = win32process.GetWindowThreadProcessId(hwnd)
        if owned is not None and owner not in owned:
            return True
        record(hwnd)
        if win32gui.IsWindowVisible(hwnd):
            walk_children(hwnd)
        return True

    win32gui.EnumWindows(collect_top, None)
    return found


def dialog_text(hwnd: int) -> str:
    """All static text in the dialog, joined - that is what identifies it."""
    if not WINDOWS:
        return linux_dialogs.dialog_text(hwnd)
    parts: list[str] = []

    def collect(child, _):
        if win32gui.GetClassName(child) in ("Static", "SysLink"):
            text = win32gui.GetWindowText(child)
            if text:
                parts.append(text)
        return True

    with contextlib.suppress(Exception):  # dialog closed while we looked at it
        win32gui.EnumChildWindows(hwnd, collect, None)
    return " ".join(parts)


def buttons_of(dialog) -> list:
    """Direct children plus one level down: dialog buttons often sit inside
    group boxes and tab pages. On Linux a bounded walk: GTK nests them deeper."""
    if not WINDOWS:
        return linux_dialogs.elements(dialog)
    out = []
    for child in dialog.GetChildren():
        out.append(child)
        with contextlib.suppress(Exception):
            out.extend(child.GetChildren())
    return out


def click_button(
    hwnd: int, labels: tuple[str, ...], *, fallback_ids: tuple[int, ...] = ()
) -> str | None:
    """Press the first matching button, through UIA rather than the pointer so
    a background dialog can be answered without stealing focus.

    When no label matches, `fallback_ids` names Win32 button IDs to try in
    order (IDNO, IDCANCEL) - how a prompt in
    another UI language is still answered. IDYES is refused: an automatic
    Yes is how Word ends up in safe mode. X11 buttons have no IDs: on Linux
    only the labels are tried."""
    if IDYES in fallback_ids:
        raise ValueError("IDYES is never pressed automatically")
    dialog = auto.ControlFromHandle(hwnd)
    if dialog is None:
        return None
    buttons = [b for b in buttons_of(dialog) if b.ControlTypeName == "ButtonControl"]
    for label in labels:
        for button in buttons:
            if (button.Name or "").strip().strip("&").casefold() == label.casefold():
                _press(button)
                return label
    for control_id in fallback_ids if WINDOWS else ():
        for button in buttons:
            if _control_id(button) == control_id:
                _press(button)
                return f"{(button.Name or '').strip()} (id {control_id})"
    return None


def _control_id(button) -> int | None:
    try:
        return win32gui.GetDlgCtrlID(button.NativeWindowHandle)
    except Exception:  # noqa: BLE001 - a windowless button has no ID
        return None


def _press(button) -> None:
    pattern = button.GetInvokePattern()
    if pattern is not None:
        pattern.Invoke()
    else:
        from . import ui  # ui imports this module

        ui.click(button, mode="mouse", activate=False, allow_disabled=True)


def policy(ctx) -> tuple[tuple, ...]:
    """(text to look for, buttons to try[, fallback button IDs]) for the
    prompts this app knows; see click_button for the IDs.

    Core knows none: an app supplies its table through a `dialog_policy` hook,
    and only when its profile says `dialogs = "auto"`.
    """
    if ctx is None or ctx.profile is None or ctx.profile.dialogs != "auto":
        return ()
    known: list[tuple] = []
    for fn in ctx.registry.hooks_for(ctx.name, "dialog_policy"):
        known += list(fn(ctx) or ())
    return tuple(known)


def reap(
    pids: set[int] | list[int] | None,
    prompts: tuple[tuple, ...],
    *,
    classes: tuple[str, ...] = BASE_DIALOG_CLASSES,
    handles: list[int] | None = None,
) -> list[dict]:
    """Answer the prompts the policy names, and only those: guessing at an
    unknown dialog is worse than reporting it and stopping. `handles`, when
    given, gets the hwnd of every dialog answered (for `remaining`)."""
    dismissed: list[dict] = []
    for dialog in dialog_windows(pids, classes=classes):
        haystack = (dialog["title"] + " " + dialog["text"]).casefold()
        for needle, labels, *ids in prompts:
            if needle not in haystack:
                continue
            clicked = click_button(dialog["hwnd"], labels, fallback_ids=ids[0] if ids else ())
            if clicked is None:
                win32gui.PostMessage(dialog["hwnd"], win32con.WM_CLOSE, 0, 0)
                clicked = "close"
            dismissed.append(_answered(dialog, clicked))
            if handles is not None:
                handles.append(dialog["hwnd"])
            break
    return dismissed


def _answered(dialog: dict, clicked: str) -> dict:
    return {"title": dialog["title"], "text": dialog["text"][:200], "answered": clicked}


def close_all(
    pids: set[int] | list[int] | None,
    *,
    prefer: tuple[str, ...] = CLOSE_ORDER,
    classes: tuple[str, ...] = BASE_DIALOG_CLASSES,
    handles: list[int] | None = None,
) -> list[dict]:
    """Shut every dialog, trying the least destructive button first.
    `handles`, when given, gets the hwnd of every dialog closed."""
    closed: list[dict] = []
    for dialog in dialog_windows(pids, classes=classes):
        clicked = click_button(dialog["hwnd"], prefer)
        if clicked is None:
            win32gui.PostMessage(dialog["hwnd"], win32con.WM_CLOSE, 0, 0)
            clicked = "close"
        closed.append(_answered(dialog, clicked))
        if handles is not None:
            handles.append(dialog["hwnd"])
    return closed


def remaining(
    pids: set[int] | list[int] | None,
    answered: list[int],
    *,
    classes: tuple[str, ...] = BASE_DIALOG_CLASSES,
    timeout: float = CLOSE_SETTLE,
) -> list[dict]:
    """The dialogs still open once the `answered` ones (hwnds) are gone, or
    after `timeout`: an answered dialog that is still listed then did not
    close, and is reported."""
    last: list[dict] = []

    def settled() -> bool:
        last[:] = dialog_windows(pids, classes=classes)
        return not any(dialog["hwnd"] in answered for dialog in last)

    with contextlib.suppress(Timeout):
        wait_until(settled, timeout=timeout, interval=0.1, what="answered dialogs to close")
    return last
