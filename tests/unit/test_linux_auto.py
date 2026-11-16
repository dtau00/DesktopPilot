"""The uiautomation duck type over AT-SPI, on fake accessibles: types, names,
ids, rects, the desktop's flattened children, timeouts and dead objects,
the patterns, and matching top-level objects to X windows."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from desktoppilot.core import ui
from desktoppilot.core.errors import PilotError, WalkCut, is_uia_timeout
from desktoppilot.linux import auto

_paths = itertools.count(1)


class GError(Exception):
    pass


class Acc:
    """A fake Atspi.Accessible: what auto.py calls on one."""

    def __init__(
        self,
        role,
        name="",
        children=(),
        *,
        states=("showing", "enabled"),
        attrs=None,
        aid="",
        actions=(),
        rect=(0, 0, 0, 0),
        text=None,
        editable=False,
        value=None,
        pid=100,
        bus=":1.5",
        toolkit=("gtk", "3.24.52"),
        on_action=None,
    ):
        self.role, self.name, self.states, self.attrs, self.aid = (
            role,
            name,
            set(states),
            attrs,
            aid,
        )
        self.actions, self.rect, self.text, self.editable, self.value = (
            actions,
            rect,
            text,
            editable,
            value,
        )
        self.pid, self.toolkit, self.on_action = pid, toolkit, on_action
        self.app = SimpleNamespace(bus_name=bus)
        self.path = f"/obj/{next(_paths)}"
        self.parent = None
        self.selected_child = None
        self.children = list(children)
        for child in self.children:
            child.parent = self
        self.done: list[int] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_state_set(self):
        states = [SimpleNamespace(value_nick=s) for s in self.states]
        return SimpleNamespace(get_states=lambda: states)

    def get_attributes(self):
        return self.attrs

    def get_accessible_id(self):
        return self.aid

    def get_child_count(self):
        return -1 if "defunct" in self.states else len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_parent(self):
        return self.parent

    def get_index_in_parent(self):
        return self.parent.children.index(self)

    def get_process_id(self):
        return self.pid

    def get_application(self):
        node = self
        while node.parent is not None and node.role != "application":
            node = node.parent
        return node

    def get_toolkit_name(self):
        return self.toolkit[0]

    def get_toolkit_version(self):
        return self.toolkit[1]

    def is_action(self):
        return bool(self.actions)

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, i):
        return self.actions[i]

    def do_action(self, i):
        self.done.append(i)
        return self.on_action(self, i) if self.on_action else True

    def is_component(self):
        return True

    def get_extents(self, kind):
        x, y, w, h = self.rect
        return SimpleNamespace(x=x, y=y, width=w, height=h)

    def is_text(self):
        return self.text is not None

    def is_editable_text(self):
        return self.editable

    def is_value(self):
        return self.value is not None

    def get_current_value(self):
        return self.value

    def set_current_value(self, value):
        self.value = value
        return True

    def is_selection(self):
        return True

    def select_child(self, index):
        for i, child in enumerate(self.children):
            child.states.discard("selected")
            if i == index:
                child.states.add("selected")
        return True

    def is_hypertext(self):
        return False

    def is_collection(self):
        return False


FAKE_ATSPI = SimpleNamespace(
    CoordType=SimpleNamespace(SCREEN="screen", WINDOW="window"),
    Text=SimpleNamespace(get_text=lambda acc, start, end: acc.text),
    EditableText=SimpleNamespace(
        set_text_contents=lambda acc, text: setattr(acc, "text", text) or True
    ),
    TableCell=SimpleNamespace(get_position=lambda acc: (True, 0, acc.attrs["column"])),
    ScrollType=SimpleNamespace(ANYWHERE=0),
)


@pytest.fixture(autouse=True)
def fake_atspi(monkeypatch):
    monkeypatch.setattr(auto, "_atspi", FAKE_ATSPI)
    monkeypatch.setattr(auto, "_GLIB_ERROR", (GError,))
    monkeypatch.setattr(auto, "MEMO_TTL", 0.0)
    monkeypatch.setattr(auto, "STATE_WAIT", 0.2)
    monkeypatch.setattr(auto, "_RELATIVE", {})
    monkeypatch.setattr(auto, "_HANDLES", {})
    monkeypatch.setattr(auto, "_CONTROLS", {})


def desktop_of(*apps) -> Acc:
    desktop = Acc("desktop frame", children=apps)
    FAKE_ATSPI.get_desktop = lambda index: desktop
    return desktop


def window(*children, name="Win", rect=(100, 100, 400, 300), **app_kw) -> Acc:
    """An application with one frame holding `children`; returns the frame."""
    frame = Acc("frame", name, children, rect=rect)
    desktop_of(Acc("application", "app", [frame], **app_kw))
    return frame


def control(acc) -> auto.Control:
    return auto.Control(acc)


# ------------------------------------------------------------ reads


def test_a_name_loses_gtk3s_accelerator_padding():
    assert control(Acc("menu item", "New      ")).Name == "New"


def test_an_unnamed_item_takes_its_label_childs_name():
    item = Acc("list item", "", [Acc("panel", "", [Acc("label", "Two")])])
    assert control(item).Name == "Two"


def test_a_dead_object_raises_instead_of_reading_empty():
    with pytest.raises(auto.ElementGone):
        _ = control(Acc("button", "", states=("defunct",))).Name


def test_an_empty_answer_that_took_the_timeout_is_a_uia_timeout(monkeypatch):
    monkeypatch.setattr(auto, "_SILENT", 0.0)
    with pytest.raises(auto.AtspiTimeout) as caught:
        _ = control(Acc("button", "")).Name
    assert is_uia_timeout(caught.value)


def test_a_no_reply_error_is_a_uia_timeout_and_any_other_one_is_gone():
    def no_reply():
        raise GError("atspi_error: Did not receive a reply. Possible causes include ...")

    def gone():
        raise GError("atspi_error: The application no longer exists")

    with pytest.raises(auto.AtspiTimeout) as caught:
        auto._call("name", no_reply)
    assert is_uia_timeout(caught.value)
    with pytest.raises(auto.ElementGone):
        auto._call("name", gone)


def test_an_error_on_a_live_object_is_a_refusal_not_a_gone_element():
    """GTK4 answers grab_focus with a bare atspi_error: the object is alive,
    so the call was refused (AtspiError, a PilotError), not ElementGone."""

    class Refuses(Acc):
        def grab_focus(self):
            raise GError("atspi_error:  (1)")

    live, dead = Refuses("panel"), Refuses("panel", states=("defunct",))
    with pytest.raises(auto.AtspiError) as caught:
        auto._call("grab focus", live.grab_focus)
    assert isinstance(caught.value, PilotError)
    with pytest.raises(auto.ElementGone):
        auto._call("grab focus", dead.grab_focus)


class Focusable(Acc):
    """grab_focus answers `answer` (an exception is raised); `takes` says
    whether the object shows FOCUSED afterwards anyway."""

    def __init__(self, *args, answer=True, takes=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.answer, self.takes = answer, takes

    def grab_focus(self):
        if self.takes or self.answer is True:
            self.states.add("focused")
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def no_x_window(monkeypatch):
    monkeypatch.setattr(auto.Control, "NativeWindowHandle", property(lambda self: 0))
    monkeypatch.setattr(auto, "FOCUS_WAIT", 0.05)


def test_set_focus_grabs_the_focus(no_x_window):
    entry = Focusable("entry")
    window(entry)
    assert control(entry).SetFocus() is True


def test_set_focus_refused_but_focused_anyway_succeeds(no_x_window):
    entry = Focusable("entry", answer=GError("atspi_error:  (1)"), takes=True)
    window(entry)
    assert control(entry).SetFocus() is True


@pytest.mark.parametrize("answer", [False, GError("atspi_error:  (1)")])
def test_set_focus_that_does_not_take_says_to_click_first(no_x_window, answer):
    entry = Focusable("entry", answer=answer)
    window(entry)
    with pytest.raises(PilotError, match="ui click") as caught:
        control(entry).SetFocus()
    assert not isinstance(caught.value, auto.ElementGone)
    assert "focused" not in entry.states


def test_a_radio_menu_item_takes_its_label_childs_name():
    item = Acc("radio menu item", "", [Acc("label", "Large")])
    assert control(item).Name == "Large"


def test_a_timed_out_children_read_cuts_the_walk(monkeypatch):
    monkeypatch.setattr(auto, "_SILENT", 0.0)
    node = control(Acc("panel", states=("defunct",)))
    with pytest.raises(WalkCut):
        ui._children(node)


def test_a_gone_object_has_no_children_for_the_walk():
    assert ui._children(control(Acc("panel", states=("defunct",)))) == []


def test_automation_id_drops_gtkbuilder_ids_and_falls_back_to_the_dom_id():
    assert control(Acc("panel", aid="message_area")).AutomationId == "message_area"
    assert control(Acc("panel", aid="___object_2___")).AutomationId == ""
    assert control(Acc("button", attrs={"id": "b1", "class": "btn"})).AutomationId == "b1"


def test_class_name_is_the_dom_class_else_the_role():
    assert control(Acc("button", attrs={"class": "btn"})).ClassName == "btn"
    assert control(Acc("check box")).ClassName == "check box"


def test_a_submenu_type_depends_on_its_parent():
    menu = Acc("menu", "File")
    Acc("menu bar", children=[menu])
    assert control(menu).ControlTypeName == "MenuItemControl"


def test_a_tree_tables_cells_are_tree_items_in_the_first_column_only():
    first, second = Acc("table cell", attrs={"column": 0}), Acc("table cell", attrs={"column": 1})
    Acc("tree table", children=[first, second])
    assert [control(c).ControlTypeName for c in (first, second)] == [
        "TreeItemControl",
        "DataItemControl",
    ]


def test_runtime_id_is_bus_and_path():
    acc = Acc("button")
    assert control(acc).GetRuntimeId() == (":1.5", acc.path)


# ------------------------------------------------------------ tree


def test_the_desktops_children_are_every_apps_top_level_windows():
    one, two = Acc("frame", "One"), Acc("dialog", "Two")
    desktop_of(Acc("application", children=[one]), Acc("application", children=[two]))
    kids = auto.GetRootControl().GetChildren()
    assert [k.accessible for k in kids] == [one, two]
    assert isinstance(kids[0].GetParentControl(), auto._Desktop)


def test_a_top_level_windows_parent_is_the_desktop_and_it_is_its_own_top():
    button = Acc("button", "OK")
    frame = window(Acc("panel", children=[button]))
    found = control(button)
    assert found.GetTopLevelControl().accessible is frame
    assert isinstance(found.GetTopLevelControl().GetParentControl(), auto._Desktop)
    assert control(frame).GetTopLevelControl().accessible is frame


def test_an_object_with_no_place_has_an_empty_rect_and_is_offscreen():
    item = control(Acc("menu item", "New", rect=(auto.INT_MIN, auto.INT_MIN, 1, 1)))
    assert item.BoundingRectangle == auto.EMPTY
    assert item.IsOffscreen


def test_window_relative_extents_move_by_the_windows_client_origin(monkeypatch):
    button = Acc("button", "Hello", rect=(10, 20, 80, 30))
    window(button, toolkit=("GTK", "4.22.4"))
    monkeypatch.setattr(auto, "_handle_of", lambda c: 0x42)
    monkeypatch.setattr(auto.x11, "x_rect", lambda w: (1000, 500, 1400, 800))
    assert control(button).BoundingRectangle == auto.Rect(1010, 520, 1090, 550)


def test_a_top_level_windows_rect_is_its_x_frame(monkeypatch):
    frame = window()
    monkeypatch.setattr(auto, "_handle_of", lambda c: 0x42)
    monkeypatch.setattr(auto.x11, "frame_rect", lambda w: (95, 70, 505, 405))
    assert control(frame).BoundingRectangle == auto.Rect(95, 70, 505, 405)
    assert control(frame).NativeWindowHandle == 0x42


# ------------------------------------------------------------ windows


def x_windows(monkeypatch, windows: dict[int, dict]) -> None:
    monkeypatch.setattr(auto.x11, "client_windows", lambda: list(windows))
    monkeypatch.setattr(auto.x11, "exists", lambda w: w in windows)
    for name in ("pid", "title", "x_rect", "frame_rect"):
        monkeypatch.setattr(auto.x11, name, lambda w, name=name: windows[w][name])


def test_a_top_level_object_finds_its_x_window_by_pid_title_and_rect(monkeypatch):
    frame = window(name="Doc - Editor", rect=(100, 100, 400, 300))
    x_windows(
        monkeypatch,
        {
            1: {"pid": 100, "title": "Other", "x_rect": (0, 0, 50, 50), "frame_rect": (0, 0, 50, 50)},
            2: {"pid": 100, "title": "Doc - Editor", "x_rect": (105, 129, 495, 395), "frame_rect": (100, 100, 500, 400)},
            3: {"pid": 999, "title": "Doc - Editor", "x_rect": (100, 100, 500, 400), "frame_rect": (100, 100, 500, 400)},
        },
    )  # fmt: skip
    assert control(frame).NativeWindowHandle == 2
    found = auto.ControlFromHandle(2)
    assert found.accessible is frame
    assert auto.ControlFromHandle(1) is None


def test_an_untitled_popup_matches_by_rect_alone(monkeypatch):
    popup = Acc("window", "", rect=(300, 400, 200, 90))
    desktop_of(Acc("application", children=[Acc("frame", "Main", rect=(0, 0, 900, 900)), popup]))
    x_windows(
        monkeypatch,
        {
            7: {"pid": 100, "title": "", "x_rect": (300, 400, 500, 490), "frame_rect": (300, 400, 500, 490)},
            8: {"pid": 100, "title": "Main", "x_rect": (0, 0, 900, 900), "frame_rect": (0, 0, 900, 900)},
        },
    )  # fmt: skip
    assert auto.ControlFromHandle(7).accessible is popup


def test_a_window_with_no_fitting_object_has_no_control(monkeypatch):
    window(name="Main", rect=(0, 0, 900, 900))
    x_windows(
        monkeypatch,
        {
            9: {
                "pid": 100,
                "title": "",
                "x_rect": (50, 60, 80, 90),
                "frame_rect": (50, 60, 80, 90),
            }
        },
    )
    assert auto.ControlFromHandle(9) is None


def test_is_popup_by_role_or_x_window(monkeypatch):
    combo_list = window(name="")
    combo_list.role = "window"
    assert auto.is_popup(control(combo_list))
    frame = window(name="")
    monkeypatch.setattr(auto, "_handle_of", lambda c: 5)
    monkeypatch.setattr(auto.x11, "window_type", lambda w: ["menu"])
    monkeypatch.setattr(auto.x11, "is_override_redirect", lambda w: True)
    assert auto.is_popup(control(frame))


# ------------------------------------------------------------ patterns


def test_invoke_offers_a_button_but_not_a_checkbox_or_an_entry():
    assert control(Acc("button", actions=("click",))).GetInvokePattern() is not None
    assert control(Acc("check box", actions=("click",))).GetInvokePattern() is None
    assert control(Acc("entry", actions=("activate",))).GetInvokePattern() is None
    assert control(Acc("panel", actions=("dodefault",))).GetInvokePattern() is None
    assert control(Acc("link", actions=("jump",))).GetInvokePattern() is not None


def test_invoke_refuses_a_disabled_element_and_a_refused_action():
    off = Acc("button", actions=("click",), states=("showing",))
    with pytest.raises(PilotError, match="disabled"):
        control(off).GetInvokePattern().Invoke()
    assert off.done == []
    refused = Acc("button", actions=("click",), on_action=lambda acc, i: False)
    with pytest.raises(PilotError, match="refused"):
        control(refused).GetInvokePattern().Invoke()


def test_toggle_waits_for_the_state_and_a_no_op_raises():
    def check(acc, i):
        acc.states ^= {"checked"}
        return True

    box = Acc("check box", actions=("click",), on_action=check)
    pattern = control(box).GetTogglePattern()
    assert pattern.ToggleState == auto.ToggleState.Off
    pattern.Toggle()
    assert pattern.ToggleState == auto.ToggleState.On
    stuck = control(Acc("check box", actions=("click",))).GetTogglePattern()
    with pytest.raises(PilotError, match="did nothing"):
        stuck.Toggle()
    with pytest.raises(PilotError, match="no action"):
        control(Acc("check box")).GetTogglePattern().Toggle()


def test_value_writes_through_editable_text_and_refuses_read_only():
    entry = Acc("entry", text="abc", editable=True, states=("showing", "enabled", "editable"))
    pattern = control(entry).GetValuePattern()
    assert (pattern.Value, pattern.IsReadOnly) == ("abc", False)
    pattern.SetValue("new")
    assert pattern.Value == "new"
    fixed = control(Acc("entry", text="x", editable=True)).GetValuePattern()
    assert fixed.IsReadOnly
    with pytest.raises(PilotError, match="read-only"):
        fixed.SetValue("y")


def test_value_without_editable_text_says_so():
    web = Acc("entry", text="abc", states=("showing", "enabled", "editable"))
    with pytest.raises(PilotError, match="EditableText") as caught:
        control(web).GetValuePattern().SetValue("x")
    assert "cdp set-value" in str(caught.value)
    assert "ui type` with the window in front" in str(caught.value)


def test_a_combo_boxs_value_is_its_selected_choice():
    combo = Acc(
        "combo box",
        "",
        [
            Acc(
                "menu",
                children=[Acc("menu item", "One"), Acc("menu item", "Two", states=("selected",))],
            )
        ],
    )
    assert control(combo).GetValuePattern().Value == "Two"


def test_select_goes_through_the_parents_selection():
    tab = Acc("page tab", "B", states=("showing", "enabled"))
    Acc("page tab list", children=[Acc("page tab", "A", states=("selected",)), tab])
    pattern = control(tab).GetSelectionItemPattern()
    assert not pattern.IsSelected
    pattern.Select()
    assert pattern.IsSelected


def test_a_combo_choice_selects_through_its_action():
    two = Acc("menu item", "Two", states=("showing", "enabled", "selectable"), actions=("select",))
    Acc("combo box", children=[Acc("menu", children=[two])])
    control(two).GetSelectionItemPattern().Select()
    assert two.done == [0]


@pytest.mark.parametrize("closes", ["item hides", "menu hides", "item dies"])
def test_a_radio_menu_item_whose_menu_closes_counts_as_selected(closes):
    def act(acc, i):
        if closes == "item hides":
            acc.states.discard("showing")
        elif closes == "menu hides":
            acc.parent.states.discard("showing")
        else:
            acc.states.add("defunct")
        return True

    item = Acc("radio menu item", "Big", actions=("click",), on_action=act)
    Acc("menu", children=[item])
    assert control(item).GetSelectionItemPattern().Select()
    assert item.done == [0]


def test_a_radio_menu_item_in_a_closed_menu_must_read_as_checked():
    item = Acc("radio menu item", "Big", actions=("click",))
    Acc("menu", children=[item], states=("enabled",))
    with pytest.raises(PilotError, match="did nothing"):
        control(item).GetSelectionItemPattern().Select()


def test_a_radio_menu_item_that_stays_open_must_read_as_checked():
    item = Acc("radio menu item", "Big", actions=("click",))
    Acc("menu", children=[item])
    with pytest.raises(PilotError, match="did nothing"):
        control(item).GetSelectionItemPattern().Select()
    item.on_action = lambda acc, i: acc.states.add("checked") or True
    assert control(item).GetSelectionItemPattern().Select()


def test_a_gtk3_combo_is_expanded_while_its_list_shows():
    def popup(acc, i):
        acc.children[0].states.add("showing")
        return True

    combo = Acc("combo box", "One", [Acc("menu", states=())], actions=("press",), on_action=popup)
    pattern = control(combo).GetExpandCollapsePattern()
    assert pattern.ExpandCollapseState == auto.ExpandCollapseState.Collapsed
    pattern.Expand()
    assert pattern.ExpandCollapseState == auto.ExpandCollapseState.Expanded


def test_a_gtk4_drop_down_expands_through_its_toggle_button():
    def opens(acc, i):
        acc.parent.states.add("expanded")
        return True

    opener = Acc("toggle button", actions=("click",), on_action=opens)
    combo = Acc("combo box", "One", [opener], states=("showing", "enabled", "expandable"))
    control(combo).GetExpandCollapsePattern().Expand()
    assert opener.done == [0]


def test_an_expand_that_does_not_show_raises():
    node = Acc(
        "table cell", states=("showing", "enabled", "expandable"), actions=("expand or contract",)
    )
    with pytest.raises(PilotError, match="did nothing"):
        control(node).GetExpandCollapsePattern().Expand()


def test_scroll_sets_the_bars_value():
    bar = Acc("scroll bar", rect=(490, 0, 10, 200), value=0.0, states=("showing", "vertical"))
    bar.get_minimum_value = lambda: 0.0
    bar.get_maximum_value = lambda: 1000.0
    pane = control(Acc("scroll pane", children=[Acc("text"), bar], rect=(0, 0, 500, 200)))
    pattern = pane.GetScrollPattern()
    assert pattern.VerticallyScrollable and not pattern.HorizontallyScrollable
    pattern.SetScrollPercent(pattern.NoScrollValue, 50)
    assert pattern.VerticalScrollPercent == 50.0
    assert pattern.VerticalViewSize == pytest.approx(100 * 200 / 1200)


def test_get_pattern_by_id():
    button = control(Acc("button", actions=("click",)))
    assert isinstance(button.GetPattern(auto.PatternId.InvokePattern), auto.InvokePattern)
    assert button.GetPattern(auto.PatternId.ItemContainerPattern) is None


def test_describe_reads_a_control_like_a_uia_one():
    button = Acc("button", " OK ", rect=(10, 20, 30, 40), attrs={"id": "ok"}, actions=("click",))
    window(button)
    assert ui.describe(control(button), verbose=True) == {
        "type": "Button",
        "name": "OK",
        "id": "ok",
        "class": "button",
        "rect": [10, 20, 30, 40],
        "enabled": True,
        "offscreen": False,
        "patterns": ["Invoke"],
    }


def test_send_keys_takes_uiautomations_interval(monkeypatch):
    sent = []
    monkeypatch.setattr(auto._input, "send_keys", lambda keys, wait: sent.append((keys, wait)))
    auto.SendKeys("{Ctrl}s", interval=0.02, waitTime=0)
    auto.SendKeys("x", 0.02, 0.1)
    assert sent == [("{Ctrl}s", 0), ("x", 0.1)]


class HyperAcc(Acc):
    """Chromium's way: the Text holds one U+FFFC per child element."""

    def is_hypertext(self):
        return True


def _links(acc):
    starts = [i for i, ch in enumerate(acc.text) if ch == "￼"]
    return [
        SimpleNamespace(get_start_index=lambda s=s: s, get_object=lambda i, c=c: c)
        for s, c in zip(starts, acc.children, strict=False)
    ]


@pytest.fixture
def hypertext(monkeypatch):
    monkeypatch.setattr(
        FAKE_ATSPI,
        "Hypertext",
        SimpleNamespace(
            get_n_links=lambda acc: len(_links(acc)),
            get_link=lambda acc, i: _links(acc)[i],
        ),
        raising=False,
    )


def test_a_contenteditables_value_is_its_paragraphs_text(hypertext):
    editor = HyperAcc(
        "section",
        text="￼￼",
        states=("showing", "enabled", "editable"),
        children=[HyperAcc("paragraph", text="Reply with"), HyperAcc("paragraph", text="OK")],
    )
    assert control(editor).GetValuePattern().Value == "Reply with\nOK"


def test_reading_a_paragraph_reads_its_childrens_text(hypertext):
    para = HyperAcc(
        "paragraph",
        text="Hi ￼!",
        children=[HyperAcc("link", "there", text="there")],
    )
    assert ui.text_of(control(para)) == "Hi there!"
    assert ui.text_of(control(HyperAcc("paragraph", text="plain"))) == "plain"
