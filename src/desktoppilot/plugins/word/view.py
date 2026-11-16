"""What `word layout` and `word visible` share: the document window, the view
settings, page numbers and an error list that keeps one bad item from failing
the whole read.

Coordinates are physical screen pixels: importing `uiautomation` makes this
process per-monitor DPI aware, and Window.GetPoint answers in physical pixels.
"""

from __future__ import annotations

import ctypes
import re
from difflib import SequenceMatcher

import pythoncom
import uiautomation  # noqa: F401 - imported for the per-monitor DPI awareness it sets
import win32gui

from .com import com_retry

WD_ACTIVE_END_PAGE_NUMBER = 3
WD_HORIZONTAL_POSITION_RELATIVE_TO_PAGE = 5
WD_VERTICAL_POSITION_RELATIVE_TO_PAGE = 6
WD_COLLAPSE_START = 1
# The document host: holds the canvas and its scrollbar, never a pane. Word
# names it after the file, so "outline-copy" would pass for pane:Outline.
DOCUMENT_HOST_CLASS = "_WwB"
# The window classes from the frame down to the document canvas.
FRAME_CHAIN = ("_WwF", DOCUMENT_HOST_CLASS, "_WwG")
DOCUMENT_CLASS = FRAME_CHAIN[-1]
SNIPPET = 30
# One item's COM call; the whole read must not sit out com_retry's 20 s on each.
ITEM_TIMEOUT = 5.0
VIEW_TYPES = {
    1: "normal",
    2: "outline",
    3: "print",
    4: "print-preview",
    5: "master",
    6: "web",
    7: "reading",
    8: "conflict",
}
_WHITESPACE = re.compile(r"[\s\x00-\x1f]+")


class Errors:
    """COM reads that may fail one item at a time, collected instead of raised."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def get(self, what: str, read, default=None):
        try:
            return com_retry(read, timeout=ITEM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - one bad item is reported, not fatal
            self.add(what, exc)
            return default

    def add(self, what: str, exc: BaseException) -> None:
        self.items.append(f"{what}: {type(exc).__name__} {exc}")


def head(text: str | None, length: int = SNIPPET) -> str | None:
    return None if text is None else text[:length]


def tail(text: str | None, length: int = SNIPPET) -> str | None:
    return None if text is None else text[-length:]


def same_text(a: str | None, b: str | None) -> bool | None:
    """Whether two snippets of the same text agree: whitespace and control
    characters ignored, and a shared run of 60% of the shorter one is enough,
    because UIA includes list numbers and footnote marks that Word's
    Range.Text leaves out. None when either is empty."""
    left = _WHITESPACE.sub("", a or "")
    right = _WHITESPACE.sub("", b or "")
    if not left or not right:
        return None
    run = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
    return run.size >= 0.6 * min(len(left), len(right))


def is_empty_rect(rect) -> bool:
    left, top, right, bottom = rect
    return right <= left or bottom <= top


def centre(rect: list[int]) -> tuple[int, int]:
    left, top, right, bottom = rect
    return (left + right) // 2, (top + bottom) // 2


def page_at(rng) -> int:
    return int(rng.Information(WD_ACTIVE_END_PAGE_NUMBER))


def get_point(window, rng) -> list[int]:
    """Window.GetPoint's four results are [out] parameters, which late-bound
    pywin32 neither accepts nor returns; InvokeTypes with PARAMFLAG_FOUT does."""
    ole = window._oleobj_
    out = (pythoncom.VT_BYREF | pythoncom.VT_I4, pythoncom.PARAMFLAG_FOUT)
    result = ole.InvokeTypes(
        ole.GetIDsOfNames("GetPoint"),
        0,
        pythoncom.DISPATCH_METHOD,
        (pythoncom.VT_EMPTY, 0),
        (out,) * 4 + ((pythoncom.VT_DISPATCH, pythoncom.PARAMFLAG_FIN),),
        0,
        0,
        0,
        0,
        rng._oleobj_,
    )
    return list(result)


def _children(parent: int, cls: str) -> list[int]:
    found: list[int] = []
    child = win32gui.FindWindowEx(parent, 0, cls, None)
    while child:
        found.append(child)
        child = win32gui.FindWindowEx(parent, child, cls, None)
    return found


def document_hwnds(frame: int) -> list[int]:
    """The frame's visible _WwG windows, found by walking _WwF -> _WwB -> _WwG.
    More than one means a split window."""
    level = [frame]
    for cls in FRAME_CHAIN:
        level = [child for parent in level for child in _children(parent, cls)]
    return [h for h in level if win32gui.IsWindowVisible(h)]


def window_rect(hwnd: int) -> list[int]:
    return list(win32gui.GetWindowRect(hwnd))


def largest(hwnds: list[int]) -> int | None:
    def area(hwnd: int) -> int:
        left, top, right, bottom = window_rect(hwnd)
        return (right - left) * (bottom - top)

    return max(hwnds, key=area, default=None)


def window_dpi(hwnd: int) -> int | None:
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(hwnd)) or None
    except (AttributeError, OSError):
        return None


def view_state(window, errors: Errors) -> dict:
    """The view settings a layout depends on, and the document window's rect."""
    frame = errors.get("hwnd", lambda: int(window.Hwnd))
    document = largest(document_hwnds(frame)) if frame else None
    view_type = errors.get("view type", lambda: int(window.View.Type))
    return {
        "hwnd": frame,
        "document_hwnd": document,
        "document_rect": window_rect(document) if document else None,
        "dpi": window_dpi(frame) if frame else None,
        "zoom": errors.get("zoom", lambda: int(window.View.Zoom.Percentage)),
        "view": VIEW_TYPES.get(view_type, view_type),
        "vertical_percent_scrolled": errors.get(
            "scroll", lambda: int(window.VerticalPercentScrolled)
        ),
    }
