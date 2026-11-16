"""Word's roots against fake UIA trees: which add-in pane `pane:TITLE` means,
the ribbon's error when a tab is missing, and which untitled windows
`close_addin_windows` may close. No Word is started here."""

from __future__ import annotations

import re
import time

import pytest

from desktoppilot.core import profiles, selectors, ui
from desktoppilot.core.errors import (
    Ambiguous,
    AppNotRunning,
    NotPresent,
    PilotError,
    Timeout,
    WalkCut,
)
from desktoppilot.core.registry import AppContext, Registry
from desktoppilot.plugins.word import plugin, roots


class Rect:
    def width(self) -> int:
        return 400

    def height(self) -> int:
        return 300


class Fake:
    def __init__(self, kind: str, name: str = "", children=(), *, hwnd: int = 0, auto_id: str = ""):
        self.ControlTypeName = kind + "Control"
        self.Name = name
        self.AutomationId = auto_id
        self.ClassName = ""
        self.IsOffscreen = False
        self.NativeWindowHandle = hwnd
        self.BoundingRectangle = Rect()
        self.children = list(children)

    def GetChildren(self):
        return self.children


# The host-window name a WPF add-in gives its task panes, as its profile lists it.
HOST_NAME = "MyAddin pane host"
HOSTS = (HOST_NAME,)
ADDIN_CTX = AppContext(
    name="myaddin",
    profile=profiles.Profile(name="myaddin", options={roots.PANE_HOSTS_OPTION: [HOST_NAME]}),
)


def host(header: str, *texts: str) -> Fake:
    return Fake(
        "Pane",
        HOST_NAME,
        [Fake("Button", auto_id="CloseWindow"), Fake("Text", header)]
        + [Fake("Text", t) for t in texts],
    )


TASKS = host("Task List", "Filter :")
NAVIGATION = host("Navigation", "Help")


@pytest.fixture
def frame(monkeypatch):
    def with_children(*children):
        window = Fake("Window", "sample - Word", children)
        monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
        return window

    return with_children


def test_pane_title_picks_the_host_with_that_header(frame):
    frame(Fake("Pane", "", [NAVIGATION, TASKS]))
    assert roots.task_pane(ADDIN_CTX, "Task List") is TASKS
    assert roots.task_pane(ADDIN_CTX, "navigation") is NAVIGATION


def test_pane_title_collapses_whitespace_and_takes_a_partial_header(frame):
    frame(NAVIGATION, TASKS)
    assert roots.task_pane(ADDIN_CTX, "  task   list ") is TASKS


def test_an_exact_header_wins_over_a_partial_one(frame):
    report = host("Navigation History", "x")
    frame(report, NAVIGATION)
    assert roots.task_pane(ADDIN_CTX, "Navigation") is NAVIGATION


def test_bare_pane_with_one_host_is_that_host(frame):
    frame(TASKS)
    assert roots.task_pane(ADDIN_CTX) is TASKS


def test_bare_pane_with_two_hosts_names_them_instead_of_guessing(frame):
    frame(NAVIGATION, TASKS)
    with pytest.raises(Ambiguous) as err:
        roots.task_pane(ADDIN_CTX)
    message = str(err.value)
    assert "'Navigation'" in message and "'Task List'" in message
    assert "pane:TITLE" in message


def test_with_no_pane_hosts_set_a_host_is_found_as_any_pane(frame):
    tasks = host("Task List", "Filter :")
    navigation = host("Navigation", "Help")
    window = frame(Fake("Pane", "MsoDockRight", [tasks, navigation]))
    window.ClassName = roots.WORD_WINDOW_CLASS
    linked(window)
    word = AppContext(name="word", profile=profiles.load("word"))
    assert roots.pane_hosts(None) == roots.pane_hosts(word) == ()
    # Two hosts open, yet no Ambiguous: unlisted, neither is a host.
    assert roots.task_pane(word, "Task List") is tasks


def test_pane_hosts_must_be_a_list_of_strings():
    ctx = AppContext(
        name="bad", profile=profiles.Profile(name="bad", options={"pane_hosts": "MyAddin"})
    )
    with pytest.raises(PilotError, match="pane_hosts must be a list of strings"):
        roots.pane_hosts(ctx)


def test_an_ambiguous_root_fails_find_at_once(frame, monkeypatch):
    frame(NAVIGATION, TASKS)

    def resolve(sel, ctx, timeout):
        return roots.task_pane(ctx, sel.root_arg)

    monkeypatch.setattr(ui, "resolve_root", resolve)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    with pytest.raises(Ambiguous):
        ui.find("pane//List", ADDIN_CTX, timeout=30)


# ------------------------------------------------------------------ ribbon


def test_a_missing_tab_keeps_its_message_when_the_ribbon_walk_fails_after(monkeypatch):
    ribbon = Fake("Pane", "Ribbon", [Fake("TabItem", "Home"), Fake("TabItem", "Insert")])
    calls = []

    def fake_ribbon(ctx, timeout=0):
        calls.append(timeout)
        if len(calls) > roots.TAB_MIN_WALKS:
            raise PilotError("ribbon not found in the Word window")
        return ribbon

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)
    monkeypatch.setattr(roots, "TAB_RETRY", 1.0)

    with pytest.raises(PilotError) as err:
        roots.ribbon_click(None, "MyAddin>Task List")

    assert "ribbon tab not found: 'MyAddin'" in str(err.value)
    assert "['Home', 'Insert']" in str(err.value)
    assert len(calls) > roots.TAB_MIN_WALKS, "a busy ribbon is walked again before giving up"


def test_a_tab_that_appears_on_a_later_walk_is_clicked(monkeypatch):
    empty = Fake("Pane", "Ribbon")
    tab = Fake("TabItem", "MyAddin")
    button = Fake("Button", "Task List")
    built = Fake("Pane", "Ribbon", [tab, button])
    walks = iter([empty, built])
    monkeypatch.setattr(roots, "ribbon", lambda ctx, timeout=0: next(walks))
    monkeypatch.setattr(roots, "select_tab", lambda ctx, t, mode="auto": "select")
    monkeypatch.setattr(ui, "click", lambda control, **kw: "Invoke")

    result = roots.ribbon_click(None, "MyAddin>Task List")
    assert result["clicked_via"] == "Invoke"


def test_a_tab_is_read_from_the_tab_strip_not_the_whole_ribbon(monkeypatch):
    lower = Fake("Pane", "Lower Ribbon", [Fake("TabItem", "MyAddin")])
    strip = Fake("Tab", "Ribbon Tabs", [Fake("TabItem", "Home"), Fake("TabItem", "MyAddin")])
    ribbon = Fake("Pane", "Ribbon", [Fake("Pane", "", [strip]), lower])
    monkeypatch.setattr(roots, "ribbon", lambda ctx, timeout=0: ribbon)

    root, tabs = roots._find_tab(None, "  myaddin ", timeout=5)
    assert root is ribbon and tabs == [strip.children[1]]


def test_a_missing_tab_lists_the_tabs_of_the_walk_that_missed(monkeypatch):
    walks = []

    def fake_ribbon(ctx, timeout=0):
        walks.append(timeout)
        return Fake("Pane", "Ribbon", [Fake("TabItem", f"Home{len(walks)}")])

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)
    monkeypatch.setattr(roots, "TAB_RETRY", 0.0)
    monkeypatch.setattr(roots, "TAB_POLL", 0.0)

    with pytest.raises(PilotError) as err:
        roots._find_tab(None, "MyAddin", timeout=5)
    assert not isinstance(err.value, WalkCut)
    assert "ribbon tab not found" in str(err.value)
    assert "tabs are ['Home3'] (3 walks)" in str(err.value)
    assert len(walks) == roots.TAB_MIN_WALKS


def test_walks_that_list_no_tabs_are_busy_not_a_missing_tab(monkeypatch):
    walks = []

    def fake_ribbon(ctx, timeout=0):
        walks.append(timeout)
        return Fake("Pane", "Ribbon", [Fake("TabItem", "Home")] if len(walks) == 1 else [])

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)
    monkeypatch.setattr(roots, "TAB_RETRY", 0.0)
    monkeypatch.setattr(roots, "TAB_POLL", 0.01)

    with pytest.raises(WalkCut) as err:
        roots._find_tab(None, "MyAddin", timeout=0.3)
    assert "not found" not in str(err.value)
    assert "['Home']" in str(err.value) and "no tabs" in str(err.value)
    assert len(walks) > roots.TAB_MIN_WALKS, "empty walks go on until the deadline"


def test_one_slow_walk_that_eats_the_timeout_is_busy_not_a_missing_tab(monkeypatch):
    def fake_ribbon(ctx, timeout=0):
        time.sleep(timeout)
        raise PilotError("ribbon not found in the Word window")

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)

    with pytest.raises(WalkCut) as err:
        roots._find_tab(None, "MyAddin", timeout=0.2)
    assert "ribbon tab not found" not in str(err.value)
    assert "(0 of 1 walks listed tabs)" in str(err.value)


def test_wpf_hosts_does_not_walk_the_document_or_the_ribbon():
    class Untouchable(Fake):
        def GetChildren(self):
            raise AssertionError("walked a subtree that holds no task pane")

    window = Fake(
        "Window",
        "sample - Word",
        [
            Untouchable("Pane", "Ribbon"),
            Untouchable("Document", "Page 1 content"),
            Fake("Pane", "MsoDockRight", [TASKS]),
        ],
    )
    assert roots.wpf_hosts(window, hosts=HOSTS) == [TASKS]


# ------------------------------------------------------------ add-in close


def test_close_addin_windows_closes_the_untitled_toast_only(frame, monkeypatch):
    toast = Fake(
        "Window",
        "",
        [Fake("Text", "3 items need review"), Fake("Button"), Fake("Button")],
        hwnd=101,
    )
    floating_pane = Fake("Window", "", [TASKS], hwnd=102)
    strip = Fake("Window", "", [Fake("Pane")], hwnd=103)
    foreign = Fake("Window", "", [Fake("Button")], hwnd=104)
    frame(floating_pane, toast, strip, foreign)
    classes = {
        101: "HwndWrapper[MyAddin.vsto;VSTA_Main;x]",
        102: "Windows.Win32.UI.WindowsAndMessaging.Win32Window",
        103: "HwndWrapper[MyAddin.vsto;VSTA_Main;y]",
        104: "SomeOtherClass",
    }
    monkeypatch.setattr(roots.win32gui, "GetClassName", classes.__getitem__)
    monkeypatch.setattr(
        roots.apps,
        "windows",
        lambda profile: [
            {"hwnd": 200, "class": "HwndWrapper[MyAddin.vsto;z]", "title": "Inspector"}
        ],
    )
    closed = []
    monkeypatch.setattr(ui, "close_window", lambda hwnd, **kw: closed.append(hwnd))

    result = roots.close_addin_windows(type("Ctx", (), {"profile": None})())

    assert result == ["Inspector", roots.UNTITLED]
    assert closed == [200, 101]


INSPECTOR = {
    "hwnd": 200,
    "class": "HwndWrapper[MyAddin.vsto;z]",
    "title": "Inspector",
}


@pytest.fixture
def closing(monkeypatch):
    """One titled add-in window open; returns the hwnds close_window was given."""
    monkeypatch.setattr(roots.apps, "windows", lambda profile: [INSPECTOR])
    closed = []
    monkeypatch.setattr(ui, "close_window", lambda hwnd, **kw: closed.append(hwnd))
    return closed


def test_close_addin_windows_says_when_the_frame_did_not_answer(closing, monkeypatch):
    def no_frame(ctx, timeout=0):
        raise Timeout("Word main window")

    monkeypatch.setattr(roots, "word_window", no_frame)

    with pytest.raises(WalkCut) as err:
        roots.close_addin_windows(Ctx())
    assert err.value.hits == ["Inspector"]
    assert closing == [200]
    assert "not checked" in str(err.value)


def test_close_addin_windows_says_when_the_toast_walk_timed_out(closing, monkeypatch):
    from _ctypes import COMError

    class TimedOut(Fake):
        def GetChildren(self):
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    window = TimedOut("Window", "sample - Word")
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    with pytest.raises(WalkCut) as err:
        roots.close_addin_windows(Ctx())
    assert err.value.hits == ["Inspector"]
    assert "not checked" in str(err.value)


def test_close_addin_windows_with_no_toast_is_a_plain_empty_answer(closing, frame):
    frame(Fake("Pane", "MsoDockRight", [TASKS]))
    assert roots.close_addin_windows(Ctx()) == ["Inspector"]
    assert closing == [200]


# ------------------------------------------------------------ a busy Word

DELAY = 0.05
SLACK = 0.2  # scheduling jitter on a loaded machine


class Slow(Fake):
    """A control whose children read blocks, as on a Word busy filling a pane."""

    def GetChildren(self):
        time.sleep(DELAY)
        return self.children


def busy_frame(*children) -> Slow:
    """A Word frame whose ribbon sits behind 36 panes that each block."""
    clutter = [Slow("Pane", "", [Slow("Pane") for _ in range(6)]) for _ in range(6)]
    return Slow("Window", "sample - Word", [*clutter, *children])


def test_a_busy_ribbon_walk_stops_at_the_timeout_and_is_not_not_found(monkeypatch):
    window = busy_frame(Slow("Pane", "", [Slow("Pane", "Ribbon")]))
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    start = time.monotonic()
    with pytest.raises(WalkCut) as err:
        roots.ribbon(None, timeout=0.3)
    assert time.monotonic() - start < 0.3 + DELAY + SLACK
    assert "not found" not in str(err.value)


def test_a_cut_ribbon_walk_says_the_time_spent_rounded_and_its_budget(monkeypatch):
    window = busy_frame(Slow("Pane", "", [Slow("Pane", "Ribbon")]))
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    with pytest.raises(WalkCut) as err:
        roots.ribbon(None, timeout=0.3)
    message = str(err.value)
    assert re.search(r"cut after \d+\.\ds \(budget 0\.3s\)", message)
    assert not re.search(r"\d\.\d{2,}", message), "no float with more than one decimal"


def test_a_ribbon_missing_from_a_complete_walk_is_not_found(monkeypatch):
    window = Fake("Window", "sample - Word", [Fake("Pane", "MsoDockRight")])
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    with pytest.raises(PilotError, match="ribbon not found") as err:
        roots.ribbon(None, timeout=0.3)
    assert not isinstance(err.value, WalkCut)


def test_a_cut_ribbon_walk_is_retried_within_the_tab_budget(monkeypatch):
    built = Fake("Pane", "Ribbon", [Fake("TabItem", "Home"), Fake("TabItem", "MyAddin")])
    calls = []

    def fake_ribbon(ctx, timeout=0):
        calls.append(timeout)
        if len(calls) == 1:
            raise WalkCut("the ribbon walk was cut at 15s; Word is busy")
        return built

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)
    monkeypatch.setattr(roots, "TAB_POLL", 0.0)

    root, tabs = roots._find_tab(None, "MyAddin", timeout=5)
    assert root is built and tabs == [built.children[1]]
    assert len(calls) == 2 and all(t <= 5 for t in calls)


def test_find_tab_on_a_busy_word_ends_at_its_timeout_and_says_cut(monkeypatch):
    window = busy_frame(Slow("Pane", "Ribbon", [Slow("TabItem", "MyAddin")]))
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    start = time.monotonic()
    with pytest.raises(WalkCut) as err:
        roots._find_tab(None, "MyAddin", timeout=0.4)
    assert time.monotonic() - start < 0.4 + DELAY + SLACK
    assert "ribbon tab not found" not in str(err.value)
    assert "cut" in str(err.value)


class Ctx:
    profile = None


@pytest.fixture
def desktop(monkeypatch):
    """Top-level windows, topmost first, as {hwnd: (class, pid)}; Word pid 7 is
    the one this tool launched. Returns the hwnds whose UIA element was made."""
    made: list[int] = []
    windows: dict[int, tuple] = {}

    def unused():
        raise AssertionError("the UIA desktop leaves out a busy Word; it must not be asked")

    monkeypatch.setattr(roots, "_frames", {})
    monkeypatch.setattr(roots, "FRAME_MAKES", [])
    monkeypatch.setattr(
        roots.session.apps, "pids", lambda profile: sorted({p for _, p in windows.values()})
    )
    monkeypatch.setattr(
        roots.session.apps.Session,
        "load",
        staticmethod(lambda profile: roots.session.apps.Session("word", 7, 0.0, True)),
    )
    monkeypatch.setattr(roots.auto, "GetRootControl", unused)
    monkeypatch.setattr(
        roots.session.win32gui, "EnumWindows", lambda fn, arg: [fn(h, arg) for h in list(windows)]
    )
    monkeypatch.setattr(roots.session.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(roots.session.win32gui, "GetClassName", lambda hwnd: windows[hwnd][0])
    monkeypatch.setattr(roots.session.win32gui, "GetWindowText", lambda hwnd: "Word")
    monkeypatch.setattr(
        roots.session.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, windows[hwnd][1])
    )
    monkeypatch.setattr(roots.auto, "ControlFromHandle", lambda hwnd: made.append(hwnd) or hwnd)

    def given(frames: dict) -> list[int]:
        windows.clear()
        windows.update(frames)
        return made

    return given


WORD_CTX = type("Ctx", (), {"profile": profiles.load("word"), "name": "word"})()


def test_word_window_is_found_through_win32_without_the_uia_desktop(desktop):
    made = desktop({10: ("Chrome_WidgetWin_1", 7), 30: (roots.WORD_WINDOW_CLASS, 7)})

    assert roots.word_window(WORD_CTX, timeout=1) == 30
    assert roots.word_window(WORD_CTX, timeout=1) == 30
    assert made == [30], "making the element blocks on a busy Word, so it is made once"


def test_a_foreign_word_on_top_is_never_taken_for_ours(desktop):
    desktop({20: (roots.WORD_WINDOW_CLASS, 99), 30: (roots.WORD_WINDOW_CLASS, 7)})

    assert roots.frame_hwnd(WORD_CTX) == 30
    assert roots.word_window(WORD_CTX, timeout=1) == 30


def test_only_a_foreign_word_running_is_refused_not_driven(desktop):
    desktop({20: (roots.WORD_WINDOW_CLASS, 99)})
    with pytest.raises(roots.session.NotOurWord):
        roots.word_window(WORD_CTX, timeout=1)


def test_two_document_windows_taking_turns_are_each_made_once(desktop):
    made = desktop({30: (roots.WORD_WINDOW_CLASS, 7), 40: (roots.WORD_WINDOW_CLASS, 7)})
    assert roots.word_window(WORD_CTX, timeout=1) == 30
    desktop({40: (roots.WORD_WINDOW_CLASS, 7), 30: (roots.WORD_WINDOW_CLASS, 7)})
    assert roots.word_window(WORD_CTX, timeout=1) == 40
    desktop({30: (roots.WORD_WINDOW_CLASS, 7), 40: (roots.WORD_WINDOW_CLASS, 7)})
    assert roots.word_window(WORD_CTX, timeout=1) == 30
    assert made == [30, 40]


def test_the_frame_cache_stays_small(desktop, monkeypatch):
    monkeypatch.setattr(roots, "FRAMES_KEPT", 2)
    for hwnd in (30, 40, 50):
        desktop({hwnd: (roots.WORD_WINDOW_CLASS, 7)})
        roots.word_window(WORD_CTX, timeout=1)
    assert [key[0] for key in roots._frames] == [40, 50]


def test_the_ribbon_walk_skips_the_addin_ui(monkeypatch):
    walked = []

    class Untouchable(Fake):
        def GetChildren(self):
            walked.append(self)
            return []

    toolbar = Untouchable("Window")
    toolbar.ClassName = roots.WPF_WINDOW_UIA_CLASS
    ribbon = Fake("Pane", "Ribbon")
    window = Fake(
        "Window",
        "sample - Word",
        [
            Fake("Window", "", [Untouchable("Pane", HOST_NAME)]),
            toolbar,
            Fake("Pane", "MsoDockTop", [Fake("ToolBar", "", [ribbon])]),
        ],
    )
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
    assert roots.ribbon(ADDIN_CTX, timeout=1) is ribbon
    assert walked == [], "the add-in's WPF UI holds no ribbon"


def test_no_word_frame_in_time_is_a_cut_ribbon_walk(monkeypatch):
    def no_frame(ctx, timeout=0):
        raise roots.Timeout("timed out after 0.1s waiting for Word main window")

    monkeypatch.setattr(roots, "word_window", no_frame)
    with pytest.raises(WalkCut, match="no Word frame"):
        roots.ribbon(None, timeout=0.1)


@pytest.mark.parametrize(
    "walk",
    [
        lambda: roots.ribbon_tabs(None, timeout=5),
        lambda: roots._find_tab(None, "Review", timeout=5),
    ],
    ids=["tabs", "find_tab"],
)
@pytest.mark.parametrize(
    "error",
    [AppNotRunning("word is not running"), roots.session.NotOurWord("not ours")],
    ids=["not-running", "not-ours"],
)
def test_the_tab_walks_fail_at_once_when_there_is_no_word_to_walk(walk, error, monkeypatch):
    calls = []

    def no_word(ctx):
        calls.append(ctx)
        raise error

    monkeypatch.setattr(roots.session, "require_owned", no_word)
    start = time.monotonic()
    with pytest.raises(type(error)):
        walk()
    assert len(calls) == 1 and time.monotonic() - start < 1


def test_ribbon_click_shares_one_deadline_across_its_lookups(monkeypatch):
    seen = []

    def fake_find_tab(ctx, name, timeout):
        seen.append(timeout)
        time.sleep(0.2)
        ribbon = Fake("Pane", "Ribbon", [Slow("Pane", "", [Slow("Button", "Other")])])
        return ribbon, [Fake("TabItem")]

    monkeypatch.setattr(roots, "_find_tab", fake_find_tab)
    monkeypatch.setattr(roots, "select_tab", lambda ctx, t, mode="auto": "select")

    start = time.monotonic()
    with pytest.raises(roots.Timeout):
        roots.ribbon_click(None, "MyAddin>Task List", timeout=0.5)
    assert seen == [pytest.approx(0.5, abs=0.05)]
    assert time.monotonic() - start < 0.5 + DELAY + SLACK


def test_a_pane_lookup_on_a_busy_word_stops_at_its_deadline():
    window = busy_frame(Slow("Pane", "MsoDockRight", [TASKS]))

    start = time.monotonic()
    with pytest.raises(WalkCut):
        roots.host_titled(
            roots.wpf_hosts(window, hosts=HOSTS, deadline=start + 0.3),
            "Task List",
            deadline=start + 0.3,
        )
    assert time.monotonic() - start < 0.3 + DELAY + SLACK


def test_a_header_read_past_the_deadline_is_cut_not_missing():
    slow_host = Slow("Pane", HOST_NAME, [Slow("Text", "Navigation")])
    with pytest.raises(WalkCut):
        roots.host_titled([slow_host], "Navigation", deadline=time.monotonic() + 0.01)
    assert roots.host_titled([slow_host], "Navigation", deadline=time.monotonic() + 30) is slow_host


# ------------------------------------------------------------------ settle


class Settling(Fake):
    """A frame whose children read is slow for its first `slow` reads."""

    def __init__(self, slow: int):
        super().__init__("Window", "sample - Word", [Fake("Pane")])
        self.slow = slow
        self.reads = 0

    def GetChildren(self):
        self.reads += 1
        if self.slow is None or self.reads <= self.slow:
            time.sleep(DELAY)
        return self.children


@pytest.fixture
def quick_settle(monkeypatch):
    monkeypatch.setattr(roots, "SETTLE_FAST", 0.02)
    monkeypatch.setattr(roots, "SETTLE_POLL", 0.0)

    def with_frame(window):
        monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
        return window

    return with_frame


def test_settle_waits_until_a_frame_read_is_fast_again(quick_settle):
    quick_settle(Settling(slow=2))
    result = roots.settle(Ctx())

    assert result["settled"] is True
    assert result["reads"] == 3


def test_settle_on_a_word_that_stays_slow_gives_up_without_raising(quick_settle):
    quick_settle(Settling(slow=None))
    start = time.monotonic()
    result = roots.settle(Ctx(), timeout=0.2)

    assert result["settled"] is False
    assert time.monotonic() - start < 0.2 + DELAY + SLACK


def test_settle_with_no_word_frame_is_not_settled(monkeypatch):
    def no_frame(ctx, timeout=0):
        raise Timeout("Word main window")

    monkeypatch.setattr(roots, "word_window", no_frame)
    assert roots.settle(Ctx())["settled"] is False


# ------------------------------------------------------------ backstage


def test_a_backstage_walk_word_timed_out_is_cut_not_closed(monkeypatch):
    from _ctypes import COMError

    class TimedOut(Fake):
        def GetChildren(self):
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    window = TimedOut("Window", "sample - Word")
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    with pytest.raises(WalkCut) as err:
        roots.backstage(None, timeout=0.3)
    assert "not open" not in str(err.value)


def test_backstage_missing_from_a_complete_walk_is_not_present(frame):
    frame(Fake("Pane", "MsoDockTop"))
    with pytest.raises(NotPresent, match="Backstage is not open"):
        roots.backstage(None, timeout=0.3)


def test_a_cut_walk_after_a_covering_one_is_not_present(monkeypatch):
    from _ctypes import COMError

    class TimesOutLater(Fake):
        reads = 0

        def GetChildren(self):
            TimesOutLater.reads += 1
            if TimesOutLater.reads > 1:
                raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))
            return self.children

    window = TimesOutLater("Window", "sample - Word", [Fake("Pane", "MsoDockTop")])
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    with pytest.raises(NotPresent, match="Backstage is not open"):
        roots.backstage(None, timeout=1)


def test_a_closed_backstage_walk_skips_the_addin_ui(frame):
    walked = []

    class Recorded(Fake):
        def GetChildren(self):
            walked.append(self.Name)
            return self.children

    frame(Recorded("Pane", HOST_NAME), Fake("Pane", "MsoDockTop"))
    with pytest.raises(NotPresent):
        roots.backstage(ADDIN_CTX, timeout=0.3)
    assert not walked


def test_an_open_backstage_is_found(frame):
    view = Fake("Pane", roots.BACKSTAGE_NAME)
    frame(Fake("Pane", "", [view]))
    assert roots.backstage(None, timeout=1) is view


# ------------------------------------------------------ pane:TITLE fallback


def linked(node: Fake) -> Fake:
    """Give a fake tree parent links, for the climb to a pane container."""
    for child in node.children:
        child.GetParentControl = lambda node=node: node
        linked(child)
    return node


def word_frame(frame, *children) -> Fake:
    window = frame(*children)
    window.ClassName = roots.WORD_WINDOW_CLASS
    return linked(window)


def test_a_ribbon_button_named_like_a_pane_does_not_answer_as_the_pane(frame):
    button = Fake("Button", "Task List")
    ribbon = Fake("Pane", "Ribbon", [Fake("Group", "Tools", [button])])
    word_frame(frame, Fake("Pane", "MsoDockTop", [Fake("ToolBar", "", [ribbon])]))

    with pytest.raises(NotPresent, match="task pane not found"):
        roots.task_pane(ADDIN_CTX, "Task List", timeout=1)


def test_the_addin_toolbar_button_does_not_answer_as_the_pane(frame):
    toolbar = Fake("Window", "", [Fake("Pane", "", [Fake("Button", "Task List")])])
    toolbar.ClassName = roots.WPF_WINDOW_UIA_CLASS
    word_frame(frame, toolbar)

    with pytest.raises(NotPresent):
        roots.task_pane(ADDIN_CTX, "Task List", timeout=1)


def test_a_label_in_one_of_words_own_panes_answers_with_that_pane(frame):
    pane = Fake("Pane", "", [Fake("Text", "Navigation")])
    word_frame(frame, Fake("Pane", "MsoDockLeft", [pane]))

    assert roots.task_pane(ADDIN_CTX, "Navigation", timeout=1) is pane


def test_a_stray_label_with_no_pane_around_it_does_not_answer(frame):
    word_frame(frame, Fake("Text", "Navigation"))

    with pytest.raises(NotPresent):
        roots.task_pane(ADDIN_CTX, "Navigation", timeout=1)


def document_host(name: str) -> Fake:
    """Word's document host: named after the file, holding the canvas."""
    host_pane = Fake("Pane", name, [Fake("Document", name), Fake("Pane", "Vertical")])
    host_pane.ClassName = roots.DOCUMENT_HOST_CLASS
    return host_pane


def test_a_document_named_like_a_pane_does_not_answer_as_the_pane(frame):
    word_frame(frame, Fake("Pane", "", [document_host("navigation-copy  -  Compatibility Mode")]))

    with pytest.raises(NotPresent, match="task pane not found"):
        roots.task_pane(ADDIN_CTX, "Navigation", timeout=1)


def test_the_pane_is_found_beside_a_document_named_like_it(frame):
    navigation = host("Navigation", "Help")
    word_frame(
        frame,
        Fake("Pane", "", [document_host("navigation-copy  -  Compatibility Mode")]),
        Fake("Window", "Navigation", [navigation]),
    )

    assert roots.task_pane(ADDIN_CTX, "Navigation", timeout=1) is navigation


def test_the_climb_to_a_pane_stops_at_the_document_host(frame):
    label = Fake("Text", "Navigation")
    host_pane = Fake("Pane", "navigation-copy", [label])
    host_pane.ClassName = roots.DOCUMENT_HOST_CLASS
    word_frame(frame, Fake("Pane", "", [host_pane]))

    assert roots._pane_ancestor(label) is None


# ------------------------------------------------------ tabs and controls


def lower_ribbon(*groups: Fake) -> Fake:
    lower = Fake("Pane", "Lower Ribbon", list(groups))
    lower.ClassName = roots.LOWER_RIBBON_CLASS
    return lower


def tab_strip(*names: str) -> Fake:
    return Fake("Tab", "Ribbon Tabs", [Fake("TabItem", n) for n in names])


class Switching(Fake):
    """A Lower Ribbon that still shows the last tab's group for `reads`
    reads after the switch: Word builds a tab's controls lazily."""

    def __init__(self, before: Fake, after: Fake, reads: int):
        super().__init__("Pane", "Lower Ribbon", [before])
        self.ClassName = roots.LOWER_RIBBON_CLASS
        self.after, self.reads = after, reads

    def GetChildren(self):
        self.reads -= 1
        return [self.after] if self.reads < 0 else self.children


@pytest.fixture
def tabs(monkeypatch):
    """A ribbon whose tab switch is recorded, not performed."""
    monkeypatch.setattr(roots, "select_tab", lambda ctx, t, mode="auto": "select")
    monkeypatch.setattr(roots, "TAB_POLL", 0.0)

    def with_ribbon(*children):
        ribbon = Fake("Pane", "Ribbon", list(children))
        monkeypatch.setattr(roots, "ribbon", lambda ctx, timeout=0: ribbon)
        return ribbon

    return with_ribbon


def test_ribbon_controls_for_an_unknown_tab_fails_and_lists_the_tabs(tabs, monkeypatch):
    home = Fake("Group", "Home", [Fake("Button", "Paste")])
    tabs(tab_strip("Home", "MyAddin"), lower_ribbon(home))
    monkeypatch.setattr(roots, "TAB_RETRY", 0.0)

    with pytest.raises(PilotError) as err:
        roots.ribbon_controls(None, "Nope", timeout=2)
    assert not isinstance(err.value, WalkCut)
    assert "ribbon tab not found: 'Nope'" in str(err.value)
    assert "['Home', 'MyAddin']" in str(err.value)


def test_ribbon_controls_waits_for_the_tab_and_lists_only_its_panel(tabs):
    home = Fake("Group", "Home", [Fake("Button", "Paste")])
    addin = Fake("Group", "MyAddin", [Fake("Button", "Task List")])
    tabs(tab_strip("Home", "MyAddin"), Switching(home, addin, reads=2))

    assert roots.ribbon_controls(None, "MyAddin", timeout=5) == ["Task List"]


def test_a_click_right_after_a_tab_switch_hits_that_tabs_control(tabs, monkeypatch):
    review_compare = Fake("Button", "Compare")
    addin_compare = Fake("Button", "Compare")
    tabs(
        tab_strip("Review", "MyAddin"),
        Switching(
            Fake("Group", "Review", [review_compare]),
            Fake("Group", "MyAddin", [addin_compare]),
            reads=2,
        ),
    )
    clicked = []
    monkeypatch.setattr(ui, "click", lambda control, **kw: clicked.append(control) or "Invoke")

    roots.ribbon_click(None, "MyAddin>Compare", timeout=5)
    assert clicked == [addin_compare]


def test_ribbon_click_passes_allow_disabled_to_the_click(tabs, monkeypatch):
    tabs(tab_strip("Home"), Fake("Group", "Home", [Fake("Button", "Cut")]))
    seen = []
    monkeypatch.setattr(ui, "click", lambda control, **kw: seen.append(kw["allow_disabled"]))

    roots.ribbon_click(None, "Home>Cut", timeout=5)
    roots.ribbon_click(None, "Home>Cut", timeout=5, allow_disabled=True)
    assert seen == [False, True]


def test_ribbon_click_takes_allow_disabled():
    from typer.testing import CliRunner

    from desktoppilot.cli import app

    help_text = CliRunner().invoke(app, ["word", "ribbon", "click", "--help"]).output
    assert "--allow-disabled" in help_text


def test_ribbon_tabs_on_a_ribbon_with_no_tabs_built_is_cut_not_empty(tabs):
    tabs(Fake("Pane", "Lower Ribbon"))
    with pytest.raises(WalkCut, match="no ribbon walk listed tabs"):
        roots.ribbon_tabs(None, timeout=0.2)


def test_ribbon_tabs_answers_the_first_walk_that_lists_them(monkeypatch):
    walks = iter(
        [WalkCut("cut"), Fake("Pane", "Ribbon"), Fake("Pane", "Ribbon", [tab_strip("Home")])]
    )

    def fake_ribbon(ctx, timeout=0):
        walk = next(walks)
        if isinstance(walk, Exception):
            raise walk
        return walk

    monkeypatch.setattr(roots, "ribbon", fake_ribbon)
    monkeypatch.setattr(roots, "TAB_POLL", 0.0)
    assert roots.ribbon_tabs(None, timeout=5) == ["Home"]


# --------------------------------------------------- times said are spent


def test_no_frame_for_the_toast_check_says_the_time_spent_not_the_budget(closing, monkeypatch):
    def no_frame(ctx, timeout=0):
        raise Timeout("Word main window")

    monkeypatch.setattr(roots, "word_window", no_frame)

    with pytest.raises(WalkCut) as err:
        roots.close_addin_windows(Ctx(), timeout=5.0)
    assert "after 0.0s (budget 5.0s)" in str(err.value)


def test_a_frame_element_made_past_the_budget_is_cut_then_cached(desktop, monkeypatch):
    desktop({30: (roots.WORD_WINDOW_CLASS, 7)})

    def slow_make(hwnd):
        time.sleep(0.2)
        return "frame"

    monkeypatch.setattr(roots.auto, "ControlFromHandle", slow_make)

    start = time.monotonic()
    with pytest.raises(WalkCut, match="took 0.2s"):
        roots.word_window(WORD_CTX, timeout=0.1)
    assert time.monotonic() - start < 0.2 + SLACK
    [made] = roots.FRAME_MAKES
    assert made["hwnd"] == 30 and made["seconds"] == pytest.approx(0.2, abs=0.1)
    assert roots.word_window(WORD_CTX, timeout=0.1) == "frame"


def test_a_popup_lookup_on_a_busy_word_is_cut_not_absent(monkeypatch, word_roots):
    monkeypatch.setattr(roots.auto, "GetRootControl", lambda: Fake("Pane", "Desktop"))
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: busy_frame())

    start = time.monotonic()
    with pytest.raises(WalkCut):
        roots.popup_roots(None, deadline=start + 0.2)
    assert time.monotonic() - start < 0.2 + DELAY + SLACK
    assert ui.is_gone("popup", word_roots, timeout=0.2) is False


def test_an_unreadable_desktop_child_does_not_break_the_popup_lookup(monkeypatch):
    class Gone(Fake):
        @property
        def ClassName(self):
            raise RuntimeError("element vanished")

        @ClassName.setter
        def ClassName(self, value):
            pass

    popup = Fake("Window", "")
    popup.ClassName = roots.POPUP_CLASS
    monkeypatch.setattr(
        roots.auto, "GetRootControl", lambda: Fake("Pane", "", [Gone("Window"), popup])
    )
    monkeypatch.setattr(
        roots, "word_window", lambda ctx, timeout=0: Fake("Window", "sample - Word")
    )
    assert roots.popup_roots(None, deadline=time.monotonic() + 1) == [popup]


def test_a_window_that_will_not_close_is_reported_not_counted(frame, monkeypatch):
    monkeypatch.setattr(roots.apps, "windows", lambda profile: [INSPECTOR])
    frame(Fake("Pane", "MsoDockRight"))

    def refuse(hwnd, **kw):
        raise Timeout("timed out after 10s waiting for the window to close")

    monkeypatch.setattr(ui, "close_window", refuse)
    monkeypatch.setattr(roots.win32gui, "IsWindow", lambda hwnd: True)

    not_closed: list = []
    assert roots.close_addin_windows(Ctx(), not_closed=not_closed) == []
    assert not_closed == [
        {
            "window": "Inspector",
            "hwnd": 200,
            "error": "timed out after 10s waiting for the window to close",
        }
    ]


def test_a_window_that_vanished_before_its_close_is_neither(frame, monkeypatch):
    monkeypatch.setattr(roots.apps, "windows", lambda profile: [INSPECTOR])
    frame(Fake("Pane", "MsoDockRight"))

    def gone(hwnd, **kw):
        raise OSError("invalid window handle")

    monkeypatch.setattr(ui, "close_window", gone)
    monkeypatch.setattr(roots.win32gui, "IsWindow", lambda hwnd: False)

    not_closed: list = []
    assert roots.close_addin_windows(Ctx(), not_closed=not_closed) == []
    assert not_closed == []


# ------------------------------------------------------- presence probes


@pytest.fixture
def word_roots():
    """A registry holding Word's roots, as `pilot word ui wait` resolves them."""
    reg = Registry()
    profile = profiles.load("word")
    profile.options = {roots.PANE_HOSTS_OPTION: [HOST_NAME]}
    plugin.register_roots(reg, profile)
    return AppContext(name="word", profile=profile, registry=reg)


def slow_closed_pane_frame(monkeypatch) -> Slow:
    """A Word frame with no add-in pane whose pane:TITLE walk takes ~1.7 s
    (34 reads of 0.05 s): past a second, as six walks took on an idle Word."""
    window = Slow("Window", "sample - Word", [Slow("Pane", "MsoDockTop") for _ in range(33)])
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
    return window


def test_a_closed_pane_whose_lookup_outlasts_a_second_reads_as_gone(monkeypatch, word_roots):
    slow_closed_pane_frame(monkeypatch)
    selector = selectors.parse("pane:Task List")

    with pytest.raises(WalkCut):  # what the old fixed 1 s budget got, every time
        ui.resolve_root(selector, word_roots, timeout=ui.LEGACY_PROBE_TIMEOUT, probe=True)

    start = time.monotonic()
    result = ui.wait_for("pane:Task List", word_roots, gone=True, timeout=10)
    assert result["gone"] is True
    assert ui.LEGACY_PROBE_TIMEOUT < time.monotonic() - start < 4


def test_an_open_pane_never_reads_as_gone(frame, word_roots):
    frame(Fake("Pane", "MsoDockRight", [TASKS]))
    start = time.monotonic()
    with pytest.raises(Timeout):
        ui.wait_for("pane:Task List", word_roots, gone=True, timeout=0.5)
    assert time.monotonic() - start < 0.5 + SLACK + ui.LEGACY_PROBE_TIMEOUT


def test_a_cut_pane_walk_is_never_gone(monkeypatch, word_roots):
    from _ctypes import COMError

    class TimedOut(Fake):
        def GetChildren(self):
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    window = TimedOut("Window", "sample - Word")
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
    assert ui.is_gone("pane:Task List", word_roots) is False
    with pytest.raises(Timeout):
        ui.wait_for("pane:Task List", word_roots, gone=True, timeout=0.5)


def test_a_pane_walk_cut_by_its_probe_budget_is_not_gone(monkeypatch, word_roots):
    slow_closed_pane_frame(monkeypatch)
    assert ui.is_gone("pane:Task List", word_roots, timeout=0.3) is False


def test_a_backstage_probe_walks_once_and_does_not_wait(monkeypatch, word_roots):
    walks = []

    class Counted(Fake):
        def GetChildren(self):
            walks.append(1)
            return self.children

    window = Counted("Window", "sample - Word", [Fake("Pane", "MsoDockTop")])
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)

    start = time.monotonic()
    assert ui.is_gone("backstage", word_roots, timeout=10) is True
    assert time.monotonic() - start < SLACK
    assert len(walks) == 1


def test_a_backstage_probe_cut_before_any_walk_is_not_gone(monkeypatch, word_roots):
    from _ctypes import COMError

    class TimedOut(Fake):
        def GetChildren(self):
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    window = TimedOut("Window", "sample - Word")
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
    assert ui.is_gone("backstage", word_roots) is False


def test_find_still_waits_for_backstage_to_open(monkeypatch):
    view = Fake("Pane", roots.BACKSTAGE_NAME)
    window = Fake("Window", "sample - Word", [Fake("Pane", "MsoDockTop")])
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: window)
    reads = []

    def opens_on_the_third_walk():
        reads.append(1)
        if len(reads) == 3:
            window.children.append(view)
        return list(window.children)

    window.GetChildren = opens_on_the_third_walk
    assert roots.backstage(None, timeout=5) is view


def test_an_addin_window_probe_looks_once(monkeypatch, word_roots):
    looks = []
    monkeypatch.setattr(roots.apps, "require_running", lambda profile: [1])
    monkeypatch.setattr(roots.apps, "windows", lambda profile: looks.append(1) or [])
    start = time.monotonic()
    assert ui.is_gone("win:Inspector", word_roots, timeout=10) is True
    assert time.monotonic() - start < SLACK
    assert looks == [1]


def test_a_pane_lookup_walks_the_frame_once(frame):
    reads = []

    class Counted(Fake):
        def GetChildren(self):
            reads.append(self.Name)
            return self.children

    window = frame(Counted("Pane", "MsoDockTop", [Counted("Pane", "MsoDockLeft")]))
    window.GetChildren = lambda: reads.append(window.Name) or window.children
    with pytest.raises(NotPresent):
        roots.task_pane(ADDIN_CTX, "Task List", timeout=1)
    assert reads == ["sample - Word", "MsoDockTop", "MsoDockLeft"]
