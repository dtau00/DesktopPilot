"""The win32api functions the core calls: process handles over /proc
(`procs.py`), screen metrics and the pointer over X11 (`x11.py`)."""

from __future__ import annotations

from dataclasses import dataclass

from . import procs, win32con, x11


@dataclass(frozen=True)
class ProcessHandle:
    """What OpenProcess hands back: the pid and its creation time, so a pid
    reused since the open reads as exited. Holds no OS resource."""

    pid: int
    created: float | None

    def Close(self) -> None:
        pass

    def __int__(self) -> int:
        return self.pid


def OpenProcess(access: int, inherit: bool, pid: int) -> ProcessHandle:
    """Raises ProcessLookupError when the pid is not running, as
    OpenProcess raises for an exited process."""
    if not procs.alive(pid):
        raise ProcessLookupError(f"no process {pid}")
    return ProcessHandle(pid, procs.created(pid))


def CloseHandle(handle: ProcessHandle) -> None:
    handle.Close()


def GetSystemMetrics(index: int) -> int:
    """The SM_*SCREEN and SM_*VIRTUALSCREEN metrics, from the root window:
    X11 has one screen spanning every monitor."""
    left, top, width, height = x11.screen_rect()
    metrics = {
        win32con.SM_XVIRTUALSCREEN: left,
        win32con.SM_YVIRTUALSCREEN: top,
        win32con.SM_CXVIRTUALSCREEN: width,
        win32con.SM_CYVIRTUALSCREEN: height,
        win32con.SM_CXSCREEN: width,
        win32con.SM_CYSCREEN: height,
    }
    if index not in metrics:
        raise NotImplementedError(f"linux: GetSystemMetrics({index})")
    return metrics[index]


def GetCursorPos() -> tuple[int, int]:
    """The pointer's screen point, as win32api's (x, y) tuple."""
    return x11.pointer()
