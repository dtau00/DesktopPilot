"""Screen copies: a control's screenshot, where a plain shot sits on screen, a
screen rect as an image, and whether the window is minimized or covered.

On Linux every copy comes from the X root window (`x11.grab`), and a
top-level window's plain shot is its X frame, decorations included."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..linux import x11
from . import ui
from .errors import PilotError
from .native import WINDOWS, auto, win32con, win32gui, win32process
from .registry import AppContext
from .util import wait_until


def window_origin(ctx: AppContext) -> tuple[int, int]:
    """The top-left of a plain shot of the main window - its DWM frame, not
    BoundingRectangle and its invisible resize borders - so a point read off
    that shot clicks where it was read."""
    if ctx is None or ctx.profile is None:
        raise PilotError(
            f"--relative needs an app with a window; {getattr(ctx, 'name', '?')} has none"
        )
    left, top, _, _ = ui.capture_origin(ui.main_window(ctx))
    return left, top


def screenshot(control, path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        ui.capture_rect(*ui.capture_origin(control)).save(target)
    elif not control.CaptureToImage(str(target)):
        raise PilotError("screenshot failed: " + str(target))
    wait_until(
        lambda: target.exists() and target.stat().st_size > 0,
        timeout=5.0,
        what="screenshot file",
    )
    return target


def _top_level_handle(control) -> int:
    """The root window of the nearest windowed ancestor-or-self; 0 if none."""
    while control:
        handle = control.NativeWindowHandle
        if handle:
            return win32gui.GetAncestor(handle, win32con.GA_ROOT) or handle
        control = control.GetParentControl()
    return 0


def capture_origin(control) -> tuple[int, int, int, int]:
    """(left, top, width, height) on screen of what a plain shot of `control`
    holds. The rule of uiautomation's Bitmap.FromControl: a control with no
    size is shot as its first sized ancestor, a top-level window as its DWM
    frame, without the invisible resize borders."""
    rect = control.BoundingRectangle
    while rect.width() == 0 or rect.height() == 0:
        control = control.GetParentControl()
        if not control:
            raise PilotError("nothing to capture: the element and its ancestors have no size")
        rect = control.BoundingRectangle
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    handle = control.NativeWindowHandle
    if handle and win32gui.GetAncestor(handle, win32con.GA_ROOT) == handle:
        frame = _frame_bounds(handle)
        if frame is not None:
            left, top, right, bottom = frame
    return left, top, right - left, bottom - top


def _frame_bounds(handle: int) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of a top-level window as it shows: its DWM
    frame, or on X11 its window manager frame."""
    if not WINDOWS:
        return x11.frame_rect(handle)
    frame = auto.DwmGetWindowExtendFrameBounds(handle) if auto.DwmIsCompositionEnabled() else None
    return None if frame is None else (frame.left, frame.top, frame.right, frame.bottom)


def capture_rect(left: int, top: int, width: int, height: int) -> Image.Image:
    """A screen rect as a PIL image, copied the way `screenshot` copies it:
    through the root control, whose own origin is taken back out, so any
    virtual-screen coordinate works - negative ones included. On X11 the
    root window is the virtual screen."""
    if not WINDOWS:
        return x11.grab(left, top, width, height)
    root = auto.GetRootControl()
    root_left, root_top, _, _ = ui.capture_origin(root)
    bitmap = auto.Bitmap.FromControl(
        root, x=left - root_left, y=top - root_top, width=width, height=height
    )
    if not bitmap:
        raise PilotError(f"screen capture failed: {width}x{height} at {left},{top}")
    with bitmap:
        size = (bitmap.Width, bitmap.Height)
        # ARGB words, little endian: the bytes run B, G, R, A.
        pixels = bytes(bitmap.GetAllPixelColors())
    return Image.frombuffer("RGBA", size, pixels, "raw", "BGRA", 0, 1).convert("RGB")


def is_minimized(control) -> bool:
    handle = _top_level_handle(control)
    return bool(handle) and bool(win32gui.IsIconic(handle))


def is_occluded(control, rect: tuple[int, int, int, int]) -> bool:
    """Whether another process's window is on top at the centre or a corner
    (2 px in) of the screen rect. A screen copy captures whatever is on top;
    a menu or popup of the app itself is part of the picture, not in its way."""
    target = _top_level_handle(control)
    if not target or target == win32gui.GetDesktopWindow():
        return False
    _, owner = win32process.GetWindowThreadProcessId(target)
    left, top, width, height = rect
    inset_x, inset_y = min(2, (width - 1) // 2), min(2, (height - 1) // 2)
    right, bottom = left + width - 1 - inset_x, top + height - 1 - inset_y
    points = [
        (left + width // 2, top + height // 2),
        (left + inset_x, top + inset_y),
        (right, top + inset_y),
        (left + inset_x, bottom),
        (right, bottom),
    ]
    for point in points:
        hit = win32gui.WindowFromPoint(point)
        if not hit:
            continue
        root = win32gui.GetAncestor(hit, win32con.GA_ROOT) or hit
        if root != target and win32process.GetWindowThreadProcessId(root)[1] != owner:
            return True
    return False
