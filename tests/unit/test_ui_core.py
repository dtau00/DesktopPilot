"""The driver's decision-making: which of the pointer and the UIA patterns
acts, and the pattern commands that replace pointer work.

Stub controls, so none of this needs a real app.
"""

from __future__ import annotations

import time

import pytest

from desktoppilot.core import ui
from desktoppilot.core.errors import NotPresent, PilotError, Timeout, WalkCut

from .compat import COMError

REAL_ACTIVATE_APP, REAL_RESTORE_APP = ui.activate_app, ui.restore_app


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(ui, "activate_app", lambda ctx=None, control=None: None)
    monkeypatch.setattr(ui, "restore_app", lambda ctx=None, control=None: None)


@pytest.fixture(autouse=True)
def pointer(monkeypatch) -> list[tuple]:
    """Every pointer click, as (x, y, button, double)."""
    clicks: list[tuple] = []
    monkeypatch.setattr(
        ui,
        "click_point",
        lambda x, y, button="left", double=False: clicks.append((x, y, button, double)) or "mouse",
    )
    return clicks


# ------------------------------------------------------------ click modes


class Rect:
    def __init__(self, w: int, h: int):
        self.left, self.top = 100, 200
        self._w, self._h = w, h

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


class StubControl:
    """A control that offers the named patterns and records every click."""

    def __init__(self, *, patterns=(), offscreen=False, size=(80, 30)):
        self.patterns = set(patterns)
        self.IsOffscreen = offscreen
        self.BoundingRectangle = Rect(*size)
        self.calls: list[str] = []

    def _pattern(self, name, action):
        if name not in self.patterns:
            raise AttributeError(name)  # uiautomation does this for a wrong type
        control = self

        class Pattern:
            Invoke = Select = Toggle = DoDefaultAction = lambda self: control.calls.append(action)

        return Pattern()

    def GetInvokePattern(self):
        return self._pattern("Invoke", "Invoke")

    def GetSelectionItemPattern(self):
        return self._pattern("SelectionItem", "SelectionItem")

    def GetTogglePattern(self):
        return self._pattern("Toggle", "Toggle")

    def GetLegacyIAccessiblePattern(self):
        return self._pattern("LegacyIAccessible", "LegacyIAccessible")


def click(control, **kwargs):
    return ui.click(control, activate=False, **kwargs)


def test_auto_prefers_a_pattern_over_the_pointer():
    control = StubControl(patterns=["Invoke", "LegacyIAccessible"])
    assert click(control) == "Invoke"
    assert control.calls == ["Invoke"]


def test_auto_falls_back_to_the_pointer_without_a_strong_pattern(pointer):
    control = StubControl(patterns=["LegacyIAccessible"])
    assert click(control) == "mouse"
    assert control.calls == []
    assert pointer == [(140, 215, "left", False)]


def test_auto_takes_the_weak_pattern_when_the_pointer_cannot_reach():
    control = StubControl(patterns=["LegacyIAccessible"], offscreen=True)
    assert click(control) == "LegacyIAccessible"


def test_auto_fails_when_nothing_can_act():
    with pytest.raises(PilotError, match="no way to click"):
        click(StubControl(offscreen=True))


def test_mouse_mode_never_uses_a_pattern(pointer):
    control = StubControl(patterns=["Invoke"])
    assert click(control, mode="mouse") == "mouse"
    assert control.calls == []
    assert pointer == [(140, 215, "left", False)]


def test_mouse_mode_fails_on_an_offscreen_element():
    with pytest.raises(PilotError, match="offscreen"):
        click(StubControl(patterns=["Invoke"], offscreen=True), mode="mouse")


def test_invoke_mode_never_uses_the_pointer():
    control = StubControl(patterns=["LegacyIAccessible"])
    assert click(control, mode="invoke") == "LegacyIAccessible"
    assert control.calls == ["LegacyIAccessible"]


def test_invoke_mode_fails_without_a_pattern():
    with pytest.raises(PilotError, match="no usable UIA pattern"):
        click(StubControl(), mode="invoke")


class Refusing(StubControl):
    """Offers Invoke, and the call fails."""

    def GetInvokePattern(self):
        def refuse(pattern):
            raise OSError("access denied")

        return type("Pattern", (), {"Invoke": refuse})()


def test_invoke_mode_names_the_pattern_that_refused():
    with pytest.raises(PilotError, match="access denied") as err:
        click(Refusing(), mode="invoke")
    assert isinstance(err.value.__cause__, OSError)


def gone_error() -> COMError:
    return COMError(-2147220991, None, (None, None, None, 0, None))


class Unreadable(StubControl):
    """An element whose position read fails: it went away mid-click."""

    def _gone(self):
        raise gone_error()

    BoundingRectangle = property(_gone, lambda self, value: None)


def test_an_unreadable_pointer_target_falls_back_to_the_weak_pattern():
    control = Unreadable(patterns=["LegacyIAccessible"])
    assert click(control) == "LegacyIAccessible"


def test_an_unreadable_pointer_target_is_a_pilot_error():
    with pytest.raises(PilotError, match="pointer cannot reach"):
        click(Unreadable(), mode="mouse")


@pytest.mark.parametrize("kwargs", [{"double": True}, {"button": "right"}])
def test_a_pointer_only_gesture_uses_the_pointer(kwargs, pointer):
    control = StubControl(patterns=["Invoke"])
    assert click(control, **kwargs) == "mouse"
    assert control.calls == []
    assert pointer == [(140, 215, kwargs.get("button", "left"), kwargs.get("double", False))]


@pytest.mark.parametrize("kwargs", [{"double": True}, {"button": "right"}])
def test_invoke_mode_refuses_a_pointer_only_gesture(kwargs):
    with pytest.raises(PilotError, match="needs --mode mouse"):
        click(StubControl(patterns=["Invoke"]), mode="invoke", **kwargs)


def test_an_unknown_mode_is_refused():
    with pytest.raises(PilotError, match="mode must be"):
        click(StubControl(), mode="pointer")


def _record_focus(monkeypatch) -> list[str]:
    calls: list[str] = []

    def record(name):
        return lambda ctx=None, control=None: calls.append(name)

    monkeypatch.setattr(ui, "activate_app", record("activate"))
    monkeypatch.setattr(ui, "restore_app", record("restore"))
    return calls


def test_a_pattern_route_restores_but_does_not_focus(monkeypatch):
    """SetFocus costs time and steals the caret; a pattern click needs neither."""
    calls = _record_focus(monkeypatch)
    assert ui.click(StubControl(patterns=["Invoke"])) == "Invoke"
    assert calls == ["restore"]


def test_the_pointer_route_activates(monkeypatch):
    calls = _record_focus(monkeypatch)
    assert ui.click(StubControl(patterns=[]), mode="mouse") == "mouse"
    assert calls == ["activate"]


def test_an_auto_fallback_to_the_pointer_activates_late(monkeypatch):
    calls = _record_focus(monkeypatch)
    assert ui.click(StubControl(patterns=["LegacyIAccessible"])) == "mouse"
    assert calls == ["restore", "activate"]


# ------------------------------------------------------- app with no profile


class TopLevel:
    NativeWindowHandle = 7

    def __init__(self):
        self.calls: list[str] = []

    def SetActive(self, **_):
        self.calls.append("SetActive")

    def SetFocus(self):
        self.calls.append("SetFocus")


class OwnedControl(StubControl):
    """A control whose top-level window is a stub, so activation is visible."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.window = TopLevel()

    def GetTopLevelControl(self):
        return self.window


@pytest.fixture
def desktop_ctx(monkeypatch):
    """`desktop`: no profile, so no main window - reaching for one is the bug."""
    from desktoppilot.core.registry import AppContext, Registry

    def no_main_window(*args, **kwargs):
        raise AssertionError("main_window called for an app with no profile")

    monkeypatch.setattr(ui, "activate_app", REAL_ACTIVATE_APP)
    monkeypatch.setattr(ui, "restore_app", REAL_RESTORE_APP)
    monkeypatch.setattr(ui, "main_window", no_main_window)
    return AppContext(name="desktop", profile=None, registry=Registry())


def test_a_null_app_click_activates_the_controls_own_window(desktop_ctx, pointer):
    control = OwnedControl(patterns=[])
    assert ui.click(control, ctx=desktop_ctx, mode="mouse") == "mouse"
    assert control.window.calls == ["SetActive", "SetFocus"]
    assert len(pointer) == 1


def test_a_null_app_pattern_click_restores_only_a_minimized_window(desktop_ctx, monkeypatch):
    monkeypatch.setattr(ui.win32gui, "IsIconic", lambda hwnd: hwnd == 7)
    control = OwnedControl(patterns=["Invoke"])
    assert ui.click(control, ctx=desktop_ctx) == "Invoke"
    assert control.window.calls == ["SetActive"]


def test_a_null_app_with_no_control_activates_nothing(desktop_ctx):
    ui.activate_app(desktop_ctx)
    ui.restore_app(desktop_ctx)


def test_relative_needs_an_app_with_a_window(desktop_ctx):
    with pytest.raises(PilotError, match="--relative needs an app with a window; desktop"):
        ui.window_origin(desktop_ctx)


# ---------------------------------------------------------- capture origin


class ScreenRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Windowed:
    def __init__(self, rect, handle=0, parent=None):
        self.BoundingRectangle = ScreenRect(*rect)
        self.NativeWindowHandle = handle
        self.parent = parent

    def GetParentControl(self):
        return self.parent


@pytest.fixture
def dwm(monkeypatch):
    """Handle 1 is a top-level window whose DWM frame sits 7 px inside its rect."""
    monkeypatch.setattr(ui.win32gui, "GetAncestor", lambda handle, flag: 1)
    monkeypatch.setattr(ui.auto, "DwmIsCompositionEnabled", lambda: True)
    monkeypatch.setattr(
        ui.auto,
        "DwmGetWindowExtendFrameBounds",
        lambda handle: ScreenRect(107, 200, 1093, 993) if handle == 1 else None,
    )


@pytest.mark.windows("the DWM frame")
def test_a_top_level_window_is_shot_as_its_dwm_frame(dwm):
    window = Windowed((100, 200, 1100, 1000), handle=1)
    assert ui.capture_origin(window) == (107, 200, 986, 793)


def test_a_child_control_is_shot_as_its_bounding_rectangle(dwm):
    child = Windowed((300, 400, 350, 420), handle=2)
    assert ui.capture_origin(child) == (300, 400, 50, 20)


def test_a_control_with_no_size_is_shot_as_its_first_sized_ancestor(dwm):
    parent = Windowed((10, 20, 60, 80))
    assert ui.capture_origin(Windowed((5, 5, 5, 5), parent=parent)) == (10, 20, 50, 60)


@pytest.mark.windows("the DWM frame")
def test_relative_measures_from_the_origin_of_a_window_shot(dwm, monkeypatch):
    from desktoppilot.core.profiles import Profile
    from desktoppilot.core.registry import AppContext, Registry

    window = Windowed((100, 200, 1100, 1000), handle=1)
    monkeypatch.setattr(ui, "main_window", lambda ctx: window)
    ctx = AppContext(name="notepad", profile=Profile(name="notepad"), registry=Registry())
    assert ui.window_origin(ctx) == (107, 200)


# --------------------------------------------------------- value patterns


class ValueControl:
    def __init__(self, *, value="", read_only=False, supported=True):
        self.value, self.read_only, self.supported = value, read_only, supported

    def GetValuePattern(self):
        if not self.supported:
            raise AttributeError("GetValuePattern")
        owner = self

        class Pattern:
            IsReadOnly = owner.read_only

            @property
            def Value(self):
                return owner.value

            def SetValue(self, text, **_):
                owner.value = text

        return Pattern()

    Name = "stub"
    ControlTypeName = "EditControl"
    AutomationId = ClassName = ""
    IsEnabled = True
    IsOffscreen = False

    class BoundingRectangle:
        left = top = 0

        @staticmethod
        def width():
            return 10

        @staticmethod
        def height():
            return 10


def test_set_value_writes_through_the_pattern():
    control = ValueControl(value="old")
    result = ui.set_value(control, "new")
    assert (result["value"], result["verified"]) == ("new", True)
    assert control.value == "new"


def test_set_value_reports_an_unverified_write_when_the_element_goes():
    class Vanishing(ValueControl):
        def GetValuePattern(self):
            owner = self

            class Pattern:
                IsReadOnly = False

                @property
                def Value(self):
                    raise gone_error()

                def SetValue(self, text, **_):
                    owner.value = text

            return Pattern()

    control = Vanishing()
    result = ui.set_value(control, "new")
    assert control.value == "new"
    assert (result["set"], result["value"], result["verified"]) == ("new", None, False)


def test_set_value_clears():
    control = ValueControl(value="old")
    ui.set_value(control, "")
    assert control.value == ""


def test_set_value_refuses_a_read_only_control():
    with pytest.raises(PilotError, match="read-only"):
        ui.set_value(ValueControl(read_only=True), "x")


def test_set_value_refuses_a_control_without_the_pattern():
    with pytest.raises(PilotError, match="no Value pattern"):
        ui.set_value(ValueControl(supported=False), "x")


class ScrollControl:
    def __init__(self, *, vertical=True, horizontal=False, supported=True):
        self.vertical, self.horizontal, self.supported = vertical, horizontal, supported
        self.set_to = None

    def GetScrollPattern(self):
        if not self.supported:
            raise AttributeError("GetScrollPattern")
        owner = self

        class Pattern:
            NoScrollValue = -1
            VerticallyScrollable = owner.vertical
            HorizontallyScrollable = owner.horizontal
            VerticalScrollPercent = 0.0
            HorizontalScrollPercent = 0.0
            VerticalViewSize = 5.0

            def SetScrollPercent(self, h, v, **_):
                owner.set_to = (h, v)

        return Pattern()


def test_scroll_sets_the_vertical_percent_only():
    control = ScrollControl()
    ui.scroll(control, vertical=50)
    assert control.set_to == (-1, 50)  # -1 = NoScrollValue, leave the axis alone


def test_scroll_needs_an_axis():
    with pytest.raises(PilotError, match="0-100"):
        ui.scroll(ScrollControl())


def test_scroll_refuses_an_out_of_range_percent():
    with pytest.raises(PilotError, match="must be 0-100"):
        ui.scroll(ScrollControl(), vertical=140)


def test_scroll_refuses_an_axis_the_control_does_not_have():
    with pytest.raises(PilotError, match="not scroll horizontally"):
        ui.scroll(ScrollControl(), horizontal=10)


def test_scroll_refuses_a_control_without_the_pattern():
    with pytest.raises(PilotError, match="no Scroll pattern"):
        ui.scroll(ScrollControl(supported=False), vertical=10)


class SelectControl:
    """A combo box: it expands, and its items live in a popup elsewhere."""

    Name = "Editing"

    def __init__(self, *, selectable=False, expandable=True):
        self.selectable, self.expandable = selectable, expandable
        self.selected = self.expanded = False

    def GetSelectionItemPattern(self):
        if not self.selectable:
            raise AttributeError("GetSelectionItemPattern")
        owner = self

        class Pattern:
            def Select(self):
                owner.selected = True

        return Pattern()

    def GetExpandCollapsePattern(self):
        if not self.expandable:
            raise AttributeError("GetExpandCollapsePattern")
        owner = self

        class Pattern:
            ExpandCollapseState = 0  # Collapsed

            def Expand(self, **_):
                owner.expanded = True

        return Pattern()


def test_select_without_an_item_selects_the_control():
    control = SelectControl(selectable=True)
    assert ui.select_item(control)["selected"] == "Editing"
    assert control.selected


def test_select_without_an_item_reads_the_item_before_it_dies_and_says_via(monkeypatch):
    """A radio menu item: selecting it closes the menu and kills the item, so
    its label is read first; `via` is there as with --item."""

    class Radio(SelectControl):
        ControlTypeName = "MenuItemControl"

        def __init__(self):
            super().__init__(selectable=True)
            self.dead = False

        @property
        def Name(self):
            if self.dead:
                raise RuntimeError("the element is gone")
            return "Large"

        def GetSelectionItemPattern(self):
            owner = self

            class Pattern:
                def Select(self):
                    owner.dead = True

            return Pattern()

    monkeypatch.setattr(ui, "describe", lambda control, **_: {"name": control.Name})
    result = ui.select_item(Radio())
    assert (result["selected"], result["expanded"], result["via"]) == (
        "Large",
        False,
        "SelectionItem",
    )
    assert result["element"] == {"name": "Large"}


def test_select_without_an_item_needs_the_pattern():
    with pytest.raises(PilotError, match="name an --item"):
        ui.select_item(SelectControl())


def test_select_expands_then_clicks_the_item_in_the_popup(monkeypatch):
    control = SelectControl()

    class Item:
        Name = "Editing"

    item = Item()
    clicked = []
    monkeypatch.setattr(
        ui, "_find_item", lambda scope, name, **_: item if scope == "popup" else None
    )
    monkeypatch.setattr(ui, "popup_roots", lambda deadline=None: ["popup"])
    monkeypatch.setattr(ui, "click", lambda target, **_: clicked.append(target) or "Invoke")

    result = ui.select_item(control, "Editing", timeout=1)

    assert (result["selected"], result["expanded"], result["via"]) == ("Editing", True, "Invoke")
    assert clicked == [item]


def test_select_hunts_only_item_types():
    """A Group named like the item must never match - it once changed Word's
    editing mode."""
    assert ui._find_item(Node("Pane", "", [Node("Group", "Editing")]), "Editing") is None


def test_select_walks_a_scope_once_and_ranks_the_item_types():
    radio, item = Node("RadioButton", "Editing"), Node("Button", "Editing ")
    inner = Node("Pane", "", [radio, item])
    scope = Node("Pane", "", [Node("Group", "Editing"), inner])
    assert ui._find_item(scope, "editing") is item
    assert (scope.walked, inner.walked) == (1, 1)


def test_a_cut_item_hunt_raises_walk_cut():
    with pytest.raises(WalkCut):
        ui._find_item(Node("Pane", "", [Node("ListItem", "x")]), "x", deadline=0.0)


def test_a_select_whose_hunts_are_all_cut_says_busy(monkeypatch):
    def cut(scope, name, **_):
        raise WalkCut("cut")

    monkeypatch.setattr(ui, "_find_item", cut)
    monkeypatch.setattr(ui, "popup_roots", lambda deadline=None: [])
    with pytest.raises(WalkCut, match="busy"):
        ui.select_item(SelectControl(), "Editing", timeout=0.3)


def test_a_select_that_finds_nothing_is_not_found(monkeypatch):
    monkeypatch.setattr(ui, "_find_item", lambda scope, name, **_: None)
    monkeypatch.setattr(ui, "popup_roots", lambda deadline=None: [])
    with pytest.raises(Timeout) as err:
        ui.select_item(SelectControl(), "Editing", timeout=0.3)
    assert not isinstance(err.value, WalkCut)


# ------------------------------------------------------------- stale guard


class DeadControl:
    """An element that acted on the UI and unmounted doing it."""

    @property
    def BoundingRectangle(self):
        raise OSError("element not available")

    @property
    def ControlTypeName(self):
        raise OSError("element not available")

    @property
    def Name(self):
        raise OSError("element not available")

    def GetChildren(self):
        raise OSError("element not available")


def test_describe_reports_a_stale_element_instead_of_raising():
    info = ui.describe(DeadControl())
    assert info["stale"] is True
    assert "not available" in info["detail"]


def test_a_stale_element_is_simply_not_a_match():
    from desktoppilot.core.selectors import Step

    assert ui._matches(DeadControl(), Step("Button", True)) is False
    assert ui._children(DeadControl()) == []


def test_text_of_falls_back_to_the_text_pattern():
    class Document:
        def GetValuePattern(self):
            raise AttributeError("GetValuePattern")

        def GetLegacyIAccessiblePattern(self):
            raise AttributeError("GetLegacyIAccessiblePattern")

        def GetTextPattern(self):
            class Range:
                def GetText(self, _):
                    return "hello"

            return type("Pattern", (), {"DocumentRange": Range()})()

    assert ui.text_of(Document()) == "hello"


# ------------------------------------------------------------- search walk


class Node:
    """A tree node that counts how often its children were asked for."""

    def __init__(self, kind: str, name: str = "", children=()):
        self.ControlTypeName = kind + "Control"
        self.Name = name
        self.IsOffscreen = False
        self.children = list(children)
        self.walked = 0

    def GetChildren(self):
        self.walked += 1
        return self.children


def test_search_stops_at_the_limit_with_the_same_first_hit():
    from desktoppilot.core.selectors import Step

    deep = Node("Button", "deep")
    tree = Node("Pane", "", [Node("Button", "a"), Node("Pane", "", [deep]), Node("Button", "b")])
    first = ui.search(tree, Step("Button", True), limit=1)
    assert tree.children[1].walked == 0, "nothing past the limit is walked"
    full = ui.search(tree, Step("Button", True))
    assert [c.Name for c in full] == ["a", "b", "deep"]
    assert first == full[:1]


def test_search_prune_skips_the_subtree_but_still_matches_the_node():
    from desktoppilot.core.selectors import Step

    inner = Node("Button", "inner")
    pruned = Node("Button", "Ribbon", [inner])
    tree = Node("Pane", "", [pruned, Node("Button", "other")])
    seen = []

    def prune(control, depth):
        seen.append((control.Name, depth))
        return control.Name == "Ribbon"

    hits = ui.search(tree, Step("Button", True), prune=prune)
    assert [c.Name for c in hits] == ["Ribbon", "other"]
    assert pruned.walked == 0
    assert ("Ribbon", 1) in seen


def test_search_prunes_a_node_whose_prune_check_raises():
    from desktoppilot.core.selectors import Step

    tree = Node("Pane", "", [Node("Pane", "", [Node("Button", "gone")])])

    def prune(control, depth):
        raise OSError("element not available")

    assert ui.search(tree, Step("Button", True), prune=prune) == []


SLACK = 0.2  # scheduling jitter on a loaded machine


class SlowNode(Node):
    """A node whose children read blocks, as on a busy app."""

    delay = 0.05

    def GetChildren(self):
        time.sleep(self.delay)
        return super().GetChildren()


def slow_tree(width: int = 6, depth: int = 3) -> SlowNode:
    if depth == 0:
        return SlowNode("Button", "leaf")
    return SlowNode("Pane", "", [slow_tree(width, depth - 1) for _ in range(width)])


def test_a_deadline_bounds_the_wall_time_of_a_walk_on_a_busy_app():
    from desktoppilot.core.selectors import Step

    tree = slow_tree()  # 259 reads at 0.05 s: 13 s unbounded
    start = time.monotonic()
    with pytest.raises(WalkCut) as err:
        ui.search(tree, Step("Button", True), deadline=start + 0.3)
    elapsed = time.monotonic() - start
    assert elapsed < 0.3 + SlowNode.delay + SLACK, "overruns by one blocked call at most"
    assert len(err.value.hits) < 6**3, "the hits so far travel with the cut"


def test_a_cut_walk_raises_rather_than_answering_an_empty_list():
    from desktoppilot.core.selectors import Step

    tree = SlowNode("Pane", "", [SlowNode("Pane", "", [SlowNode("Button", "deep")])])
    with pytest.raises(WalkCut):
        ui.search(tree, Step("Button", True), deadline=time.monotonic() + 0.01)
    with pytest.raises(WalkCut):
        ui.search(tree, Step("Button", False), deadline=time.monotonic() - 1)


def test_a_children_read_the_app_timed_out_cuts_the_walk():
    from desktoppilot.core.selectors import Step

    from .compat import COMError

    class TimedOut(Node):
        def GetChildren(self):
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    class Gone(Node):
        def GetChildren(self):
            raise COMError(-2147220991, None, (None, None, None, 0, None))

    found = Node("Button", "a")
    with pytest.raises(WalkCut) as err:
        ui.search(Node("Pane", "", [found, TimedOut("Pane")]), Step("Button", True))
    assert err.value.hits == [found]
    assert ui.search(Node("Pane", "", [found, Gone("Pane")]), Step("Button", True)) == [found]


def test_a_walk_that_ends_before_its_deadline_is_complete():
    from desktoppilot.core.selectors import Step

    tree = Node("Pane", "", [Node("Button", "a"), Node("Pane", "", [Node("Button", "b")])])
    hits = ui.search(tree, Step("Button", True), deadline=time.monotonic() + 30)
    assert [c.Name for c in hits] == ["a", "b"]


def test_a_cut_walk_is_not_gone(monkeypatch):
    def cut(*args, **kwargs):
        raise WalkCut("cut")

    monkeypatch.setattr(ui, "find_all", cut)
    assert ui.is_gone("ribbon//Button", None) is False


def test_find_gives_the_root_what_is_left_of_its_wait(monkeypatch):
    given = []

    def resolve(sel, ctx, timeout):
        given.append(timeout)
        return Node("Pane", "", [Node("Button", "ok")])

    monkeypatch.setattr(ui, "resolve_root", resolve)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    ui.find("app//Button", None, timeout=30)
    assert given[0] > 25


class Cuttable(Node):
    """A root whose first `complete` children reads answer, and every later
    one times out, as a Word that turns busy mid-wait does."""

    def __init__(self, complete: int):
        super().__init__("Pane", "", [Node("Text", "no button here")])
        self.complete = complete

    def GetChildren(self):
        from .compat import COMError

        if self.walked >= self.complete:
            raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))
        return super().GetChildren()


@pytest.mark.parametrize(("selector", "name"), [("app//Button", "ok"), ("app//Button#2", "deep")])
def test_find_stops_each_walk_at_the_match_it_takes(monkeypatch, selector, name):
    deep = Node("Button", "deep", [Node("Pane", "", [Node("Button", "deeper")])])
    later = Node("Pane", "", [deep])
    root = Node("Pane", "", [Node("Button", "ok"), later])
    monkeypatch.setattr(ui, "resolve_root", lambda sel, ctx, timeout: root)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    assert ui.find(selector, None, timeout=1).Name == name
    assert deep.walked == 0


def find_under(monkeypatch, root, timeout: float = 1.0):
    monkeypatch.setattr(ui, "resolve_root", lambda sel, ctx, timeout: root)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    return ui.find("app//Button[name='NoSuchThing']", None, timeout=timeout)


def test_a_find_whose_walks_all_complete_times_out_as_not_found(monkeypatch):
    with pytest.raises(Timeout) as err:
        find_under(monkeypatch, Cuttable(complete=10**6))
    assert not isinstance(err.value, WalkCut)
    assert "complete walk(s) found no match" in str(err.value)
    assert "busy" not in str(err.value)


def test_a_find_whose_walks_are_all_cut_says_the_app_is_busy(monkeypatch):
    with pytest.raises(WalkCut) as err:
        find_under(monkeypatch, Cuttable(complete=0))
    assert "cut" in str(err.value) and "busy" in str(err.value)
    assert "found no match" not in str(err.value)


def test_a_find_cut_only_at_the_end_is_still_not_found(monkeypatch):
    root = Cuttable(complete=2)
    with pytest.raises(Timeout) as err:
        find_under(monkeypatch, root)
    assert not isinstance(err.value, WalkCut)
    assert "2 complete walk(s) found no match" in str(err.value)
    assert "more cut)" in str(err.value)
    assert "busy" not in str(err.value)


# ------------------------------------------------------------------ roots


def test_a_present_probe_looks_once():
    looks = []
    with pytest.raises(NotPresent, match="the pane is not there"):
        ui.present(lambda: looks.append(1), timeout=10, what="the pane", probe=True)
    assert looks == [1]


def test_a_present_wait_polls_until_the_root_appears():
    answers = iter([None, None, "root"])
    assert ui.present(lambda: next(answers), timeout=5, what="the pane") == "root"
    with pytest.raises(NotPresent, match="the pane"):
        ui.present(lambda: None, timeout=0.1, what="the pane")


def test_valid_roots_are_core_plus_plugin_and_app_needs_a_profile():
    from desktoppilot.core.profiles import Profile
    from desktoppilot.core.registry import AppContext, Registry

    reg = Registry()
    reg.root("pane", lambda arg, timeout: None, for_app="word")
    reg.root("app", lambda arg, timeout: None, for_app="word")
    no_profile = AppContext(name="word", profile=None, registry=reg)
    with_profile = AppContext(name="word", profile=Profile(name="word"), registry=reg)

    assert ui.valid_roots(no_profile) == ["win", "focus", "dialog", "desktop", "app", "pane"]
    assert ui.valid_roots(with_profile) == ["app", "win", "focus", "dialog", "desktop", "pane"]
    assert ui.valid_roots(None) == ["win", "focus", "dialog", "desktop"]


# ------------------------------------------------------------ hover


@pytest.fixture
def cursor(monkeypatch) -> dict:
    """The pointer: move_point puts it at a point, GetCursorPos reads it."""
    from desktoppilot.core import actions

    state = {"at": (0, 0), "moves": []}

    def move(x, y):
        state["at"] = (x, y)
        state["moves"].append((x, y))
        return "mouse"

    monkeypatch.setattr(ui, "move_point", move)
    monkeypatch.setattr(actions.win32api, "GetCursorPos", lambda: state["at"])
    return state


def test_hover_moves_to_the_centre_and_leaves_the_pointer_there(cursor):
    done = ui.hover(StubControl())
    assert done == {"x": 140, "y": 215, "via": "mouse", "held": True}
    assert cursor["moves"] == [(140, 215)]


def test_hover_of_an_offscreen_element_moves_nothing(cursor):
    with pytest.raises(PilotError, match="the pointer cannot reach it"):
        ui.hover(StubControl(offscreen=True))
    assert cursor["moves"] == []


def test_a_dwell_holds_for_its_seconds(cursor):
    start = time.monotonic()
    assert ui.hover_point(5, 6, dwell=0.3)["held"] is True
    assert time.monotonic() - start >= 0.3


def test_a_dwell_ends_early_when_something_else_moves_the_pointer(cursor, monkeypatch):
    from desktoppilot.core import actions

    reads = iter([(5, 6), (9, 9)])
    monkeypatch.setattr(actions.win32api, "GetCursorPos", lambda: next(reads, (9, 9)))
    start = time.monotonic()
    assert ui.hover_point(5, 6, dwell=10)["held"] is False
    assert time.monotonic() - start < 2
