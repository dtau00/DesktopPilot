"""An app's dialogs on X11: the Linux side of `core/dialogs.py`.

X11 has no dialog window class like Win32's #32770; a dialog is a client
window the window manager is told is one. A window of the app's pids counts
when any of these holds:

- its `_NET_WM_WINDOW_TYPE` is dialog, or its `_NET_WM_STATE` modal;
- it is transient for another window of the app and typed normal or not
  typed (EWMH reads an untyped transient window as a dialog);
- its WM_CLASS (instance or class) is in the profile's `dialog_classes`;
- it is not typed at all and its AT-SPI object is a dialog, alert or file
  chooser.

The text is what the dialog's labels say, read from the AT-SPI tree. GTK
nests a dialog's buttons a few levels down, so they are found by the same
bounded walk.
"""

from __future__ import annotations

import contextlib
import time

from . import auto, win32gui, x11

DIALOG_ROLES = ("dialog", "alert", "file chooser")
# The elements whose names are the dialog's message, as Static and SysLink
# are on Windows.
TEXT_ROLES = ("label", "static", "link", "heading", "paragraph", "caption")
# A button's caption is a label inside it: not part of the message.
CONTROL_ROLES = ("push button", "button", "toggle button", "check box", "radio button", "combo box")
# A dialog is a message and a few buttons: the walk stays small.
WALK_DEPTH = 8
WALK_NODES = 300
WALK_SECONDS = 2.0


def dialog_windows(pids: set[int] | None, classes: tuple[str, ...]) -> list[dict]:
    """Dialogs of the pids (None: any process), topmost first, as
    {hwnd, title, text}."""
    wanted = {c.casefold() for c in classes}
    owned = [w for w in x11.client_windows() if _owned(w, pids)]
    found = []
    for hwnd in owned:
        with contextlib.suppress(Exception):  # closed while we looked at it
            if _is_dialog(hwnd, set(owned) - {hwnd}, wanted):
                found.append({"hwnd": hwnd, "title": x11.title(hwnd), "text": dialog_text(hwnd)})
    return found


def _owned(hwnd: int, pids: set[int] | None) -> bool:
    try:
        if x11.is_override_redirect(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        return pids is None or x11.pid(hwnd) in pids
    except Exception:  # noqa: BLE001 - a window gone mid-listing is not the app's
        return False


def _is_dialog(hwnd: int, others: set[int], classes: set[str]) -> bool:
    types = set(x11.window_type(hwnd))
    if "dialog" in types or "modal" in x11.state(hwnd):
        return True
    if types <= {"normal"} and x11.transient_for(hwnd) in others:
        return True
    if any(part.casefold() in classes for part in x11.wm_class(hwnd) if part):
        return True
    return not types and _role(_control(hwnd)) in DIALOG_ROLES


def _control(hwnd: int):
    return auto.ControlFromHandle(hwnd)


def _role(control) -> str | None:
    try:
        return control.accessible.get_role_name()
    except Exception:  # noqa: BLE001 - no AT-SPI object, or a dead one
        return None


def dialog_text(hwnd: int) -> str:
    """The names of the dialog's labels, links and headings, in tree order."""
    try:
        control = _control(hwnd)
    except Exception:  # noqa: BLE001 - no AT-SPI object: no text, as no Static on Windows
        return ""
    if control is None:
        return ""
    names: list[str] = []
    for node in elements(control, prune=CONTROL_ROLES):
        with contextlib.suppress(Exception):  # a dead element is skipped
            name = (node.Name or "").strip() if _role(node) in TEXT_ROLES else ""
            if name and name not in names:
                names.append(name)
    return " ".join(names)


def elements(control, prune: tuple[str, ...] = ()) -> list:
    """The control's descendants in tree order, cut at WALK_DEPTH levels,
    WALK_NODES elements or WALK_SECONDS, whichever comes first. Below an
    element of a `prune` role nothing is walked."""
    deadline = time.monotonic() + WALK_SECONDS
    found: list = []
    stack = [(child, 1) for child in reversed(_children(control))]
    while stack and len(found) < WALK_NODES and time.monotonic() < deadline:
        node, depth = stack.pop()
        found.append(node)
        if depth < WALK_DEPTH and not (prune and _role(node) in prune):
            stack += [(child, depth + 1) for child in reversed(_children(node))]
    return found


def _children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:  # noqa: BLE001 - a dead element has none
        return []
