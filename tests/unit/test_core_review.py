"""Review findings on the core: each test pins one contract that the code
before it broke. Fakes only, no desktop."""

from __future__ import annotations

import json
import time

import pytest
import typer
from typer.testing import CliRunner

from desktoppilot.core import apps, commands, paths, pytest_plugin, selectors, ui
from desktoppilot.core.errors import (
    UIA_E_TIMEOUT,
    Ambiguous,
    Disabled,
    NotPresent,
    PilotError,
    Timeout,
    WalkCut,
    is_uia_timeout,
)
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

from .compat import PLAIN_APP, COMError

runner = CliRunner()


class Rect:
    left = top = 0

    def width(self) -> int:
        return 10

    def height(self) -> int:
        return 10


class Node:
    BoundingRectangle = Rect()

    def __init__(self, kind: str, name: str = "", children=()):
        self.ControlTypeName = kind + "Control"
        self.Name = name
        self.AutomationId = self.ClassName = ""
        self.IsOffscreen = False
        self.IsEnabled = True
        self.children = list(children)

    def GetChildren(self):
        return self.children


class TimedOut(Node):
    def GetChildren(self):
        raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))


class SlowNode(Node):
    def GetChildren(self):
        time.sleep(0.05)
        return super().GetChildren()


def ctx_with_root(root: str, resolver) -> AppContext:
    reg = Registry()
    reg.root(root, resolver, for_app="fake")
    return AppContext(name="fake", profile=None, registry=reg)


def raising(exc):
    def resolver(arg, timeout):
        raise exc

    return resolver


# ---------------------------------------------- is_gone: only absence is gone


@pytest.mark.parametrize(
    ("selector", "exc"),
    [
        ("thing//Button[name='x'", None),  # parse error
        ("nosuch//Button", None),  # unknown root
        ("thing//Button", Ambiguous("two panes")),
        ("thing//Button", PilotError("the plugin broke")),
    ],
)
def test_only_absence_is_gone(selector, exc):
    ctx = ctx_with_root("thing", raising(exc or NotPresent("closed")))
    with pytest.raises(PilotError):
        ui.is_gone(selector, ctx)


def test_a_root_that_is_not_present_is_gone():
    assert ui.is_gone("thing//Button", ctx_with_root("thing", raising(NotPresent("closed"))))


def test_no_match_under_a_present_root_is_gone():
    ctx = ctx_with_root("thing", lambda arg, timeout: Node("Pane", "", [Node("Edit")]))
    assert ui.is_gone("thing//Button", ctx)
    assert not ui.is_gone("thing//Edit", ctx)


def test_wait_gone_fails_at_once_on_a_broken_selector():
    ctx = ctx_with_root("thing", raising(Ambiguous("two panes")))
    start = time.monotonic()
    with pytest.raises(Ambiguous):
        ui.wait_for("thing//Button", ctx, gone=True, timeout=5)
    with pytest.raises(PilotError, match="unknown selector root"):
        ui.wait_for("nosuch//Button", ctx, gone=True, timeout=5)
    assert time.monotonic() - start < 2


def test_wait_gone_does_not_pass_on_a_root_error():
    ctx = ctx_with_root("thing", raising(PilotError("the plugin broke")))
    with pytest.raises(Timeout, match="the plugin broke"):
        ui.wait_for("thing//Button", ctx, gone=True, timeout=0.3)


def test_is_gone_asks_a_root_that_takes_probe_to_look_once():
    seen = []

    def resolver(arg, timeout, probe=False):
        seen.append((timeout, probe))
        raise NotPresent("closed")

    assert ui.is_gone("thing//Button", ctx_with_root("thing", resolver))
    assert seen == [(ui.PROBE_TIMEOUT, True)]


def test_a_root_that_cannot_probe_keeps_the_short_budget():
    seen = []

    def resolver(arg, timeout):
        seen.append(timeout)
        raise NotPresent("closed")

    assert ui.is_gone("thing//Button", ctx_with_root("thing", resolver))
    assert seen == [ui.LEGACY_PROBE_TIMEOUT]


def test_find_never_probes():
    seen = []

    def resolver(arg, timeout, probe=False):
        seen.append(probe)
        return Node("Pane", "", [Node("Button")])

    ui.find("thing//Button", ctx_with_root("thing", resolver), timeout=1)
    assert seen == [False]


def test_a_core_window_probe_looks_once(monkeypatch):
    looks = []
    monkeypatch.setattr(apps, "windows", lambda profile, **k: looks.append(1) or [])
    ctx = AppContext(name="x", profile=None, registry=Registry())
    start = time.monotonic()
    with pytest.raises(NotPresent):
        ui.named_window(ctx, "Nope", timeout=10, probe=True)
    assert time.monotonic() - start < 0.5
    assert looks == [1]


def test_a_core_window_root_that_is_not_open_is_not_present(monkeypatch):
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])
    with pytest.raises(NotPresent):
        ui.named_window(
            AppContext(name="x", profile=None, registry=Registry()), "Nope", timeout=0.1
        )


# --------------------------------------------------------------- close_window


def test_closing_an_element_without_a_window_handle_is_refused(monkeypatch):
    posted = []
    monkeypatch.setattr(ui.win32gui, "PostMessage", lambda *a: posted.append(a))
    element = Node("Button")
    element.NativeWindowHandle = 0
    with pytest.raises(PilotError, match="no window handle"):
        ui.close_window(element, timeout=0.1)
    assert posted == []


# -------------------------------------------------------- CLI errors are JSON


def test_an_unexpected_exception_is_json_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(commands.fallback, "on_failure", lambda ctx, command: {"screenshot": "s"})

    def body():
        raise OSError("RPC server unavailable")

    with pytest.raises(typer.Exit) as err:
        commands.run(body, ctx=object(), command="ui click")
    assert err.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": "OSError",
        "message": "RPC server unavailable",
        "internal": True,
        "screenshot": "s",
    }


def test_a_bad_argument_combination_is_json_and_exit_1():
    from desktoppilot.cli import app

    result = runner.invoke(app, [PLAIN_APP, "ui", "click"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "PilotError"


def test_a_malformed_point_is_json_and_exit_1(monkeypatch):
    from desktoppilot.cli import app

    monkeypatch.setattr(ui, "activate_app", lambda *a, **k: None)
    result = runner.invoke(app, ["desktop", "--no-shot", "ui", "click", "--at", "ten,20"])
    assert result.exit_code == 1
    assert "X,Y" in json.loads(result.output)["message"]


# ----------------------------------------- a timed-out children read is a cut


def test_a_timed_out_children_read_is_a_cut_not_a_leaf():
    with pytest.raises(WalkCut):
        ui._children(TimedOut("Pane"))


def test_tree_and_flatten_say_when_a_walk_was_cut():
    root = Node("Pane", "root", [Node("Button", "a"), TimedOut("Pane", "busy")])
    walk: dict = {}
    dump = ui.tree(root, report=walk)
    assert walk["complete"] is False and walk["cut"] == 1
    assert dump["children"][1]["cut"] is True

    walk = {}
    names = [c["name"] for c in ui.flatten(root, report=walk, only_visible=False)]
    assert names == ["root", "a", "busy"] and walk["complete"] is False

    walk = {}
    ui.tree(Node("Pane", "root", [Node("Button", "a")]), report=walk)
    assert walk == {"complete": True, "cut": 0}


def test_scope_contains_never_answers_false_from_a_cut_walk(monkeypatch):
    root = Node("Pane", "root", [Node("Button", "Send"), TimedOut("Pane", "busy")])
    monkeypatch.setattr(ui, "find", lambda selector, ctx, **k: root)
    scope = pytest_plugin.Scope(None, "app")
    assert scope.contains("send") is True
    with pytest.raises(WalkCut):
        scope.contains("Preferences")
    with pytest.raises(WalkCut):
        scope.texts()


def test_the_wrapper_expand_walk_is_incomplete_after_a_timed_out_read():
    stats = ui._expand_all_walk(TimedOut("Tree"), time.monotonic() + 10, 0)
    assert stats["complete"] is False


# ----------------------------------------------------------- a cut list count


class Items:
    def FindItemByProperty(self, after, prop, value):
        return type("Item", (), {"CurrentName": "row"})()


def test_a_list_count_cut_at_its_deadline_raises_with_its_floor():
    with pytest.raises(WalkCut) as err:
        ui._enumerate_items(Items(), names=0, cap=1000, deadline=time.monotonic() - 1)
    assert err.value.count == 1


# --------------------------------------------- find is bounded by its timeout


def slow_tree(width: int = 4, depth: int = 3) -> SlowNode:
    if depth == 0:
        return SlowNode("Button", "leaf")
    return SlowNode("Pane", "", [slow_tree(width, depth - 1) for _ in range(width)])


def test_find_does_not_block_past_its_timeout(monkeypatch):
    tree = slow_tree()  # 85 reads at 0.05 s: 4 s for one unbounded attempt
    monkeypatch.setattr(ui, "resolve_root", lambda sel, ctx, timeout: tree)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    start = time.monotonic()
    with pytest.raises(Timeout):
        ui.find("app//Edit", None, timeout=0.3)
    assert time.monotonic() - start < 0.3 + 0.05 + 0.5


# ------------------------------------ find_all: indexes, duplicates, deadline


def test_find_all_honours_the_index_of_each_step(monkeypatch):
    first, second = (
        Node("Pane", "p1", [Node("Button", "a"), Node("Button", "b")]),
        Node("Pane", "p2", [Node("Button", "c"), Node("Button", "d")]),
    )
    monkeypatch.setattr(
        ui,
        "resolve_root",
        lambda sel, ctx, timeout, probe=False: Node("Window", "", [first, second]),
    )
    assert [c.Name for c in ui.find_all("app/Pane/Button#2", None)] == ["b", "d"]
    assert [c.Name for c in ui.find_all("app/Pane#2/Button", None)] == ["c", "d"]


def test_find_all_lists_a_nested_match_once(monkeypatch):
    button = Node("Button", "ok")
    inner = Node("Pane", "inner", [button])
    monkeypatch.setattr(
        ui,
        "resolve_root",
        lambda sel, ctx, timeout, probe=False: Node("Pane", "", [Node("Pane", "outer", [inner])]),
    )
    assert ui.find_all("app//Pane//Button", None) == [button]


def test_a_probe_walk_is_bounded_by_its_timeout(monkeypatch):
    tree = slow_tree()  # 85 reads at 0.05 s: 4 s unbounded
    monkeypatch.setattr(ui, "resolve_root", lambda sel, ctx, timeout, probe=False: tree)
    start = time.monotonic()
    with pytest.raises(WalkCut):
        ui.find_all("app//Edit", None, timeout=0.3, probe=True)
    assert time.monotonic() - start < 0.3 + 0.05 + 0.5
    start = time.monotonic()
    assert ui.is_gone("app//Edit", None, timeout=0.3) is False
    assert time.monotonic() - start < 0.3 + 0.05 + 0.5


# ----------------------------------------------- timed-out reads cut the walk


def timeout_error() -> COMError:
    return COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))


def busy(attr: str) -> type:
    """A Node class whose `attr` read times out, as on a busy app."""

    def read(self):
        raise timeout_error()

    return type("Busy" + attr, (Node,), {attr: property(read, lambda self, value: None)})


class Valued(Node):
    def GetValuePattern(self):
        return type("Pattern", (), {"Value": "ok"})()


class BusyValue(Node):
    def GetValuePattern(self):
        def read(self):
            raise timeout_error()

        return type("Pattern", (), {"Value": property(read)})()


def test_is_uia_timeout_reads_the_hresult_or_the_first_arg():
    assert is_uia_timeout(timeout_error())
    assert is_uia_timeout(OSError(UIA_E_TIMEOUT))
    assert not is_uia_timeout(COMError(-2147220991, None, (None, None, None, 0, None)))
    assert not is_uia_timeout(OSError())


@pytest.mark.parametrize(
    ("busy_node", "selector"),
    [
        (busy("ControlTypeName"), "app//Button"),
        (busy("Name"), "app//Button[name='ok']"),
        (BusyValue, "app//Button[value='ok']"),
        (busy("IsOffscreen"), "app//Button"),
    ],
)
def test_a_timed_out_property_read_cuts_the_walk_with_its_hits(busy_node, selector):
    found = Valued("Button", "ok")
    tree = Node("Pane", "", [found, busy_node("Button")])
    step = selectors.parse(selector).steps[0]
    with pytest.raises(WalkCut) as err:
        ui.search(tree, step)
    assert err.value.hits == [found]


def test_a_stale_property_read_is_still_simply_not_a_match():
    def read(self):
        raise COMError(-2147220991, None, (None, None, None, 0, None))

    stale = type("Stale", (Node,), {"Name": property(read, lambda self, value: None)})
    tree = Node("Pane", "", [stale("Button"), Node("Button", "ok")])
    hits = ui.search(tree, selectors.parse("app//Button[name='ok']").steps[0])
    assert [c.Name for c in hits] == ["ok"]


def test_a_prune_check_that_times_out_cuts_the_walk():
    tree = Node("Pane", "", [Node("Pane", "", [Node("Button")])])

    def prune(control, depth):
        raise timeout_error()

    with pytest.raises(WalkCut):
        ui.search(tree, selectors.parse("app//Button").steps[0], prune=prune)


def test_a_timed_out_property_read_is_not_gone():
    ctx = ctx_with_root("thing", lambda arg, timeout: Node("Pane", "", [busy("Name")("Button")]))
    assert ui.is_gone("thing//Button[name='x']", ctx) is False


def test_a_find_whose_property_reads_time_out_is_busy_not_not_found(monkeypatch):
    root = Node("Pane", "", [busy("Name")("Button")])
    monkeypatch.setattr(ui, "resolve_root", lambda sel, ctx, timeout: root)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    with pytest.raises(WalkCut, match="busy") as err:
        ui.find("app//Button[name='x']", None, timeout=0.3)
    assert "found no match" not in str(err.value)


def test_a_verbose_describe_of_a_busy_value_keeps_the_rest():
    info = ui.describe(BusyValue("Edit", "box"), verbose=True)
    assert "stale" not in info
    assert info["name"] == "box" and "value" not in info
    assert info["value_error"]


# ------------------------------------------ click waits for a disabled target


def test_a_disabled_target_is_refused_unless_allowed():
    control = Node("Button")
    control.IsEnabled = False
    control.GetInvokePattern = lambda: type("P", (), {"Invoke": lambda self: None})()
    with pytest.raises(Disabled):
        ui.click(control, activate=False)
    assert ui.click(control, activate=False, allow_disabled=True) == "Invoke"


class EnablesLater(Node):
    """Disabled for the first `polls` reads of IsEnabled, then enabled."""

    def __init__(self, polls: int):
        super().__init__("Button")
        self.reads = 0
        self.polls = polls
        self.invoked = 0
        self.GetInvokePattern = lambda: type("P", (), {"Invoke": lambda p: self._invoke()})()

    def _invoke(self):
        self.invoked += 1

    def _read(self):
        return self.reads > self.polls

    @property
    def IsEnabled(self):
        self.reads += 1
        return self._read()

    @IsEnabled.setter
    def IsEnabled(self, value):
        pass


def test_a_click_waits_for_a_disabled_target_to_enable(monkeypatch):
    monkeypatch.setattr(ui, "ENABLED_POLL", 0.01)
    control = EnablesLater(polls=3)
    assert ui.click(control, activate=False, timeout=5) == "Invoke"
    assert control.invoked == 1
    assert control.reads == 4


def test_a_target_that_stays_disabled_raises_after_the_timeout(monkeypatch):
    monkeypatch.setattr(ui, "ENABLED_POLL", 0.01)
    control = EnablesLater(polls=10**9)
    start = time.monotonic()
    with pytest.raises(Disabled, match=r"still disabled after 0\.[3-9]s"):
        ui.click(control, activate=False, timeout=0.4)
    assert 0.35 <= time.monotonic() - start < 2
    assert control.invoked == 0


def test_a_stale_disabled_target_is_found_again(monkeypatch):
    monkeypatch.setattr(ui, "ENABLED_POLL", 0.01)

    class Stale(EnablesLater):
        def _read(self):
            if self.reads == 1:
                return False
            raise COMError(-2147220991, None, (None, None, None, 0, None))

    fresh = EnablesLater(polls=0)
    assert ui.click(Stale(polls=0), activate=False, timeout=5, refind=lambda: fresh) == "Invoke"
    assert fresh.invoked == 1


def test_ui_click_takes_allow_disabled():
    from desktoppilot.cli import app

    assert "--allow-disabled" in runner.invoke(app, [PLAIN_APP, "ui", "click", "--help"]).output


# ------------------------------------------------------------- an adopted app


@pytest.fixture
def running(tmp_path, monkeypatch):
    profile = Profile(name="fake", exe=["fake.exe"], ready_timeout=0.3, window_size=[1600, 1000])
    reg = Registry()
    ctx = AppContext(name="fake", profile=profile, registry=reg)
    window = {"hwnd": 5, "dips": [1200, 800], "matches_profile": False}
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(apps, "pids", lambda profile: [11])
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 5)
    monkeypatch.setattr(apps, "status", lambda ctx: {"app": ctx.name, "window": window})
    monkeypatch.setattr(apps, "resize", lambda *a: pytest.fail("an adopted window was resized"))
    return ctx, reg, window


def test_an_adopted_app_passes_the_ready_checks_and_reports_its_layout(running):
    ctx, reg, window = running
    checked = []
    reg.hook("ready", lambda ctx: checked.append(1) or True, for_app="fake")

    result = apps.launch(ctx, fresh=False)

    assert result["adopted"] is True and result["ready"] and checked
    # nothing was pinned; the window it has is in `window`, as for any status
    assert result["layout"] is None and result["window"] == window


def test_an_adopted_app_that_never_gets_ready_fails(running):
    ctx, reg, _ = running
    reg.hook("ready", lambda ctx: False, for_app="fake")
    with pytest.raises(Timeout):
        apps.launch(ctx, fresh=False)


# -------------------------------------------------- main_hwnd's while_waiting


def test_a_raising_while_waiting_does_not_hide_the_window(monkeypatch):
    monkeypatch.setattr(
        apps, "windows", lambda profile, main_only=False: [{"hwnd": 7, "rect": [0, 0, 5, 5]}]
    )

    def explode():
        raise OSError("dialog vanished mid-read")

    assert apps.main_hwnd(Profile(name="fake"), timeout=0.5, while_waiting=explode) == 7


def test_a_raising_while_waiting_is_named_when_the_window_never_comes(monkeypatch):
    monkeypatch.setattr(apps, "windows", lambda profile, main_only=False: [])

    def explode():
        raise OSError("dialog vanished mid-read")

    with pytest.raises(Timeout, match="dialog vanished"):
        apps.main_hwnd(Profile(name="fake"), timeout=0.3, while_waiting=explode)


# ------------------------------------------------------- app resize arguments


def test_resize_with_only_a_width_is_an_error(monkeypatch):
    from desktoppilot.cli import app

    resized = []
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 5)
    monkeypatch.setattr(apps, "resize", lambda hwnd, w, h: resized.append((w, h)) or {})
    result = runner.invoke(app, [PLAIN_APP, "app", "resize", "1200"])
    assert result.exit_code == 1 and resized == []
    assert "HEIGHT" in json.loads(result.output)["message"]


# ---------------------------------------------------------- expand --all help


def test_expand_all_help_does_not_claim_depth_first():
    from desktoppilot.cli import app

    output = runner.invoke(
        app, [PLAIN_APP, "ui", "expand", "--help"], env={"COLUMNS": "300"}
    ).output
    assert "breadth first" in output


# --------------------------------- pytest plugin: artifacts and stray dialogs


INNER = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("the app never got ready")

def test_setup_fails(broken):
    pass

def test_passes():
    pass
"""


@pytest.fixture
def inner_run(pytester, monkeypatch):
    captured, closing = [], [[{"title": "Save changes?", "answered": "Cancel"}]]
    monkeypatch.setattr(
        pytest_plugin.artifacts,
        "capture",
        lambda name, ctx, directory: (
            captured.append(name) or {"dir": "d", "saved": {}, "failed": {}}
        ),
    )
    monkeypatch.setattr(pytest_plugin.apps, "pids", lambda profile: [])
    monkeypatch.setattr(pytest_plugin.apps, "launched_here", lambda profile: True)
    monkeypatch.setattr(
        pytest_plugin.dialogs,
        "close_all",
        lambda pids, **k: closing.pop() if closing else [],
    )
    pytester.makepyfile(INNER)
    reports = pytester.inline_run("--app", PLAIN_APP, "-p", "no:cacheprovider").getreports(
        "pytest_runtest_logreport"
    )
    return captured, reports


def test_a_failing_setup_saves_artifacts(inner_run):
    captured, _ = inner_run
    assert any(name.endswith("test_setup_fails") for name in captured)


def test_dialogs_closed_after_a_test_are_reported(inner_run):
    _, reports = inner_run
    teardowns = [r for r in reports if r.when == "teardown"]
    sections = [
        s for r in teardowns for s in r.sections if s[0] == "desktoppilot stray dialogs (teardown)"
    ]
    assert len(sections) == 1 and "Save changes?" in sections[0][1]
    props = [p for r in teardowns for p in r.user_properties]
    assert (
        "desktoppilot_stray_dialogs",
        [{"title": "Save changes?", "answered": "Cancel"}],
    ) in props


def test_dialogs_in_an_adopted_app_are_reported_and_never_clicked(pytester, monkeypatch):
    monkeypatch.setattr(pytest_plugin.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(pytest_plugin.apps, "launched_here", lambda profile: False)
    monkeypatch.setattr(
        pytest_plugin.dialogs, "close_all", lambda *a, **k: pytest.fail("clicked in adopted app")
    )
    monkeypatch.setattr(
        pytest_plugin.dialogs,
        "dialog_windows",
        lambda pids, **k: [{"hwnd": 1, "title": "Save changes?", "text": "Save?"}],
    )
    pytester.makepyfile("def test_passes():\n    pass\n")

    reports = pytester.inline_run("--app", PLAIN_APP, "-p", "no:cacheprovider").getreports(
        "pytest_runtest_logreport"
    )

    sections = [s for r in reports for s in r.sections if "stray dialogs" in s[0]]
    assert len(sections) == 1 and "left open in an adopted app" in sections[0][1]


def test_a_bad_app_name_is_reported_with_the_failure_not_an_internal_error(pytester):
    pytester.makepyfile("def test_fails():\n    assert False\n")

    run = pytester.inline_run("--app", "no-such-app", "-p", "no:cacheprovider")

    assert run.ret == 1  # tests failed, not INTERNALERROR (3)
    calls = [r for r in run.getreports("pytest_runtest_logreport") if r.when == "call"]
    sections = dict(calls[0].sections)
    assert "unknown profile" in sections["desktoppilot artifacts"]
