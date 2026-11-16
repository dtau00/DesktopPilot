"""Counting what a list or a tree really holds, not what it has realized.

Fakes shaped like the UIA surface ui.count_items touches: a virtualized list
whose ItemContainerPattern ends on a NULL COM pointer, and a WPF tree that
builds a node's children only once it is expanded.
"""

from __future__ import annotations

import pytest

from desktoppilot.core import count as ui_count
from desktoppilot.core import ui, uiacache
from desktoppilot.core.errors import PilotError, Timeout, WalkCut

from .compat import COMError


class NullPointer:
    """What comtypes hands back at the end of the walk: falsy, but not None."""

    def __bool__(self) -> bool:
        return False


class Element:
    def __init__(self, name: str):
        self.CurrentName = name


class ItemContainer:
    """The raw pattern: FindItemByProperty(after, 0, 0) returns the next item."""

    def __init__(self, names: list[str] | None, *, end=None):
        self.names = names
        self.end = NullPointer() if end is None else end
        self.calls = 0

    def FindItemByProperty(self, after, prop, value):
        self.calls += 1
        index = 0 if after is None else after.index + 1
        if self.names is not None and index >= len(self.names):
            return self.end
        item = Element(self.names[index] if self.names else f"item {index}")
        item.index = index
        return item


class Wrapper:
    def __init__(self, raw):
        self.pattern = raw


class Scroll:
    VerticallyScrollable = True
    VerticalViewSize = 4.22


class ListControl:
    ControlTypeName = "ListControl"
    Name = ""

    def __init__(self, container=None, realized: int = 5, scroll=None):
        self.container = container
        self.realized = [Row(f"row {i}") for i in range(realized)]
        self.scroll = scroll

    def GetPattern(self, pattern_id):
        return Wrapper(self.container) if self.container else None

    def GetChildren(self):
        return self.realized

    def GetScrollPattern(self):
        if self.scroll is None:
            raise AttributeError("no scroll")
        return self.scroll


class Row:
    ControlTypeName = "ListItemControl"

    def __init__(self, name: str):
        self.Name = name

    def GetChildren(self):
        return []


def test_the_walk_ends_on_a_falsy_null_pointer_that_is_not_none():
    container = ItemContainer([f"term {i}" for i in range(63)])
    result = ui.count_items(ListControl(container))

    assert result["count"] == 63
    assert result["via"] == "ItemContainerPattern"
    assert result["names"][:2] == ["term 0", "term 1"]
    assert len(result["names"]) == ui.DEFAULT_NAMES
    assert container.calls == 64


def test_a_none_end_also_stops_the_walk():
    container = ItemContainer(["a", "b"])
    container.end = None
    assert ui.count_items(ListControl(container))["count"] == 2


def test_a_provider_that_never_ends_is_capped_and_never_counted(monkeypatch):
    monkeypatch.setattr(ui, "COUNT_CAP", 250)
    with pytest.raises(WalkCut, match="cap") as err:
        ui.count_items(ListControl(ItemContainer(None)), names=3)
    assert err.value.count == 250


def test_without_the_pattern_it_counts_children_and_flags_a_virtualized_list():
    result = ui.count_items(ListControl(realized=5, scroll=Scroll()))

    assert result["count"] == 5
    assert result["via"] == "children"
    assert result["names"][0] == "row 0"
    assert "virtualized" in result["virtualized_hint"]


def test_children_of_a_list_that_fits_carry_no_hint():
    result = ui.count_items(ListControl(realized=3))
    assert result == {"count": 3, "via": "children", "names": ["row 0", "row 1", "row 2"]}


class Tab(Row):
    """A Notepad tab: its children are its title and its close button."""

    ControlTypeName = "TabItemControl"

    def GetChildren(self):
        return [Row("title"), Row("Close Tab")]


def test_counting_one_item_says_to_count_its_list_instead():
    result = ui.count_items(Tab("Untitled. Unmodified."))
    assert result["count"] == 2 and result["via"] == "children"
    assert "TabItem" in result["item_hint"] and "list that holds it" in result["item_hint"]


class Box:
    """A layout node: a Pane, Group, Custom, List or Menu holding `kids`."""

    def __init__(self, kind: str, kids: list, name: str = ""):
        self.ControlTypeName, self.Name, self.kids = f"{kind}Control", name, kids

    def GetChildren(self):
        return self.kids

    def GetPattern(self, pattern_id):
        return None


def test_rows_inside_an_unnamed_wrapper_are_counted_not_the_wrapper():
    """VS Code's command palette: List > unnamed Group > ListItems."""
    palette = Box("List", [Box("Group", [Row("New File"), Row("Open"), Row("Save")])])
    result = ui.count_items(palette)
    assert (result["count"], result["via"]) == (3, "children")
    assert result["names"] == ["New File", "Open", "Save"]


def test_a_combo_counts_the_options_of_its_drop_down():
    """Chromium's <select>: ComboBox > menu list > MenuItems."""
    option = Row("A")
    option.ControlTypeName = "MenuItemControl"
    other = Row("B")
    other.ControlTypeName = "MenuItemControl"
    combo = Box("ComboBox", [Box("Menu", [option, other], name="Pick")])
    assert ui.count_items(combo)["count"] == 2


def test_a_named_group_and_a_wrapper_without_rows_keep_the_children_count():
    named = Box("List", [Box("Group", [Row("a"), Row("b")], name="Recent")])
    assert ui.count_items(named)["count"] == 1
    empty = Box("List", [Box("Pane", [Label("x"), Label("y")])])
    assert ui.count_items(empty)["count"] == 1


def test_a_list_with_rows_of_its_own_ignores_wrappers_beside_them():
    mixed = Box("List", [Row("a"), Box("Group", [Row("b"), Row("c")])])
    assert ui.count_items(mixed)["count"] == 2


# -------------------------------------------------------------------- tree


class Expander:
    def __init__(self, node):
        self.node = node

    @property
    def ExpandCollapseState(self):
        if not self.node.hidden:
            return 3  # LeafNode
        return 1 if self.node.open else 0

    def Expand(self, waitTime=0):
        self.node.open = True


class Label:
    ControlTypeName = "TextControl"

    def __init__(self, text: str):
        self.Name = text

    def GetChildren(self):
        return []


class TreeNode:
    """A WPF tree row: its children exist only once it is expanded."""

    ControlTypeName = "TreeItemControl"
    Name = "MyAddin.UI.Tree.NodeViewModel"

    def __init__(self, text: str, children=()):
        self.text = text
        self.hidden = list(children)
        self.open = False

    def GetChildren(self):
        return [Label(self.text)] + (self.hidden if self.open else [])

    def GetExpandCollapsePattern(self):
        return Expander(self)


class Tree:
    ControlTypeName = "TreeControl"
    Name = ""

    def __init__(self, roots):
        self.roots = roots

    def GetChildren(self):
        return self.roots

    def GetPattern(self, pattern_id):
        # A tree's ItemContainerPattern sees its top level only.
        return Wrapper(ItemContainer([r.text for r in self.roots]))


def outline() -> Tree:
    return Tree(
        [
            TreeNode(
                "1 Services", [TreeNode("1.1 Scope", [TreeNode("(a) Fees")]), TreeNode("1.2")]
            ),
            TreeNode("2 Term"),
            TreeNode("3 Law", [TreeNode("3.1 Venue")]),
        ]
    )


def test_the_wrapper_walk_opens_every_collapsed_node_depth_first():
    tree = outline()
    result = ui.expand_all(tree, names=10)

    assert result["nodes"] == 7
    assert result["depths"] == {0: 3, 1: 3, 2: 1}
    assert result["expanded"] == 3
    assert result["complete"] is True
    assert result["names"] == [
        "1 Services",
        "1.1 Scope",
        "(a) Fees",
        "1.2",
        "2 Term",
        "3 Law",
        "3.1 Venue",
    ]


def test_a_past_deadline_counts_what_is_visible_without_expanding():
    result = ui.expand_all(outline(), deadline=0.0)
    assert result["nodes"] == 3
    assert result["expanded"] == 0
    assert result["complete"] is False


def test_count_on_a_tree_expands_it_instead_of_trusting_its_top_level():
    result = ui.count_items(outline())

    assert result["count"] == 7
    assert result["via"] == "expand"
    assert result["passes"] == [7, 7]
    assert result["complete"] is True


class FakeTime:
    """ui's clock, advanced only by the tree below: the deadline is crossed
    exactly where a test says."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        pass


class TimedTree(Tree):
    """A tree whose top-level read costs `step` seconds, as on a busy Word."""

    def __init__(self, roots, clock: FakeTime, step: float):
        super().__init__(roots)
        self.clock = clock
        self.step = step

    def GetChildren(self):
        self.clock.now += self.step
        return self.roots


def timed_outline(monkeypatch, step: float) -> TimedTree:
    clock = FakeTime()
    monkeypatch.setattr(ui_count, "time", clock)  # count.py reads the clock, not ui
    return TimedTree(outline().roots, clock, step)


def test_a_pass_cut_at_the_deadline_is_never_the_count(monkeypatch):
    # Pass 1 ends at 6 s of 10; pass 2 reads the top level at 12 s, past it.
    result = ui.count_items(timed_outline(monkeypatch, 6.0), timeout=10.0)

    assert result["count"] == 7
    assert result["count"] != 3
    assert result["complete"] is False
    assert result["passes"] == [7]
    assert result["cut_pass"] == 3


def test_no_complete_pass_raises_walk_cut_instead_of_a_number(monkeypatch):
    with pytest.raises(WalkCut) as err:
        ui.count_items(timed_outline(monkeypatch, 11.0), timeout=10.0)

    message = str(err.value)
    assert "unknown" in message
    assert "3 node(s) reached" in message
    assert isinstance(err.value, Timeout)


def test_two_agreeing_complete_passes_are_complete():
    result = ui.count_items(outline())

    assert result["passes"] == [7, 7]
    assert result["complete"] is True
    assert "cut_pass" not in result


def test_no_expand_counts_the_top_level_through_the_pattern():
    result = ui.count_items(outline(), expand=False)
    assert (result["count"], result["via"]) == (3, "ItemContainerPattern")


def test_expand_one_node():
    node = TreeNode("1", [TreeNode("1.1")])
    assert ui.expand(node) == {"expanded": True, "state": "expanded"}
    assert node.open is True
    assert ui.expand(node) == {"expanded": False, "state": "expanded"}
    assert ui.expand(TreeNode("leaf")) == {"expanded": False, "state": "leaf"}


def test_expand_refuses_a_control_without_the_pattern():
    with pytest.raises(PilotError, match="ExpandCollapse"):
        ui.expand(Row("x"))


def test_tree_nodes_does_not_probe_a_tree_items_text_children():
    probed = []

    class Text(Label):
        def GetExpandCollapsePattern(self):
            probed.append(self.Name)
            return Expander(TreeNode("x"))

    class Row(TreeNode):
        def GetChildren(self):
            return [Text("a"), Text("b")]

    assert ui._tree_nodes(Row("1")) == []
    assert probed == []


def timeout_error() -> COMError:
    return COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))


class BusyExpander(Expander):
    """Expand times out; with `state`, the state read does too."""

    def __init__(self, node, *, state: bool):
        super().__init__(node)
        self.state = state

    @property
    def ExpandCollapseState(self):
        if self.state:
            raise timeout_error()
        return super().ExpandCollapseState

    def Expand(self, waitTime=0):
        raise timeout_error()


@pytest.mark.parametrize("state", [False, True])
def test_a_timed_out_expand_is_a_cut_pass_not_a_leaf(state):
    class BusyNode(TreeNode):
        def GetExpandCollapsePattern(self):
            return BusyExpander(self, state=state)

    tree = Tree([TreeNode("1"), BusyNode("2", [TreeNode("2.1")])])
    assert ui.expand_all(tree)["complete"] is False
    with pytest.raises(WalkCut):
        ui.count_items(tree)


def test_an_expand_refused_for_another_reason_keeps_the_pass_complete():
    class Refusing(Expander):
        def Expand(self, waitTime=0):
            raise OSError("not expandable")

    class Stubborn(TreeNode):
        def GetExpandCollapsePattern(self):
            return Refusing(self)

    result = ui.expand_all(Tree([Stubborn("1", [TreeNode("1.1")])]))
    assert (result["nodes"], result["complete"]) == (1, True)


def test_a_tree_without_a_raw_element_uses_the_walk():
    result = ui.expand_all(outline())

    assert result["via"] == "walk"
    assert (result["nodes"], result["expanded"], result["complete"]) == (7, 3, True)
    assert result["depths"] == {0: 3, 1: 3, 2: 1}


# ------------------------------------------------------ the cached surface


class FakeArray:
    def __init__(self, elements):
        self.elements = elements
        self.Length = len(elements)

    def GetElement(self, i):
        return self.elements[i]


class CachedExpander:
    def __init__(self, element):
        self.element = element

    def QueryInterface(self, iid):
        return self

    def Expand(self):
        self.element.open = True


class FakeElement:
    """A raw IUIAutomationElement of a WPF tree: a collapsed node's children
    are absent until it opens."""

    def __init__(self, name: str, children=(), surface=None):
        self.name = name
        self.children = list(children)
        self.open = False
        self.surface = surface

    @property
    def state(self) -> int:
        if not self.children:
            return 3
        return 1 if self.open else 0

    def FindAllBuildCache(self, scope, condition, request):
        self.surface.fetch(self)
        return FakeArray(self.children if self.open else [])

    def GetCachedPropertyValue(self, prop):
        return {30070: self.state, 30005: self.name}[prop]

    def GetCachedPattern(self, pattern_id):
        return CachedExpander(self)


class Surface:
    """Counts fetches; optionally advances a FakeTime clock per fetch."""

    def __init__(self, clock=None, step: float = 0.0):
        self.fetches = 0
        self.clock = clock
        self.step = step

    def fetch(self, element) -> None:
        self.fetches += 1
        if self.clock is not None:
            self.clock.now += self.step


class CachedNode:
    ControlTypeName = "TreeControl"
    Name = ""

    def __init__(self, element):
        self.Element = element

    def GetChildren(self):
        return []


def cached_outline(surface: Surface) -> CachedNode:
    def node(name, *kids):
        return FakeElement(name, kids, surface)

    root = node(
        "",
        node("1 Services", node("1.1 Scope", node("(a) Fees")), node("1.2")),
        node("2 Term"),
        node("3 Law", node("3.1 Venue")),
    )
    root.open = True
    return CachedNode(root)


@pytest.fixture
def cached(monkeypatch):
    """The uiacache seam with no COM client: the fakes answer its calls."""
    monkeypatch.setattr(ui.uiacache, "tree_item_request", lambda: (object(), object()))
    monkeypatch.setattr(ui.uiacache, "wrap", lambda el: TreeNode(el.name))
    return Surface()


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
def test_cached_expand_opens_every_node_in_one_pass_and_never_fetches_a_leaf(cached):
    result = ui.expand_all(cached_outline(cached), names=10)

    assert result["nodes"] == 7
    assert result["expanded"] == 3
    assert result["depths"] == {0: 3, 1: 3, 2: 1}
    assert result["complete"] is True
    assert result["via"] == "cache"
    assert result["names"] == [
        "1 Services",
        "2 Term",
        "3 Law",
        "1.1 Scope",
        "1.2",
        "3.1 Venue",
        "(a) Fees",
    ]
    non_leaves = 3  # 1 Services, 1.1 Scope, 3 Law
    assert cached.fetches == 1 + non_leaves


def test_cached_expand_stops_at_the_deadline_and_says_so(cached, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(ui_count, "time", clock)  # count.py reads the clock, not ui
    cached.clock, cached.step = clock, 6.0

    result = ui.expand_all(cached_outline(cached), deadline=5.0)

    assert result["complete"] is False
    assert result["nodes"] == 3
    assert result["expanded"] == 0
    assert result["via"] == "cache"
    assert cached.fetches == 1


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
def test_count_items_proves_a_count_through_the_cache(cached):
    result = ui.count_items(cached_outline(cached))

    assert result["count"] == 7
    assert result["passes"] == [7, 7]
    assert result["complete"] is True


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
def test_a_uia_timeout_on_a_fetch_cuts_the_cached_pass(cached, monkeypatch):
    from .compat import COMError

    tree = cached_outline(cached)
    services = tree.Element.children[0]

    def timed_out(scope, condition, request):
        raise COMError(ui.UIA_E_TIMEOUT, None, (None, None, None, 0, None))

    services.FindAllBuildCache = timed_out
    result = ui.expand_all(tree)

    assert result["complete"] is False
    assert result["via"] == "cache"


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
@pytest.mark.parametrize(
    ("error", "complete"), [(timeout_error(), False), (OSError("refused"), True)]
)
def test_a_timed_out_cached_expand_cuts_the_pass(cached, monkeypatch, error, complete):
    def refuse(self):
        raise error

    monkeypatch.setattr(CachedExpander, "Expand", refuse)
    result = ui.expand_all(cached_outline(cached))
    assert result["complete"] is complete
    assert result["expanded"] == 0


class FindAll:
    """A raw element whose FindAllBuildCache raises `error` or returns `found`."""

    def __init__(self, *, error=None, found=None):
        self.error, self.found = error, found

    def FindAllBuildCache(self, scope, condition, request):
        if self.error is not None:
            raise self.error
        return self.found


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
def test_tree_item_children_lets_a_uia_timeout_through(cached):
    with pytest.raises(COMError):
        uiacache.tree_item_children(FindAll(error=timeout_error()))


@pytest.mark.windows("the IUIAutomation cache (uiacache, comtypes)")
def test_tree_item_children_reads_another_com_error_as_no_children(cached):
    gone = COMError(-2147220991, None, (None, None, None, 0, None))
    assert uiacache.tree_item_children(FindAll(error=gone)) == []
    assert uiacache.tree_item_children(FindAll(found=None)) == []
    assert uiacache.tree_item_children(FindAll(found=FakeArray(["a", "b"]))) == ["a", "b"]


def test_a_raw_root_without_tree_items_falls_back_to_the_walk(cached):
    empty = FakeElement("", (), cached)
    empty.open = True
    control = CachedNode(empty)
    control.GetChildren = outline().GetChildren

    result = ui.expand_all(control)
    assert result["via"] == "walk"
    assert result["nodes"] == 7


# ------------------------------------------------ not a tree: never expanded


class DropDown:
    """A ribbon SplitButton: it offers ExpandCollapse, and Expand opens a menu."""

    ControlTypeName = "SplitButtonControl"

    def __init__(self, name: str):
        self.Name = name
        self.hidden = True
        self.open = False
        self.expands = 0

    def GetChildren(self):
        return []

    def GetExpandCollapsePattern(self):
        drop_down = self

        class Pattern(Expander):
            def Expand(self, waitTime=0):
                drop_down.expands += 1
                drop_down.open = True

        return Pattern(self)


class TabStrip:
    """The ribbon's tab strip: TabItems, plus a Share drop-down beside them."""

    ControlTypeName = "TabControl"
    Name = "Ribbon Tabs"

    def __init__(self):
        self.tabs = [Row(name) for name in ("File", "Home", "Insert", "Layout")]
        for tab in self.tabs:
            tab.ControlTypeName = "TabItemControl"
        self.share = DropDown("Share")

    def GetChildren(self):
        return [*self.tabs, self.share]

    def GetPattern(self, pattern_id):
        return None


def test_a_tab_strip_with_a_drop_down_is_counted_as_a_list_and_never_expanded():
    strip = TabStrip()
    result = ui.count_items(strip)

    assert not ui.is_tree_like(strip)
    assert (result["count"], result["via"]) == (5, "children")
    assert strip.share.expands == 0
    assert strip.share.open is False


def test_expand_all_refuses_a_control_that_is_not_a_tree():
    strip = TabStrip()
    with pytest.raises(PilotError, match="not a tree"):
        ui.expand_all(strip)
    assert strip.share.expands == 0


def test_a_tree_item_is_tree_like_and_expands_its_subtree():
    node = TreeNode("1 Services", [TreeNode("1.1 Scope", [TreeNode("(a) Fees")])])
    node.open = True

    assert ui.is_tree_like(node)
    result = ui.expand_all(node)
    assert (result["nodes"], result["expanded"]) == (2, 1)
