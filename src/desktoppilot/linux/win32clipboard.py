"""The win32clipboard functions the core calls, over xclip and the
CLIPBOARD selection (what Ctrl+C and Ctrl+V use; not PRIMARY).

X11 has no open/close protocol: the selection belongs to whichever client
last claimed it, and xclip serves a written text from a background process
until another client claims it. Open and Close only keep the core's
open, act, close pattern; text is UTF-8 (`UTF8_STRING`).
"""

from __future__ import annotations

import subprocess
import threading

from ..core.errors import PilotError
from ..core.util import wait_until
from . import x11
from .win32con import CF_TEXT, CF_UNICODETEXT

XCLIP = ("xclip", "-selection", "clipboard")
TIMEOUT = 3.0
TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "STRING", "TEXT", "text/plain")
META_TARGETS = frozenset(
    {"TARGETS", "MULTIPLE", "TIMESTAMP", "SAVE_TARGETS", "DELETE", "INSERT_SELECTION"}
)


def _xclip(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    """xclip on the CLIPBOARD selection. A write forks a server that keeps
    the selection: its output goes nowhere and it runs in its own session,
    so neither this call nor the CLI's exit waits for it."""
    try:
        if data is None:
            return subprocess.run(
                [*XCLIP, *args], capture_output=True, timeout=TIMEOUT, check=False
            )
        return subprocess.run(
            [*XCLIP, *args],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            timeout=TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise PilotError("xclip is not installed (sudo apt install xclip)") from None


def _read(target: str) -> bytes | None:
    """The owner's data for `target`; None when it offers none."""
    done = _xclip("-o", "-t", target)
    return done.stdout if done.returncode == 0 else None


def _targets() -> list[str]:
    """What the owner offers; [] when nobody owns the selection."""
    if not x11.selection_owner("CLIPBOARD"):
        return []
    return (_read("TARGETS") or b"").decode("utf-8", "replace").split()


def _is_text(fmt: int) -> bool:
    return fmt in (CF_UNICODETEXT, CF_TEXT)


def OpenClipboard(hwnd=None) -> None:
    """No lock to take on X11."""


def CloseClipboard() -> None:
    """No lock to give back on X11."""


def EmptyClipboard() -> None:
    """Own the selection with empty text (xclip of empty input). Leaving it
    without an owner would let a clipboard manager put the old text back."""
    SetClipboardData(CF_UNICODETEXT, "")


def GetClipboardData(fmt: int = CF_UNICODETEXT) -> str:
    """The selection as text (`xclip -o -t UTF8_STRING`, else another text
    target the owner offers)."""
    if not _is_text(fmt):
        raise PilotError(f"clipboard format {fmt} has no X11 emulation; only text")
    offered = _targets()
    for target in TEXT_TARGETS:
        if target in offered:
            data = _read(target)
            if data is not None:
                return data.decode("utf-8" if target != "STRING" else "latin-1", "replace")
    raise PilotError("the clipboard holds no text")


def SetClipboardData(fmt: int, data: str) -> None:
    """Own the selection with `data` (`xclip -i`), and wait until xclip's
    server owns it, so a read right after sees the new text."""
    if not _is_text(fmt):
        raise PilotError(f"clipboard format {fmt} has no X11 emulation; only text")
    before = x11.selection_owner("CLIPBOARD")
    done = _xclip("-i", data=data.encode("utf-8"))
    if done.returncode != 0:
        raise PilotError(f"xclip could not take the clipboard (exit {done.returncode})")
    wait_until(
        lambda: x11.selection_owner("CLIPBOARD") not in (0, before),
        timeout=TIMEOUT,
        interval=0.01,
        what="xclip to own the clipboard",
    )


def IsClipboardFormatAvailable(fmt: int) -> bool:
    """CF_UNICODETEXT / CF_TEXT: the owner offers a text target (`-t TARGETS`)."""
    return _is_text(fmt) and any(t in TEXT_TARGETS for t in _targets())


def CountClipboardFormats() -> int:
    """How many data targets the owner offers; 0 when nobody owns the
    selection, or it holds only the empty text EmptyClipboard left."""
    formats = [t for t in _targets() if t not in META_TARGETS]
    if formats == ["UTF8_STRING"] and _read("UTF8_STRING") == b"":
        return 0
    return len(formats)


class _OwnerWatch:
    """Counts selection-owner changes (XFixes) on a connection of its own,
    so its events never mix with the shared display's."""

    def __init__(self) -> None:
        from Xlib import display
        from Xlib.ext import xfixes

        self.display = display.Display()
        self.display.xfixes_query_version()
        self.display.xfixes_select_selection_input(
            self.display.screen().root,
            self.display.intern_atom("CLIPBOARD"),
            xfixes.XFixesSetSelectionOwnerNotifyMask
            | xfixes.XFixesSelectionWindowDestroyNotifyMask
            | xfixes.XFixesSelectionClientCloseNotifyMask,
        )
        self.display.sync()
        self.count = 1

    def number(self) -> int:
        while self.display.pending_events():
            self.display.next_event()
            self.count += 1
        return self.count


_watch: _OwnerWatch | None = None
_watch_lock = threading.Lock()


def GetClipboardSequenceNumber() -> int:
    """A number that changes whenever the selection changes hands, a copy
    in the app that already owns it included: the count of XFixes
    selection-owner events since this process first asked."""
    global _watch
    with _watch_lock:
        if _watch is None:
            _watch = _OwnerWatch()
        return _watch.number()
