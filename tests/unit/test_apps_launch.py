"""A launch that cannot finish must not leave the app it started running."""

from __future__ import annotations

import os
import sys

import pytest

from desktoppilot.core import apps, paths
from desktoppilot.core.errors import PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry


class FakeProcess:
    pid = 4242

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """A profile with a real exe, a stubbed Popen and a recorded kill."""
    exe = tmp_path / "fake.exe"
    exe.write_bytes(b"")
    profile = Profile(name="fake", exe=[str(exe)], ready_timeout=0.2)
    reg = Registry()
    ctx = AppContext(name="fake", profile=profile, registry=reg)

    killed: list[bool] = []
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(apps, "pids", lambda profile: [])
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 99)
    monkeypatch.setattr(
        apps.win32process, "GetWindowThreadProcessId", lambda hwnd: (0, FakeProcess.pid)
    )
    monkeypatch.setattr(
        apps,
        "kill",
        lambda ctx, force=False, **k: killed.append(force) or {"killed": [1], "quit_errors": []},
    )
    monkeypatch.setattr(apps, "status", lambda ctx: {"app": ctx.name})
    return ctx, reg, killed


def test_a_ready_hook_that_times_out_kills_what_this_launch_started(launcher):
    ctx, reg, killed = launcher
    reg.hook("ready", lambda ctx: False, for_app="fake")

    with pytest.raises(PilotError):
        apps.launch(ctx, pin_size=False)

    assert killed == [True], "the half-started app was left running"


def test_a_failing_post_launch_hook_kills_what_this_launch_started(launcher):
    ctx, reg, killed = launcher

    def explode(ctx):
        raise RuntimeError("no")

    reg.hook("post_launch", explode, for_app="fake")

    with pytest.raises(RuntimeError):
        apps.launch(ctx, pin_size=False)

    assert killed == [True]


def test_a_launch_that_works_kills_nothing(launcher):
    ctx, reg, killed = launcher
    reg.hook("ready", lambda ctx: True, for_app="fake")

    result = apps.launch(ctx, pin_size=False)

    assert killed == []
    assert result["hwnd"] == 99 and result["adopted"] is False


@pytest.mark.windows("the default error mode (CREATE_DEFAULT_ERROR_MODE)")
def test_the_app_gets_the_default_error_mode_so_its_crashes_reach_wer(launcher, monkeypatch):
    ctx, reg, killed = launcher
    started: list[dict] = []
    monkeypatch.setattr(
        apps.subprocess, "Popen", lambda *a, **k: started.append(k) or FakeProcess()
    )

    apps.launch(ctx, pin_size=False)

    assert started[0]["creationflags"] & apps.subprocess.CREATE_DEFAULT_ERROR_MODE


def test_attach_after_a_launch_keeps_the_app_ours(launcher, monkeypatch):
    ctx, reg, killed = launcher
    reg.hook("ready", lambda ctx: True, for_app="fake")
    apps.launch(ctx, pin_size=False)
    launched = apps.Session.load(ctx.profile)
    monkeypatch.setattr(apps, "pids", lambda profile: [FakeProcess.pid])

    apps.attach(ctx)

    assert apps.Session.load(ctx.profile) == launched
    assert launched.pid == FakeProcess.pid and launched.launched is True


def test_attach_to_an_instance_this_tool_did_not_start_leaves_it_foreign(launcher, monkeypatch):
    ctx, _, _ = launcher
    monkeypatch.setattr(apps, "pids", lambda profile: [777])

    apps.attach(ctx)

    saved = apps.Session.load(ctx.profile)
    assert saved.pid is None and saved.launched is False


def test_attach_after_our_launch_exited_leaves_the_new_instance_foreign(launcher, monkeypatch):
    ctx, _, _ = launcher
    apps.Session(app="fake", pid=FakeProcess.pid, started_at=1.0, launched=True).save(ctx.profile)
    monkeypatch.setattr(apps, "pids", lambda profile: [777])

    apps.attach(ctx)

    saved = apps.Session.load(ctx.profile)
    assert saved.pid is None and saved.launched is False


def test_a_known_prompt_before_the_main_window_is_answered_while_waiting(launcher, monkeypatch):
    ctx, reg, killed = launcher
    ctx.profile.dialogs = "auto"
    safe_mode = (("safe mode", ("No",)),)
    reg.hook("dialog_policy", lambda ctx: safe_mode, for_app="fake")
    answered: list = []
    monkeypatch.setattr(apps.dialogs, "reap", lambda pids, prompts, **k: answered.append(prompts))

    def main_hwnd(profile, *, timeout=None, while_waiting=None):
        while_waiting()
        return 99

    monkeypatch.setattr(apps, "main_hwnd", main_hwnd)

    apps.launch(ctx, pin_size=False)

    assert answered == [safe_mode]


def test_main_hwnd_runs_while_waiting_after_every_look_that_missed(monkeypatch):
    shown = iter([[], [], [{"hwnd": 7, "rect": [0, 0, 10, 10]}]])
    monkeypatch.setattr(apps, "windows", lambda profile, main_only=False: next(shown))
    polls: list[int] = []

    hwnd = apps.main_hwnd(Profile(name="fake"), timeout=5, while_waiting=lambda: polls.append(1))

    assert hwnd == 7 and len(polls) == 2


class FakeRun:
    def __init__(self, stdout: str):
        self.stdout = stdout


def fake_tasklist(
    monkeypatch, stdout: str, parents: dict | None = None, created: dict | None = None
) -> Profile:
    monkeypatch.setattr(apps.subprocess, "run", lambda *a, **k: FakeRun(stdout))
    monkeypatch.setattr(apps, "_parents", lambda: parents or {})
    monkeypatch.setattr(apps, "created", lambda pid: (created or {}).get(pid))
    return Profile(name="word", exe=[r"C:\Office\WINWORD.EXE"])


WORD_AND_ITS_CHILD = (
    '"WINWORD.EXE","14608","Console","1","400,000 K"\n'
    '"WINWORD.EXE","12920","Console","1","90,000 K"\n'
)


@pytest.mark.windows("tasklist and taskkill")
def test_a_word_started_by_the_running_word_is_not_another_instance(monkeypatch):
    # Word's Error Reporting snapshot of itself: its parent is the Word it clones.
    profile = fake_tasklist(
        monkeypatch,
        WORD_AND_ITS_CHILD,
        parents={14608: 16780, 12920: 14608},
        created={14608: 100.0, 12920: 108.0},
    )

    assert apps.pids(profile) == [14608]
    assert apps.memory(profile) == {14608: 400_000 * 1024}
    assert apps.all_pids(profile) == [14608, 12920]


@pytest.mark.windows("tasklist and taskkill")
def test_two_words_started_by_someone_else_are_two_instances(monkeypatch):
    profile = fake_tasklist(
        monkeypatch,
        WORD_AND_ITS_CHILD,
        parents={14608: 16780, 12920: 5000},
        created={14608: 100.0, 12920: 108.0},
    )

    assert apps.pids(profile) == [14608, 12920]


@pytest.mark.windows("tasklist and taskkill")
def test_a_parent_pid_reused_by_a_later_word_does_not_hide_an_instance(monkeypatch):
    profile = fake_tasklist(
        monkeypatch,
        WORD_AND_ITS_CHILD,
        parents={14608: 16780, 12920: 14608},
        created={14608: 200.0, 12920: 108.0},
    )

    assert apps.pids(profile) == [14608, 12920]


def test_parents_reads_the_real_process_table():
    assert apps._parents()[os.getpid()] == os.getppid()


@pytest.mark.windows("tasklist and taskkill")
def test_kill_waits_for_the_child_of_an_instance_too(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = Profile(name="word", exe=[r"C:\Office\WINWORD.EXE"])
    ctx = AppContext(name="word", profile=profile, registry=Registry())
    listings = iter([[14608], [14608, 12920], [12920], []])
    killed: list = []
    monkeypatch.setattr(apps, "pids", lambda profile: [14608])
    monkeypatch.setattr(apps, "all_pids", lambda profile: next(listings))
    monkeypatch.setattr(apps.subprocess, "run", lambda args, **k: killed.append(args))
    monkeypatch.setattr(apps, "_self_and_ancestors", lambda: {1, 2})

    assert apps.kill(ctx, force=True)["killed"] == [14608]
    assert killed == [["taskkill", "/F", "/T", "/PID", "14608"]]


def python_kill(monkeypatch, tmp_path, running: list[int]) -> tuple[AppContext, list]:
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = Profile(name="python", exe=[r"C:\Python\python.exe"])
    ctx = AppContext(name="python", profile=profile, registry=Registry())
    alive = set(running)
    killed: list = []

    def taskkill(args, **k):
        killed.append(args)
        alive.difference_update(int(a) for a in args if a.isdecimal())

    monkeypatch.setattr(apps, "pids", lambda profile: sorted(alive))
    monkeypatch.setattr(apps, "all_pids", lambda profile: sorted(alive))
    monkeypatch.setattr(apps.subprocess, "run", taskkill)
    return ctx, killed


@pytest.mark.windows("tasklist and taskkill")
def test_kill_never_targets_this_process_or_its_parent(monkeypatch, tmp_path):
    me, parent = os.getpid(), os.getppid()
    ctx, killed = python_kill(monkeypatch, tmp_path, [me, parent, 999_991])

    assert apps.kill(ctx, force=True)["killed"] == [999_991]
    assert killed == [["taskkill", "/F", "/T", "/PID", "999991"]]


def test_kill_with_only_this_process_left_is_a_no_op(monkeypatch, tmp_path):
    ctx, killed = python_kill(monkeypatch, tmp_path, [os.getpid(), os.getppid()])

    assert apps.kill(ctx, force=True)["killed"] == []
    assert killed == []


def test_self_and_ancestors_follows_the_parent_chain_and_stops_on_a_loop(monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(apps, "_parents", lambda: {me: 20, 20: 30, 30: 20, 40: me})

    assert apps._self_and_ancestors() == {me, 20, 30}


@pytest.mark.windows("tasklist and taskkill")
def test_memory_reads_the_working_set_of_every_pid(monkeypatch):
    profile = fake_tasklist(
        monkeypatch,
        '"WINWORD.EXE","4242","Console","1","1,234,567 K"\n'
        '"WINWORD.EXE","77","Console","1","348,160 K"\n',
    )

    assert apps.pids(profile) == [4242, 77]
    assert apps.memory(profile) == {4242: 1_234_567 * 1024, 77: 348_160 * 1024}


@pytest.mark.windows("tasklist and taskkill")
def test_memory_reads_digits_only_whatever_the_locale_separator(monkeypatch):
    profile = fake_tasklist(monkeypatch, '"WINWORD.EXE","4242","Console","1","1.234.567 K"\n')

    assert apps.memory(profile) == {4242: 1_234_567 * 1024}


def test_memory_is_empty_when_the_app_is_not_running(monkeypatch):
    profile = fake_tasklist(
        monkeypatch, "INFO: No tasks are running which match the specified criteria.\n"
    )

    assert apps.pids(profile) == []
    assert apps.memory(profile) == {}


# ----------------------------------------------------------------- resize


class FakeWindow:
    """The Win32 calls resize makes, on one window whose rect it records."""

    def __init__(self, rect):
        self.rect = list(rect)  # left, top, right, bottom
        self.moves: list[tuple[int, int, int, int]] = []
        self.shown: list[int] = []

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)

    def GetWindowRect(self, hwnd):
        return tuple(self.rect)

    def MoveWindow(self, hwnd, left, top, width, height, repaint):
        self.moves.append((left, top, width, height))
        self.rect = [left, top, left + width, top + height]


@pytest.fixture
def screen(monkeypatch):
    """Set the window's DPI, its monitor's work area and where it starts."""

    def make(dpi, work, rect=(100, 50, 900, 650), physical=True):
        window = FakeWindow(rect)
        for name in ("ShowWindow", "GetWindowRect", "MoveWindow"):
            monkeypatch.setattr(apps.win32gui, name, getattr(window, name))
        monkeypatch.setattr(apps, "window_dpi", lambda hwnd: dpi)
        monkeypatch.setattr(apps, "_coords_are_physical", lambda: physical)
        monkeypatch.setattr(apps.win32api, "MonitorFromWindow", lambda hwnd, flags: "monitor")
        monkeypatch.setattr(apps.win32api, "GetMonitorInfo", lambda monitor: {"Work": work})
        return window

    return make


def inside(rect, work) -> bool:
    left, top, right, bottom = rect
    return left >= work[0] and top >= work[1] and right <= work[2] and bottom <= work[3]


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_at_96_dpi_a_dip_is_a_pixel(screen):
    window = screen(96, (0, 0, 1920, 1040))

    got = apps.resize(7, 1600, 1000)

    assert window.moves[-1][2:] == (1600, 1000)
    assert got["scale"] == 1.0 and got["physical"] == [1600, 1000]
    assert got["dips"] == [1600, 1000] and got["clamped"] is False
    # it started at top 50: 1000 tall would end at 1050, below the work area
    assert window.moves[-1][:2] == (100, 40) and inside(window.rect, (0, 0, 1920, 1040))


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_at_150_percent_the_size_is_scaled_to_the_same_layout(screen):
    work = (0, 0, 2560, 1528)
    window = screen(144, work, rect=(300, 200, 1900, 1200))

    got = apps.resize(7, 1600, 1000)

    assert window.moves[-1] == (160, 28, 2400, 1500)
    assert inside(window.rect, work)
    assert got == {
        "dpi": 144,
        "scale": 1.5,
        "requested_dips": [1600, 1000],
        "physical": [2400, 1500],
        "dips": [1600, 1000],
        "clamped": False,
        "size_differs": False,
        "work_area": [0, 0, 2560, 1528],
    }


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_a_window_that_already_fits_keeps_its_place(screen):
    window = screen(144, (0, 0, 2560, 1528), rect=(20, 10, 900, 600))

    apps.resize(7, 1600, 1000)

    assert window.moves[-1] == (20, 10, 2400, 1500)


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_a_screen_too_small_for_the_scaled_size_clamps_it_and_says_so(screen):
    work = (0, 0, 1920, 1040)
    window = screen(144, work, rect=(300, 200, 1900, 1200))

    got = apps.resize(7, 1600, 1000)

    assert got["clamped"] is True
    assert window.moves[-1] == (0, 0, 1920, 1040)
    assert inside(window.rect, work)
    assert got["physical"] == [1920, 1040] and got["dips"] == [1280, 693]


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_only_the_dimension_that_does_not_fit_is_cut(screen):
    window = screen(144, (0, 0, 2560, 1400))

    got = apps.resize(7, 1600, 1000)

    assert got["clamped"] is True
    assert window.moves[-1] == (0, 0, 2400, 1400)
    assert got["dips"] == [1600, 933]


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_the_window_is_moved_inside_a_work_area_off_the_origin(screen):
    # a second monitor left of the primary, taskbar at its top
    work = (-2560, 40, 0, 1600)
    window = screen(144, work, rect=(-200, -100, 600, 500))

    got = apps.resize(7, 1600, 1000)

    assert got["clamped"] is False
    assert window.moves[-1] == (-2400, 40, 2400, 1500)
    assert inside(window.rect, work)


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_a_dpi_unaware_caller_moves_in_dips_but_reports_physical(screen):
    window = screen(144, (0, 0, 1707, 1019), physical=False)

    got = apps.resize(7, 1600, 1000)

    assert window.moves[-1][2:] == (1600, 1000)
    assert got["physical"] == [2400, 1500] and got["clamped"] is False


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_resize_restores_a_maximized_window_first(screen):
    window = screen(96, (0, 0, 1920, 1040))

    apps.resize(7, 1600, 1000)

    assert window.shown == [apps.win32con.SW_RESTORE]


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_window_dpi_falls_back_to_96_when_nothing_answers(monkeypatch):
    class NoWindll:
        @property
        def windll(self):
            raise AttributeError("windll")

    def no_dc(hwnd):
        raise OSError("no dc")

    monkeypatch.setattr(apps, "ctypes", NoWindll())
    monkeypatch.setattr(apps.win32gui, "GetDC", no_dc)

    assert apps.window_dpi(7) == 96


def test_the_launch_result_carries_the_layout_it_pinned(launcher, monkeypatch):
    ctx, reg, killed = launcher
    ctx.profile.window_size = [1600, 1000]
    pinned = {"dpi": 144, "scale": 1.5, "dips": [1600, 1000], "clamped": False}
    calls: list = []
    monkeypatch.setattr(apps, "resize", lambda hwnd, w, h: calls.append((hwnd, w, h)) or pinned)

    result = apps.launch(ctx)

    assert calls == [(99, 1600, 1000)]
    assert result["layout"] == pinned


def test_a_launch_that_does_not_pin_says_so(launcher):
    ctx, reg, killed = launcher
    ctx.profile.window_size = [1600, 1000]

    assert apps.launch(ctx, pin_size=False)["layout"] is None


def test_status_reports_the_main_window_layout_against_the_profile(monkeypatch):
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: [])
    profile = Profile(name="fake", window_size=[1600, 1000])
    ctx = AppContext(name="fake", profile=profile, registry=Registry())
    monkeypatch.setattr(apps, "memory", lambda profile, rows=None: {4242: 0})
    monkeypatch.setattr(
        apps,
        "windows",
        lambda profile, **k: [
            {"hwnd": 5, "title": "stub", "is_main": True, "rect": [0, 0, 10, 10]},
            {"hwnd": 7, "title": "Doc - Word", "is_main": True, "rect": [0, 0, 1600, 1000]},
            {"hwnd": 9, "title": "pane", "is_main": False, "rect": [0, 0, 3000, 3000]},
        ],
    )
    # the scale went from 150% to 100% under it: 2400 physical is 2400 DIPs now
    monkeypatch.setattr(
        apps,
        "layout",
        lambda hwnd: {"dpi": 96, "scale": 1.0, "physical": [2400, 1500], "dips": [2400, 1500]},
    )

    window = apps.status(ctx)["window"]

    assert window["hwnd"] == 7
    assert window["requested_dips"] == [1600, 1000]
    assert window["matches_profile"] is False


def test_status_window_is_none_without_a_main_window(monkeypatch):
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: [])
    ctx = AppContext(name="fake", profile=Profile(name="fake"), registry=Registry())
    monkeypatch.setattr(apps, "memory", lambda profile, rows=None: {})
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])

    assert apps.status(ctx)["window"] is None


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_layout_converts_the_window_rect_to_dips(screen):
    screen(144, (0, 0, 2560, 1528), rect=(10, 10, 2410, 1510))

    assert apps.layout(7) == {
        "dpi": 144,
        "scale": 1.5,
        "physical": [2400, 1500],
        "dips": [1600, 1000],
    }


@pytest.mark.windows("per-monitor DPI and the work area through Win32")
def test_resize_reports_the_size_the_window_took_not_the_one_it_asked(screen, monkeypatch):
    window = screen(96, (0, 0, 1920, 1040))

    def move_with_a_minimum(hwnd, left, top, width, height, repaint):
        window.moves.append((left, top, width, height))
        window.rect = [left, top, left + max(width, 1700), top + height]

    monkeypatch.setattr(apps.win32gui, "MoveWindow", move_with_a_minimum)
    got = apps.resize(7, 1600, 1000)

    assert got["dips"] == [1700, 1000] and got["physical"] == [1700, 1000]
    assert got["size_differs"] is True


# ------------------------------------------------- session key and errors


def word_like(name: str) -> Profile:
    return Profile(name=name, exe=[r"C:\Office\WINWORD.EXE"])


@pytest.mark.windows("a Windows exe path as the image name")
def test_the_session_is_keyed_by_the_image_so_word_and_its_child_share_it(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    apps.Session(app="word", pid=4242, started_at=1.0, launched=True).save(word_like("word"))

    assert apps.Session.path(word_like("wordchild")).name == "WINWORD.EXE.json"
    assert apps.Session.load(word_like("wordchild")).pid == 4242


def test_a_corrupt_session_file_is_reported_by_status_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session.path(profile).parent.mkdir(parents=True)
    apps.Session.path(profile).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: [])
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])
    ctx = AppContext(name="word", profile=profile, registry=Registry())

    status = apps.status(ctx)

    assert status["session"] is None
    assert "WINWORD.EXE.json" in status["session_error"]
    with pytest.raises(PilotError, match="corrupt"):
        apps.Session.load(profile)


@pytest.mark.windows("tasklist and taskkill")
def test_status_runs_tasklist_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: calls.append(image) or [])
    monkeypatch.setattr(apps.win32gui, "EnumWindows", lambda fn, arg: None)
    ctx = AppContext(name="word", profile=word_like("word"), registry=Registry())

    apps.status(ctx)

    assert calls == ["WINWORD.EXE"]


def test_a_failing_status_hook_is_named_in_status_errors(monkeypatch):
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: [])
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])
    reg = Registry()

    def broken_extras(ctx):
        raise OSError("COM busy")

    reg.hook("status", broken_extras, for_app="word")
    reg.hook("status", lambda ctx: {"extra": 1}, for_app="word")
    ctx = AppContext(name="word", profile=word_like("word"), registry=reg)

    status = apps.status(ctx)

    assert status["extra"] == 1
    assert status["status_errors"] == ["broken_extras: OSError: COM busy"]


def test_a_failing_quit_hook_is_named_in_quit_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    reg = Registry()

    def quit_word(ctx):
        raise RuntimeError("RPC unavailable")

    reg.hook("quit", quit_word, for_app="word")
    ctx = AppContext(name="word", profile=word_like("word"), registry=reg)
    alive = [14608]
    monkeypatch.setattr(apps, "pids", lambda profile: list(alive))
    monkeypatch.setattr(apps, "all_pids", lambda profile: list(alive))
    monkeypatch.setattr(apps.procs, "taskkill", lambda pids: alive.clear())
    monkeypatch.setattr(apps, "_self_and_ancestors", lambda: {1})

    result = apps.kill(ctx)

    assert result == {
        "killed": [14608],
        "quit_errors": ["quit_word: RuntimeError: RPC unavailable"],
    }


def test_pre_kill_hooks_take_the_context_and_keywords(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    seen: list = []
    reg = Registry()
    reg.hook("pre_kill", lambda ctx, **kw: seen.append((ctx.name, kw)), for_app="word")
    ctx = AppContext(name="word", profile=word_like("word"), registry=reg)
    alive = [7]
    monkeypatch.setattr(apps, "pids", lambda profile: list(alive))
    monkeypatch.setattr(apps, "all_pids", lambda profile: list(alive))
    monkeypatch.setattr(apps.procs, "taskkill", lambda pids: alive.clear())
    monkeypatch.setattr(apps, "_self_and_ancestors", lambda: {1})

    apps.kill(ctx, force=True)

    assert seen == [("word", {"pids": [7], "force": True})]


def test_a_refused_fresh_launch_keeps_the_fallback_journal(launcher, monkeypatch):
    from desktoppilot.core import fallback

    ctx, reg, killed = launcher
    fallback.record(ctx, "coordinates", "ui click", "1,2")

    def refuse(ctx, **kw):
        raise PilotError("a foreign instance holds a document")

    monkeypatch.setattr(apps, "kill", lambda ctx, **k: refuse(ctx))

    with pytest.raises(PilotError):
        apps.launch(ctx, fresh=True)

    assert fallback.used("fake")


@pytest.mark.windows("tasklist and taskkill")
def test_an_unknown_creation_time_does_not_hide_an_instance(monkeypatch):
    profile = fake_tasklist(
        monkeypatch,
        WORD_AND_ITS_CHILD,
        parents={14608: 16780, 12920: 14608},
        created={14608: 100.0},  # the second one's creation time is unreadable
    )

    assert apps.pids(profile) == [14608, 12920]


def test_an_adopted_launch_reports_no_pinned_layout(launcher, monkeypatch):
    ctx, reg, killed = launcher
    monkeypatch.setattr(apps, "pids", lambda profile: [11])
    monkeypatch.setattr(apps, "status", lambda ctx: {"app": ctx.name, "window": {"hwnd": 5}})

    result = apps.launch(ctx, fresh=False)

    assert result["adopted"] is True and result["layout"] is None


@pytest.mark.windows("tasklist and taskkill")
def test_state_runs_tasklist_once(monkeypatch):
    from desktoppilot.core import state as state_mod

    calls: list[str] = []
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: calls.append(image) or [])
    monkeypatch.setattr(apps.win32gui, "EnumWindows", lambda fn, arg: None)
    monkeypatch.setattr(state_mod.dialogs_mod, "dialog_windows", lambda pids, **k: [])
    monkeypatch.setattr(state_mod, "_foreground", lambda pids: {})
    ctx = AppContext(name="word", profile=word_like("word"), registry=Registry())

    state_mod.snapshot(ctx)

    assert calls == ["WINWORD.EXE"]


def test_the_session_records_the_pid_that_owns_the_main_window(launcher, monkeypatch):
    """A launcher stub (Win11 Notepad) hands off: the window's owner is ours."""
    ctx, reg, killed = launcher
    monkeypatch.setattr(FakeProcess, "pid", 1)
    monkeypatch.setattr(apps.win32process, "GetWindowThreadProcessId", lambda hwnd: (0, 2))

    apps.launch(ctx, pin_size=False)

    saved = apps.Session.load(ctx.profile)
    assert saved.pid == 2 and saved.launched is True
    monkeypatch.setattr(apps, "pids", lambda profile: [2])
    assert apps.launched_here(ctx.profile)


# ------------------------------------------------------------ whose instance


def test_attach_to_a_foreign_instance_records_the_pids_running_then(launcher, monkeypatch):
    ctx, _, _ = launcher
    monkeypatch.setattr(apps, "pids", lambda profile: [777, 778])

    apps.attach(ctx)

    assert apps.Session.load(ctx.profile).pids == [777, 778]


def test_owned_pids_is_the_launched_instance_while_it_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session(app="word", pid=7, started_at=100.0, launched=True).save(profile)

    assert apps.owned_pids(profile, running=[99, 7]) == [7]
    assert apps.owned_pids(profile, running=[99]) == []


def test_owned_pids_after_an_adopt_is_exactly_the_recorded_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session(app="word", pid=None, started_at=100.0, launched=False, pids=[7]).save(profile)
    # 99 is older than the adopt but was not running then: not ours.
    monkeypatch.setattr(apps, "created", {7: 50.0, 99: 60.0}.get)

    assert apps.owned_pids(profile, running=[7, 99]) == [7]


def test_a_recorded_pid_that_was_reused_after_the_adopt_is_not_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session(app="word", pid=None, started_at=100.0, launched=False, pids=[7]).save(profile)
    monkeypatch.setattr(apps, "created", {7: 150.0}.get)

    assert apps.owned_pids(profile, running=[7]) == []


def test_an_adopt_session_without_pids_falls_back_to_creation_time(tmp_path, monkeypatch):
    """A session file written before `pids` existed still loads."""
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session.path(profile).parent.mkdir(parents=True)
    apps.Session.path(profile).write_text(
        '{"app": "word", "pid": null, "started_at": 100.0, "launched": false}', encoding="utf-8"
    )
    monkeypatch.setattr(apps, "created", {7: 50.0, 99: 150.0}.get)

    assert apps.owned_pids(profile, running=[7, 99]) == [7]


def test_with_no_session_no_instance_is_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")

    assert apps.owned_pids(word_like("word"), running=[7]) == []


def test_an_adopted_instance_is_owned_but_not_launched_here(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    profile = word_like("word")
    apps.Session(app="word", pid=None, started_at=100.0, launched=False, pids=[7]).save(profile)
    monkeypatch.setattr(apps, "pids", lambda profile: [7])
    monkeypatch.setattr(apps, "created", {7: 50.0}.get)

    assert apps.owned_pids(profile) == [7]
    assert apps.launched_here(profile) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_on_linux_an_instance_this_runs_inside_is_spared_whole(monkeypatch):
    """`pilot code app kill` from VS Code's terminal: the editor's main process
    is an ancestor, its renderers are not, and none of them may die."""
    tree = {os.getpid(): 30, 30: 20, 20: 10, 10: 1, 11: 10, 12: 10, 50: 1}
    images = {10: "code", 20: "code", 11: "code", 12: "code", 50: "code", 30: "bash"}
    monkeypatch.setattr(apps, "_parents", lambda: tree)
    monkeypatch.setattr(apps.linux_procs, "parents", lambda: tree)
    monkeypatch.setattr(apps.linux_procs, "image", lambda pid: images.get(pid, "python"))

    spared = apps._spared(Profile(name="code", exe=["/snap/bin/code"]))

    assert {10, 11, 12, 20, 30, os.getpid()} <= spared and 50 not in spared


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the snap environment")
def test_on_linux_the_launch_drops_the_snaps_environment(launcher, monkeypatch):
    ctx, _, _ = launcher
    seen = {}
    monkeypatch.setenv("NO_AT_BRIDGE", "1")
    monkeypatch.setenv("XDG_DATA_DIRS_VSCODE_SNAP_ORIG", "/usr/share")
    monkeypatch.setattr(
        apps.linux_procs, "spawn", lambda args, env, log: seen.update(env) or FakeProcess()
    )
    ctx.profile.env = {"MY_FLAG": "1"}

    apps.launch(ctx, pin_size=False)

    assert "NO_AT_BRIDGE" not in seen and seen["XDG_DATA_DIRS"] == "/usr/share"
    assert seen["MY_FLAG"] == "1"


@pytest.mark.parametrize("windows", [True, False])
def test_the_launch_drops_electron_run_as_node(launcher, monkeypatch, windows):
    """A pilot run from VS Code's extension host must not start Electron as Node."""
    ctx, _, _ = launcher
    seen = {}
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr(apps, "WINDOWS", windows)
    monkeypatch.setattr(apps.subprocess, "CREATE_DEFAULT_ERROR_MODE", 0, raising=False)
    monkeypatch.setattr(
        apps.subprocess, "Popen", lambda args, env, **k: seen.update(env) or FakeProcess()
    )
    monkeypatch.setattr(
        apps.linux_procs, "spawn", lambda args, env, log: seen.update(env) or FakeProcess()
    )

    apps.launch(ctx, pin_size=False)

    assert seen and "ELECTRON_RUN_AS_NODE" not in seen
