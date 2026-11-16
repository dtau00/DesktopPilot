"""Clipboard read/write.

An Electron or custom-drawn app often exposes no UIA value for its text, and
no object model (like Word COM) to read it from; select-all plus copy is the only
reliable way to get the content out, so it is a first-class command here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from .native import win32clipboard, win32con
from .util import wait_until


def _with_clipboard[T](fn) -> T:
    """The clipboard is a shared global another process can hold open, so
    opening it is retried rather than failing the whole command."""

    def attempt():
        win32clipboard.OpenClipboard()
        try:
            return (fn(),)
        finally:
            win32clipboard.CloseClipboard()

    return wait_until(attempt, timeout=5.0, interval=0.1, what="the clipboard")[0]


def read() -> str:
    def get():
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return ""
        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""

    return _with_clipboard(get)


def write(text: str) -> int:
    def put():
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return len(text)

    return _with_clipboard(put)


def clear() -> bool:
    _with_clipboard(lambda: win32clipboard.EmptyClipboard() or True)
    return True


@contextlib.contextmanager
def preserved() -> Iterator[dict]:
    """Puts the clipboard's text back on exit, failure included. Other
    formats (an image, rich text) cannot be: the yielded dict's `restored`
    is "text", or False when the clipboard held no text or the restore
    failed - never a claim the restore cannot back."""
    saved = read()
    not_text = not saved and win32clipboard.CountClipboardFormats() > 0
    outcome: dict = {"restored": False}
    try:
        yield outcome
    finally:
        try:
            if saved:
                write(saved)
            else:
                clear()
            outcome["restored"] = False if not_text else "text"
        except Exception:  # noqa: BLE001 - must not mask the body's own error
            outcome["restored"] = False
