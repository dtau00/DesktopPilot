"""Recording on X11: the Linux side of `core/recording.py`.

ffmpeg's x11grab copies the screen from the X server named by $DISPLAY: a
box of the root window, or (`--follow`) one window by its id, which it
tracks as it moves. The microphone is a PulseAudio (or PipeWire-Pulse)
source. The terminal to minimize is a terminal emulator's window that runs
this process; VS Code's integrated terminal is never one of them.
"""

from __future__ import annotations

import os

from . import procs, x11

INSTALL_HINT = "install it with: sudo apt install ffmpeg"
MIC_HINT = "pactl list short sources"
# WM_CLASS (instance or class, any case) of the terminal emulators whose
# window is minimized while recording. A terminal inside another app (VS
# Code, an IDE) is part of that app's window and stays.
TERMINAL_CLASSES = frozenset(
    {
        "xfce4-terminal",
        "gnome-terminal-server",
        "gnome-terminal",
        "konsole",
        "xterm",
        "uxterm",
        "kitty",
        "alacritty",
        "tilix",
        "terminator",
        "wezterm",
        "org.wezfurlong.wezterm",
        "foot",
    }
)
# Window types of the desktop itself, not of an app in the way.
SHELL_TYPES = frozenset({"desktop", "dock", "notification", "tooltip"})


def display() -> str:
    return os.environ.get("DISPLAY") or ":0"


def input_args(target: dict, fps: int, screen: str) -> list[str]:
    """x11grab's input. A box (the window's frame at start, a region, the
    screen) is `-video_size WxH -i SCREEN+X,Y`; a followed window is its
    X window id, whose size at start fixes the frame size."""
    args = ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "1"]
    if target.get("follow"):
        return [*args, "-window_id", str(target["hwnd"]), "-i", screen]
    x, y, w, h = target["rect"]
    return [*args, "-video_size", f"{w}x{h}", "-i", f"{screen}+{x},{y}"]


def mic_args(mic: str) -> list[str]:
    return ["-f", "pulse", "-i", mic]


def is_shell(hwnd: int) -> bool:
    """A panel, the desktop, a notification or a tooltip: never in the way."""
    try:
        return bool(SHELL_TYPES & set(x11.window_type(hwnd)))
    except Exception:  # noqa: BLE001 - a window gone since the listing is in nobody's way
        return True


def terminal_windows(exclude: int) -> list[int]:
    """The terminal emulator this process runs in: the windows of an
    ancestor whose WM_CLASS is a known terminal. When one of them is active,
    only that one: the others are not in front. Only mapped, unminimized
    windows with a size that are not the target."""
    chain = procs.ancestors()
    found = []
    for hwnd in x11.client_windows():
        try:
            if hwnd == exclude or x11.pid(hwnd) not in chain:
                continue
            if not {part.casefold() for part in x11.wm_class(hwnd)} & TERMINAL_CLASSES:
                continue
            if not x11.is_mapped(hwnd) or "hidden" in x11.state(hwnd):
                continue
            left, top, right, bottom = x11.frame_rect(hwnd)
        except Exception:  # noqa: BLE001 - closed while we looked at it
            continue
        if right > left and bottom > top:
            found.append(hwnd)
    active = x11.active_window()
    return [active] if active in found else found
