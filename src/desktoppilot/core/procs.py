"""Processes by image name: the one place that runs tasklist and taskkill.

On Linux the same rows and kills come from /proc (`desktoppilot.linux.procs`).
"""

from __future__ import annotations

import csv
import ctypes
import subprocess
from collections.abc import Iterable

from ..linux import procs as linux_procs
from .native import WINDOWS


def tasklist(image: str) -> list[list[str]]:
    """The `tasklist` CSV rows of one image: image name, pid, session name,
    session number, memory usage. Empty when none runs."""
    if not WINDOWS:
        return linux_procs.tasklist(image)
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [
        row
        for row in csv.reader(out.splitlines())
        if len(row) >= 2 and row[0].casefold() == image.casefold()
    ]


def pid_of(row: list[str]) -> int:
    return int(row[1])


def working_set(row: list[str]) -> int:
    """Bytes. "Mem Usage" reads "1,234,567 K" with the locale's thousands
    separator ("1.234.567 K", "1 234 567 K"), so only its digits count."""
    digits = "".join(ch for ch in row[4] if ch.isdecimal()) if len(row) > 4 else ""
    return int(digits) * 1024 if digits else 0


def taskkill(pids: Iterable[int]) -> None:
    """Force-kill each pid with its child tree. Never raises: the caller waits
    for the processes to go and reports what did not."""
    if not WINDOWS:
        return linux_procs.kill(pids)
    args = [arg for pid in pids for arg in ("/PID", str(pid))]
    if args:
        subprocess.run(["taskkill", "/F", "/T", *args], capture_output=True, text=True, check=False)


def pids_of_image(image: str) -> list[int]:
    """Every process of one image, for a caller with no profile for it."""
    return [pid_of(row) for row in tasklist(image)]


def images_by_pid() -> dict[int, str]:
    """The image name of every running process."""
    if not WINDOWS:
        return linux_procs.images_by_pid()
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=False
    ).stdout
    return {int(row[1]): row[0] for row in csv.reader(out.splitlines()) if len(row) >= 2}


def taskkill_images(images: Iterable[str]) -> None:
    """Force-kill every process of each image. Never raises, as taskkill."""
    if not WINDOWS:
        return linux_procs.kill_images(images)
    args = [arg for image in images for arg in ("/IM", image)]
    if args:
        subprocess.run(["taskkill", "/F", *args], capture_output=True, text=True, check=False)


_QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_COMMAND_LINE_CLASS = 60  # ProcessCommandLineInformation (Windows 8.1+)


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


def command_line(pid: int) -> str | None:
    """The process's command line, or None when it is gone or not readable.
    Windows asks the kernel (NtQueryInformationProcess, which needs only
    limited query access, so another user's process works too); Linux reads
    /proc/PID/cmdline."""
    if not WINDOWS:
        return linux_procs.command_line(pid)
    kernel32, ntdll = ctypes.WinDLL("kernel32"), ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]  # fmt: skip
    handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(handle, _COMMAND_LINE_CLASS, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(
            handle, _COMMAND_LINE_CLASS, buffer, size, ctypes.byref(size)
        )
        if status != 0:
            return None
        text = _UnicodeString.from_buffer(buffer)
        return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
    finally:
        kernel32.CloseHandle(handle)
