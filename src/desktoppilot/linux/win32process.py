"""The win32process functions the core calls: a window's owner over X11,
process state over /proc (`procs.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

from . import procs, x11
from .win32api import ProcessHandle

STILL_ACTIVE = 259


def GetWindowThreadProcessId(hwnd: int) -> tuple[int, int]:
    """(thread id, pid) of the window's owner. X11 knows no thread: 0. The
    pid is `x11.pid` - `_NET_WM_PID`, else the XRes client pid."""
    return 0, x11.pid(hwnd)


def GetExitCodeProcess(handle: ProcessHandle) -> int:
    """STILL_ACTIVE while the process (the same one, not a reused pid)
    runs. Another process's exit code cannot be read on Linux: 0 after."""
    return STILL_ACTIVE if procs.alive(handle.pid, handle.created) else 0


def GetProcessTimes(handle: ProcessHandle) -> dict:
    """Only CreationTime, the one key the core reads, as an aware datetime."""
    if handle.created is None:
        raise ProcessLookupError(f"no process {handle.pid}")
    return {"CreationTime": datetime.fromtimestamp(handle.created, UTC)}
