"""Screen recording: the ffmpeg command line, the state file the processes
share, finding ffmpeg, and `start` / `stop` / `status` against a stand-in
supervisor. No ffmpeg runs here except in the one `ui` test, which records
a real Notepad (pytest tests/unit/test_recording.py -m ui).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from desktoppilot.cli import app
from desktoppilot.core import apps, paths, recording
from desktoppilot.core.errors import Covered, PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

from .compat import PLAIN_APP

runner = CliRunner()

WINDOW = {
    "kind": "window",
    "title": "Untitled - Notepad",
    "hwnd": 42,
    "rect": [156, 156, 1000, 800],
    "clipped": False,
}
FOLLOW = {"kind": "window", "follow": True, "title": "Untitled - Notepad", "hwnd": 42}
SCREEN = {"kind": "screen", "rect": [0, 0, 2560, 1600]}
REGION = {"kind": "region", "rect": [-100, 20, 640, 480]}


REAL_CONSOLE_WINDOWS = recording.console_windows


@pytest.fixture(autouse=True)
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    return tmp_path / "sessions"


@pytest.fixture(autouse=True)
def consoles(monkeypatch):
    """The terminals `start` would minimize: none, unless a test says so -
    never the real one running the suite."""
    hwnds: list[int] = []
    monkeypatch.setattr(recording, "console_windows", lambda exclude: list(hwnds))
    return hwnds


@pytest.fixture
def videos(tmp_path, monkeypatch):
    folder = tmp_path / "Videos"
    folder.mkdir()
    monkeypatch.setattr(recording, "videos_dir", lambda: folder)
    return folder


class FakeWindows:
    """Top-level windows for the win32 calls the console filter makes."""

    def __init__(self):
        self.console = 0
        self.owner = 0
        self.foreground = 0
        self.windows: dict[int, dict] = {}
        self.parents: dict[int, int] = {}
        self.names: dict[int, str] = {}
        self.shown: list[tuple[int, int]] = []

    def add(
        self,
        hwnd,
        cls="ConsoleWindowClass",
        visible=True,
        iconic=False,
        title="T",
        rect=None,
        pid=0,
    ):
        self.windows[hwnd] = {
            "pid": pid,
            "cls": cls,
            "visible": visible,
            "iconic": iconic,
            "title": title,
            "rect": rect or (100, 100, 900, 700),
        }

    def chain(self, *names):
        """Our ancestors, parent first: pid 101, 102, ... named `names`."""
        child = recording.os.getpid()
        for pid, name in enumerate(names, 101):
            self.parents[child], self.names[pid] = pid, name
            child = pid

    def enum(self, callback, extra):
        for hwnd in list(self.windows):
            if callback(hwnd, extra) is False:
                return

    def show(self, hwnd, how):
        self.shown.append((hwnd, how))
        self.windows[hwnd]["iconic"] = how == recording.win32con.SW_MINIMIZE


@pytest.fixture
def fake_windows(monkeypatch):
    fake = FakeWindows()
    gui = recording.win32gui
    monkeypatch.setattr(recording.win32console, "GetConsoleWindow", lambda: fake.console)
    monkeypatch.setattr(gui, "GetAncestor", lambda hwnd, flag: fake.owner)
    monkeypatch.setattr(gui, "GetForegroundWindow", lambda: fake.foreground)
    monkeypatch.setattr(gui, "GetClassName", lambda hwnd: fake.windows[hwnd]["cls"])
    monkeypatch.setattr(gui, "IsWindowVisible", lambda hwnd: fake.windows[hwnd]["visible"])
    monkeypatch.setattr(gui, "IsIconic", lambda hwnd: fake.windows[hwnd]["iconic"])
    monkeypatch.setattr(gui, "IsWindow", lambda hwnd: hwnd in fake.windows)
    monkeypatch.setattr(gui, "GetWindowText", lambda hwnd: fake.windows[hwnd]["title"])
    monkeypatch.setattr(gui, "GetWindowRect", lambda hwnd: fake.windows[hwnd]["rect"])
    monkeypatch.setattr(gui, "ShowWindow", fake.show)
    monkeypatch.setattr(gui, "EnumWindows", fake.enum)
    monkeypatch.setattr(
        recording.win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (0, fake.windows[hwnd]["pid"]),
    )
    monkeypatch.setattr(recording.apps, "_parents", lambda: fake.parents)
    monkeypatch.setattr(recording, "_process_name", fake.names.get)
    return fake


@pytest.fixture
def ctx():
    """An app with a window of its own; `pilot desktop` (no profile) records
    the screen by default."""
    profile = Profile(name="notepad", exe=["notepad.exe"])
    return AppContext(name="notepad", profile=profile, registry=Registry())


def argv(target, mic=None):
    return recording.ffmpeg_argv("ffmpeg.exe", target, "out.mp4", fps=30, mic=mic)


def after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# ------------------------------------------------------------------ argv


@pytest.mark.windows("the gdigrab and dshow command line")
@pytest.mark.parametrize("target", [WINDOW, FOLLOW, SCREEN, REGION])
def test_every_target_is_gdigrab_with_the_pointer_and_a_playable_mp4(target):
    args = argv(target)
    assert args[0] == "ffmpeg.exe" and args[-2:] == ["-y", "out.mp4"]
    assert after(args, "-f") == "gdigrab"
    assert after(args, "-draw_mouse") == "1"
    assert after(args, "-framerate") == "30"
    assert after(args, "-pix_fmt") == "yuv420p"
    assert after(args, "-movflags") == "+faststart"
    assert after(args, "-c:v") == "libx264"
    assert "dshow" not in args and "-c:a" not in args


@pytest.mark.windows("the gdigrab and dshow command line")
def test_a_window_is_the_desktop_under_its_rect_by_default():
    args = argv(WINDOW)
    assert after(args, "-i") == "desktop"
    assert not any(a.startswith("title=") for a in args)
    assert (after(args, "-offset_x"), after(args, "-offset_y")) == ("156", "156")
    assert after(args, "-video_size") == "1000x800"


@pytest.mark.windows("the gdigrab and dshow command line")
def test_a_followed_window_is_matched_by_its_exact_title():
    args = argv(FOLLOW)
    assert after(args, "-i") == "title=Untitled - Notepad"
    assert "-offset_x" not in args and "-video_size" not in args


def test_a_window_inside_the_screen_is_not_clipped():
    assert recording.window_rect((156, 156, 1000, 800), (0, 0, 2560, 1600)) == (
        (156, 156, 1000, 800),
        False,
    )


def test_a_window_past_the_screen_edge_is_clipped_to_it():
    rect, clipped = recording.window_rect((-50, 1400, 1000, 800), (0, 0, 2560, 1600))
    assert rect == (0, 1400, 950, 200) and clipped


def test_a_window_off_every_monitor_is_refused():
    with pytest.raises(PilotError, match="off every monitor"):
        recording.window_rect((3000, 0, 400, 300), (0, 0, 2560, 1600))


@pytest.mark.windows("the gdigrab and dshow command line")
def test_the_screen_is_the_whole_desktop():
    args = argv(SCREEN)
    assert after(args, "-i") == "desktop"
    assert "-offset_x" not in args and "-video_size" not in args


@pytest.mark.windows("the gdigrab and dshow command line")
def test_a_region_is_an_offset_and_a_size_on_the_desktop():
    args = argv(REGION)
    assert after(args, "-i") == "desktop"
    assert (after(args, "-offset_x"), after(args, "-offset_y")) == ("-100", "20")
    assert after(args, "-video_size") == "640x480"
    assert args.index("-video_size") < args.index("-i")


@pytest.mark.windows("the gdigrab and dshow command line")
def test_a_mic_adds_a_dshow_input_and_aac():
    args = argv(WINDOW, mic="Microphone (Realtek Audio)")
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert inputs == ["desktop", "audio=Microphone (Realtek Audio)"]
    assert args[args.index("dshow") - 1] == "-f"
    assert after(args, "-c:a") == "aac" and after(args, "-b:a") == "128k"


# ----------------------------------------------------------------- state


def test_the_state_file_round_trips_next_to_the_session_file(session_dir):
    path = recording.state_file("notepad")
    state = {"app": "notepad", "pid": 7, "path": "C:\\x.mp4", "target": WINDOW, "mic": None}
    recording.write_state(path, state)
    assert path.parent == session_dir
    assert recording.read_state(path) == state
    assert not path.with_name(path.name + ".tmp").exists()
    assert recording.stop_file(path).name == "notepad.recording.json.stop"


def test_a_missing_or_torn_state_file_reads_as_none(session_dir):
    path = recording.state_file("notepad")
    assert recording.read_state(path) is None
    session_dir.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert recording.read_state(path) is None


# ---------------------------------------------------------------- ffmpeg


def test_ffmpeg_on_path_wins(monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda name: f"C:\\bin\\{name}.exe")
    assert recording.find_ffmpeg() == Path("C:\\bin\\ffmpeg.exe")


@pytest.mark.windows("ffmpeg from winget")
def test_ffmpeg_is_found_where_winget_put_it(tmp_path, monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    package = (
        tmp_path / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    )
    exe = package / "ffmpeg-9.0.2-full_build/bin/ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (exe.parent / "ffprobe.exe").write_bytes(b"")

    assert recording.find_ffmpeg() == exe
    assert recording.find_ffprobe() == exe.parent / "ffprobe.exe"


@pytest.mark.windows("ffmpeg from winget")
def test_no_ffmpeg_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(PilotError, match="winget install Gyan.FFmpeg"):
        recording.find_ffmpeg()


# ----------------------------------------------------------- start / stop


class FakeSupervisor:
    """Stands in for the detached supervisor: does at once what the real one
    does when ffmpeg starts - or dies, when `fail` holds a log line."""

    fail: str | None = None

    flags = 0

    def __init__(self, argv, **kwargs):
        FakeSupervisor.flags = kwargs["creationflags"]
        path = Path(argv[-1])
        state = recording.read_state(path)
        if self.fail:
            Path(state["log"]).write_text(f"noise\n{self.fail}\n", encoding="utf-8")
            recording.write_state(path, state | {"exit_code": 1})
        else:
            Path(state["path"]).write_bytes(b"mp4")
            recording.write_state(path, state | {"pid": 4242, "started": time.time()})
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def fake_start(monkeypatch):
    monkeypatch.setattr(recording, "find_ffmpeg", lambda: Path("ffmpeg.exe"))
    monkeypatch.setattr(recording.ui, "virtual_screen_rect", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(recording.subprocess, "Popen", FakeSupervisor)
    FakeSupervisor.fail = None
    return FakeSupervisor


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_start_returns_once_ffmpeg_writes_the_file(ctx, tmp_path, fake_start, session_dir):
    out = tmp_path / "demo.mp4"

    result = recording.start(ctx, out=str(out), full_screen=True)

    assert result["path"] == str(out) and result["pid"] == 4242
    assert result["target"] == {"kind": "screen", "rect": [0, 0, 1920, 1080]}
    assert result["mic"] is None and "warning" not in result
    state = recording.read_state(recording.state_file("notepad"))
    assert after(state["argv"], "-i") == "desktop"


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_the_supervisor_gets_a_console_with_no_window(ctx, tmp_path, fake_start):
    """DETACHED_PROCESS would leave the interpreter the venv launcher starts
    without a console, and Windows would open one on top of the recording."""
    recording.start(ctx, out=str(tmp_path / "w.mp4"), full_screen=True)
    assert fake_start.flags & subprocess.CREATE_NO_WINDOW
    assert not fake_start.flags & subprocess.DETACHED_PROCESS


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_a_dead_ffmpeg_fails_start_with_its_log(ctx, tmp_path, fake_start):
    fake_start.fail = "Could not find audio only device with name [nosuch]"

    with pytest.raises(PilotError, match=r"(?s)exited \(1\).*nosuch"):
        recording.start(ctx, out=str(tmp_path / "x.mp4"), full_screen=True, mic="nosuch")
    assert not recording.state_file("notepad").exists()


def test_the_desktop_records_the_screen_by_default(tmp_path, fake_start, monkeypatch):
    """`pilot desktop` has no window of its own: no flag means the screen."""
    desktop = AppContext(name="desktop", profile=None, registry=Registry())
    seen = {}

    def start(ctx, target_file, target, ffmpeg, **kwargs):
        seen["target"] = target
        return {"path": str(target_file)}

    monkeypatch.setattr(recording, "_start", start)
    monkeypatch.setattr(recording, "console_windows", lambda exclude: [])
    recording.start(desktop, out=str(tmp_path / "d.mp4"))
    assert seen["target"] == {"kind": "screen", "rect": [0, 0, 1920, 1080]}


def test_a_second_start_is_refused_while_one_runs(ctx, tmp_path, monkeypatch):
    recording.write_state(
        recording.state_file("notepad"),
        {"pid": 4242, "started": time.time(), "path": "C:\\running.mp4"},
    )
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: True)

    with pytest.raises(PilotError, match=r"already recording to C:\\running\.mp4"):
        recording.start(ctx, out=str(tmp_path / "b.mp4"), full_screen=True)


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_an_existing_file_needs_overwrite(ctx, tmp_path, fake_start):
    out = tmp_path / "taken.mp4"
    out.write_bytes(b"old")
    with pytest.raises(PilotError, match="--overwrite"):
        recording.start(ctx, out=str(out), full_screen=True)
    assert recording.start(ctx, out=str(out), full_screen=True, overwrite=True)["pid"]


def test_the_null_app_has_no_window_to_follow(tmp_path, fake_start):
    desktop = AppContext(name="desktop", profile=None, registry=Registry())
    with pytest.raises(PilotError, match="--full-screen or --region"):
        recording.start(desktop, out=str(tmp_path / "x.mp4"), follow=True)


def test_stop_reports_what_the_finished_file_holds(ctx, tmp_path, monkeypatch):
    out = tmp_path / "done.mp4"
    out.write_bytes(b"x" * 10)
    started = time.mktime((2026, 10, 5, 14, 30, 12, 0, 0, -1))
    recording.write_state(
        recording.state_file("notepad"),
        {
            "pid": 1,
            "path": str(out),
            "exit_code": 0,
            "target": WINDOW,
            "started": started,
            "stopped": started + 5,
            "mic": "Mic (USB)",
            "log": "C:\\x.log",
        },
    )
    probed = recording.parse_probe(json.loads(PROBE_WITH_AUDIO))
    monkeypatch.setattr(recording, "probe", lambda path: json.loads(json.dumps(probed)))

    result = recording.stop(ctx)

    assert result == {
        "path": str(out),
        "duration_s": 5.23,
        "size_bytes": 16675,
        "size_mb": 0.02,
        "video": probed["video"],
        "audio": probed["audio"] | {"device": "Mic (USB)"},
        "target": WINDOW,
        "started": "2026-10-05T14:30:12",
        "stopped": "2026-10-05T14:30:17",
        "exit_code": 0,
        "restored_windows": [],
        "log": "C:\\x.log",
    }
    assert not recording.state_file("notepad").exists()


def test_a_silent_recording_says_audio_null(ctx, tmp_path, monkeypatch):
    out = tmp_path / "quiet.mp4"
    out.write_bytes(b"x")
    recording.write_state(
        recording.state_file("notepad"), {"pid": 1, "path": str(out), "exit_code": 0}
    )
    monkeypatch.setattr(
        recording, "probe", lambda path: recording.parse_probe(json.loads(PROBE_VIDEO_ONLY))
    )

    result = recording.stop(ctx)

    assert "audio" in result and result["audio"] is None
    assert result["video"]["fps"] == 29.97


# ------------------------------------------------------------------- probe

PROBE_WITH_AUDIO = """{
  "streams": [
    {"codec_name": "h264", "codec_type": "video", "width": 990, "height": 794,
     "r_frame_rate": "30/1", "avg_frame_rate": "30/1", "nb_frames": "157",
     "bit_rate": "25391"},
    {"codec_name": "aac", "codec_type": "audio", "sample_rate": "48000",
     "channels": 1, "r_frame_rate": "0/0", "avg_frame_rate": "0/0",
     "nb_frames": "245", "bit_rate": "128000"}
  ],
  "format": {"duration": "5.233333", "size": "16675", "bit_rate": "25490"}
}"""
PROBE_VIDEO_ONLY = """{
  "streams": [
    {"codec_name": "h264", "codec_type": "video", "width": 1920, "height": 1080,
     "r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001", "nb_frames": "90"}
  ],
  "format": {"duration": "3.003", "size": "40000", "bit_rate": "106560"}
}"""


def test_probe_reads_the_video_and_the_audio_stream():
    assert recording.parse_probe(json.loads(PROBE_WITH_AUDIO)) == {
        "duration_s": 5.23,
        "size_bytes": 16675,
        "video": {
            "codec": "h264",
            "width": 990,
            "height": 794,
            "fps": 30,
            "frames": 157,
            "bit_rate_kbps": 25,
        },
        "audio": {"codec": "aac", "sample_rate": 48000, "channels": 1},
    }


def test_probe_without_audio_and_without_a_stream_bit_rate():
    probed = recording.parse_probe(json.loads(PROBE_VIDEO_ONLY))
    assert probed["audio"] is None
    assert probed["video"]["fps"] == 29.97 and probed["video"]["frames"] == 90
    assert probed["video"]["bit_rate_kbps"] == 107


def test_probe_of_an_empty_answer():
    assert recording.parse_probe({}) == {
        "duration_s": None,
        "size_bytes": None,
        "video": None,
        "audio": None,
    }


@pytest.mark.parametrize(
    ("text", "fps"),
    [("30/1", 30), ("30000/1001", 29.97), ("25", 25), ("15/2", 7.5), ("0/0", None), (None, None)],
)
def test_frame_rate_fractions(text, fps):
    assert recording.frame_rate(text) == fps


def test_stop_without_a_recording_says_so(ctx):
    with pytest.raises(PilotError, match="no recording for notepad"):
        recording.stop(ctx)


@pytest.mark.windows("taskkill")
def test_force_kills_and_warns(ctx, tmp_path, monkeypatch):
    killed: list[list[str]] = []
    out = tmp_path / "torn.mp4"
    out.write_bytes(b"x")
    recording.write_state(
        recording.state_file("notepad"), {"pid": 5, "supervisor_pid": 6, "path": str(out)}
    )
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: True)
    monkeypatch.setattr(recording.subprocess, "run", lambda args, **kw: killed.append(args))
    monkeypatch.setattr(recording, "probe", lambda path: recording.parse_probe({}))

    result = recording.stop(ctx, force=True)

    assert [args[-1] for args in killed] == ["6", "5"]
    assert "may not play" in result["warning"]


# --------------------------------------------------------------- consoles


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_a_windows_terminal_owning_our_hidden_console_is_minimized(fake_windows):
    fake_windows.add(1, visible=False)
    fake_windows.add(2, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    fake_windows.console, fake_windows.owner = 1, 2
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [2]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_the_conpty_window_visible_at_zero_size_is_left_alone(fake_windows):
    fake_windows.add(1, cls="PseudoConsoleWindow", rect=(0, 0, 0, 0))
    fake_windows.add(2, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    fake_windows.console, fake_windows.owner = 1, 2
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [2]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_a_console_in_the_foreground_counts_and_any_other_app_does_not(fake_windows):
    fake_windows.add(5, cls="ConsoleWindowClass")
    fake_windows.foreground = 5
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [5]
    fake_windows.add(6, cls="Chrome_WidgetWin_1")
    fake_windows.foreground = 6
    assert REAL_CONSOLE_WINDOWS(exclude=0) == []


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_the_target_and_minimized_windows_are_left_alone(fake_windows):
    fake_windows.add(1)
    fake_windows.add(2, cls="CASCADIA_HOSTING_WINDOW_CLASS", iconic=True)
    fake_windows.add(3, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    fake_windows.console, fake_windows.owner, fake_windows.foreground = 1, 2, 3
    assert REAL_CONSOLE_WINDOWS(exclude=3) == [1]
    assert REAL_CONSOLE_WINDOWS(exclude=1) == [3]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_a_window_found_twice_is_minimized_once(fake_windows):
    fake_windows.add(4)
    fake_windows.console = fake_windows.owner = fake_windows.foreground = 4
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [4]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_with_no_terminal_the_app_that_launched_us_is_minimized(fake_windows):
    fake_windows.chain("cmd.exe", "claude.exe", "claude.exe", "sihost.exe", "explorer.exe")
    fake_windows.add(10, cls="Chrome_WidgetWin_1", title="Claude", pid=103)
    fake_windows.add(11, cls="Chrome_WidgetWin_1", visible=False, pid=103)
    fake_windows.add(12, cls="Shell_TrayWnd", pid=105)
    fake_windows.add(13, cls="OpusApp", title="Document1 - Word", pid=999)
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [10]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_the_walk_stops_at_the_session_shell(fake_windows):
    fake_windows.chain("cmd.exe", "sihost.exe", "claude.exe")
    fake_windows.add(10, cls="Chrome_WidgetWin_1", pid=103)
    assert REAL_CONSOLE_WINDOWS(exclude=0) == []
    fake_windows.names[102] = "explorer.exe"
    assert REAL_CONSOLE_WINDOWS(exclude=0) == []


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_vs_code_that_launched_us_is_left_alone(fake_windows):
    fake_windows.chain("powershell.exe", "Code.exe", "Code.exe", "explorer.exe")
    fake_windows.add(10, cls="Chrome_WidgetWin_1", title="VS Code", pid=102)
    fake_windows.add(11, cls="Chrome_WidgetWin_1", title="VS Code", pid=103)
    assert REAL_CONSOLE_WINDOWS(exclude=0) == []


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_the_launcher_is_not_the_target_and_waits_behind_a_terminal(fake_windows):
    fake_windows.chain("claude.exe", "sihost.exe")
    fake_windows.add(10, cls="Chrome_WidgetWin_1", pid=101)
    assert REAL_CONSOLE_WINDOWS(exclude=10) == []
    fake_windows.add(2, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    fake_windows.foreground = 2
    assert REAL_CONSOLE_WINDOWS(exclude=0) == [2]


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_start_minimizes_the_terminal_and_stop_brings_it_back(
    ctx, tmp_path, fake_start, fake_windows, consoles, monkeypatch
):
    fake_windows.add(7, cls="CASCADIA_HOSTING_WINDOW_CLASS", title="pilot")
    consoles.append(7)
    monkeypatch.setattr(recording, "probe", lambda path: recording.parse_probe({}))
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: False)

    started = recording.start(ctx, out=str(tmp_path / "m.mp4"), full_screen=True)

    assert started["minimized_windows"] == [{"hwnd": 7, "title": "pilot"}]
    assert recording.read_state(recording.state_file("notepad"))["minimized"] == [7]
    assert fake_windows.shown == [(7, recording.win32con.SW_MINIMIZE)]

    recording.stop(ctx)

    assert fake_windows.shown[-1] == (7, recording.win32con.SW_SHOWNOACTIVATE)


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_a_failed_start_brings_the_terminal_back(ctx, tmp_path, fake_start, fake_windows, consoles):
    fake_windows.add(7)
    consoles.append(7)
    fake_start.fail = "Could not find window"

    with pytest.raises(PilotError):
        recording.start(ctx, out=str(tmp_path / "x.mp4"), full_screen=True)

    assert fake_windows.shown[-1] == (7, recording.win32con.SW_SHOWNOACTIVATE)


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_restore_skips_a_window_that_is_gone_or_back_already(fake_windows):
    fake_windows.add(8)
    recording.restore([8, 9])
    assert fake_windows.shown == []


# ---------------------------------------------------------------- covering

TARGET_HWND, TARGET_PID = 100, 1
AREA = (100, 100, 1000, 800)


class ZOrder:
    """Top-level windows from the top down, for the calls covering_windows makes."""

    def __init__(self):
        self.order: list[int] = []
        self.windows: dict[int, dict] = {}

    def add(self, hwnd, *, pid=2, rect=(0, 0, 400, 300), cls="Chrome_WidgetWin_1", **flags):
        self.order.append(hwnd)
        self.windows[hwnd] = {
            "pid": pid,
            "rect": rect,
            "cls": cls,
            "title": f"window {hwnd}",
            "visible": flags.get("visible", True),
            "iconic": flags.get("iconic", False),
            "cloaked": flags.get("cloaked", False),
        }

    def next(self, hwnd, how):
        index = self.order.index(hwnd) + 1
        return self.order[index] if index < len(self.order) else 0


@pytest.fixture
def zorder(monkeypatch):
    z = ZOrder()
    gui = recording.win32gui
    monkeypatch.setattr(gui, "GetTopWindow", lambda parent: z.order[0] if z.order else 0)
    monkeypatch.setattr(gui, "GetWindow", z.next)
    monkeypatch.setattr(gui, "IsWindowVisible", lambda h: z.windows[h]["visible"])
    monkeypatch.setattr(gui, "IsIconic", lambda h: z.windows[h]["iconic"])
    monkeypatch.setattr(gui, "GetClassName", lambda h: z.windows[h]["cls"])
    monkeypatch.setattr(gui, "GetWindowText", lambda h: z.windows[h]["title"])
    monkeypatch.setattr(
        recording.win32process, "GetWindowThreadProcessId", lambda h: (0, z.windows[h]["pid"])
    )
    monkeypatch.setattr(recording, "_cloaked", lambda h: z.windows[h]["cloaked"])
    monkeypatch.setattr(recording, "_shell", lambda h: False)
    monkeypatch.setattr(recording, "_frame", lambda h: z.windows[h]["rect"])
    monkeypatch.setattr(recording, "_process_name", lambda pid: f"app{pid}.exe")
    return z


def covering(z, ignore=()):
    z.add(TARGET_HWND, pid=TARGET_PID, rect=AREA)
    return recording.covering_windows(AREA, TARGET_HWND, list(ignore))


def test_a_window_above_that_overlaps_is_listed_with_its_share(zorder):
    zorder.add(7, pid=3, rect=(600, 500, 1000, 1000))
    assert covering(zorder) == [
        {
            "hwnd": 7,
            "title": "window 7",
            "class": "Chrome_WidgetWin_1",
            "pid": 3,
            "process": "app3.exe",
            "rect": [600, 500, 1000, 1000],
            "overlap_pct": 25.0,
        }
    ]


def test_a_window_above_beside_the_area_is_not(zorder):
    zorder.add(7, rect=(1100, 100, 300, 300))
    assert covering(zorder) == []


def test_a_window_below_the_target_is_not(zorder):
    zorder.add(TARGET_HWND, pid=TARGET_PID, rect=AREA)
    zorder.add(7, rect=AREA)
    assert recording.covering_windows(AREA, TARGET_HWND) == []


@pytest.mark.parametrize(
    "window",
    [
        {"cloaked": True},
        {"pid": TARGET_PID},
        {"cls": "Shell_TrayWnd"},
        {"cls": "Windows.UI.Core.CoreWindow"},
        {"visible": False},
        {"iconic": True, "cls": "CASCADIA_HOSTING_WINDOW_CLASS"},
        {"rect": (200, 200, 0, 0)},
    ],
    ids=[
        "cloaked",
        "same-process",
        "taskbar",
        "core-window",
        "hidden",
        "minimized-console",
        "no-size",
    ],
)
def test_what_does_not_cover(zorder, window):
    zorder.add(7, **({"rect": AREA} | window))
    assert covering(zorder) == []


def test_a_console_start_minimized_is_ignored(zorder):
    zorder.add(7, rect=AREA, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    assert covering(zorder, ignore=[7]) == []


def test_the_message_names_each_window_and_its_share():
    message = recording.covered_message(
        [
            {"title": "Character Map", "class": "#32770", "process": "charmap.exe", "pid": 9}
            | {"overlap_pct": 34.0},
            {"title": "", "class": "Popup", "process": None, "pid": 12, "overlap_pct": 2.5},
        ]
    )
    assert message.startswith(
        "2 window(s) cover the recording area: 'Character Map' (charmap.exe, 34% overlap), "
        "'Popup' (12, 2.5% overlap); ask the user"
    )
    assert message.endswith("or to record anyway with --allow-covered")


COVER = {"hwnd": 7, "title": "Character Map", "class": "#32770", "pid": 3}
COVER |= {"process": "charmap.exe", "rect": [0, 0, 500, 500], "overlap_pct": 34.0}


@pytest.fixture
def covered_start(monkeypatch, fake_start):
    """`start` in the default window mode, with one window over the area;
    --full-screen and --region still take the real target."""
    window = {"kind": "window", "hwnd": 42, "title": "Notepad", "rect": [0, 0, 990, 795]}
    real_target = recording._target

    def target(main, full_screen, region, follow):
        if full_screen or region:
            return real_target(main, full_screen, region, follow)
        return dict(window, clipped=False)

    monkeypatch.setattr(recording, "_main_window", lambda ctx: object())
    monkeypatch.setattr(recording.ui, "_top_level_handle", lambda window: 42)
    monkeypatch.setattr(recording, "_target", target)
    monkeypatch.setattr(recording, "covering_windows", lambda rect, below, ignore: [COVER])
    return fake_start


@pytest.mark.windows("the console windows record minimizes (win32console)")
def test_a_covered_area_refuses_the_start_and_brings_the_terminal_back(
    ctx, tmp_path, covered_start, fake_windows, consoles, monkeypatch
):
    fake_windows.add(5, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    consoles.append(5)
    spawned = []
    monkeypatch.setattr(recording.subprocess, "Popen", lambda *a, **kw: spawned.append(a))

    with pytest.raises(Covered) as err:
        recording.start(ctx, out=str(tmp_path / "c.mp4"))

    assert err.value.details == {"covering": [COVER]}
    assert "'Character Map' (charmap.exe, 34% overlap)" in str(err.value)
    assert spawned == [] and not recording.state_file("notepad").exists()
    assert fake_windows.shown[-1] == (5, recording.win32con.SW_SHOWNOACTIVATE)


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_allow_covered_records_and_lists_them(ctx, tmp_path, covered_start):
    result = recording.start(ctx, out=str(tmp_path / "c.mp4"), allow_covered=True)
    assert result["covering"] == [COVER] and result["pid"] == 4242


@pytest.mark.windows("the gdigrab supervisor's process creation flags")
def test_nothing_covering_is_an_empty_list_and_other_modes_are_unchecked(
    ctx, tmp_path, covered_start, monkeypatch
):
    monkeypatch.setattr(recording, "covering_windows", lambda rect, below, ignore: [])
    assert recording.start(ctx, out=str(tmp_path / "a.mp4"))["covering"] == []
    screen = recording.start(ctx, out=str(tmp_path / "b.mp4"), full_screen=True, overwrite=True)
    assert screen["covering"] is None


def test_the_cli_error_carries_the_covering_windows(tmp_path, covered_start, monkeypatch):
    monkeypatch.setattr(recording, "videos_dir", lambda: tmp_path)
    result = runner.invoke(app, [PLAIN_APP, "--no-shot", "record", "start", "c.mp4"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "Covered"
    assert payload["details"] == {"covering": [COVER]}
    assert "--allow-covered" in payload["message"]


# ------------------------------------------------------------------ output


def test_no_path_is_a_timestamped_file_in_videos(videos):
    path = recording.recording_path("notepad", None)
    assert path.parent == videos
    assert re.fullmatch(r"notepad-\d{8}-\d{6}\.mp4", path.name)


def test_a_bare_name_goes_in_videos(videos):
    assert recording.recording_path("notepad", "demo.mp4") == videos / "demo.mp4"


@pytest.mark.windows("a backslash path")
def test_a_path_with_a_directory_is_used_as_given(videos, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert recording.recording_path("notepad", "clips/demo.mp4") == tmp_path / "clips" / "demo.mp4"
    assert recording.recording_path("notepad", ".\\demo.mp4") == tmp_path / "demo.mp4"
    absolute = tmp_path / "abs" / "demo.mp4"
    assert recording.recording_path("notepad", str(absolute)) == absolute


def test_a_gif_goes_next_to_its_source_or_in_videos_by_name(videos, tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"mp4")
    monkeypatch.setattr(recording, "find_ffmpeg", lambda: Path("ffmpeg.exe"))
    monkeypatch.setattr(recording, "_ffmpeg", lambda argv: Path(argv[-1]).write_bytes(b"gif"))

    assert recording.gif(str(source))["path"] == str(tmp_path / "clip.gif")
    assert recording.gif(str(source), "demo.gif")["path"] == str(videos / "demo.gif")


@pytest.mark.windows("the Videos known folder (SHGetKnownFolderPath)")
def test_videos_falls_back_to_the_home_folder(tmp_path, monkeypatch):
    def missing(*args):
        raise OSError("no known folder")

    monkeypatch.setattr(recording.shell, "SHGetKnownFolderPath", missing)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert recording.videos_dir() == tmp_path / "Videos"
    assert (tmp_path / "Videos").is_dir()


# ----------------------------------------------------------------- status


def test_status_without_a_recording(ctx):
    assert recording.status(ctx) == {"recording": False}


def test_a_stale_state_file_is_reported_once_and_removed(ctx, monkeypatch):
    path = recording.state_file("notepad")
    recording.write_state(path, {"pid": 99, "started": 1.0, "path": "C:\\gone.mp4"})
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: False)

    assert recording.status(ctx) == {
        "recording": False,
        "stale": True,
        "exit_code": None,
        "path": "C:\\gone.mp4",
    }
    assert not path.exists()
    assert recording.status(ctx) == {"recording": False}


def test_status_of_a_running_recording(ctx, monkeypatch):
    recording.write_state(
        recording.state_file("notepad"),
        {"pid": 7, "started": time.time() - 5, "path": "C:\\r.mp4", "target": SCREEN},
    )
    monkeypatch.setattr(recording, "_alive", lambda pid, started=None: True)

    result = recording.status(ctx)

    assert result["recording"] and result["pid"] == 7 and result["target"] == SCREEN
    assert 4.5 <= result["elapsed_s"] < 30


# -------------------------------------------------------------------- cli


def test_the_destination_is_given_once():
    result = runner.invoke(app, [PLAIN_APP, "record", "start", "a.mp4", "--out", "b.mp4"])
    assert result.exit_code == 1
    assert "PATH or --out" in json.loads(result.output)["message"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--full-screen", "--region", "0,0,10,10"],
        ["--follow", "--full-screen"],
        ["--follow", "--region", "0,0,10,10"],
    ],
)
def test_screen_region_and_follow_are_exclusive(tmp_path, flags):
    result = runner.invoke(
        app, [PLAIN_APP, "--no-shot", "record", "start", str(tmp_path / "x.mp4"), *flags]
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in json.loads(result.output)["message"]


# --------------------------------------------------------------- real app


@pytest.fixture
def notepad(request):
    if "ui" not in (request.config.getoption("-m") or ""):
        pytest.skip("records a real Notepad; run with -m ui")
    try:
        recording.find_ffmpeg()
        recording.find_ffprobe()
    except PilotError as exc:
        pytest.skip(str(exc))
    from desktoppilot.core.pytest_plugin import app_context

    pilot = app_context("notepad")
    info = apps.launch(pilot)
    yield pilot
    if not info.get("adopted"):
        apps.kill(pilot)


@pytest.mark.ui
def test_a_real_notepad_recording_plays(notepad, tmp_path, monkeypatch, session_dir):
    out = tmp_path / "notepad.mp4"
    started = recording.start(notepad, out=str(out), fps=30)
    try:
        assert started["target"]["kind"] == "window" and "follow" not in started["target"]
        assert recording.status(notepad)["recording"]
        time.sleep(3)
    finally:
        stopped = recording.stop(notepad)

    print(json.dumps(stopped, indent=2))
    assert stopped["exit_code"] == 0 and "warning" not in stopped
    assert stopped["size_bytes"] == out.stat().st_size > 0
    assert stopped["video"]["codec"] == "h264" and stopped["video"]["fps"] == 30
    assert stopped["audio"] is None and stopped["duration_s"] >= 2
    probe = subprocess.run(
        [str(recording.find_ffprobe()), "-v", "error", "-show_entries"]
        + ["format=duration:stream=codec_name", "-of", "json", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    print(probe.stdout)
    data = json.loads(probe.stdout)
    assert float(data["format"]["duration"]) >= 2
    assert data["streams"][0]["codec_name"] == "h264"
