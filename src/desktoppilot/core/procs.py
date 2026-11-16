"""Processes by image name: the one place that runs tasklist and taskkill.

On Linux the same rows and kills come from /proc (`desktoppilot.linux.procs`).
"""

from __future__ import annotations

import csv
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
