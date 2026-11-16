r"""Screen recording: the screen under the app's main window, the window
itself (--follow), the whole screen or a region, to MP4 with ffmpeg's gdigrab
(on Linux x11grab; see linux/recording.py).

`record stop` runs in a new process, so it cannot reach the ffmpeg that
`record start` began. `start` therefore hands ffmpeg to a detached
supervisor (`python -m desktoppilot.core.recording --supervise STATE`) that
holds ffmpeg's stdin. Everything between the processes goes through files
next to the session file:

- `<app>.recording.json`, the state: written by `start` as the request
  (argv, path, target), rewritten by the supervisor with ffmpeg's pid once it
  runs, and with `exit_code` once ffmpeg has ended;
- `<app>.recording.json.stop`, which `stop` touches; the supervisor then
  sends ffmpeg `q`, the clean way to end it, so the MP4 gets its index;
- `<app>.recording.log`, ffmpeg's stderr - where a wrong window title or a
  missing microphone shows up.

The argv builders are pure, so the unit tests need no ffmpeg.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from ctypes import wintypes
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from ..linux import procs as linux_procs
from ..linux import recording as linux_recording
from ..linux import x11
from . import imaging, paths, ui
from .errors import Covered, PilotError, Timeout
from .native import WINDOWS, win32api, win32con, win32gui, win32process
from .registry import AppContext
from .util import wait_until

if WINDOWS:
    import pywintypes
    import win32console
    from win32com.shell import shell, shellcon

INSTALL_HINT = (
    "install it with: winget install Gyan.FFmpeg (then open a new shell)"
    if WINDOWS
    else linux_recording.INSTALL_HINT
)
# The terminal `pilot` runs in: Windows Terminal, and a plain conhost.
CONSOLE_CLASSES = ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass")
# Shell windows that sit above everything and are not in anyone's way.
SHELL_CLASSES = (
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
    "tooltips_class32",
    "Windows.UI.Core.CoreWindow",
)
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
# How long `start` waits for ffmpeg to run and create the file, how long the
# supervisor gives ffmpeg to finish the file after `q`, and how long `stop`
# waits for all of that.
START_TIMEOUT = 5.0
STOP_GRACE = 10.0
STOP_TIMEOUT = 15.0
POLL = 0.2
LOG_TAIL = 20
STILL_ACTIVE = 259
# gdigrab of the window, the screen or a box of it; the pointer is drawn in.
# Even dimensions because yuv420p needs them; faststart so the file plays
# while it is still being copied somewhere.
VIDEO_OUT = (
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-crf",
    "23",
    "-pix_fmt",
    "yuv420p",
    "-vf",
    "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    "-movflags",
    "+faststart",
)
AUDIO_OUT = ("-c:a", "aac", "-b:a", "128k")
# Off Windows, Popen refuses any creationflags but 0.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0


# ------------------------------------------------------------------ ffmpeg


def _winget_dirs() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA")
    return [Path(local) / "Microsoft" / "WinGet"] if local and WINDOWS else []


def _find_tool(name: str) -> Path:
    """On PATH, else where winget put it: a shell that was open during the
    install does not see winget's PATH change."""
    found = shutil.which(name)
    if found:
        return Path(found)
    for root in _winget_dirs():
        link = root / "Links" / f"{name}.exe"
        if link.is_file():
            return link
        hits = sorted((root / "Packages").glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"))
        if hits:
            return hits[-1]
    raise PilotError(f"{name} not found; {INSTALL_HINT}")


def find_ffmpeg() -> Path:
    return _find_tool("ffmpeg")


def find_ffprobe() -> Path:
    return _find_tool("ffprobe")


def input_args(target: dict, fps: int) -> list[str]:
    """gdigrab's input: the whole virtual screen, or a box of it in screen
    pixels - a region, or the window's rect at start. A followed window is
    matched by its exact title and copied from its own pixels. On Linux,
    x11grab of $DISPLAY."""
    if not WINDOWS:
        return linux_recording.input_args(target, fps, linux_recording.display())
    args = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1"]
    if target.get("follow"):
        return [*args, "-i", f"title={target['title']}"]
    if target["kind"] != "screen":
        x, y, w, h = target["rect"]
        args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
    return [*args, "-i", "desktop"]


def window_rect(frame: imaging.Rect, screen: imaging.Rect) -> tuple[imaging.Rect, bool]:
    """The window's frame cut to the virtual screen, and whether it was cut:
    gdigrab refuses a box that reaches off the desktop."""
    rect = imaging._intersect(frame, screen)
    if rect[2] <= 0 or rect[3] <= 0:
        raise PilotError(f"the main window {','.join(map(str, frame))} is off every monitor")
    return rect, rect != tuple(frame)


def ffmpeg_argv(
    ffmpeg: str | Path, target: dict, out: str | Path, *, fps: int, mic: str | None
) -> list[str]:
    """The whole command line. No audio at all unless `mic` names a
    DirectShow device (on Linux a PulseAudio source). -nostats keeps the log
    readable; stdin stays open, since `q` on it is how the recording ends."""
    argv = [str(ffmpeg), "-hide_banner", "-nostats", *input_args(target, fps)]
    if mic and not WINDOWS:
        argv += linux_recording.mic_args(mic)
    elif mic:
        argv += ["-f", "dshow", "-i", f"audio={mic}"]
    argv += VIDEO_OUT
    if mic:
        argv += AUDIO_OUT
    return [*argv, "-y", str(out)]


PROBE_ENTRIES = (
    "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,"
    "r_frame_rate,avg_frame_rate,nb_frames,sample_rate,channels,bit_rate"
)


def probe(path: str | Path) -> dict:
    """What the file holds, through one ffprobe call. A file ffmpeg did not
    finish (killed, no index) fails here, and says so."""
    argv = [str(find_ffprobe()), "-v", "error", "-show_entries", PROBE_ENTRIES]
    argv += ["-of", "json", str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        raise PilotError(f"ffprobe could not read {path}: {done.stderr.strip()}")
    return parse_probe(json.loads(done.stdout or "{}"))


def parse_probe(data: dict) -> dict:
    """ffprobe's JSON as duration_s, size_bytes, and the first video and audio
    stream - None when there is none, so "no sound" is said, not implied."""
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _number(fmt.get("duration"), float)
    return {
        "duration_s": round(duration, 2) if duration is not None else None,
        "size_bytes": _number(fmt.get("size"), int),
        "video": video
        and {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": frame_rate(video.get("r_frame_rate")),
            "frames": _number(video.get("nb_frames"), int),
            "bit_rate_kbps": _kbps(video.get("bit_rate") or fmt.get("bit_rate")),
        },
        "audio": audio
        and {
            "codec": audio.get("codec_name"),
            "sample_rate": _number(audio.get("sample_rate"), int),
            "channels": audio.get("channels"),
        },
    }


def frame_rate(text: str | None) -> int | float | None:
    """ffprobe's "30/1" as 30, "30000/1001" as 29.97; "0/0" as None."""
    try:
        rate = Fraction(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if rate <= 0:
        return None
    return int(rate) if rate.denominator == 1 else round(float(rate), 2)


def _number(value, kind):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def _kbps(value) -> int | None:
    bits = _number(value, int)
    return round(bits / 1000) if bits is not None else None


def local_iso(epoch: float | None) -> str | None:
    """Local wall time to the second."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds") if epoch else None


# ------------------------------------------------------------------- state


def state_file(app: str) -> Path:
    """Next to the session file and the fallback journal."""
    return paths.SESSION_DIR / f"{app}.recording.json"


def log_file(app: str) -> Path:
    return paths.SESSION_DIR / f"{app}.recording.log"


def stop_file(state: Path) -> Path:
    return state.with_name(state.name + ".stop")


def write_state(path: Path, state: dict) -> None:
    """Through a temp file and a rename, so a reader in another process never
    sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clear(path: Path) -> None:
    path.unlink(missing_ok=True)
    stop_file(path).unlink(missing_ok=True)


def _alive(pid: int | None, started: float | None = None) -> bool:
    """Running, and - when `started` is known - created no later than it, so
    a pid Windows has handed to a new process does not count."""
    if not WINDOWS:
        return linux_procs.alive(pid, started)
    if not pid:
        return False
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error:
        return False
    try:
        if win32process.GetExitCodeProcess(handle) != STILL_ACTIVE:
            return False
        if started is None:
            return True
        created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
        return created <= started + 1.0
    except pywintypes.error:
        return True
    finally:
        win32api.CloseHandle(handle)


def recording(state: dict) -> bool:
    """ffmpeg is still at it. A request the supervisor has not picked up yet
    counts while it is young: `start` is still waiting on it."""
    if "exit_code" in state:
        return False
    if state.get("pid") is None:
        return time.time() - state.get("requested", 0) < START_TIMEOUT + 5
    return _alive(state["pid"], state.get("started"))


def log_tail(path: str | Path, lines: int = LOG_TAIL) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(line.rstrip() for line in text.splitlines()[-lines:] if line.strip())


# ------------------------------------------------------------------ output


def videos_dir() -> Path:
    """The user's Videos folder, created if missing."""
    try:
        if WINDOWS:
            folder = Path(shell.SHGetKnownFolderPath(shellcon.FOLDERID_Videos, 0, None))
        else:
            folder = _xdg_videos()
    except Exception:  # noqa: BLE001 - a missing known folder falls back, never fails
        folder = Path.home() / "Videos"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _xdg_videos() -> Path:
    """XDG_VIDEOS_DIR as `xdg-user-dir` reads it from user-dirs.dirs; the
    home folder itself means "not set"."""
    out = subprocess.run(
        ["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not out or Path(out) == Path.home():
        raise PilotError("XDG_VIDEOS_DIR is not set")
    return Path(out)


def place(name: str) -> Path:
    """A bare file name goes in Videos; a path with a directory part, absolute
    or relative to the cwd, is used as given."""
    path = os.path.expanduser(name)
    if not os.path.dirname(path):
        return videos_dir() / path
    return Path(path).resolve()


def recording_path(app: str, out: str | None) -> Path:
    """OUT placed as `place` does, else a timestamped file in Videos, so two
    recordings never collide."""
    if out:
        return place(out)
    return videos_dir() / f"{app}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"


# ---------------------------------------------------------------- consoles


def console_windows(exclude: int) -> list[int]:
    """The terminal this command runs in, which would sit on top of the
    recording: our console window and its root owner (the Windows Terminal
    window behind a ConPTY), and the foreground window when it is a console.
    Only visible, unminimized windows with a size that are not the target:
    the ConPTY's own console window is "visible" at 0x0. On Linux, a terminal
    emulator's window above this process (linux/recording.py)."""
    if not WINDOWS:
        return linux_recording.terminal_windows(exclude)
    candidates = []
    console = win32console.GetConsoleWindow()
    if console:
        candidates += [console, win32gui.GetAncestor(console, win32con.GA_ROOTOWNER)]
    foreground = win32gui.GetForegroundWindow()
    if foreground and win32gui.GetClassName(foreground) in CONSOLE_CLASSES:
        candidates.append(foreground)
    found: list[int] = []
    for hwnd in candidates:
        if not hwnd or hwnd == exclude or hwnd in found:
            continue
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            continue
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right > left and bottom > top:
            found.append(hwnd)
    return found


def minimize(hwnds: list[int]) -> list[dict]:
    """Minimized, not hidden: a hidden terminal looks like a lost one."""
    done = []
    for hwnd in hwnds:
        title = win32gui.GetWindowText(hwnd)
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        done.append({"hwnd": hwnd, "title": title})
    return done


def restore(hwnds: list[int]) -> list[dict]:
    """Back, without taking the focus; returns the ones it brought back. Best
    effort: a window closed meanwhile or already restored is left alone."""
    restored = []
    for hwnd in hwnds:
        try:
            if win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
                restored.append({"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd)})
        except Exception:  # noqa: BLE001 - restoring never fails a stop
            pass
    return restored


# ---------------------------------------------------------------- covering


def covering_windows(rect: imaging.Rect, below: int, ignore: list[int] = ()) -> list[dict]:
    """Other apps' windows above `below` in the z-order that overlap `rect`:
    what a screen copy of the rect would record instead of the target. A
    popup or tooltip of the target's own process is part of the picture."""
    _, target_pid = win32process.GetWindowThreadProcessId(below)
    area = rect[2] * rect[3]
    hits = []
    for hwnd in _windows_above(below):
        if hwnd in ignore or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            continue
        cls = win32gui.GetClassName(hwnd)
        if cls in SHELL_CLASSES or _cloaked(hwnd) or _shell(hwnd):
            continue
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == target_pid:
            continue
        frame = _frame(hwnd)
        overlap = imaging._intersect(frame, rect)
        if frame[2] <= 0 or frame[3] <= 0 or overlap[2] <= 0 or overlap[3] <= 0:
            continue
        hits.append(
            {
                "hwnd": hwnd,
                "title": win32gui.GetWindowText(hwnd),
                "class": cls,
                "pid": pid,
                "process": _process_name(pid),
                "rect": list(frame),
                "overlap_pct": round(100 * overlap[2] * overlap[3] / area, 1),
            }
        )
    return hits


def _windows_above(hwnd: int) -> Iterator[int]:
    """Top-level windows from the top of the z-order down to `hwnd`."""
    current = win32gui.GetTopWindow(None)
    while current and current != hwnd:
        yield current
        current = win32gui.GetWindow(current, win32con.GW_HWNDNEXT)


def _dwm_attribute(hwnd: int, attribute: int, value) -> bool:
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)
    )
    return result == 0


def _shell(hwnd: int) -> bool:
    """On Linux, a panel, the desktop, a notification or a tooltip, by window
    type; on Windows SHELL_CLASSES says it."""
    return not WINDOWS and linux_recording.is_shell(hwnd)


def _cloaked(hwnd: int) -> bool:
    """A UWP app keeps cloaked windows at full size that nobody sees. X11
    has no cloaking."""
    if not WINDOWS:
        return False
    value = wintypes.DWORD(0)
    return _dwm_attribute(hwnd, DWMWA_CLOAKED, value) and value.value != 0


def _frame(hwnd: int) -> imaging.Rect:
    """The visible frame, without the invisible resize borders a window's
    rect carries: those overlap a neighbour by a few pixels. On X11 the
    window manager's frame."""
    if not WINDOWS:
        left, top, right, bottom = x11.frame_rect(hwnd)
        return left, top, right - left, bottom - top
    rect = wintypes.RECT()
    if not _dwm_attribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, rect):
        rect.left, rect.top, rect.right, rect.bottom = win32gui.GetWindowRect(hwnd)
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _process_name(pid: int) -> str | None:
    """The image name, read with the least access, so an elevated process
    still has one."""
    if not WINDOWS:
        return linux_procs.image(pid)
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            wintypes.HANDLE(int(handle)), 0, buffer, ctypes.byref(size)
        )
        return Path(buffer.value).name if ok else None
    finally:
        win32api.CloseHandle(handle)


def covered_message(covering: list[dict]) -> str:
    listed = ", ".join(
        f"{w['title'] or w['class']!r} ({w['process'] or w['pid']}, {w['overlap_pct']:g}% overlap)"
        for w in covering
    )
    return (
        f"{len(covering)} window(s) cover the recording area: {listed}; ask the user "
        "whether to close or move them, or to record anyway with --allow-covered"
    )


# ------------------------------------------------------------------ target


def _main_window(ctx: AppContext):
    if ctx.profile is None:
        raise PilotError(f"{ctx.name} has no window of its own; use --full-screen or --region")
    window = ui.main_window(ctx)
    if ui.is_minimized(window):
        raise PilotError("the main window is minimized; `ui activate` it first")
    return window


def _target(window, full_screen: bool, region: str | None, follow: bool) -> dict:
    if full_screen:
        return {"kind": "screen", "rect": list(ui.virtual_screen_rect())}
    if region:
        return {"kind": "region", "rect": list(imaging.parse_region(region))}
    hwnd = ui._top_level_handle(window)
    title = win32gui.GetWindowText(hwnd)
    if follow:
        if WINDOWS and not title:
            # gdigrab finds the window by its exact title; x11grab by its id.
            raise PilotError("the main window has no title to follow; drop --follow")
        return {"kind": "window", "follow": True, "hwnd": hwnd, "title": title}
    ui.activate(window)
    rect, clipped = window_rect(ui.capture_origin(window), ui.virtual_screen_rect())
    return {"kind": "window", "hwnd": hwnd, "title": title, "rect": list(rect), "clipped": clipped}


def describe(target: dict) -> str:
    if target["kind"] == "window":
        where = "follow" if target.get("follow") else ",".join(map(str, target["rect"]))
        return f"window {target['title']!r} {where}"
    return f"{target['kind']} {','.join(map(str, target['rect']))}"


# --------------------------------------------------------------- commands


def start(
    ctx: AppContext,
    *,
    out: str | None = None,
    full_screen: bool = False,
    region: str | None = None,
    fps: int = 30,
    mic: str | None = None,
    overwrite: bool = False,
    follow: bool = False,
    allow_covered: bool = False,
) -> dict:
    """Start recording, through a detached supervisor, and return once ffmpeg
    is writing the file. The terminal this runs in is minimized first, so it
    is not in the video; `stop` restores it. In the default window mode,
    another app's window over the area refuses the start (Covered) unless
    `allow_covered`. JSON: path, target, fps, mic, pid, log,
    minimized_windows, covering (null when not checked)."""
    path = state_file(ctx.name)
    current = read_state(path)
    if current is not None and recording(current):
        raise PilotError(
            f"{ctx.name} is already recording to {current['path']}; `record stop` it first"
        )
    if fps <= 0:
        raise PilotError(f"--fps wants a positive number; got {fps}")
    if sum((full_screen, bool(region), follow)) > 1:
        raise PilotError("--full-screen, --region and --follow are mutually exclusive")
    if ctx.profile is None and not region and not follow:
        full_screen = True  # `pilot desktop` has no window of its own: the screen
    target_file = recording_path(ctx.name, out)
    if target_file.exists() and not overwrite:
        raise PilotError(f"{target_file} exists; pass --overwrite or another path")
    ffmpeg = find_ffmpeg()
    window = None if full_screen or region else _main_window(ctx)
    exclude = ui._top_level_handle(window) if window else 0
    # A followed window is copied from its own pixels: nothing on top shows.
    hidden = [] if follow else console_windows(exclude)
    minimized = minimize(hidden)
    try:
        target = _target(window, full_screen, region, follow)
        covering = None
        if target["kind"] == "window" and not follow:
            # Still above the activated target: topmost windows, or ones the
            # activation did not push down.
            covering = covering_windows(tuple(target["rect"]), target["hwnd"], hidden)
            if covering and not allow_covered:
                raise Covered(covered_message(covering), {"covering": covering})
        result = _start(ctx, target_file, target, ffmpeg, fps=fps, mic=mic, minimized=minimized)
        return result | {"covering": covering}
    except Exception:
        restore(hidden)
        raise


def _start(
    ctx: AppContext,
    target_file: Path,
    target: dict,
    ffmpeg: Path,
    *,
    fps: int,
    mic: str | None,
    minimized: list[dict],
) -> dict:
    path = state_file(ctx.name)
    argv = ffmpeg_argv(ffmpeg, target, target_file, fps=fps, mic=mic)
    # Gone first, so the file appearing below means ffmpeg is writing it.
    target_file.unlink(missing_ok=True)
    log = log_file(ctx.name)
    _clear(path)
    write_state(
        path,
        {
            "app": ctx.name,
            "pid": None,
            "supervisor_pid": None,
            "path": str(target_file),
            "log": str(log),
            "requested": time.time(),
            "started": None,
            "target": target,
            "fps": fps,
            "mic": mic,
            "argv": argv,
            "minimized": [window["hwnd"] for window in minimized],
        },
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "desktoppilot.core.recording", "--supervise", str(path)],
        **_detached(),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def running() -> dict | None:
        state = read_state(path) or {}
        if "exit_code" in state:
            raise PilotError(_failed(state, log))
        if state.get("pid") is None and supervisor.poll() is not None:
            raise PilotError(f"the recording supervisor exited ({supervisor.returncode})")
        if state.get("pid") and target_file.exists():
            return state
        return None

    warning = None
    try:
        state = wait_until(running, timeout=START_TIMEOUT, interval=POLL, fatal=(PilotError,))
    except Timeout:
        state = read_state(path) or {}
        if not recording(state):
            _clear(path)
            raise PilotError(_failed(state, log)) from None
        warning = f"ffmpeg runs but has not created {target_file} yet; check `record status`"
    except PilotError:
        _clear(path)
        raise
    result = {
        "path": str(target_file),
        "target": target,
        "fps": fps,
        "mic": mic,
        "pid": state.get("pid"),
        "log": str(log),
        "minimized_windows": minimized,
    }
    return result | ({"warning": warning} if warning else {})


def _detached() -> dict:
    """How the supervisor outlives `start`: on Windows a process group and a
    console with no window, not DETACHED_PROCESS - the venv's python.exe is a
    launcher, and the interpreter it starts would get a console of its own,
    a terminal window on top of the recording. On Linux a session of its
    own, out of reach of the terminal's hangup and Ctrl+C."""
    if WINDOWS:
        return {"creationflags": _NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _failed(state: dict, log: Path) -> str:
    code = state.get("exit_code")
    head = "ffmpeg exited" + (f" ({code})" if code is not None else "") + " before recording"
    tail = log_tail(log)
    return f"{head}; its log:\n{tail}" if tail else head


def stop(ctx: AppContext, *, force: bool = False) -> dict:
    """End the recording, wait for a playable file and report what it holds.
    --force kills ffmpeg, which leaves an MP4 with no index. JSON: path,
    duration_s, size_bytes, size_mb, video, audio (null: no sound), target,
    started, stopped, exit_code, restored_windows, log (+ warning)."""
    path = state_file(ctx.name)
    state = read_state(path)
    if state is None:
        raise PilotError(f"no recording for {ctx.name}")
    warning = None
    if force:
        for pid in (state.get("supervisor_pid"), state.get("pid")):
            if _alive(pid) and not WINDOWS:
                linux_procs.kill([pid], grace=0)
            elif _alive(pid):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    creationflags=_NO_WINDOW,
                )
        warning = "ffmpeg was killed: the file may not play"
        state["stopped"] = time.time()
    elif "exit_code" not in state:
        stop_file(path).touch()

        def ended() -> dict | None:
            now = read_state(path)
            if now is None or "exit_code" in now:
                return now or state
            if not _alive(now.get("pid")) and not _alive(now.get("supervisor_pid")):
                return now
            return None

        try:
            state = wait_until(ended, timeout=STOP_TIMEOUT, interval=POLL)
        except Timeout:
            raise PilotError(
                f"the recording did not stop within {STOP_TIMEOUT:g}s; "
                "`record stop --force` kills it (the file may not play)"
            ) from None
    restored = restore(state.get("minimized", []))
    _clear(path)
    out = Path(state["path"])
    facts = {"duration_s": None, "size_bytes": None, "video": None, "audio": None}
    if out.exists():
        facts["size_bytes"] = out.stat().st_size
        try:
            facts |= probe(out)
        except PilotError as exc:
            warning = warning or str(exc)
    else:
        warning = warning or f"{out} was not written; see {state.get('log')}"
    if facts["audio"] is not None:
        facts["audio"]["device"] = state.get("mic")
    size = facts["size_bytes"]
    result = {
        "path": str(out),
        "duration_s": facts["duration_s"],
        "size_bytes": size,
        "size_mb": round(size / 2**20, 2) if size is not None else None,
        "video": facts["video"],
        "audio": facts["audio"],
        "target": state.get("target"),
        "started": local_iso(state.get("started")),
        "stopped": local_iso(state.get("stopped")),
        "exit_code": state.get("exit_code"),
        "restored_windows": restored,
        "log": state.get("log"),
    }
    return result | ({"warning": warning} if warning else {})


def status(ctx: AppContext) -> dict:
    """Recording or not. A state file whose ffmpeg is gone (the window
    closed, the machine slept) is reported once as stale and removed.
    JSON: recording, path, started, elapsed_s, target, pid - or recording:
    false (+ stale, exit_code, path)."""
    path = state_file(ctx.name)
    state = read_state(path)
    if state is None:
        return {"recording": False}
    if not recording(state):
        restore(state.get("minimized", []))
        _clear(path)
        return {
            "recording": False,
            "stale": True,
            "exit_code": state.get("exit_code"),
            "path": state.get("path"),
        }
    started = state.get("started") or state.get("requested") or time.time()
    return {
        "recording": True,
        "path": state["path"],
        "started": local_iso(started),
        "elapsed_s": round(time.time() - started, 1),
        "target": state.get("target"),
        "pid": state.get("pid"),
    }


def gif(src: str, out: str | None = None, *, fps: int = 10, width: int = 800) -> dict:
    """A GIF of a recording through a two-pass palette: GIF has 256 colours,
    and one palette made for this clip beats the generic one by far. A bare
    OUT goes in Videos; no OUT means SRC with .gif. JSON: path, size_bytes."""
    source = Path(src).expanduser().resolve()
    if not source.is_file():
        raise PilotError(f"no such file: {source}")
    if fps <= 0 or width <= 0:
        raise PilotError("--fps and --width want positive numbers")
    target = place(out) if out else source.with_suffix(".gif")
    if target == source:
        raise PilotError(f"would overwrite SRC {str(source)!r}; pass -o OUT")
    ffmpeg = str(find_ffmpeg())
    scale = f"fps={fps},scale={width}:-1:flags=lanczos"
    with tempfile.TemporaryDirectory(prefix="desktoppilot-gif-") as temp:
        palette = str(Path(temp) / "palette.png")
        _ffmpeg(
            [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(source)]
            + ["-vf", f"{scale},palettegen", palette]
        )
        _ffmpeg(
            [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(source), "-i", palette]
            + ["-lavfi", f"{scale}[x];[x][1:v]paletteuse", str(target)]
        )
    return {"path": str(target), "size_bytes": target.stat().st_size}


def _ffmpeg(argv: list[str]) -> None:
    done = subprocess.run(argv, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        raise PilotError(f"ffmpeg failed ({done.returncode}): {done.stderr.strip()[-2000:]}")


# -------------------------------------------------------------- supervisor


def supervise(path: Path) -> int:
    """The detached half of `start`: run ffmpeg, then end it with `q` when
    the stop file appears. ffmpeg, like this process, gets a console with no
    window, so nothing opens on the desktop - or in the recording."""
    state = read_state(path)
    if state is None:
        return 2
    stop = stop_file(path)
    with open(state["log"], "wb") as log:
        try:
            proc = subprocess.Popen(
                state["argv"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            log.write(f"could not start ffmpeg: {exc}\n".encode())
            write_state(path, state | {"exit_code": -1, "supervisor_pid": os.getpid()})
            return 1
        state |= {"pid": proc.pid, "supervisor_pid": os.getpid(), "started": time.time()}
        write_state(path, state)
        wait_until(
            lambda: proc.poll() is not None or stop.exists(), timeout=math.inf, interval=POLL
        )
        requested = proc.poll() is None
        if requested:
            try:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(STOP_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    # The state stays, with the exit code in it, for `stop` or `status` to
    # report and remove; only a `stop --force` that got here first has
    # removed it already.
    if path.exists():
        write_state(path, state | {"exit_code": _signed(proc.returncode), "stopped": time.time()})
    stop.unlink(missing_ok=True)
    return 0


def _signed(code: int) -> int:
    """Windows hands back ffmpeg's negative AVERROR codes unsigned: -5, not
    4294967291."""
    return code - 2**32 if code >= 2**31 else code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "--supervise":
        print("usage: python -m desktoppilot.core.recording --supervise STATE_FILE")
        return 2
    return supervise(Path(args[1]))


if __name__ == "__main__":
    sys.exit(main())
