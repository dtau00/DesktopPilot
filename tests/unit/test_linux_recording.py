"""Recording on X11: the x11grab and pulse command line, the detached
supervisor, a forced stop, which terminal is minimized - never VS Code - and
what counts as covering. X11 is faked; no ffmpeg runs."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from desktoppilot.core import paths, recording
from desktoppilot.core.errors import PilotError
from desktoppilot.core.registry import AppContext, Registry
from desktoppilot.linux import recording as linux_recording

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="x11grab")

WINDOW = {"kind": "window", "title": "Mousepad", "hwnd": 42, "rect": [156, 156, 1000, 800]}
FOLLOW = {"kind": "window", "follow": True, "title": "Mousepad", "hwnd": 0x3A00007}
SCREEN = {"kind": "screen", "rect": [0, 0, 2560, 1600]}
REGION = {"kind": "region", "rect": [10, 20, 640, 480]}


@pytest.fixture(autouse=True)
def display(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY", ":10.0")
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")


def argv(target, mic=None):
    return recording.ffmpeg_argv("ffmpeg", target, "out.mp4", fps=30, mic=mic)


def after(args, flag):
    return args[args.index(flag) + 1]


@pytest.mark.parametrize(
    ("target", "size", "source"),
    [
        (WINDOW, "1000x800", ":10.0+156,156"),
        (SCREEN, "2560x1600", ":10.0+0,0"),
        (REGION, "640x480", ":10.0+10,20"),
    ],
)
def test_a_box_is_its_size_and_an_offset_on_the_display(target, size, source):
    args = argv(target)
    assert after(args, "-f") == "x11grab" and after(args, "-draw_mouse") == "1"
    assert after(args, "-framerate") == "30" and after(args, "-video_size") == size
    assert after(args, "-i") == source and args.index("-video_size") < args.index("-i")
    assert after(args, "-c:v") == "libx264" and args[-2:] == ["-y", "out.mp4"]
    assert "pulse" not in args and "-c:a" not in args


def test_a_followed_window_is_its_x_window_id():
    args = argv(FOLLOW)
    assert after(args, "-window_id") == str(0x3A00007) and after(args, "-i") == ":10.0"
    assert "-video_size" not in args


def test_a_mic_is_a_pulse_source():
    args = argv(SCREEN, mic="alsa_input.usb-mic.mono")
    assert [args[i + 1] for i, a in enumerate(args) if a == "-i"][1] == "alsa_input.usb-mic.mono"
    assert args[args.index("pulse") - 1] == "-f" and after(args, "-c:a") == "aac"


def test_no_ffmpeg_says_apt(monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", "/nowhere")
    with pytest.raises(PilotError, match="sudo apt install ffmpeg"):
        recording.find_ffmpeg()


# ------------------------------------------------------------- supervisor


class FakeSupervisor:
    kwargs: dict = {}

    def __init__(self, argv, **kwargs):
        FakeSupervisor.kwargs = kwargs
        path = Path(argv[-1])
        state = recording.read_state(path)
        Path(state["path"]).write_bytes(b"mp4")
        recording.write_state(path, state | {"pid": 4242, "started": time.time()})

    def poll(self):
        return None


def test_the_supervisor_runs_in_a_session_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(recording, "find_ffmpeg", lambda: Path("ffmpeg"))
    monkeypatch.setattr(recording, "console_windows", lambda exclude: [])
    monkeypatch.setattr(recording.ui, "virtual_screen_rect", lambda: (0, 0, 2560, 1600))
    monkeypatch.setattr(recording.subprocess, "Popen", FakeSupervisor)
    ctx = AppContext(name="mousepad", profile=None, registry=Registry())

    result = recording.start(ctx, out=str(tmp_path / "s.mp4"), full_screen=True)

    assert result["pid"] == 4242 and result["target"] == SCREEN
    assert FakeSupervisor.kwargs["start_new_session"] is True
    assert "creationflags" not in FakeSupervisor.kwargs
    state = recording.read_state(recording.state_file("mousepad"))
    assert after(state["argv"], "-f") == "x11grab"


def test_force_is_sigkill_with_no_grace(tmp_path, monkeypatch):
    killed = []
    out = tmp_path / "torn.mp4"
    out.write_bytes(b"x")
    recording.write_state(
        recording.state_file("mousepad"), {"pid": 5, "supervisor_pid": 6, "path": str(out)}
    )
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: True)
    monkeypatch.setattr(
        recording.linux_procs, "kill", lambda pids, grace: killed.append((pids, grace))
    )
    monkeypatch.setattr(recording, "probe", lambda path: recording.parse_probe({}))

    result = recording.stop(
        AppContext(name="mousepad", profile=None, registry=Registry()), force=True
    )

    assert killed == [([6], 0), ([5], 0)] and "may not play" in result["warning"]


# --------------------------------------------------------------- terminals

ME = os.getpid()
TERMINAL, OTHER_TERMINAL, CODE, TARGET, MINIMIZED = 1, 2, 3, 4, 5


@pytest.fixture
def desktop(monkeypatch):
    """Windows: pid, WM_CLASS, state. The terminal and VS Code both run us."""
    windows = {
        TERMINAL: (100, ("xfce4-terminal", "Xfce4-terminal"), []),
        OTHER_TERMINAL: (100, ("xfce4-terminal", "Xfce4-terminal"), []),
        CODE: (200, ("code", "code_code"), []),
        TARGET: (300, ("mousepad", "Mousepad"), []),
        MINIMIZED: (100, ("xfce4-terminal", "Xfce4-terminal"), ["hidden"]),
    }
    x = linux_recording.x11
    monkeypatch.setattr(linux_recording.procs, "ancestors", lambda: {ME, 100, 200})
    monkeypatch.setattr(x, "client_windows", lambda stacking=True: list(windows))
    monkeypatch.setattr(x, "pid", lambda w: windows[w][0])
    monkeypatch.setattr(x, "wm_class", lambda w: windows[w][1])
    monkeypatch.setattr(x, "state", lambda w: windows[w][2])
    monkeypatch.setattr(x, "is_mapped", lambda w: True)
    monkeypatch.setattr(x, "frame_rect", lambda w: (0, 0, 800, 600))
    active = {"window": TERMINAL}
    monkeypatch.setattr(x, "active_window", lambda: active["window"])
    return active


def test_the_active_terminal_running_us_is_minimized_and_never_vs_code(desktop):
    assert recording.console_windows(exclude=TARGET) == [TERMINAL]


def test_with_none_active_every_window_of_that_terminal(desktop):
    desktop["window"] = CODE
    assert recording.console_windows(exclude=TARGET) == [TERMINAL, OTHER_TERMINAL]


def test_in_vs_codes_terminal_nothing_is_minimized(desktop, monkeypatch):
    monkeypatch.setattr(linux_recording.procs, "ancestors", lambda: {ME, 200})
    desktop["window"] = CODE
    assert recording.console_windows(exclude=0) == []


# ---------------------------------------------------------------- covering


@pytest.mark.parametrize(
    ("types", "shell"),
    [
        (["dock"], True),
        (["desktop"], True),
        (["notification"], True),
        (["normal"], False),
        ([], False),
    ],
)
def test_panels_and_the_desktop_never_cover(monkeypatch, types, shell):
    monkeypatch.setattr(linux_recording.x11, "window_type", lambda w: types)
    assert recording._shell(7) is shell
    assert recording._cloaked(7) is False


def test_the_frame_is_the_x_frame_as_a_box(monkeypatch):
    monkeypatch.setattr(recording.x11, "frame_rect", lambda w: (100, 50, 900, 650))
    assert recording._frame(7) == (100, 50, 800, 600)
