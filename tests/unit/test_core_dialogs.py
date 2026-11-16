"""Answering a dialog by Win32 button ID when no label matches - how a prompt
in another UI language still gets its No."""

from __future__ import annotations

import pytest

from desktoppilot.core import dialogs


class Button:
    ControlTypeName = "ButtonControl"

    def __init__(self, name: str, hwnd: int):
        self.Name, self.NativeWindowHandle = name, hwnd
        self.pressed = False

    def GetInvokePattern(self):
        owner = self

        class Pattern:
            def Invoke(self):
                owner.pressed = True

        return Pattern()

    def GetChildren(self):
        return []


class Dialog:
    def __init__(self, buttons):
        self.buttons = buttons

    def GetChildren(self):
        return self.buttons


@pytest.fixture
def ja_prompt(monkeypatch):
    """はい / いいえ: labels no English policy matches. hwnd 100+id stands in
    for the button's window; GetDlgCtrlID reads the id back."""
    yes, no = Button("はい(Y)", 100 + dialogs.IDYES), Button("いいえ(X)", 100 + dialogs.IDNO)
    monkeypatch.setattr(dialogs.auto, "ControlFromHandle", lambda hwnd: Dialog([yes, no]))
    monkeypatch.setattr(dialogs.win32gui, "GetDlgCtrlID", lambda hwnd: hwnd - 100)
    return yes, no


@pytest.mark.windows("Win32 dialog button IDs")
def test_no_label_match_falls_back_to_the_button_id(ja_prompt):
    yes, no = ja_prompt
    answered = dialogs.click_button(1, ("No",), fallback_ids=(dialogs.IDNO, dialogs.IDCANCEL))
    assert answered == "いいえ(X) (id 7)"
    assert no.pressed and not yes.pressed


def test_without_fallback_ids_nothing_is_pressed(ja_prompt):
    yes, no = ja_prompt
    assert dialogs.click_button(1, ("No",)) is None
    assert not (yes.pressed or no.pressed)


def test_yes_is_never_pressed_by_id(ja_prompt):
    with pytest.raises(ValueError, match="IDYES"):
        dialogs.click_button(1, ("No",), fallback_ids=(dialogs.IDYES,))


def test_reap_passes_the_policy_ids(monkeypatch):
    monkeypatch.setattr(
        dialogs, "dialog_windows", lambda pids, **_: [{"hwnd": 1, "title": "", "text": "safe"}]
    )
    given = []
    monkeypatch.setattr(
        dialogs,
        "click_button",
        lambda hwnd, labels, fallback_ids=(): given.append(fallback_ids) or "No",
    )
    dialogs.reap([1], (("safe", ("No",), (dialogs.IDNO,)), ("other", ("OK",))))
    assert given == [(dialogs.IDNO,)]


def test_a_button_without_invoke_takes_the_virtual_screen_pointer(monkeypatch):
    from desktoppilot.core import ui

    class Rect:
        left, top = -800, 100

        def width(self):
            return 80

        def height(self):
            return 24

    class PointerOnly:
        IsOffscreen = False
        BoundingRectangle = Rect()

        def GetInvokePattern(self):
            return None

    clicks = []
    monkeypatch.setattr(ui, "click_point", lambda x, y, **kw: clicks.append((x, y, kw)) or "mouse")
    dialogs._press(PointerOnly())
    assert clicks == [(-760, 112, {"button": "left", "double": False})]


def test_remaining_waits_for_a_closed_dialog_to_go(monkeypatch):
    """A close request returns before the app destroys the window: the
    closed dialog is not reported as remaining while it goes."""
    listings = iter([[{"hwnd": 1}, {"hwnd": 2}], [{"hwnd": 1}, {"hwnd": 2}], [{"hwnd": 2}]])
    monkeypatch.setattr(dialogs, "dialog_windows", lambda pids, **_: next(listings))
    assert dialogs.remaining([5], [1], timeout=2.0) == [{"hwnd": 2}]


def test_remaining_reports_a_dialog_that_did_not_close(monkeypatch):
    monkeypatch.setattr(dialogs, "dialog_windows", lambda pids, **_: [{"hwnd": 1}])
    assert dialogs.remaining([5], [1], timeout=0.2) == [{"hwnd": 1}]


def test_close_all_hands_back_the_handles_it_closed(monkeypatch):
    monkeypatch.setattr(
        dialogs, "dialog_windows", lambda pids, **_: [{"hwnd": 3, "title": "Q", "text": "?"}]
    )
    monkeypatch.setattr(dialogs, "click_button", lambda hwnd, labels, **_: "Cancel")
    handles: list[int] = []
    closed = dialogs.close_all([5], handles=handles)
    assert (closed[0]["answered"], handles) == ("Cancel", [3])
