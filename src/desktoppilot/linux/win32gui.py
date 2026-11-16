"""The win32gui functions the core calls, over EWMH/X11 (`x11.py`).

An hwnd is the X window id of a client window. Top-level means a client
window in `x11.client_windows()`; X11 has no child HWNDs, so the child-window
calls see none and the core falls back to the AT-SPI tree. A window that is
gone reads as empty (no title, no class, an empty rect) instead of raising.
"""

from __future__ import annotations

from . import win32con, x11


class error(Exception):  # noqa: N801 - pywintypes.error's name
    """What a call with no X11 meaning raises, as pywin32 raises its error."""


def EnumWindows(callback, extra) -> None:
    """callback(hwnd, extra) for every client window, topmost first; a False
    (not None) return stops the enumeration, as pywin32's does."""
    for hwnd in x11.client_windows():
        result = callback(hwnd, extra)
        if result is not None and not result:
            return


def EnumChildWindows(hwnd: int, callback, extra) -> None:
    """X11 clients have no child HWNDs a caller could use: calls nothing."""


def GetAncestor(hwnd: int, flag: int) -> int:
    """GA_ROOT: the client window holding hwnd. GA_ROOTOWNER: from there,
    follow `WM_TRANSIENT_FOR` to the window it belongs to. GA_PARENT: the
    desktop, for a client window."""
    top = x11.toplevel(hwnd)
    if flag == win32con.GA_PARENT:
        return GetDesktopWindow() if top == hwnd else top
    if flag == win32con.GA_ROOTOWNER:
        seen = {top}
        owner = x11.transient_for(top) if top else None
        while owner and owner not in seen:
            seen.add(owner)
            top = owner
            owner = x11.transient_for(owner)
    return top


def GetClassName(hwnd: int) -> str:
    """The `WM_CLASS` class part (e.g. "Code"), which profiles name in
    `window_class`."""
    return x11.wm_class(hwnd)[1]


def GetDesktopWindow() -> int:
    """The root window's id."""
    with x11.lock():
        return x11.display().screen().root.id


def GetDlgCtrlID(hwnd: int) -> int:
    """No dialog control ids on X11: raises, as for a windowless button."""
    raise error(f"no dialog control id on X11 (window {hwnd:#x})")


def GetForegroundWindow() -> int:
    """`_NET_ACTIVE_WINDOW`, or 0."""
    return x11.active_window() or 0


def GetTopWindow(hwnd) -> int:
    """The topmost client window (hwnd None or the desktop), or 0: a client
    has no child windows."""
    if hwnd and hwnd != GetDesktopWindow():
        return 0
    windows = x11.client_windows()
    return windows[0] if windows else 0


def GetWindow(hwnd: int, cmd: int) -> int:
    """GW_HWNDNEXT / GW_HWNDPREV: the next client window down / up the stacking
    order; GW_HWNDFIRST / LAST: the top / bottom one; GW_OWNER: the
    transient-for window. 0 when there is none."""
    if cmd == win32con.GW_OWNER:
        return x11.transient_for(hwnd) or 0
    if cmd == win32con.GW_CHILD:
        return 0
    windows = x11.client_windows()
    if not windows:
        return 0
    if cmd == win32con.GW_HWNDFIRST:
        return windows[0]
    if cmd == win32con.GW_HWNDLAST:
        return windows[-1]
    if hwnd not in windows:
        return 0
    at = windows.index(hwnd) + (1 if cmd == win32con.GW_HWNDNEXT else -1)
    return windows[at] if 0 <= at < len(windows) else 0


def GetWindowRect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the visible frame, decorations included."""
    return x11.frame_rect(hwnd)


def GetWindowText(hwnd: int) -> str:
    """The window title (`x11.title`)."""
    return x11.title(hwnd)


def IsIconic(hwnd: int) -> bool:
    """Minimized: `_NET_WM_STATE_HIDDEN`, or WM_STATE IconicState."""
    return x11.is_minimized(hwnd)


def IsWindow(hwnd: int) -> bool:
    return x11.exists(hwnd)


def IsWindowVisible(hwnd: int) -> bool:
    """A client the WM manages, minimized included (Windows calls a minimized
    window visible), or a mapped override-redirect popup."""
    if x11.wm_state(hwnd) in (x11.NORMAL, x11.ICONIC):
        return True
    return x11.is_override_redirect(hwnd) and x11.is_mapped(hwnd)


def MoveWindow(hwnd: int, left: int, top: int, width: int, height: int, repaint: bool) -> None:
    """Move and size the frame (`x11.move_resize`), waiting until it took."""
    x11.move_resize(hwnd, left, top, width, height)


def PostMessage(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    """Only WM_CLOSE has a meaning: `_NET_CLOSE_WINDOW`. Any other message
    raises."""
    if msg != win32con.WM_CLOSE:
        raise error(f"message {msg:#x} has no X11 meaning; only WM_CLOSE does")
    x11.close(hwnd)


def ShowWindow(hwnd: int, cmd: int) -> None:
    """SW_MINIMIZE / SW_SHOWMINIMIZED minimize. SW_SHOWNOACTIVATE and SW_SHOW
    restore without the focus; SW_RESTORE and SW_SHOWNORMAL restore and
    activate, as on Windows; SW_MAXIMIZE maximizes and activates. SW_HIDE
    raises: withdrawing another client's window is not the WM's to undo."""
    if cmd in (win32con.SW_MINIMIZE, win32con.SW_SHOWMINIMIZED):
        x11.minimize(hwnd)
    elif cmd in (win32con.SW_SHOWNOACTIVATE, win32con.SW_SHOW):
        x11.restore(hwnd)
    elif cmd in (win32con.SW_RESTORE, win32con.SW_SHOWNORMAL):
        x11.restore(hwnd)
        x11.activate(hwnd)
    elif cmd == win32con.SW_MAXIMIZE:
        x11.maximize(hwnd)
        x11.activate(hwnd)
    else:
        raise error(f"ShowWindow({cmd}) has no X11 emulation")


def WindowFromPoint(point: tuple[int, int]) -> int:
    """The topmost client window whose frame holds the point, or 0."""
    return x11.window_at(*point) or 0
