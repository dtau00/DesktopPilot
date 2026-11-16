"""App process lifecycle: launch, attach, list windows, shut down.

Launching adopts a running instance by default, because the app on a dev VM is
usually the user's; `launch = "fresh"` in the profile, or `--fresh`, kills it
first. The session file holds process bookkeeping only (pid, start time, who
started it, the pids an adopt joined) and is keyed by the process image, so apps
that drive the same exe (word and a plugin that extends it) agree on which
instance is ours.
Which app a command acts on comes from the command line, never from here.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ..linux import procs as linux_procs
from ..linux import x11
from . import dialogs, paths, procs
from .errors import AppNotRunning, PilotError, Timeout
from .native import WINDOWS, win32api, win32con, win32gui, win32process
from .profiles import Profile
from .registry import AppContext
from .util import wait_until

if WINDOWS:
    import win32print

STATE_DIR = paths.STATE_DIR  # plugins still read it from here


@dataclass
class Session:
    app: str  # the app that wrote it; informational
    pid: int | None
    started_at: float
    launched: bool
    # After an adopt: the instances running then. None in a file written
    # before it existed - owned_pids then falls back to creation times.
    pids: list[int] | None = None

    @staticmethod
    def path(profile: Profile) -> Path:
        return paths.SESSION_DIR / f"{profile.image_name}.json"

    def save(self, profile: Profile) -> None:
        path = self.path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")

    @staticmethod
    def load(profile: Profile) -> Session | None:
        """None when there is no session. A file that does not parse raises:
        read as "none" it would quietly make our own instance foreign."""
        path = Session.path(profile)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PilotError(f"session file {path} is unreadable: {exc}") from exc
        try:
            return Session(**json.loads(text))
        except (ValueError, TypeError) as exc:
            raise PilotError(f"session file {path} is corrupt: {exc}") from exc


def app_log(profile: Profile) -> Path:
    """Where a launch on Linux sends the app's stdout and stderr, next to the
    session file; each launch starts it afresh."""
    return paths.SESSION_DIR / f"{profile.image_name}.log"


def launched_here(profile: Profile) -> bool:
    """Whether the running instance is one this tool started - the only kind
    it may click in without being asked."""
    try:
        saved = Session.load(profile)
    except PilotError:
        return False
    return bool(saved and saved.launched and saved.pid in pids(profile))


def owned_pids(profile: Profile, *, running: list[int] | None = None) -> list[int]:
    """The running instances this tool drives: the one it launched, or after
    an adopt the ones running then. [] with no session; a corrupt session
    file raises (see Session.load). Driving is not clicking: an adopted
    instance is owned, but not launched_here."""
    running = pids(profile) if running is None else running
    saved = Session.load(profile)
    if saved is None:
        return []
    if saved.pid is not None:
        return [saved.pid] if saved.pid in running else []

    def adopted(pid: int) -> bool:
        born = created(pid)
        if saved.pids is not None:
            # The creation time only guards a pid reused since the adopt.
            return pid in saved.pids and (born is None or born <= saved.started_at)
        return born is not None and born <= saved.started_at

    return [pid for pid in running if adopted(pid)]


def profile_of(ctx: AppContext) -> Profile:
    if ctx is None or ctx.profile is None:
        raise PilotError(f"app {getattr(ctx, 'name', '?')!r} has no profile; it owns no process")
    return ctx.profile


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _parents() -> dict[int, int]:
    """Parent pid of every process, from one Toolhelp snapshot."""
    if not WINDOWS:
        return linux_procs.parents()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return {}
    found: dict[int, int] = {}
    entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found[entry.th32ProcessID] = entry.th32ParentProcessID
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _self_and_ancestors() -> set[int]:
    """This process and its parent chain. An ad-hoc profile for python.exe
    would otherwise kill the pilot running the kill, and the shell above it."""
    parents = _parents()
    chain = {os.getpid()}
    pid = parents.get(os.getpid())
    while pid and pid not in chain:
        chain.add(pid)
        pid = parents.get(pid)
    return chain


def _spared(profile: Profile) -> set[int]:
    """What a kill of the app must leave alone: this process and its
    ancestors. On Linux also every process below an ancestor that runs the
    app's image: `pilot code` in VS Code's terminal runs under the user's
    editor, whose other processes would die with its renderers."""
    spared = _self_and_ancestors()
    if WINDOWS:
        return spared
    hosts = [pid for pid in spared if linux_procs.image(pid) == profile.image_name]
    return spared | set(linux_procs.descendants(hosts)) if hosts else spared


def created(pid: int) -> float | None:
    """When the process started, in epoch seconds; None if it is gone or denied."""
    if not WINDOWS:
        return linux_procs.created(pid)
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except Exception:  # noqa: BLE001 - exited, or not ours to open
        return None
    try:
        return win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
    except Exception:  # noqa: BLE001
        return None
    finally:
        handle.Close()


def _instance_rows(profile: Profile, rows: list[list[str]] | None = None) -> list[list[str]]:
    """The tasklist rows of the app's instances. A process of the same image
    that another one started is part of that instance, not a second app: Word
    starts one to file a Windows Error Reporting snapshot of itself (no
    command line, gone in seconds). The parent must be the older process, or
    a reused pid would pass - so an unknown creation time is a separate
    instance. `rows` reuses a tasklist the caller already ran."""
    rows = _rows(profile) if rows is None else rows
    running = {procs.pid_of(row) for row in rows}
    parents = _parents() if len(running) > 1 else {}

    def spawned_by_another(pid: int) -> bool:
        parent = parents.get(pid)
        if parent is None or parent == pid or parent not in running:
            return False
        born, parent_born = created(pid), created(parent)
        return born is not None and parent_born is not None and parent_born <= born

    return [row for row in rows if not spawned_by_another(procs.pid_of(row))]


def binding_env(profile: Profile) -> str:
    """The variable that binds the app to one instance: PILOT_<NAME>_PID,
    the profile name upper-cased with `-` as `_` (PILOT_CHROME_PID)."""
    return f"PILOT_{profile.name.upper().replace('-', '_')}_PID"


def bound_pid(profile: Profile) -> int | None:
    """The pid the app is bound to, or None. Bound, the app is that process
    and its descendants only - every command acts on that instance, never
    on another one of the same image (the user's own Chrome)."""
    name = binding_env(profile)
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        pid = int(value)
    except ValueError:
        raise PilotError(f"{name}={value!r} is not a pid") from None
    if pid <= 0:
        raise PilotError(f"{name}={value!r} is not a pid")
    return pid


def tree_of(pid: int) -> set[int]:
    """The pid and every process below it."""
    return set(linux_procs.descendants([pid], _parents()))


def _rows(profile: Profile) -> list[list[str]]:
    """The tasklist rows of the profile's image; with a binding, only those
    in the bound pid's tree (none when it is gone)."""
    rows = procs.tasklist(profile.image_name)
    bound = bound_pid(profile)
    if bound is None:
        return rows
    tree = tree_of(bound)
    return [row for row in rows if procs.pid_of(row) in tree]


def pids(profile: Profile) -> list[int]:
    """The app's running instances (see _instance_rows)."""
    return [procs.pid_of(row) for row in _instance_rows(profile)]


def all_pids(profile: Profile) -> list[int]:
    """Every process of the profile's image, the children of an instance too;
    with a binding, those of the bound instance only."""
    return [procs.pid_of(row) for row in _rows(profile)]


def memory(profile: Profile, rows: list[list[str]] | None = None) -> dict[int, int]:
    """Working set in bytes per instance of the profile's image."""
    return {procs.pid_of(row): procs.working_set(row) for row in _instance_rows(profile, rows)}


def largest(found: list[dict]) -> dict:
    """The window with the largest area - an app can own several stubs."""
    return max(found, key=lambda w: w["rect"][2] * w["rect"][3])


def windows(
    profile: Profile | None, *, main_only: bool = False, owned: set[int] | None = None
) -> list[dict]:
    """Visible top-level windows the app owns; every one on the desktop when
    the app has no profile. `owned` reuses pids the caller already listed.

    Enumerated through Win32 rather than the UIA root: a tool window an app
    owns is nested under its main window in the UIA tree, so a root-children
    scan misses it.
    """
    if owned is None and profile is not None:
        owned = set(all_pids(profile))
    classes = {c.casefold() for c in profile.window_class} if profile else set()
    found: list[dict] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, owner = win32process.GetWindowThreadProcessId(hwnd)
        if owned is not None and owner not in owned:
            return True
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        is_main = (not classes or cls.casefold() in classes) and bool(title)
        # A GTK menu or tooltip carries the app's WM_CLASS and often a title:
        # on X11 only a normal window is a main one (EWMH has no Win32 shape).
        if is_main and not WINDOWS:
            is_main = x11.is_normal(hwnd)
        if main_only and not is_main:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        found.append(
            {
                "hwnd": hwnd,
                "pid": owner,
                "class": cls,
                "title": title,
                "is_main": is_main,
                "rect": [left, top, right - left, bottom - top],
            }
        )
        return True

    win32gui.EnumWindows(collect, None)
    return found


def main_hwnd(
    profile: Profile,
    *,
    timeout: float | None = None,
    while_waiting: Callable[[], object] | None = None,
    pid: int | None = None,
) -> int:
    """Largest main window - an app can own several stubs with the right class.
    `pid` looks only in that process's tree (the binding already narrows the
    default). `while_waiting` runs on every poll that did not find the window.
    It is best effort: an error from it is kept for the timeout message, and
    never stops the search."""
    errors: list[BaseException] = []

    def find():
        tree = {} if pid is None else {"owned": tree_of(pid)}
        candidates = windows(profile, main_only=True, **tree)
        if candidates:
            return largest(candidates)["hwnd"]
        if while_waiting is not None:
            try:
                while_waiting()
            except Exception as exc:  # noqa: BLE001 - a helper must not stop the search
                errors[:] = [exc]
        return None

    try:
        return wait_until(
            find,
            timeout=profile.ready_timeout if timeout is None else timeout,
            what=f"{profile.name} main window" + (f" of pid {pid}" if pid is not None else ""),
        )
    except Timeout as exc:
        if not errors:
            raise
        raise Timeout(f"{exc} (while waiting: {type(errors[0]).__name__}: {errors[0]})") from exc


def strip_title(profile: Profile, title: str) -> str:
    for suffix in profile.title_strip:
        title = title.removesuffix(suffix)
    return title.strip()


BASE_DPI = 96  # the DPI at which one DIP (layout unit) is one pixel


def window_dpi(hwnd: int) -> int:
    """The DPI the window lays itself out at: its monitor's scale for a
    per-monitor-aware app such as Word. Falls back to the system DPI, then 96.
    On X11 there is one DPI for the screen (Xft.dpi)."""
    if not WINDOWS:
        return x11.dpi()
    with contextlib.suppress(AttributeError, OSError):
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return int(dpi)
    with contextlib.suppress(Exception):
        hdc = win32gui.GetDC(0)
        try:
            dpi = win32print.GetDeviceCaps(hdc, win32con.LOGPIXELSX)
        finally:
            win32gui.ReleaseDC(0, hdc)
        if dpi:
            return int(dpi)
    return BASE_DPI


def _coords_are_physical() -> bool:
    """Whether Win32 hands this thread physical pixels. `uiautomation` makes
    the process per-monitor aware on import, so it nearly always does; a
    DPI-unaware thread is given (and gives) 96-DPI coordinates instead.
    X11 has no DPI virtualization: always physical."""
    if not WINDOWS:
        return True
    with contextlib.suppress(AttributeError, OSError):
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        context = user32.GetThreadDpiAwarenessContext()
        return user32.GetAwarenessFromDpiAwarenessContext(context) != 0
    return True


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    """Left, top, right, bottom of the work area (the screen minus the
    taskbar) of the monitor the window is on."""
    if not WINDOWS:
        return x11.work_area(hwnd)
    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    return tuple(win32api.GetMonitorInfo(monitor)["Work"])


def _scale(hwnd: int) -> tuple[int, float, float]:
    """dpi, scale (DIP to physical), and unit: one Win32 coordinate of this
    thread in DIPs - physical pixels when DPI aware, else DIPs already."""
    dpi = window_dpi(hwnd)
    scale = dpi / BASE_DPI
    return dpi, scale, scale if _coords_are_physical() else 1.0


def layout(hwnd: int) -> dict:
    """The window's size in pixels and in DIPs, and the scale between them."""
    dpi, scale, unit = _scale(hwnd)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = (right - left) / unit, (bottom - top) / unit
    return {
        "dpi": dpi,
        "scale": scale,
        "physical": [round(width * scale), round(height * scale)],
        "dips": [round(width), round(height)],
    }


def resize(hwnd: int, width: int, height: int) -> dict:
    """Pin the window to `width` x `height` DIPs (96-DPI pixels), scaled by
    its DPI, so selectors meet the same layout at every display scale.

    A size that fits the monitor's work area keeps the window's place, moved
    just inside the work area. One that does not is cut to the work area and
    placed at its top-left: an explicit rect, never SW_MAXIMIZE, so a later
    restore does not bounce it.

    Returns the layout it got: dpi, scale, requested_dips, physical, dips,
    clamped, size_differs, work_area."""
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    dpi, scale, unit = _scale(hwnd)
    want_w, want_h = round(width * unit), round(height * unit)
    work_left, work_top, work_right, work_bottom = _work_area(hwnd)
    work_w, work_h = work_right - work_left, work_bottom - work_top
    clamped = want_w > work_w or want_h > work_h
    size_w, size_h = min(want_w, work_w), min(want_h, work_h)
    if clamped:
        left, top = work_left, work_top
    else:
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        left = max(work_left, min(left, work_right - size_w))
        top = max(work_top, min(top, work_bottom - size_h))
    win32gui.MoveWindow(hwnd, left, top, size_w, size_h, True)
    # What the window took, not what it was asked: an app may enforce a
    # minimum size or snap to its own grid.
    got_left, got_top, got_right, got_bottom = win32gui.GetWindowRect(hwnd)
    got_w, got_h = got_right - got_left, got_bottom - got_top
    dips_w, dips_h = got_w / unit, got_h / unit
    return {
        "dpi": dpi,
        "scale": scale,
        "requested_dips": [width, height],
        "physical": [round(dips_w * scale), round(dips_h * scale)],
        "dips": [round(dips_w), round(dips_h)],
        "clamped": clamped,
        # a pixel of rounding either way is the size it was given
        "size_differs": abs(got_w - size_w) > 1 or abs(got_h - size_h) > 1,
        "work_area": [work_left, work_top, work_w, work_h],
    }


def require_running(profile: Profile) -> list[int]:
    found = pids(profile)
    if not found:
        raise AppNotRunning(f"{profile.name} is not running")
    return found


# ----------------------------------------------------------------- hooks


def _hook_name(fn) -> str:
    return getattr(fn, "__name__", repr(fn))


def _run_hooks(ctx: AppContext, event: str) -> None:
    for fn in ctx.registry.hooks_for(ctx.name, event):
        fn(ctx)


def _try_hooks(ctx: AppContext, event: str) -> tuple[list, list[str]]:
    """Every hook of a best-effort event: (results, errors). A failure is
    named in the JSON, never raised and never dropped."""
    results: list = []
    errors: list[str] = []
    for fn in ctx.registry.hooks_for(ctx.name, event):
        try:
            results.append(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - best effort, reported
            errors.append(f"{_hook_name(fn)}: {type(exc).__name__}: {exc}")
    return results, errors


def _wait_ready(ctx: AppContext, profile: Profile) -> list[str]:
    """A ready hook returns truthy when the app is usable - add-ins loaded, a
    window painted. Polled, because "the process exists" is not "usable"."""
    ready: list[str] = []
    for fn in ctx.registry.hooks_for(ctx.name, "ready"):
        name = _hook_name(fn)
        wait_until(lambda fn=fn: fn(ctx), timeout=profile.ready_timeout, what="ready hook " + name)
        ready.append(name)
    return ready


def _answer_known_prompts(ctx: AppContext, profile: Profile) -> None:
    """A startup prompt can come before the main window - Word's "start in
    safe mode?" after a force-kill does - so post_launch is too late for it."""
    known = dialogs.policy(ctx)
    if known:
        dialogs.reap(pids(profile), known, classes=dialogs.dialog_classes(profile))


# --------------------------------------------------------------- commands


def _fresh_journal(ctx: AppContext) -> None:
    """A new run answers `fallback_used` for itself, not for the last one."""
    from . import fallback

    fallback.truncate(ctx.name)


def launch_env(profile: Profile) -> dict[str, str]:
    """The environment an app this tool starts gets: on Linux without the
    VS Code snap's leaks (linux procs.app_env), then the profile's `env`."""
    env = dict(os.environ) if WINDOWS else linux_procs.app_env(os.environ)
    # VS Code's terminal and extension host set it; an Electron app started
    # with it runs as plain Node. A profile may still set it on purpose.
    env.pop(linux_procs.ELECTRON_AS_NODE, None)
    return env | profile.env


def start_process(args: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    """Start an app detached from this process. On Linux in a session of its
    own, its output in `log`; on Windows `log` is unused."""
    if not WINDOWS:
        return linux_procs.spawn(args, env, log)
    # The system default error mode, not ours: Git Bash hands its children
    # SEM_NOGPFAULTERRORBOX, which stops WER, so a crash would leave no
    # event 1000 and no report for `events` to find.
    flags = subprocess.CREATE_DEFAULT_ERROR_MODE
    return subprocess.Popen(args, env=env, close_fds=True, creationflags=flags)


def alive(pid: int, started: float | None = None) -> bool:
    """Running, and - when `started` is known - created no later than it, so
    a reused pid does not count. On Linux a zombie is not running."""
    if not WINDOWS:
        return linux_procs.alive(pid, started)
    born = created(pid)
    return born is not None and (started is None or born <= started + 1.0)


def launch(
    ctx: AppContext,
    *,
    extra_args: list[str] | None = None,
    fresh: bool | None = None,
    pin_size: bool = True,
) -> dict:
    """Start the app, or adopt one already running. Returns its status plus
    adopted, killed, hwnd, ready and layout (the size it pinned, or null)."""
    profile = profile_of(ctx)
    if fresh is None:
        fresh = profile.launch == "fresh"
    bound = bound_pid(profile)
    if bound is not None:
        # A process this launch starts is outside the bound tree: nothing
        # would find its window, or kill it after. So it only adopts.
        binding = f"{binding_env(profile)}={bound}"
        if fresh:
            raise PilotError(f"{binding} is set: a bound launch only adopts; unset it for --fresh")
        if not pids(profile):
            raise AppNotRunning(f"{binding} is set and that {profile.name} is not running")
        return _adopt(ctx, profile)

    killed: list[int] = []
    if fresh:
        killed = kill(ctx)["killed"]
    elif pids(profile):
        return _adopt(ctx, profile)
    # After the kill: a refused launch must not wipe the last run's journal.
    _fresh_journal(ctx)

    _run_hooks(ctx, "pre_launch")
    exe = profile.resolve_exe()
    args = [str(exe), *profile.args, *(extra_args or [])]
    proc = start_process(args, launch_env(profile), app_log(profile))

    session = Session(app=ctx.name, pid=proc.pid, started_at=time.time(), launched=True)
    session.save(profile)
    try:
        hwnd = main_hwnd(profile, while_waiting=lambda: _answer_known_prompts(ctx, profile))
        # A launcher stub hands off: ours is the process that owns the window.
        _, owner = win32process.GetWindowThreadProcessId(hwnd)
        if owner and owner != session.pid:
            session.pid = owner
            session.save(profile)
        pinned = resize(hwnd, *profile.window_size) if pin_size and profile.window_size else None
        _run_hooks(ctx, "post_launch")
        ready = _wait_ready(ctx, profile)
    except BaseException:
        # This launch started the process, so a failed start leaves an app
        # nobody owns - and the next launch inherits its half-broken state.
        # force, because it is ours: no foreign instance is being spared.
        with contextlib.suppress(Exception):
            kill(ctx, force=True)
        raise
    return status(ctx) | {
        "adopted": False,
        "killed": killed,
        "hwnd": hwnd,
        "ready": ready,
        "layout": pinned,
    }


def _adopt(ctx: AppContext, profile: Profile) -> dict:
    """Join a running instance: it must pass the same ready checks as one this
    launch started. Its window is someone else's, so it is never resized:
    layout is null, and `window` says whether it matches the profile."""
    joined = attach(ctx)
    ready = _wait_ready(ctx, profile)
    return joined | {"adopted": True, "killed": [], "ready": ready, "layout": None}


def attach(ctx: AppContext) -> dict:
    """Join a running instance. One this tool launched stays ours: its saved
    pid, if still running, is kept, or a later kill would refuse our own app
    as foreign. Otherwise the instances running now are recorded in `pids`:
    driven from here on (owned_pids), never clicked in (launched_here)."""
    profile = profile_of(ctx)
    _fresh_journal(ctx)
    running = require_running(profile)
    hwnd = main_hwnd(profile)
    try:
        saved = Session.load(profile)
    except PilotError:
        saved = None  # corrupt: re-recorded below as foreign, the safe side
    if saved is None or saved.pid is None or saved.pid not in running:
        Session(app=ctx.name, pid=None, started_at=time.time(), launched=False, pids=running).save(
            profile
        )
    return status(ctx) | {"hwnd": hwnd}


def kill(ctx: AppContext, *, force: bool = False, timeout: float = 20.0) -> dict:
    """Quit hooks first so the app can shut down cleanly; force-kill what is
    left. Returns killed (the instances) and quit_errors.

    A pre_kill hook may refuse: the kill takes every process of the image, so
    killing "the app" can take down an instance this tool never started, and
    someone else's unsaved work is not ours to destroy. `force` skips the
    refusal, and the graceful quit too: on a busy or orphaned app that quit only
    waits out its timeouts (~50 s for Word) before the kill happens anyway.

    Bound to a pid (PILOT_<APP>_PID), the kill takes that instance's tree
    only, and keeps the session file of another instance.

    This process and its ancestors are never killed, even when they run the
    app's image; with nothing else left, the kill is a no-op. On Linux the
    kill is SIGTERM, then SIGKILL, and an instance this process runs inside
    is spared whole (see _spared).
    """
    profile = profile_of(ctx)
    spared = _spared(profile)

    def targets() -> list[int]:
        return [pid for pid in all_pids(profile) if pid not in spared]

    running = [pid for pid in pids(profile) if pid not in spared]
    quit_errors: list[str] = []
    if running:
        for fn in ctx.registry.hooks_for(ctx.name, "pre_kill"):
            fn(ctx, pids=running, force=force)
        if not force:
            _, quit_errors = _try_hooks(ctx, "quit")
        procs.taskkill(targets())
        wait_until(lambda: not targets(), timeout=timeout, what=f"{profile.name} to exit")
    if bound_pid(profile) is None or _session_among(profile, running):
        Session.path(profile).unlink(missing_ok=True)
    return {"killed": running, "quit_errors": quit_errors}


def _session_among(profile: Profile, killed: list[int]) -> bool:
    """Whether the saved session is of the instances just killed. A bound
    kill takes one instance only: the session of another one stays."""
    try:
        saved = Session.load(profile)
    except PilotError:
        return False
    if saved is None:
        return False
    return bool({saved.pid, *(saved.pids or [])} & set(killed))


def _status_extras(ctx: AppContext) -> tuple[dict, list[str]]:
    """What only the app's own plugin can answer - Word's add-ins and open
    documents - and which hooks failed. Never fatal: status is what you read
    when the app misbehaves."""
    results, errors = _try_hooks(ctx, "status")
    extra: dict = {}
    for result in results:
        extra |= result or {}
    return extra, errors


def _main_window_layout(profile: Profile, found: list[dict]) -> dict | None:
    """The main window's layout now, next to the profile's window_size: the
    display scale can change under a running app (an RDP reconnect from
    another client), and a stale scale reshapes the app's layout as surely
    as a hand resize. Never fatal."""
    mains = [w for w in found if w["is_main"]]
    if not mains:
        return None
    hwnd = largest(mains)["hwnd"]
    try:
        now = layout(hwnd)
    except Exception as exc:  # noqa: BLE001 - status is what you read when the app misbehaves
        return {"hwnd": hwnd, "error": f"{type(exc).__name__}: {exc}"}
    want = profile.window_size
    return {
        "hwnd": hwnd,
        **now,
        "requested_dips": want,
        # a DIP of rounding either way is the same layout
        "matches_profile": None
        if not want
        else all(abs(have - w) <= 1 for have, w in zip(now["dips"], want, strict=True)),
    }


def status(ctx: AppContext) -> dict:
    """app, profile, running, pids, working_set_mb, session, session_error,
    status_errors, bound_pid, bound_by, window, windows - plus what the app's
    status hooks add."""
    profile = profile_of(ctx)
    try:
        saved, session_error = Session.load(profile), None
    except PilotError as exc:
        saved, session_error = None, str(exc)
    bound = bound_pid(profile)
    rows = _rows(profile)  # once: memory and windows share it
    working_set = memory(profile, rows)
    running = list(working_set)
    found = windows(profile, owned={procs.pid_of(row) for row in rows})
    extras, status_errors = _status_extras(ctx)
    return extras | {
        "app": ctx.name,
        "profile": profile.name,
        "running": bool(running),
        "pids": running,
        "working_set_mb": {pid: round(size / 2**20, 1) for pid, size in working_set.items()},
        "session": asdict(saved) if saved else None,
        "session_error": session_error,
        "status_errors": status_errors,
        "bound_pid": bound,
        "bound_by": binding_env(profile) if bound is not None else None,
        "window": _main_window_layout(profile, found),
        "windows": [w | {"title_stripped": strip_title(profile, w["title"])} for w in found],
    }
