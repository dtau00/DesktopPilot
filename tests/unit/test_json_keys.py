"""Every key a command's docstring lists under "JSON:" is one it emits.

The docstring is the command's contract for an agent reading --help, so a
key it names and never sends is a silent break. Fakes only, no desktop."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from desktoppilot.core import apps, commands, paths, ui
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import Registry

runner = CliRunner()

STATUS = [
    "app",
    "profile",
    "running",
    "pids",
    "working_set_mb",
    "session",
    "session_error",
    "status_errors",
    "bound_pid",
    "bound_by",
    "window",
    "windows",
]


class FakeProcess:
    pid = 4242


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    """A `fake` app tree whose process, windows and resize are stubbed."""
    exe = tmp_path / "fake.exe"
    exe.write_bytes(b"")
    profile = Profile(name="fake", exe=[str(exe)], ready_timeout=0.2, window_size=[800, 600])
    running: list[int] = []
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(apps, "pids", lambda profile: list(running))
    monkeypatch.setattr(apps, "all_pids", lambda profile: list(running))
    monkeypatch.setattr(apps.procs, "tasklist", lambda image: [])
    monkeypatch.setattr(apps.procs, "taskkill", lambda pids: running.clear())
    monkeypatch.setattr(apps, "_self_and_ancestors", lambda: {1})
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 99)
    monkeypatch.setattr(apps.win32process, "GetWindowThreadProcessId", lambda hwnd: (0, 4242))
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(
        apps,
        "_work_area",
        lambda hwnd: (0, 0, 1920, 1040),
    )
    rect = [0, 0, 100, 100]
    monkeypatch.setattr(apps.win32gui, "ShowWindow", lambda hwnd, cmd: None)
    monkeypatch.setattr(apps.win32gui, "GetWindowRect", lambda hwnd: tuple(rect))
    monkeypatch.setattr(
        apps.win32gui,
        "MoveWindow",
        lambda h, x, y, w, ht, r: rect.__setitem__(slice(None), [x, y, x + w, y + ht]),
    )
    monkeypatch.setattr(apps, "window_dpi", lambda hwnd: 96)
    tree = commands.build_app_cli("fake", profile, Registry())
    return tree, running


def call(tree, *args) -> dict:
    result = runner.invoke(tree, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def documented(tree, *args) -> str:
    return runner.invoke(tree, [*args, "--help"], env={"COLUMNS": "400"}).output


def assert_contract(tree, command: list[str], payload: dict, keys: list[str]) -> None:
    help_text = documented(tree, *command)
    assert [k for k in keys if k not in help_text] == [], "documented"
    assert [k for k in keys if k not in payload] == [], "emitted"


def test_app_launch(fake_app):
    tree, _ = fake_app
    keys = [*STATUS, "adopted", "killed", "hwnd", "ready", "layout"]
    assert_contract(tree, ["app", "launch"], call(tree, "app", "launch"), keys)


def test_app_launch_adopted_has_the_same_keys(fake_app):
    tree, running = fake_app
    running.append(11)
    payload = call(tree, "app", "launch", "--adopt")
    assert payload["adopted"] is True and payload["layout"] is None
    keys = [*STATUS, "adopted", "killed", "hwnd", "ready", "layout"]
    assert_contract(tree, ["app", "launch"], payload, keys)


def test_app_status(fake_app):
    tree, _ = fake_app
    assert_contract(tree, ["app", "status"], call(tree, "app", "status"), STATUS)


def test_app_attach(fake_app):
    tree, running = fake_app
    running.append(11)
    assert_contract(tree, ["app", "attach"], call(tree, "app", "attach"), [*STATUS, "hwnd"])


def test_app_kill(fake_app):
    tree, running = fake_app
    running.append(11)
    payload = call(tree, "app", "kill")
    assert payload["killed"] == [11]
    assert_contract(tree, ["app", "kill"], payload, ["killed", "quit_errors"])


def test_app_resize(fake_app):
    tree, _ = fake_app
    keys = [
        "hwnd",
        "dpi",
        "scale",
        "requested_dips",
        "physical",
        "dips",
        "clamped",
        "size_differs",
        "work_area",
    ]
    assert_contract(tree, ["app", "resize"], call(tree, "app", "resize"), keys)


def test_ui_click_at(fake_app, monkeypatch):
    tree, _ = fake_app
    monkeypatch.setattr(ui, "activate_app", lambda *a, **k: None)
    monkeypatch.setattr(ui, "click_point", lambda x, y, **k: "mouse")
    payload = call(tree, "--no-shot", "ui", "click", "--at", "5,6")
    assert payload["clicked"] is None and payload["point"] == [5, 6]
    keys = ["clicked", "point", "relative", "via", "fallback"]
    assert_contract(tree, ["ui", "click"], payload, keys)


def test_ui_hover(fake_app, monkeypatch):
    tree, _ = fake_app
    done = {"x": 5, "y": 6, "via": "mouse", "held": True}
    monkeypatch.setattr(ui, "activate_app", lambda *a, **k: None)
    monkeypatch.setattr(ui, "hover_point", lambda x, y, dwell=0.0: dict(done, x=x, y=y))
    payload = call(tree, "--no-shot", "ui", "hover", "--at", "5,6")
    assert payload["hovered"] is None and (payload["x"], payload["y"]) == (5, 6)
    keys = ["hovered", "x", "y", "via", "held", "relative", "fallback"]
    assert_contract(tree, ["ui", "hover"], payload, keys)
    monkeypatch.setattr(ui, "find", lambda *a, **k: object())
    monkeypatch.setattr(ui, "hover", lambda control, ctx=None, dwell=0.0: dict(done))
    monkeypatch.setattr(ui, "describe", lambda control, **k: {"name": "Tip"})
    payload = call(tree, "--no-shot", "ui", "hover", "app//Button", "--dwell", "0.1")
    keys = ["hovered", "x", "y", "via", "held", "element"]
    assert_contract(tree, ["ui", "hover"], payload, keys)


def test_ui_keys(fake_app, monkeypatch):
    tree, _ = fake_app
    monkeypatch.setattr(ui, "activate_app", lambda *a, **k: None)
    monkeypatch.setattr(ui, "send_keys", lambda keys, control=None: None)
    payload = call(tree, "ui", "keys", "{Esc}")
    assert_contract(tree, ["ui", "keys"], payload, ["keys", "into", "via"])


def test_events_mark(fake_app, monkeypatch, tmp_path):
    from desktoppilot.core import winevents

    tree, _ = fake_app
    monkeypatch.setattr(winevents, "MARK_DIR", tmp_path / "marks")
    monkeypatch.setattr(
        winevents.Mark, "now", staticmethod(lambda image: winevents.Mark(image, record_id=5))
    )
    payload = call(tree, "events", "mark")
    assert_contract(tree, ["events", "mark"], payload, ["mark", "record_id", "reports", "dumps"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux keys")
def test_events_dumps_on_linux(fake_app, monkeypatch, tmp_path):
    from desktoppilot.linux import events

    tree, _ = fake_app
    pattern = tmp_path / "core_pattern"
    pattern.write_text("|/usr/share/apport/apport -p%p -- %E")
    monkeypatch.setattr(events, "CORE_PATTERN", pattern)
    monkeypatch.setattr(events, "APPORT_DUMPS", tmp_path / "none")
    monkeypatch.setattr(events, "SYSTEMD_DUMPS", tmp_path / "none")
    monkeypatch.setattr(events, "_apport_active", lambda: True)
    keys = ["image", "configured", "scope", "folder", "count", "dumps", "unavailable"]
    keys += ["core_pattern", "handler", "apport_active"]
    assert_contract(tree, ["events", "dumps"], call(tree, "events", "dumps"), keys)


def test_dialogs_dismiss_without_a_policy(fake_app, monkeypatch):
    from desktoppilot.core import dialogs

    tree, _ = fake_app
    monkeypatch.setattr(dialogs, "dialog_windows", lambda pids, **k: [])
    payload = call(tree, "dialogs", "--dismiss")
    assert_contract(tree, ["dialogs"], payload, ["dismissed", "remaining", "note"])


class FakeValueControl:
    """A text box whose Value pattern takes the write and reads it back."""

    def __init__(self):
        self.Value = ""
        self.IsReadOnly = False

    def GetValuePattern(self):
        return self

    def SetValue(self, text, waitTime=0):
        self.Value = text


def test_ui_set_value(fake_app, monkeypatch):
    tree, _ = fake_app
    control = FakeValueControl()
    monkeypatch.setattr(ui, "find", lambda *a, **k: control)
    monkeypatch.setattr(ui, "restore_app", lambda *a, **k: None)
    payload = call(tree, "--no-shot", "ui", "set-value", "app//Edit", "hello")
    assert payload["verified"] is True and control.Value == "hello"
    keys = ["set", "value", "verified", "element"]
    assert_contract(tree, ["ui", "set-value"], payload, keys)


def test_capture(fake_app, monkeypatch, tmp_path):
    from desktoppilot.core import artifacts
    from desktoppilot.core import state as state_mod

    tree, _ = fake_app

    def screenshot(control, path):
        path.write_bytes(b"png")
        return path

    def walked(result):
        def walk(root, *, report, **kwargs):
            report.update(complete=True, cut=0)
            return result

        return walk

    monkeypatch.setattr(ui, "main_window", lambda ctx, **k: object())
    monkeypatch.setattr(ui, "screenshot", screenshot)
    monkeypatch.setattr(ui, "tree", walked({}))
    monkeypatch.setattr(ui, "flatten", walked([]))
    monkeypatch.setattr(state_mod, "snapshot", lambda ctx, **k: {})
    monkeypatch.setattr(artifacts.winevents, "query", lambda image, **k: {"records": []})
    payload = call(tree, "capture", "boom", "--directory", str(tmp_path))
    files = ["window.png", "state.json", "tree.json", "controls.json", "events.json"]
    assert sorted(Path(p).name for p in payload["saved"].values()) == sorted(
        [*files, "manifest.json"]
    )
    assert [f for f in [*files, "manifest.json"] if f not in documented(tree, "capture")] == []
    keys = ["dir", "saved", "failed", "complete", "fallback_used", "fallbacks"]
    assert_contract(tree, ["capture"], payload, keys)


def test_codex_open_a_new_panel(monkeypatch):
    from desktoppilot.cli import app
    from desktoppilot.plugins.vscode import codex

    class Named:
        def __init__(self, name):
            self.Name = name

    window = Named("proj - Visual Studio Code")
    fresh = codex.Panel(window=window, root=Named("Codex"))
    walks = iter([[], [fresh]])
    monkeypatch.setattr(codex, "_locate", lambda ctx, title: (window, None))
    monkeypatch.setattr(codex, "_codex_tabs", lambda target: [])
    monkeypatch.setattr(codex, "panels_in", lambda target: next(walks))
    monkeypatch.setattr(codex.palette, "run", lambda target, command: None)
    monkeypatch.setattr(codex, "_until_composer", lambda pan, timeout: ([], 2))
    payload = call(app, "vscode", "--no-shot", "codex", "open")
    assert (payload["opened"], payload["tour_pages"]) == (True, 2)
    keys = [
        "opened",
        "reused",
        "closed_previous",
        "tour_pages",
        "title",
        "window",
        "new_chat",
        "model_label",
        "permissions",
        "where",
        "generating",
        "blocked",
    ]
    assert_contract(app, ["vscode", "codex", "open"], payload, keys)


def test_vscode_git_compare(monkeypatch):
    from desktoppilot.cli import app
    from desktoppilot.plugins.vscode import git

    class Row:
        def __init__(self, name, cls="monaco-list-row"):
            self.Name, self.ClassName = name, cls

    node = Row("Comparing HEAD with main repo, has actions", "monaco-list-row selected")
    monkeypatch.setattr(git, "pick_window", lambda ctx, window: Row("repo - Visual Studio Code"))
    monkeypatch.setattr(git, "_view", lambda window: object())
    monkeypatch.setattr(git, "_nodes", lambda view, base: [node])
    monkeypatch.setattr(git.palette, "run", lambda *a, **k: None)
    monkeypatch.setattr(git, "_wait_picker", lambda timeout: None)
    monkeypatch.setattr(git.palette, "type_filter", lambda text: None)
    monkeypatch.setattr(git.palette, "pick", lambda *a, **k: "main, 0 0")
    monkeypatch.setattr(git, "_expand", lambda node: None)
    rows = [
        "Behind 0 commits",
        "Ahead 1 commit",
        "1 file changed, 2 additions (+), 0 deletions (-)",
    ]
    monkeypatch.setattr(git, "_children", lambda node: rows)
    payload = call(app, "vscode", "--no-shot", "git", "compare")
    keys = [
        "compared",
        "node",
        "existed_before",
        "ahead",
        "behind",
        "files_changed",
        "additions",
        "deletions",
        "row",
        "window",
    ]
    assert_contract(app, ["vscode", "git", "compare"], payload, keys)


# ------------------------------------------------------------ cdp


@pytest.fixture
def cdp_tree(monkeypatch):
    """A `chromeish` app with the cdp group, its one page a FakeDom."""
    from desktoppilot.core import cdp, cmd_cdp

    from .test_cdp import PAGE, FakeDom, FakeSocket

    profile = Profile(name="chromeish", args=["--remote-debugging-port=9555"])
    monkeypatch.setattr(cdp, "version", lambda port, timeout, hint="": {"Browser": "Chrome/1"})
    monkeypatch.setattr(cdp, "list_targets", lambda port, timeout, hint="": [PAGE])
    socket = FakeSocket(FakeDom("field", "x"))
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)

    reg = Registry()
    reg.group("cdp", cmd_cdp.cdp_group(), for_app=profile.name)
    return commands.build_app_cli(profile.name, profile, reg)


@pytest.mark.parametrize(
    ("command", "args", "keys"),
    [
        ("targets", [], ["port", "browser", "targets"]),
        ("target", [], ["id", "type", "title", "url", "webSocketDebuggerUrl"]),
        ("eval", ["1 + 1"], ["value"]),
        ("type", ["#q", "y"], ["typed", "into", "value"]),
        ("set-value", ["#q", "y"], ["set", "value", "verified"]),
        ("read", ["#q"], ["selector", "text", "via"]),
        ("wait", ["#q"], ["selector", "found"]),
        ("keys", ["ab{Enter}", "--into", "#q"], ["keys", "into", "events", "value"]),
    ],
)
def test_cdp(cdp_tree, command, args, keys):
    payload = call(cdp_tree, "cdp", command, *args)
    assert_contract(cdp_tree, ["cdp", command], payload, keys)


def test_cdp_click_and_shot(cdp_tree, monkeypatch, tmp_path):
    from desktoppilot.core import cdp

    monkeypatch.setattr(cdp.Page, "_script", lambda self, template, *a: {"x": 1, "y": 2})
    payload = call(cdp_tree, "cdp", "click", "#go")
    assert_contract(cdp_tree, ["cdp", "click"], payload, ["x", "y"])
    payload = call(cdp_tree, "cdp", "hover", "#go")
    assert_contract(cdp_tree, ["cdp", "hover"], payload, ["x", "y"])
    monkeypatch.setattr(cdp.Page, "send", lambda self, method, params=None: {"data": "cG5n"})
    payload = call(cdp_tree, "cdp", "shot", str(tmp_path / "page.png"))
    assert_contract(cdp_tree, ["cdp", "shot"], payload, ["path"])


def test_cdp_upload(cdp_tree, monkeypatch, tmp_path):
    from desktoppilot.core import cdp

    from .test_cdp import FakeSocket, FileInputDom

    socket = FakeSocket(FileInputDom())
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    attached = tmp_path / "shot.png"
    attached.write_bytes(b"png")
    payload = call(cdp_tree, "cdp", "upload", "input[type=file]", str(attached))
    assert payload["verified"] is True
    keys = ["selector", "files", "count", "names", "verified"]
    assert_contract(cdp_tree, ["cdp", "upload"], payload, keys)


def test_cdp_scroll(cdp_tree, monkeypatch):
    from desktoppilot.core import cdp

    from .test_cdp import FakeSocket, ScrollDom

    monkeypatch.setattr(cdp, "SETTLE", 0.001)
    socket = FakeSocket(ScrollDom())
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    payload = call(cdp_tree, "cdp", "scroll", "#box", "--by", "0,50")
    assert payload["top"] == 50
    keys = ["selector", "x", "y", "scroller", "top", "left", "max_top", "max_left"]
    assert_contract(cdp_tree, ["cdp", "scroll"], payload, keys)


def test_cdp_drag_and_navigate(cdp_tree, monkeypatch):
    from desktoppilot.core import cdp

    from .test_cdp import CENTRES, LANDED, LOADED, EventSocket

    socket = EventSocket({}, CENTRES)
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    payload = call(cdp_tree, "cdp", "drag", "#a", "#b")
    assert_contract(cdp_tree, ["cdp", "drag"], payload, ["from", "to", "mode"])
    socket = EventSocket({"Page.navigate": LOADED}, LANDED)
    payload = call(cdp_tree, "cdp", "navigate", "https://x/")
    assert_contract(cdp_tree, ["cdp", "navigate"], payload, ["url", "title"])


def test_cdp_console(cdp_tree):
    payload = call(cdp_tree, "cdp", "console")
    assert payload["entries"] == [] and payload["count"] == 0
    assert_contract(cdp_tree, ["cdp", "console"], payload, ["entries", "count", "errors"])
    help_text = documented(cdp_tree, "cdp", "console")
    for key in ("level", "source", "text", "url", "line", "timestamp"):
        assert key in help_text


class FakeRadio:
    """A radio menu item: SelectionItem, and a name."""

    Name = "Large"

    def __init__(self):
        self.selected = False

    def GetSelectionItemPattern(self):
        return self

    def Select(self):
        self.selected = True


def test_ui_select_without_an_item(fake_app, monkeypatch):
    tree, _ = fake_app
    radio = FakeRadio()
    monkeypatch.setattr(ui, "find", lambda *a, **k: radio)
    monkeypatch.setattr(ui, "activate_app", lambda *a, **k: None)
    monkeypatch.setattr(ui, "describe", lambda control, **k: {"name": control.Name})
    payload = call(tree, "--no-shot", "ui", "select", "app//MenuItem")
    assert radio.selected and payload["selected"] == "Large"
    keys = ["selected", "expanded", "via", "element"]
    assert_contract(tree, ["ui", "select"], payload, keys)


class FakeNode:
    """A collapsed tree node that opens."""

    def __init__(self):
        self.ExpandCollapseState = 0

    def GetExpandCollapsePattern(self):
        return self

    def Expand(self, waitTime=0):
        self.ExpandCollapseState = 1


def test_ui_expand(fake_app, monkeypatch):
    tree, _ = fake_app
    monkeypatch.setattr(ui, "find", lambda *a, **k: FakeNode())
    payload = call(tree, "--no-shot", "ui", "expand", "app//TreeItem")
    assert (payload["expanded"], payload["state"]) == (True, "expanded")
    assert_contract(tree, ["ui", "expand"], payload, ["selector", "expanded", "state"])


def test_dialogs_close(fake_app, monkeypatch):
    from desktoppilot.core import dialogs

    tree, running = fake_app
    running.append(11)
    open_dialogs = [{"hwnd": 3, "title": "Question", "text": "Sure?"}]
    monkeypatch.setattr(dialogs, "dialog_windows", lambda pids, **k: list(open_dialogs))
    monkeypatch.setattr(
        dialogs, "click_button", lambda hwnd, labels, **k: open_dialogs.clear() or "Cancel"
    )
    payload = call(tree, "dialogs", "--close")
    assert payload["remaining"] == [] and payload["closed"][0]["answered"] == "Cancel"
    assert_contract(tree, ["dialogs"], payload, ["closed", "remaining"])


def test_desktop_shot_takes_the_desktop(monkeypatch):
    """`pilot desktop` has no `app` root: its shot defaults to the desktop."""
    from desktoppilot.core import cmd_misc

    seen = []

    def take_shot(actx, selector, *args):
        seen.append(selector)
        raise cmd_misc.PilotError("stop here")

    monkeypatch.setattr(cmd_misc, "take_shot", take_shot)
    desktop = commands.build_app_cli("desktop", None, Registry())
    runner.invoke(desktop, ["--no-shot", "shot"])
    assert seen == ["desktop"]
