"""The one place the core imports the platform: UI Automation and Win32.

The core is written against the `uiautomation` Control duck type and a subset
of win32gui, win32process, win32api, win32con and win32clipboard. On Windows
those are the real modules, so behaviour (and every test that patches
`apps.win32gui.X`) is what it always was. On Linux they are the modules of
`desktoppilot.linux`, which implement the same names over AT-SPI and X11.

What has no Win32 shape (processes, DPI, crash records, recording) branches
on WINDOWS at function level instead, with the Linux code in
`desktoppilot.linux`.
"""

from __future__ import annotations

import sys

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
# The name profiles use in `platforms` and in their override tables.
PLATFORM = "windows" if WINDOWS else "linux" if LINUX else sys.platform

if WINDOWS:
    import uiautomation as auto
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
elif LINUX:
    from desktoppilot.linux import (
        auto,
        win32api,
        win32clipboard,
        win32con,
        win32gui,
        win32process,
    )
else:
    raise ImportError(
        f"desktoppilot drives Windows and Linux (X11) desktops; {sys.platform!r} has neither"
    )

__all__ = [
    "LINUX",
    "PLATFORM",
    "WINDOWS",
    "auto",
    "win32api",
    "win32clipboard",
    "win32con",
    "win32gui",
    "win32process",
]
