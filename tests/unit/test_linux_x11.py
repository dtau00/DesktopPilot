"""The Win32 window calls over x11.py, with the X layer patched: what each
one reports for a client, a minimized one, a popup and a window that is gone."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the X11 backend")

if sys.platform.startswith("linux"):
    from desktoppilot.linux import win32con, win32gui, win32process, x11

ROOT, APP, DIALOG, MINI, POPUP, GONE = 0x100, 0x200, 0x300, 0x400, 0x500, 0x999


@pytest.fixture
def desk(monkeypatch):
    """A desktop: POPUP on top, then DIALOG (transient for APP), APP, MINI."""
    stacking = [POPUP, DIALOG, APP, MINI]
    wm_state = {APP: x11.NORMAL, DIALOG: x11.NORMAL, MINI: x11.ICONIC}
    patches = {
        "client_windows": lambda stacking_=True: list(stacking),
        "wm_state": wm_state.get,
        "is_override_redirect": lambda w: w == POPUP,
        "is_mapped": lambda w: w in (APP, DIALOG, POPUP),
        "is_minimized": lambda w: w == MINI,
        "exists": lambda w: w in (APP, DIALOG, MINI, POPUP),
        "toplevel": lambda w: w if w in (APP, DIALOG, MINI, POPUP) else 0,
        "transient_for": {DIALOG: APP, POPUP: DIALOG}.get,
        "title": {APP: "Doc - App"}.get,
        "wm_class": lambda w: ("app", "App") if w == APP else ("", ""),
        "frame_rect": lambda w: (10, 20, 110, 220) if w == APP else (0, 0, 0, 0),
        "pid": lambda w: 42 if w == APP else 0,
        "active_window": lambda: APP,
        "window_at": lambda x, y: APP if (x, y) == (50, 50) else None,
    }
    for name, fn in patches.items():
        monkeypatch.setattr(x11, name, fn)
    monkeypatch.setattr(win32gui, "GetDesktopWindow", lambda: ROOT)
    calls = []
    for name in ("activate", "minimize", "restore", "maximize", "close", "move_resize"):
        monkeypatch.setattr(x11, name, lambda *a, _n=name: calls.append((_n, *a)))
    return calls


def test_enum_windows_goes_topmost_first_and_stops_on_false(desk):
    seen = []
    win32gui.EnumWindows(lambda h, extra: seen.append((h, extra)) or len(seen) < 2, "x")
    assert seen == [(POPUP, "x"), (DIALOG, "x")]


def test_enum_windows_goes_on_when_the_callback_returns_none(desk):
    seen = []
    win32gui.EnumWindows(lambda h, _: seen.append(h), None)
    assert seen == [POPUP, DIALOG, APP, MINI]


def test_enum_child_windows_reports_none(desk):
    win32gui.EnumChildWindows(APP, lambda h, _: pytest.fail("a child"), None)


def test_visible_is_managed_minimized_included_or_a_mapped_popup(desk):
    assert win32gui.IsWindowVisible(APP) and win32gui.IsWindowVisible(MINI)
    assert win32gui.IsWindowVisible(POPUP)
    assert not win32gui.IsWindowVisible(GONE)


def test_iconic_window_and_foreground(desk):
    assert win32gui.IsIconic(MINI) and not win32gui.IsIconic(APP)
    assert win32gui.IsWindow(APP) and not win32gui.IsWindow(GONE)
    assert win32gui.GetForegroundWindow() == APP


def test_names_rect_and_owner(desk):
    assert win32gui.GetClassName(APP) == "App"
    assert win32gui.GetWindowText(APP) == "Doc - App"
    assert win32gui.GetWindowRect(APP) == (10, 20, 110, 220)
    assert win32process.GetWindowThreadProcessId(APP) == (0, 42)


def test_a_gone_window_reads_empty(desk):
    assert win32gui.GetClassName(GONE) == ""
    assert win32gui.GetWindowRect(GONE) == (0, 0, 0, 0)
    assert win32process.GetWindowThreadProcessId(GONE) == (0, 0)


def test_ancestors(desk):
    assert win32gui.GetAncestor(DIALOG, win32con.GA_ROOT) == DIALOG
    assert win32gui.GetAncestor(POPUP, win32con.GA_ROOTOWNER) == APP
    assert win32gui.GetAncestor(APP, win32con.GA_PARENT) == ROOT


def test_the_z_order_walk(desk):
    assert win32gui.GetTopWindow(None) == POPUP
    assert win32gui.GetTopWindow(APP) == 0
    assert win32gui.GetWindow(DIALOG, win32con.GW_HWNDNEXT) == APP
    assert win32gui.GetWindow(MINI, win32con.GW_HWNDNEXT) == 0
    assert win32gui.GetWindow(DIALOG, win32con.GW_OWNER) == APP


def test_window_from_point(desk):
    assert win32gui.WindowFromPoint((50, 50)) == APP
    assert win32gui.WindowFromPoint((1, 1)) == 0


def test_show_window_commands(desk):
    win32gui.ShowWindow(APP, win32con.SW_MINIMIZE)
    win32gui.ShowWindow(APP, win32con.SW_SHOWNOACTIVATE)
    win32gui.ShowWindow(APP, win32con.SW_RESTORE)
    assert desk == [("minimize", APP), ("restore", APP), ("restore", APP), ("activate", APP)]
    with pytest.raises(win32gui.error):
        win32gui.ShowWindow(APP, win32con.SW_HIDE)


def test_wm_close_closes_and_other_messages_raise(desk):
    win32gui.PostMessage(APP, win32con.WM_CLOSE, 0, 0)
    assert desk == [("close", APP)]
    with pytest.raises(win32gui.error):
        win32gui.PostMessage(APP, 0x0100, 0, 0)


def test_move_window_moves_the_frame(desk):
    win32gui.MoveWindow(APP, 1, 2, 300, 200, True)
    assert desk == [("move_resize", APP, 1, 2, 300, 200)]


def test_no_dialog_control_ids(desk):
    with pytest.raises(win32gui.error):
        win32gui.GetDlgCtrlID(APP)


def test_frame_rect_adds_wm_borders_and_drops_a_csd_shadow(monkeypatch):
    monkeypatch.setattr(x11, "x_rect", lambda w: (100, 100, 500, 400))
    extents = {"_NET_FRAME_EXTENTS": (5, 5, 29, 5), "_GTK_FRAME_EXTENTS": (0, 0, 0, 0)}
    monkeypatch.setattr(x11, "_cardinals", lambda w, name, n: extents[name])
    assert x11.frame_rect(1) == (95, 71, 505, 405)
    assert x11.client_rect(1) == (100, 100, 500, 400)
    extents.update({"_NET_FRAME_EXTENTS": (0, 0, 0, 0), "_GTK_FRAME_EXTENTS": (5, 5, 5, 5)})
    assert x11.frame_rect(1) == (105, 105, 495, 395)


def test_a_csd_windows_client_rect_drops_its_shadow_and_lies_inside_the_frame(monkeypatch):
    """Chrome: an X window (10, 37, 1275, 1590) with a 5 px shadow is the
    window rect (15, 42, 1270, 1585); the client rect may not stick out."""
    monkeypatch.setattr(x11, "x_rect", lambda w: (10, 37, 1275, 1590))
    extents = {"_NET_FRAME_EXTENTS": (0, 0, 0, 0), "_GTK_FRAME_EXTENTS": (5, 5, 5, 5)}
    monkeypatch.setattr(x11, "_cardinals", lambda w, name, n: extents[name])
    frame, client = x11.frame_rect(1), x11.client_rect(1)
    assert frame == client == (15, 42, 1270, 1585)
    assert x11.x_rect(1) == (10, 37, 1275, 1590)
    extents["_NET_FRAME_EXTENTS"] = (2, 2, 20, 2)  # a WM frame around it too
    frame, client = x11.frame_rect(1), x11.client_rect(1)
    assert frame[0] <= client[0] and frame[1] <= client[1]
    assert client[2] <= frame[2] and client[3] <= frame[3]


def test_only_a_normal_window_is_normal(monkeypatch):
    types = {1: ["normal"], 2: [], 3: ["popup_menu"], 4: ["dialog", "normal"], 5: ["tooltip"]}
    types[6] = ["_kde_net_wm_window_type_override", "normal"]
    monkeypatch.setattr(x11, "window_type", types.get)
    monkeypatch.setattr(x11, "is_override_redirect", lambda w: w == 7)
    types[7] = ["normal"]
    assert [w for w in sorted(types) if x11.is_normal(w)] == [1, 2, 6]


def test_an_x_error_reads_as_gone(monkeypatch):
    from Xlib import error

    def gone(*_):
        raise error.BadWindow.__new__(error.BadWindow)

    monkeypatch.setattr(x11, "_prop", gone)
    monkeypatch.setattr(x11, "_win", gone)
    assert x11.title(1) == "" and x11.pid(1) == 0 and x11.state(1) == []
    assert x11.client_rect(1) == (0, 0, 0, 0) and not x11.exists(1)


def test_a_popup_with_the_apps_class_and_a_title_is_not_a_main_window(monkeypatch):
    """A GTK menu carries the app's WM_CLASS, and a title: on X11 only a
    normal window is main (`apps.windows`)."""
    from desktoppilot.core import apps
    from desktoppilot.core.profiles import Profile

    main, menu = 0x10, 0x20
    monkeypatch.setattr(
        apps.win32gui, "EnumWindows", lambda cb, extra: [cb(w, extra) for w in (menu, main)]
    )
    monkeypatch.setattr(apps.win32gui, "IsWindowVisible", lambda w: True)
    monkeypatch.setattr(apps.win32gui, "GetClassName", lambda w: "Mousepad")
    monkeypatch.setattr(apps.win32gui, "GetWindowText", lambda w: "Doc - Mousepad")
    monkeypatch.setattr(apps.win32gui, "GetWindowRect", lambda w: (0, 0, 100, 100))
    monkeypatch.setattr(apps.win32process, "GetWindowThreadProcessId", lambda w: (0, 7))
    monkeypatch.setattr(apps.x11, "is_normal", lambda w: w == main)
    profile = Profile(name="mousepad", exe=["mousepad"], window_class=["Mousepad"])
    found = {w["hwnd"]: w["is_main"] for w in apps.windows(profile, owned={7})}
    assert found == {main: True, menu: False}
    assert [w["hwnd"] for w in apps.windows(profile, owned={7}, main_only=True)] == [main]
