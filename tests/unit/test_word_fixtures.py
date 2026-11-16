"""Word's pytest fixtures' teardown: what it must record instead of hiding,
and what it must never raise. No Word is touched here."""

from __future__ import annotations

import pytest

from desktoppilot.core.errors import PilotError, WalkCut
from desktoppilot.plugins.word import fixtures

PILOT = type("Pilot", (), {"profile": None})()


class Node:
    nodeid = "test_x"

    def __init__(self):
        self.user_properties: list = []


@pytest.fixture(autouse=True)
def launched(monkeypatch):
    """A Word this run launched, unless a test says otherwise."""
    monkeypatch.setattr(fixtures.apps, "launched_here", lambda profile: True)


def test_an_adopted_word_is_never_clicked_in_and_its_add_in_windows_are_reported(monkeypatch):
    monkeypatch.setattr(fixtures.apps, "launched_here", lambda profile: False)
    monkeypatch.setattr(fixtures.roots, "close_addin_windows", pytest.fail)
    windows = [
        {"hwnd": 1, "title": "Inspector", "class": "HwndWrapper[x]"},
        {"hwnd": 2, "title": "Contract - Word", "class": "OpusApp"},
        {"hwnd": 3, "title": "", "class": "HwndWrapper[y]"},
    ]
    monkeypatch.setattr(fixtures.roots, "top_windows", lambda pilot: windows)
    node = Node()

    with pytest.warns(UserWarning, match="adopted"):
        fixtures.close_leftovers(PILOT, node)
    assert node.user_properties == [("addin_left_open", ["Inspector"])]


def test_a_toast_check_word_did_not_answer_is_recorded_not_hidden(monkeypatch):
    def cut(pilot, **kw):
        raise WalkCut("the toast walk was cut after 5.0s", ["Inspector"])

    monkeypatch.setattr(fixtures.roots, "close_addin_windows", cut)
    monkeypatch.setattr(fixtures.apps, "pids", lambda profile: [])
    node = Node()

    with pytest.warns(UserWarning, match="toast walk was cut"):
        fixtures.close_leftovers(PILOT, node)
    assert node.user_properties == [("addin_close_incomplete", "the toast walk was cut after 5.0s")]


def test_a_teardown_error_is_still_not_raised(monkeypatch):
    def broken(pilot, **kw):
        raise RuntimeError("COM went away")

    monkeypatch.setattr(fixtures.roots, "close_addin_windows", broken)
    monkeypatch.setattr(fixtures.apps, "pids", lambda profile: [])
    node = Node()
    fixtures.close_leftovers(PILOT, node)
    assert node.user_properties == []


def test_a_window_that_would_not_close_is_recorded(monkeypatch):
    def refuses(pilot, *, not_closed, **kw):
        not_closed.append({"window": "Inspector", "hwnd": 5, "error": "access denied"})
        return []

    monkeypatch.setattr(fixtures.roots, "close_addin_windows", refuses)
    monkeypatch.setattr(fixtures.apps, "pids", lambda profile: [])
    node = Node()

    with pytest.warns(UserWarning, match="left open"):
        fixtures.close_leftovers(PILOT, node)
    assert node.user_properties[0][0] == "addin_not_closed"


def test_a_document_that_will_not_close_does_not_keep_the_others_open(monkeypatch):
    closed = []

    def close_doc(app, *, save, full_name):
        if full_name == "b.docx":
            raise PilotError("Word is busy")
        closed.append(full_name)

    monkeypatch.setattr(fixtures.docs, "close_doc", close_doc)
    node = Node()

    with pytest.warns(UserWarning, match="documents left open"):
        failed = fixtures.close_opened(None, ["a.docx", "b.docx", "c.docx"], node)

    assert closed == ["c.docx", "a.docx"]
    assert failed == [{"document": "b.docx", "error": "Word is busy"}]
    assert node.user_properties == [("docs_not_closed", failed)]
