"""The add-in's own windows: the titled WPF windows and the untitled toasts,
found and closed, and every top-level window Word owns."""

from __future__ import annotations

import time

import uiautomation as auto
import win32gui

from desktoppilot.core import apps, ui
from desktoppilot.core.errors import Timeout, WalkCut
from desktoppilot.core.registry import AppContext
from desktoppilot.core.selectors import Step
from desktoppilot.core.util import collapse_whitespace

from . import roots
from .frame import DEFAULT_TIMEOUT
from .session import WORD_WINDOW_CLASS

WPF_WINDOW_CLASS_PREFIX = "HwndWrapper["
CLOSE_IDS = ("closewindow", "closebutton", "close")
UNTITLED = "(untitled)"
# One budget for close_addin_windows: the frame lookup and the toast walk.
CLOSE_TIMEOUT = 5.0


def addin_window(
    ctx: AppContext,
    title: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe: bool = False,
):
    """A window the add-in opened, matched on title (Word's frame excluded).
    A probe looks once instead of waiting for it."""

    def find_it():
        apps.require_running(ctx.profile)
        needle = (title or "").casefold()
        for win in apps.windows(ctx.profile):
            if win["class"] == WORD_WINDOW_CLASS or not win["title"]:
                continue
            if needle and needle not in win["title"].casefold():
                continue
            return auto.ControlFromHandle(win["hwnd"])
        return None

    what = "add-in window " + repr(title or "any")
    return ui.present(find_it, timeout=timeout, what=what, probe=probe)


def close_addin_windows(
    ctx: AppContext, *, timeout: float = CLOSE_TIMEOUT, not_closed: list | None = None
) -> list[str]:
    """Close the add-in's WPF windows: the titles of the titled ones, then one
    UNTITLED per untitled toast. Docked task-pane hosts are left alone:
    closing one by handle is not toggling the pane off.

    A plain list means the toasts were checked. When they could not be (no
    frame in time, the toast walk cut) it raises WalkCut, whose `hits` holds
    what was closed. `timeout` bounds the whole call. A window that would not
    close goes to `not_closed` as {"window", "hwnd", "error"}, not the list;
    one that vanished meanwhile is neither."""
    deadline = time.monotonic() + timeout
    closed: list[str] = []
    failed = [] if not_closed is None else not_closed
    for win in apps.windows(ctx.profile):
        if win["class"].startswith(WPF_WINDOW_CLASS_PREFIX) and win["title"]:
            _close(win["hwnd"], win["title"], closed, failed)
    try:
        frame = roots.word_window(ctx, timeout=roots._left(deadline))
    except Timeout as exc:
        raise WalkCut(
            f"closed {closed!r}; no Word frame after {timeout - roots._left(deadline):.1f}s (budget "
            f"{timeout:.1f}s), so untitled toasts were not checked; Word is busy",
            closed,
        ) from exc
    try:
        toasts = untitled_addin_windows(frame, deadline=deadline)
    except WalkCut as cut:
        raise WalkCut(
            f"closed {closed!r}; the toast walk was cut after {timeout - roots._left(deadline):.1f}s "
            f"(budget {timeout:.1f}s), so untitled toasts were not checked; Word is busy",
            closed,
        ) from cut
    for toast in toasts:
        _close(toast.NativeWindowHandle, UNTITLED, closed, failed)
    return closed


def _close(hwnd: int, label: str, closed: list, failed: list) -> None:
    try:
        ui.close_window(hwnd)
    except Exception as exc:  # noqa: BLE001 - reported below, unless the window is gone
        if hwnd and not win32gui.IsWindow(hwnd):
            return
        failed.append({"window": label, "hwnd": hwnd, "error": str(exc)})
        return
    closed.append(label)


def untitled_addin_windows(frame, *, deadline: float | None = None) -> list:
    """The add-in's untitled child windows of the Word frame that carry buttons
    - its toasts ("3 items need review ...") and error boxes.

    The floating task-pane host is an untitled child Window too, but of
    another class and with no Button of its own, so it never qualifies.

    Past `deadline` (time.monotonic()), or on a children read Word timed out,
    it raises WalkCut: a toast not yet read is not a toast that is absent."""
    found = []
    for child in ui.search(frame, Step("Window", False), deadline=deadline):
        try:
            if collapse_whitespace(child.Name) or child.IsOffscreen or not child.NativeWindowHandle:
                continue
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            continue
        # Outside the broad except below: a cut walk must reach the caller.
        buttons = ui.search(child, Step("Button", False), visible_only=False, deadline=deadline)
        try:
            closable = any(
                (b.AutomationId or "").casefold() in CLOSE_IDS
                or collapse_whitespace(b.Name).casefold() == "close"
                for b in buttons
            )
            if buttons and (closable or _is_wpf_window(child.NativeWindowHandle)):
                found.append(child)
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            continue
    return found


def _is_wpf_window(hwnd: int) -> bool:
    try:
        return win32gui.GetClassName(hwnd).startswith(WPF_WINDOW_CLASS_PREFIX)
    except Exception:  # noqa: BLE001 - window gone
        return False


def top_windows(ctx: AppContext) -> list[dict]:
    """Every visible top-level window Word owns, add-in windows included."""
    return [
        {"hwnd": w["hwnd"], "title": w["title"], "class": w["class"]}
        for w in apps.windows(ctx.profile)
    ]
