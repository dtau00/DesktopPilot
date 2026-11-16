"""X11 and EWMH primitives over python-xlib, for `auto`, the `win32*`
modules and recording.

Every function takes and returns X window ids (int) of client windows - the
window the app created, not the window manager's frame - unless its name
says `frame`. Rects are in root (screen) coordinates.

A window can vanish between two calls. The X error that follows reads as
"gone" (False, None, 0, "" or an empty rect), never as an exception, so a
walk over windows survives one closing under it. python-xlib is not thread
safe: every request runs under one lock.
"""

from __future__ import annotations

import contextlib
import functools
import os
import threading

from ..core.errors import PilotError, Timeout
from ..core.util import wait_until

try:
    from Xlib import X, Xatom
    from Xlib import display as _xdisplay
    from Xlib import error as _xerror
    from Xlib.protocol import event as _xevent
except ImportError:  # Windows: core/apps.py imports this module everywhere
    X = Xatom = _xdisplay = _xerror = _xevent = None

# What a request on a vanished window raises: BadWindow, BadDrawable, BadMatch.
_GONE = (_xerror.XError,) if _xerror else ()

_LOCK = threading.RLock()
_display = None
_atoms: dict[str, int] = {}
_atom_names: dict[int, str] = {}
_stamp_window = None

# WM_STATE values (ICCCM 4.1.3.1).
WITHDRAWN, NORMAL, ICONIC = 0, 1, 3
# _NET_WM_STATE actions (EWMH).
_REMOVE, _ADD = 0, 1
# Source indication 2: a pager. xfwm4 and others honour a pager's request
# where focus-stealing prevention would refuse an application's.
_PAGER = 2
_STATIC_GRAVITY = 10
# A grab bigger than this goes through PIL (XCB, one copy); a small one
# through the cached connection, which saves opening a new one.
_PIL_GRAB_PIXELS = 250_000


def lock():
    """The lock every X request takes; `input` holds it across a batch."""
    return _LOCK


def _on_error(err, request) -> None:
    """Errors of requests without a reply (a send_event to a window that just
    closed) arrive later; python-xlib would print them. A gone window is
    what they mean, and the caller sees that on its next read."""


def display():
    """One cached `Xlib.display.Display`, opened on first use from $DISPLAY."""
    global _display
    with _LOCK:
        if _display is None:
            if _xdisplay is None:
                raise PilotError("python-xlib is not installed; the X11 backend needs it")
            try:
                d = _xdisplay.Display()
            except _xerror.DisplayError as exc:
                raise PilotError(
                    f"cannot open the X display {os.environ.get('DISPLAY')!r}: {exc}; "
                    "pilot drives an X11 session (set DISPLAY)"
                ) from exc
            d.set_error_handler(_on_error)
            _display = d
        return _display


def _gone(default):
    """Run under the lock; an X error (the window is gone) returns `default`
    (called, when it is a type such as list)."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with _LOCK:
                try:
                    return fn(*args, **kwargs)
                except _GONE:
                    return default() if isinstance(default, type) else default

        return inner

    return wrap


def _atom(name: str) -> int:
    if name not in _atoms:
        _atoms[name] = display().intern_atom(name)
    return _atoms[name]


def _atom_name(atom: int) -> str:
    if atom not in _atom_names:
        _atom_names[atom] = display().get_atom_name(atom)
    return _atom_names[atom]


def _root():
    return display().screen().root


def _win(w: int):
    return display().create_resource_object("window", w)


def _prop(w: int, name: str, kind=None):
    """A property's value (array or bytes), or None when unset."""
    prop = _win(w).get_full_property(_atom(name), X.AnyPropertyType if kind is None else kind)
    return None if prop is None else prop.value


def _cardinals(w: int, name: str, count: int) -> tuple[int, ...]:
    value = _prop(w, name, Xatom.CARDINAL)
    if value is None or len(value) < count:
        return (0,) * count
    return tuple(int(v) for v in value[:count])


def _client_message(w: int, kind: str, data: list[int]) -> None:
    """An EWMH request to the window manager: a ClientMessage to the root."""
    root = _root()
    message = _xevent.ClientMessage(
        window=_win(w), client_type=_atom(kind), data=(32, (list(data) + [0] * 5)[:5])
    )
    root.send_event(message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    display().flush()


def _on_screen(x: int, y: int, width: int, height: int) -> bool:
    """Whether more than one pixel each way of the rect is on the screen:
    xfwm4's mapped 1 px edge windows and its WM-check window are not."""
    _, _, sw, sh = screen_rect()
    return min(x + width, sw) - max(x, 0) > 1 and min(y + height, sh) - max(y, 0) > 1


def _popups() -> list[int]:
    """Mapped override-redirect windows on the screen, topmost first."""
    found = []
    for child in reversed(_root().query_tree().children):
        try:
            attrs = child.get_attributes()
            if not attrs.override_redirect or attrs.map_state != X.IsViewable:
                continue
            geo = child.get_geometry()
        except _GONE:
            continue
        if _on_screen(geo.x, geo.y, geo.width, geo.height):
            found.append(child.id)
    return found


@_gone(list)
def client_windows(stacking: bool = True) -> list[int]:
    """`_NET_CLIENT_LIST_STACKING` (or `_NET_CLIENT_LIST`), topmost first,
    with mapped override-redirect windows (menus, popups, tooltips) on top."""
    name = "_NET_CLIENT_LIST_STACKING" if stacking else "_NET_CLIENT_LIST"
    ids = [int(w) for w in (_prop(_root().id, name, Xatom.WINDOW) or [])]
    ids.reverse()  # the property runs bottom to top
    return _popups() + ids


@_gone(False)
def exists(w: int) -> bool:
    """Whether the window still exists (a stale id raises BadWindow)."""
    if not w:
        return False
    _win(w).get_attributes()
    return True


def _xres_pid(w: int) -> int:
    """The pid of the client that created the window, from the X server."""
    from Xlib.ext import res

    d = display()
    if not d.has_extension("X-Resource"):
        return 0
    reply = d.res_query_client_ids([{"client": w, "mask": res.LocalClientPIDMask}])
    for client in reply.ids:
        if client.value:
            return int(client.value[0])
    return 0


@_gone(0)
def pid(w: int) -> int:
    """`_NET_WM_PID`, else the XRes client pid; 0 when neither is known."""
    value = _prop(w, "_NET_WM_PID", Xatom.CARDINAL)
    if value:
        return int(value[0])
    return _xres_pid(w)


def _text(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw).decode("latin-1")


@_gone("")
def title(w: int) -> str:
    """`_NET_WM_NAME` (UTF-8), else `WM_NAME`; "" when it has none."""
    value = _prop(w, "_NET_WM_NAME")
    if not value:
        value = _prop(w, "WM_NAME")
    return _text(value) if value else ""


@_gone(("", ""))
def wm_class(w: int) -> tuple[str, str]:
    """`WM_CLASS` as (instance, class); ("", "") when unset."""
    value = _prop(w, "WM_CLASS", Xatom.STRING)
    if not value:
        return ("", "")
    parts = _text(value).split("\0")
    return (parts[0], parts[1] if len(parts) > 1 else "")


@_gone((0, 0, 0, 0))
def x_rect(w: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the X window itself: a CSD window's
    invisible shadow included. The origin GTK4's window-relative extents
    count from."""
    win = _win(w)
    geo = win.get_geometry()
    origin = _root().translate_coords(win, 0, 0)
    return (origin.x, origin.y, origin.x + geo.width, origin.y + geo.height)


@_gone((0, 0, 0, 0))
def _shadow(w: int) -> tuple[int, int, int, int]:
    """(left, right, top, bottom) of a client-side decorated window's
    invisible shadow (`_GTK_FRAME_EXTENTS`, GTK's and Chrome's)."""
    return _cardinals(w, "_GTK_FRAME_EXTENTS", 4)


@_gone((0, 0, 0, 0))
def _decorations(w: int) -> tuple[int, int, int, int]:
    """(left, right, top, bottom) of the WM's decorations
    (`_NET_FRAME_EXTENTS`)."""
    return _cardinals(w, "_NET_FRAME_EXTENTS", 4)


def _extents(w: int) -> tuple[int, int, int, int]:
    """(left, right, top, bottom) to add to the X window's rect for the
    visible frame: the WM's decorations less a CSD window's shadow."""
    return tuple(a - b for a, b in zip(_decorations(w), _shadow(w), strict=True))


def client_rect(w: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the client area: the X window without
    a CSD window's invisible shadow, and without WM decorations. Always
    inside `frame_rect`, as a Win32 client rect is inside GetWindowRect."""
    left, top, right, bottom = x_rect(w)
    sl, sr, st, sb = _shadow(w)
    return (left + sl, top + st, right - sr, bottom - sb)


def frame_rect(w: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the visible window: the client area
    plus WM decorations (`_NET_FRAME_EXTENTS`); a CSD window's invisible
    shadow (`_GTK_FRAME_EXTENTS`) left out. What a screen copy of the window
    holds."""
    left, top, right, bottom = client_rect(w)
    el, er, et, eb = _decorations(w)
    return (left - el, top - et, right + er, bottom + eb)


def _names(w: int, name: str, prefix: str) -> list[str]:
    atoms = _prop(w, name, Xatom.ATOM) or []
    names = []
    for atom in atoms:
        full = _atom_name(int(atom))
        names.append(full.removeprefix(prefix).lower())
    return names


@_gone(list)
def window_type(w: int) -> list[str]:
    """`_NET_WM_WINDOW_TYPE` short names: normal, dialog, menu, popup_menu,
    dropdown_menu, tooltip, ..."""
    return _names(w, "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_")


def is_normal(w: int) -> bool:
    """An app's own window, not a popup: not override-redirect, and its
    first standard `_NET_WM_WINDOW_TYPE` is `normal` (an untyped window is
    normal, as ICCCM has it). A menu, a tooltip or a dialog is not."""
    if is_override_redirect(w):
        return False
    types = [t for t in window_type(w) if not t.startswith("_")]
    return not types or types[0] == "normal"


@_gone(list)
def state(w: int) -> list[str]:
    """`_NET_WM_STATE` short names: hidden, modal, maximized_vert, above, ..."""
    return _names(w, "_NET_WM_STATE", "_NET_WM_STATE_")


@_gone(None)
def wm_state(w: int) -> int | None:
    """ICCCM `WM_STATE`: NORMAL, ICONIC or WITHDRAWN, which the WM sets on
    every window it manages; None for one it does not."""
    value = _prop(w, "WM_STATE")
    return int(value[0]) if value is not None and len(value) else None


@_gone(None)
def transient_for(w: int) -> int | None:
    """`WM_TRANSIENT_FOR`: the window a dialog belongs to, or None."""
    value = _prop(w, "WM_TRANSIENT_FOR", Xatom.WINDOW)
    owner = int(value[0]) if value is not None and len(value) else 0
    return owner if owner and owner != _root().id and owner != w else None


@_gone(False)
def is_override_redirect(w: int) -> bool:
    return bool(_win(w).get_attributes().override_redirect)


@_gone(False)
def is_mapped(w: int) -> bool:
    """Viewable: mapped, and so is every ancestor (a minimized client is not)."""
    return _win(w).get_attributes().map_state == X.IsViewable


def is_minimized(w: int) -> bool:
    """`_NET_WM_STATE_HIDDEN`, or WM_STATE IconicState."""
    return "hidden" in state(w) or wm_state(w) == ICONIC


def _is_client(w: int) -> bool:
    """A window the WM manages (it has WM_STATE), or an override-redirect
    top-level window, which has no WM and so is its own client."""
    if wm_state(w) is not None:
        return True
    win = _win(w)
    return bool(win.get_attributes().override_redirect) and win.query_tree().parent.id == _root().id


def _client_in(w: int) -> int:
    """The client window inside a WM frame (the XmuClientWindow search), or 0."""
    queue = [w]
    while queue:
        current = queue.pop(0)
        if _is_client(current):
            return current
        queue += [child.id for child in _win(current).query_tree().children]
    return 0


@_gone(0)
def toplevel(w: int) -> int:
    """The client window that holds `w` (itself when it is one), or 0."""
    root = _root().id
    current = w
    while current and current != root:
        if _is_client(current):
            return current
        parent = _win(current).query_tree().parent.id
        if parent == root:  # a frame, or an unmanaged top-level window
            return _client_in(current)
        current = parent
    return 0


@_gone(None)
def active_window() -> int | None:
    """`_NET_ACTIVE_WINDOW`, or None."""
    value = _prop(_root().id, "_NET_ACTIVE_WINDOW", Xatom.WINDOW)
    active = int(value[0]) if value is not None and len(value) else 0
    return active or None


def server_time() -> int:
    """The X server's current time: the timestamp of a PropertyNotify on a
    private window. A WM checks a request's time against its last user
    event; CurrentTime reads as "old" to some."""
    global _stamp_window
    with _LOCK:
        d = display()
        if _stamp_window is None:
            _stamp_window = _root().create_window(
                -10, -10, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask
            )
        _stamp_window.change_property(_atom("_DESKTOPPILOT_TIME"), Xatom.STRING, 8, b"")
        d.flush()
        while True:  # the notify is certain to come; other events are not ours
            event = d.next_event()
            if event.type == X.PropertyNotify and event.window.id == _stamp_window.id:
                return int(event.time)


def _settle(check, timeout: float) -> bool:
    try:
        return bool(wait_until(check, timeout=timeout, interval=0.02, what="the window manager"))
    except Timeout:
        return False


def activate(w: int, timeout: float = 3.0) -> bool:
    """`_NET_ACTIVE_WINDOW` client message, source 2 (a pager, which the WM
    does not refuse), with the server time; then wait until it is active
    and holds the keyboard focus, so keys sent next reach it and not the
    window before. The WM un-minimizes it on the way. Whether it took."""
    if not exists(w):
        return False
    with _LOCK:
        _client_message(w, "_NET_ACTIVE_WINDOW", [_PAGER, server_time(), active_window() or 0])
    return _settle(lambda: active_window() == w and focus_window() == w, timeout)


@_gone(0)
def focus_window() -> int:
    """The client window that holds the X keyboard focus, or 0."""
    focus = display().get_input_focus().focus
    return toplevel(focus.id) if hasattr(focus, "id") else 0


def minimize(w: int) -> bool:
    """Iconify (`WM_CHANGE_STATE` IconicState); whether it took."""
    if not exists(w):
        return False
    with _LOCK:
        _client_message(w, "WM_CHANGE_STATE", [ICONIC])
    return _settle(lambda: is_minimized(w), 2.0)


def restore(w: int) -> bool:
    """Un-minimize (map the client: the WM deiconifies it) or, when it is
    not minimized, un-maximize. Meant as Windows' SW_SHOWNOACTIVATE, but
    xfwm4 focuses a window it deiconifies (a user time of 0 does not stop it). A window minimized from maximized comes back
    maximized, as on Windows. Whether it took."""
    if not exists(w):
        return False
    if is_minimized(w):
        with _LOCK:
            _win(w).map()
            display().flush()
        return _settle(lambda: not is_minimized(w), 2.0)
    if {"maximized_vert", "maximized_horz"} & set(state(w)):
        with _LOCK:
            _client_message(
                w,
                "_NET_WM_STATE",
                [
                    _REMOVE,
                    _atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                    _atom("_NET_WM_STATE_MAXIMIZED_HORZ"),
                    _PAGER,
                ],
            )
        return _settle(lambda: not {"maximized_vert", "maximized_horz"} & set(state(w)), 2.0)
    return True


def maximize(w: int) -> bool:
    """Add both maximized states; whether it took."""
    if not exists(w):
        return False
    with _LOCK:
        _client_message(
            w,
            "_NET_WM_STATE",
            [
                _ADD,
                _atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                _atom("_NET_WM_STATE_MAXIMIZED_HORZ"),
                _PAGER,
            ],
        )
    return _settle(lambda: {"maximized_vert", "maximized_horz"} <= set(state(w)), 2.0)


def close(w: int) -> None:
    """`_NET_CLOSE_WINDOW`: the WM asks the app, as the title-bar X does."""
    if not exists(w):
        return
    with _LOCK:
        _client_message(w, "_NET_CLOSE_WINDOW", [server_time(), _PAGER])


def move_resize(w: int, left: int, top: int, width: int, height: int) -> bool:
    """Move and size the window's visible frame (`_NET_MOVERESIZE_WINDOW`,
    static gravity: the client's own position, so no WM reads the frame
    differently). Waits until the frame has it, or has stopped changing (an
    app may enforce a minimum size); whether it got exactly that."""
    if not exists(w):
        return False
    el, er, et, eb = _extents(w)
    flags = _STATIC_GRAVITY | (0xF << 8) | (_PAGER << 12)
    client = [left + el, top + et, max(1, width - el - er), max(1, height - et - eb)]
    before = frame_rect(w)
    with _LOCK:
        _client_message(w, "_NET_MOVERESIZE_WINDOW", [flags, *client])
    target = (left, top, left + width, top + height)
    seen = {"rect": before, "polls": 0}

    def settled():
        rect = frame_rect(w)
        if rect == target:
            return True
        if rect == seen["rect"]:
            seen["polls"] += 1
        else:
            seen.update(rect=rect, polls=0)
        return rect != before and seen["polls"] >= 5

    _settle(settled, 2.0)
    return frame_rect(w) == target


@_gone(None)
def window_at(x: int, y: int) -> int | None:
    """The topmost client window whose frame holds the point: the root's
    child the X server finds there (it knows the stacking and the shapes),
    then the client inside that frame."""
    root = _root()
    child = root.translate_coords(root, x, y).child
    if child:
        found = _client_in(child.id)
        if found:
            return found
    for w in client_windows():  # a WM's own window there: look at the clients
        if is_mapped(w):
            left, top, right, bottom = frame_rect(w)
            if left <= x < right and top <= y < bottom:
                return w
    return None


@_gone((0, 0, 0, 0))
def screen_rect() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the root window: every monitor."""
    geo = _root().get_geometry()  # live: the screen info is from the connect
    return (0, 0, geo.width, geo.height)


def pointer() -> tuple[int, int]:
    """(x, y) of the pointer, in root-window pixels."""
    found = _root().query_pointer()
    return (found.root_x, found.root_y)


def monitors() -> list[tuple[int, int, int, int]]:
    """(left, top, width, height) per monitor (RandR); the screen when RandR
    has none to report."""
    with _LOCK, contextlib.suppress(Exception):
        from Xlib.ext import randr

        if display().has_extension("RANDR"):
            found = [
                (m.x, m.y, m.width_in_pixels, m.height_in_pixels)
                for m in randr.get_monitors(_root(), is_active=True).monitors
            ]
            if found:
                return found
    return [screen_rect()]


def _intersect(a, b) -> tuple[int, int, int, int]:
    """Two (left, top, right, bottom) rects' overlap; empty is zero-sized."""
    left, top = max(a[0], b[0]), max(a[1], b[1])
    return (left, top, max(left, min(a[2], b[2])), max(top, min(a[3], b[3])))


def work_area(w: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom): `_NET_WORKAREA` cut to the window's monitor
    (the one its frame overlaps most)."""
    frame = frame_rect(w)
    mons = [(x, y, x + mw, y + mh) for x, y, mw, mh in monitors()]
    monitor = max(mons, key=lambda m: _area(_intersect(m, frame)))
    with _LOCK:
        root = _root().id
        areas = list(_prop(root, "_NET_WORKAREA", Xatom.CARDINAL) or [])
        desktop = _cardinals(root, "_NET_CURRENT_DESKTOP", 1)[0]
    if len(areas) < 4:
        return monitor
    index = desktop if len(areas) >= 4 * (desktop + 1) else 0
    x, y, aw, ah = (int(v) for v in areas[4 * index : 4 * index + 4])
    cut = _intersect((x, y, x + aw, y + ah), monitor)
    return cut if _area(cut) else monitor


def _area(rect) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def dpi() -> int:
    """`Xft.dpi` from the resource database, else 96."""
    with _LOCK, contextlib.suppress(Exception):
        value = _prop(_root().id, "RESOURCE_MANAGER", Xatom.STRING)
        for line in _text(value or b"").splitlines():
            key, _, setting = line.partition(":")
            if key.strip() == "Xft.dpi" and setting.strip():
                return round(float(setting.strip()))
    return 96


def _grab_part(left: int, top: int, width: int, height: int):
    from PIL import Image, ImageGrab

    if width * height <= _PIL_GRAB_PIXELS:
        with _LOCK:
            raw = _root().get_image(left, top, width, height, X.ZPixmap, 0xFFFFFFFF)
        if raw.depth in (24, 32) and len(raw.data) == width * height * 4:
            return Image.frombytes("RGB", (width, height), raw.data, "raw", "BGRX")
    shot = ImageGrab.grab(
        bbox=(left, top, left + width, top + height), xdisplay=display().get_display_name()
    )
    return shot.convert("RGB")


def selection_owner(selection: str = "CLIPBOARD") -> int:
    """The window that owns the selection, or 0."""
    with _LOCK:
        owner = display().get_selection_owner(_atom(selection))
        return int(owner.id) if owner else 0


def selection_atom(selection: str = "CLIPBOARD") -> int:
    with _LOCK:
        return _atom(selection)


def grab(left: int, top: int, width: int, height: int):
    """A screen rect as an RGB `PIL.Image`, copied from the root window. What
    lies off the screen is black."""
    from PIL import Image

    if width <= 0 or height <= 0:
        raise PilotError(f"nothing to capture: {width}x{height} at {left},{top}")
    sl, st, sw, sh = screen_rect()
    il, it, ir, ib = _intersect((left, top, left + width, top + height), (sl, st, sl + sw, st + sh))
    if (il, it, ir, ib) == (left, top, left + width, top + height):
        return _grab_part(left, top, width, height)
    image = Image.new("RGB", (width, height))
    if ir > il and ib > it:
        image.paste(_grab_part(il, it, ir - il, ib - it), (il - left, it - top))
    return image
