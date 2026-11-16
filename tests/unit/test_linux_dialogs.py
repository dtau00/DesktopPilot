"""Dialogs on X11: which windows count, the text of their labels, and the
buttons a bounded walk finds. X11 and AT-SPI are faked."""

from __future__ import annotations

import sys

import pytest

from desktoppilot.core import dialogs
from desktoppilot.linux import dialogs as linux_dialogs

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="X11 dialogs")

MAIN, DIALOG, MODAL, TRANSIENT, TOOL, CLASSED, POPUP, FOREIGN, UNTYPED = range(1, 10)


class Node:
    """An AT-SPI object: a role, a name, children."""

    def __init__(self, role, name="", children=()):
        self.role = role
        self.Name = name
        self.ControlTypeName = "ButtonControl" if role == "push button" else "CustomControl"
        self.children = list(children)
        self.accessible = self

    def get_role_name(self):
        return self.role

    def GetChildren(self):
        return self.children


node = Node


WINDOWS = {
    # hwnd: pid, types, state, transient_for, wm_class, override-redirect
    MAIN: (10, ["normal"], [], None, ("mousepad", "Mousepad"), False),
    DIALOG: (10, ["dialog"], [], MAIN, ("mousepad", "Mousepad"), False),
    MODAL: (10, ["normal"], ["modal"], None, ("mousepad", "Mousepad"), False),
    TRANSIENT: (10, [], [], MAIN, ("mousepad", "Mousepad"), False),
    TOOL: (10, ["utility"], [], MAIN, ("mousepad", "Mousepad"), False),
    CLASSED: (10, ["normal"], [], None, ("prompt", "MyPrompt"), False),
    POPUP: (10, ["popup_menu"], [], None, ("mousepad", "Mousepad"), True),
    FOREIGN: (99, ["dialog"], [], None, ("zenity", "Zenity"), False),
    UNTYPED: (10, [], [], None, ("mousepad", "Mousepad"), False),
}

TREES = {
    DIALOG: node(
        "dialog",
        "Save changes?",
        [
            node(
                "filler",
                children=[
                    node("label", "Save changes to Untitled 1?"),
                    node("label", "Your changes will be lost."),
                    node(
                        "filler",
                        children=[
                            node("push button", "Cancel", [node("label", "Cancel")]),
                            node("button", "Save", [node("label", "Save")]),
                        ],
                    ),
                ],
            )
        ],
    ),
    UNTYPED: node("alert", "Error", [node("label", "Disk full")]),
}


@pytest.fixture
def x11(monkeypatch):
    x = linux_dialogs.x11
    monkeypatch.setattr(x, "client_windows", lambda stacking=True: list(WINDOWS))
    monkeypatch.setattr(x, "pid", lambda w: WINDOWS[w][0])
    monkeypatch.setattr(x, "window_type", lambda w: WINDOWS[w][1])
    monkeypatch.setattr(x, "state", lambda w: WINDOWS[w][2])
    monkeypatch.setattr(x, "transient_for", lambda w: WINDOWS[w][3])
    monkeypatch.setattr(x, "wm_class", lambda w: WINDOWS[w][4])
    monkeypatch.setattr(x, "is_override_redirect", lambda w: WINDOWS[w][5])
    monkeypatch.setattr(x, "title", lambda w: f"title {w}")
    monkeypatch.setattr(linux_dialogs.win32gui, "IsWindowVisible", lambda w: True)
    monkeypatch.setattr(linux_dialogs.auto, "ControlFromHandle", lambda w: TREES.get(w))


def test_dialog_modal_transient_classed_and_atspi_windows_count(x11):
    found = dialogs.dialog_windows({10}, classes=("myprompt",))
    assert [d["hwnd"] for d in found] == [DIALOG, MODAL, TRANSIENT, CLASSED, UNTYPED]


def test_a_dialog_reads_its_labels_in_tree_order(x11):
    found = {d["hwnd"]: d for d in dialogs.dialog_windows({10})}
    assert found[DIALOG] == {
        "hwnd": DIALOG,
        "title": f"title {DIALOG}",
        "text": "Save changes to Untitled 1? Your changes will be lost.",
    }
    assert found[UNTYPED]["text"] == "Disk full"


def test_no_pids_means_every_process(x11):
    assert FOREIGN in [d["hwnd"] for d in dialogs.dialog_windows(None)]
    assert FOREIGN not in [d["hwnd"] for d in dialogs.dialog_windows({10})]


def test_linux_has_no_base_dialog_class():
    assert dialogs.BASE_DIALOG_CLASSES == ()


def test_a_button_nested_in_fillers_is_pressed_by_label(x11, monkeypatch):
    pressed = []
    monkeypatch.setattr(dialogs, "_press", lambda button: pressed.append(button.Name))
    assert dialogs.click_button(DIALOG, ("Cancel",)) == "Cancel"
    assert pressed == ["Cancel"]


def test_no_label_match_presses_nothing_whatever_the_ids(x11, monkeypatch):
    monkeypatch.setattr(dialogs, "_press", lambda button: pytest.fail("pressed"))
    assert dialogs.click_button(DIALOG, ("Discard",), fallback_ids=(dialogs.IDNO,)) is None


def test_the_walk_is_bounded(monkeypatch):
    deep = node("label", "leaf")
    for _ in range(20):
        deep = node("filler", children=[deep])
    assert all(n.role == "filler" for n in linux_dialogs.elements(deep))
    monkeypatch.setattr(linux_dialogs, "WALK_NODES", 3)
    wide = node("filler", children=[node("label", str(i)) for i in range(10)])
    assert len(linux_dialogs.elements(wide)) == 3
