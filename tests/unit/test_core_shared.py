"""The helpers plugins used to copy and now take from core: label lookup,
popup windows, element identity and visibility, the preserved clipboard,
durations, and processes by image name. Stubs only - no desktop."""

from __future__ import annotations

import sys
import time
from datetime import timedelta

import pytest

from desktoppilot.core import clip, procs, ui, util
from desktoppilot.core.errors import PilotError, WalkCut


class Node:
    def __init__(self, kind: str, name: str = "", children=(), *, cls: str = "", rid=None):
        self.ControlTypeName = kind + "Control"
        self.Name = name
        self.ClassName = cls
        self.IsOffscreen = False
        self.children = list(children)
        self.rid = rid

    def GetChildren(self):
        return self.children

    def GetRuntimeId(self):
        if self.rid is None:
            raise RuntimeError("no id")
        return list(self.rid)


# ------------------------------------------------------------- by label


def test_by_label_matches_the_collapsed_case_folded_name_of_that_type():
    hit = Node("Button", "Track\nChanges ")
    scope = Node("Pane", "", [Node("Group", "Track Changes"), Node("Pane", "", [hit])])

    assert ui.by_label(scope, "track changes", control_type="Button") == [hit]
    assert len(ui.by_label(scope, "Track Changes")) == 2


def test_by_label_past_its_deadline_is_cut_not_empty():
    with pytest.raises(WalkCut):
        ui.by_label(Node("Pane", "", [Node("Button", "x")]), "x", deadline=0.0)


def test_first_is_the_first_hit_or_none():
    assert ui.first([1, 2]) == 1
    assert ui.first([]) is None


# --------------------------------------------------------------- popups


@pytest.mark.windows("popups told by their Win32 class")
def test_popup_roots_are_the_desktop_children_of_the_popup_classes(monkeypatch):
    menu, popup = Node("Menu", cls="#32768"), Node("Window", cls="NetUIToolWindow")
    desktop = Node("Pane", "", [Node("Window", cls="OpusApp"), menu, popup])
    monkeypatch.setattr(ui.auto, "GetRootControl", lambda: desktop)

    assert ui.popup_roots() == [menu]
    assert ui.popup_roots(classes=("NetUIToolWindow",)) == [popup]


@pytest.mark.skipif(sys.platform == "win32", reason="Linux tells popups by role and X window")
def test_popup_roots_on_linux_are_the_desktop_children_auto_calls_popups(monkeypatch):
    menu = Node("Window", cls="Google-chrome")
    desktop = Node("Pane", "", [Node("Window", cls="Google-chrome"), menu])
    monkeypatch.setattr(ui.auto, "GetRootControl", lambda: desktop)
    monkeypatch.setattr(ui.auto, "is_popup", lambda window: window is menu)

    assert ui.popup_roots() == [menu]


def test_popup_roots_past_the_deadline_are_cut(monkeypatch):
    monkeypatch.setattr(ui.auto, "GetRootControl", lambda: Node("Pane"))
    with pytest.raises(WalkCut):
        ui.popup_roots(deadline=time.monotonic() - 1)


# ------------------------------------------------------- identity, visibility


def test_identity_is_the_runtime_id_and_none_without_one():
    assert ui.identity(Node("Button", rid=[42, 1])) == (42, 1)
    assert ui.identity(Node("Button")) is None


def test_two_id_less_reads_never_share_an_identity():
    """id() would: a freed wrapper passes its address on to the next one."""
    assert ui.identity(Node("Button")) is None
    assert ui.identity(Node("Button")) is None


def test_unique_keeps_every_id_less_element_of_one_read():
    a, b = Node("Button", "a"), Node("Button", "b")
    same = [Node("Button", "c", rid=[1]), Node("Button", "c", rid=[1])]
    assert ui._unique([a, b, *same]) == [a, b, same[0]]


class Rect:
    def __init__(self, w, h):
        self.left, self.top, self.w, self.h = 10, 20, w, h

    def width(self):
        return self.w

    def height(self):
        return self.h


def test_is_visible_needs_on_screen_and_a_size():
    shown = Node("Button")
    shown.BoundingRectangle = Rect(5, 5)
    assert ui.is_visible(shown)
    flat = Node("Button")
    flat.BoundingRectangle = Rect(0, 5)
    assert not ui.is_visible(flat)
    hidden = Node("Button")
    hidden.BoundingRectangle, hidden.IsOffscreen = Rect(5, 5), True
    assert not ui.is_visible(hidden)


def test_an_element_that_cannot_be_read_is_not_visible():
    class Gone:
        @property
        def BoundingRectangle(self):
            raise RuntimeError("element vanished")

    assert ui.is_visible(Gone()) is False


# ------------------------------------------------------------- clipboard


class FakeClipboard:
    def __init__(self, text="", formats=None, *, fail_restore=False):
        self.text, self.fail_restore = text, fail_restore
        self.formats = (1 if text else 0) if formats is None else formats

    def read(self):
        return self.text

    def write(self, text):
        if self.fail_restore:
            raise PilotError("the clipboard is held open")
        self.text = text
        return len(text)

    def clear(self):
        if self.fail_restore:
            raise PilotError("the clipboard is held open")
        self.text = ""
        return True


@pytest.fixture
def board(monkeypatch):
    def make(*args, **kwargs):
        fake = FakeClipboard(*args, **kwargs)
        monkeypatch.setattr(clip, "read", fake.read)
        monkeypatch.setattr(clip, "write", fake.write)
        monkeypatch.setattr(clip, "clear", fake.clear)
        monkeypatch.setattr(clip.win32clipboard, "CountClipboardFormats", lambda: fake.formats)
        return fake

    return make


def test_preserved_puts_the_text_back_even_when_the_body_fails(board):
    fake = board("the user's text")
    with pytest.raises(RuntimeError), clip.preserved() as kept:
        fake.text = "copied"
        raise RuntimeError("boom")
    assert fake.text == "the user's text" and kept["restored"] == "text"


def test_preserved_says_false_when_the_clipboard_held_no_text(board):
    board("", formats=2)  # an image, say
    with clip.preserved() as kept:
        pass
    assert kept["restored"] is False


def test_preserved_says_false_when_the_restore_fails(board):
    fake = board("the user's text")
    with clip.preserved() as kept:
        fake.fail_restore = True
    assert kept["restored"] is False


def test_copy_text_gives_the_clipboard_back(board, monkeypatch):
    fake = board("the user's text")
    monkeypatch.setattr(
        ui.auto,
        "SendKeys",
        lambda keys, **_: setattr(fake, "text", "copied") if "c" in keys else None,
    )
    assert ui.copy_text() == "copied"
    assert fake.text == "the user's text"


# -------------------------------------------------------------- durations


def test_one_duration_parser_raising_a_pilot_error():
    assert util.parse_duration("90s") == timedelta(seconds=90)
    assert util.parse_duration(" 2h ") == timedelta(hours=2)
    with pytest.raises(PilotError, match="30s, 5m"):
        util.parse_duration("soon")


def test_the_event_reader_shares_it():
    from desktoppilot.core import winevents

    assert winevents.parse_duration is util.parse_duration


# -------------------------------------------------------------- processes


def test_pids_of_image_reads_the_tasklist_rows(monkeypatch):
    rows = [["msedgewebview2.exe", "10", "Console", "1", "1 K"], ["msedgewebview2.exe", "11"]]
    monkeypatch.setattr(procs, "tasklist", lambda image: rows)
    assert procs.pids_of_image("msedgewebview2.exe") == [10, 11]


@pytest.mark.windows("tasklist and taskkill")
def test_images_by_pid_lists_every_process(monkeypatch):
    out = '"System","4","Services","0","1 K"\r\n"WINWORD.EXE","13","Console","1","9 K"\r\n'
    monkeypatch.setattr(procs.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": out})())
    assert procs.images_by_pid() == {4: "System", 13: "WINWORD.EXE"}


@pytest.mark.windows("tasklist and taskkill")
def test_taskkill_images_kills_every_process_of_each_image(monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(procs.subprocess, "run", lambda args, **k: ran.append(args))
    procs.taskkill_images(["A.exe", "B.exe"])
    assert ran == [["taskkill", "/F", "/IM", "A.exe", "/IM", "B.exe"]]
