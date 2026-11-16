"""The core `pilot` commands through the CLI on real Linux apps: Mousepad
(GTK3), a GTK3 window with radio items, zenity and GNOME Calculator (GTK4),
a scratch Chrome and a scratch VS Code (Electron).

Every app is started by `pilot <app> app launch` from a throwaway profile in
a config folder of this test's own (XDG_CONFIG_HOME), on a copy of the binary
under another name where the user's own instance could be adopted (Chrome,
VS Code), so `app kill` can never reach the user's. The pilot calls run with
ELECTRON_RUN_AS_NODE set, to prove `app launch` drops it.

A command that sends input or opens a popup needs the window in front: those
tests run only with PILOT_TEST_ACTIVATE set, and before every burst the
window is activated and `_NET_ACTIVE_WINDOW` is checked to be it, else the
test stops. The clipboard's text is put back at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the X11 backend"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs an X display"),
]

if sys.platform.startswith("linux"):
    from desktoppilot.core import clip
    from desktoppilot.core.util import wait_until
    from desktoppilot.linux import x11

PILOT = str(Path(sys.executable).parent / "pilot")
SYSTEM_PYTHON = "/usr/bin/python3"
SNAP_CODE = Path("/snap/code/current/usr/share/code")
CHROME_DIR = Path("/opt/google/chrome")

activates = pytest.mark.skipif(
    not os.environ.get("PILOT_TEST_ACTIVATE"),
    reason="activates its window (set PILOT_TEST_ACTIVATE=1 when no other agent drives the desktop)",
)


@pytest.fixture(scope="module", autouse=True)
def keep_clipboard():
    saved = clip.read()
    yield
    if saved:
        clip.write(saved)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Pilot:
    """`pilot APP --no-shot ...` with the profiles of this test's config folder."""

    def __init__(self, app: str, config: Path):
        self.app = app
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(config), ELECTRON_RUN_AS_NODE="1")
        self.hwnd = 0

    def run(self, *args: str, timeout: float = 120, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PILOT, self.app, "--no-shot", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )

    def __call__(self, *args: str, ok: bool | None = True, **kw) -> dict:
        """The command's JSON; `ok` is whether it must succeed (None: either)."""
        ran = self.run(*args, **kw)
        assert ok is None or (ran.returncode == 0) is ok, ran.stdout + ran.stderr
        return json.loads(ran.stdout)

    def fail(self, *args: str, **kw) -> dict:
        return self(*args, ok=False, **kw)

    def focus(self) -> None:
        """Activate the app's main window; stop unless it is the active one."""
        hwnd = self.hwnd
        x11.activate(hwnd)
        if x11.active_window() != hwnd:
            pytest.fail(f"window {hwnd:#x} is not active; sent no input")

    def windows(self) -> list[dict]:
        return self("ui", "windows")["windows"]

    def menu_gone(self) -> bool:
        """No popup window of the app is left: one more would hold an X grab."""
        return len(self.windows()) == 1


def profile(root: Path, name: str, **keys) -> Path:
    """A user profile `name`: TOML from keys (a dict is a table, last)."""
    config = root / "config"
    folder = config / "desktoppilot" / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    tables = {k: v for k, v in keys.items() if isinstance(v, dict)}
    lines = [f'name = "{name}"']
    lines += [f"{k} = {json.dumps(v)}" for k, v in keys.items() if k not in tables]
    for table, values in tables.items():
        lines.append(f"[{table}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    (folder / f"{name}.toml").write_text("\n".join(lines) + "\n")
    return config


def launched(pilot: Pilot, *args: str) -> dict:
    """`app launch`, the main window recorded on `pilot.hwnd`."""
    info = pilot("app", "launch", *args)
    assert info["adopted"] is False and info["running"] is True
    pilot.hwnd = info["hwnd"]
    return info


def environ_of(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode().split("=", 1) for item in raw if b"=" in item)


def stop(pilot: Pilot) -> None:
    pilot("app", "kill", "--force", ok=None)


# ------------------------------------------------------------ Mousepad (GTK3)


@pytest.fixture(scope="module")
def mousepad(tmp_path_factory):
    exe = shutil.which("mousepad")
    if exe is None:
        pytest.skip("needs mousepad")
    root = tmp_path_factory.mktemp("mousepad")
    doc = root / "pilot-cmd-doc.txt"
    doc.write_text("line one\nline two\n")
    name = f"pilotmp{os.getpid()}"
    config = profile(
        root,
        name,
        exe=[exe],
        args=["--disable-server"],
        window_class=["Org.xfce.mousepad"],
        title_strip=[" - Mousepad"],
        launch="fresh",
        dialogs="explicit",
        env={"GSETTINGS_BACKEND": "memory", "XDG_DATA_HOME": str(root)},
    )
    pilot = Pilot(name, config)
    pilot.root, pilot.doc = root, doc
    info = launched(pilot, str(doc))
    pilot.pid = info["pids"][0]
    try:
        yield pilot
    finally:
        stop(pilot)


def edit_text(pilot: Pilot) -> str:
    return pilot("ui", "read", "app//Edit")["text"]


def test_launch_drops_the_leaks_of_the_callers_environment(mousepad):
    env = environ_of(mousepad.pid)
    assert "ELECTRON_RUN_AS_NODE" not in env
    assert "NO_AT_BRIDGE" not in env
    assert env["GSETTINGS_BACKEND"] == "memory"  # the profile's env still applies


def test_app_status_attach_and_resize(mousepad):
    status = mousepad("app", "status")
    assert status["running"] is True and status["pids"] == [mousepad.pid]
    assert status["windows"][0]["is_main"] is True
    assert mousepad("app", "attach")["hwnd"] == mousepad.hwnd
    sized = mousepad("app", "resize", "760", "560")
    assert sized["dips"] == [760, 560] and sized["clamped"] is False


def test_desktop_shot_defaults_to_the_desktop(mousepad, tmp_path):
    desktop = Pilot("desktop", Path(mousepad.env["XDG_CONFIG_HOME"]))
    shot = desktop("shot", str(tmp_path / "desktop.png"))
    assert shot["origin"] == [0, 0] and shot["size"][0] >= 800


def test_find_read_tree_windows_and_state(mousepad):
    found = mousepad("ui", "find", "app//Edit")
    assert found["element"]["type"] == "Edit" and "Value" in found["element"]["patterns"]
    assert found["element"]["value"] == "line one\nline two\n"
    assert mousepad("ui", "find", "app//MenuItem", "--all")["count"] >= 6
    read = mousepad("ui", "read", "app//Edit")
    assert read == {"selector": "app//Edit", "via": "uia", "text": "line one\nline two\n"}
    tree = mousepad("ui", "tree", "--levels", "3", "--limit", "50")
    assert tree["root"] == "app" and tree["tree"]["type"] == "Window" and tree["complete"] is True
    flat = mousepad("ui", "tree", "--flat")
    assert any(c["name"] == "File" for c in flat["controls"])
    assert [w["hwnd"] for w in mousepad("ui", "windows")["windows"]] == [mousepad.hwnd]
    state = mousepad("state")
    assert state["running"] is True and state["dialogs"] == []
    assert mousepad("dialogs") == {"dialogs": []}


def test_errors_have_the_documented_kinds(mousepad):
    gone = mousepad.fail("ui", "find", "app//Button[name='Nope']", "--timeout", "1")
    assert gone["error"] == "Timeout"
    bad = mousepad.fail("ui", "find", "app//Bogus", "--timeout", "1")
    assert bad["error"] == "PilotError" and "unknown control type" in bad["message"]
    mode = mousepad.fail("ui", "click", "app//Edit", "--mode", "bogus")
    assert "mode must be" in mode["message"]
    assert "SELECTOR or --at" in mousepad.fail("ui", "click")["message"]


def test_set_value_is_verified_and_read_back(mousepad):
    done = mousepad("ui", "set-value", "app//Edit", "set by value é")
    assert done["verified"] is True and done["value"] == "set by value é"
    assert edit_text(mousepad) == "set by value é"
    assert mousepad("ui", "set-value", "app//Edit", "")["verified"] is True
    assert edit_text(mousepad) == ""


def test_shot_variants(mousepad, tmp_path):
    from PIL import Image

    whole = mousepad("shot", str(tmp_path / "whole.png"))
    with Image.open(whole["path"]) as image:
        assert list(image.size) == whole["size"] == whole["image_size"]
    assert whole["origin"][0] >= 0 and whole["occluded"] in (True, False)
    part = mousepad("shot", str(tmp_path / "part.png"), "--selector", "app//Edit", "--zoom", "2")
    assert part["scale"] == 2.0
    gridded = mousepad(
        "shot", str(tmp_path / "grid.png"), "--region", "0,0,200,100", "--grid", "50"
    )
    assert gridded["grid"]["step"] == 50 and gridded["size"] == [200, 100]


def test_capture_writes_the_evidence(mousepad, tmp_path):
    done = mousepad("capture", "cmdcap", "--directory", str(tmp_path / "artifacts"))
    assert done["failed"] == {} and done["complete"]["tree"] is True
    for piece in ("screenshot", "state", "tree", "controls", "events", "manifest"):
        assert Path(done["saved"][piece]).exists() or (tmp_path / done["saved"][piece]).exists()


def test_clip_round_trips_text(mousepad):
    text = "clip é ✓\nsecond line"
    assert mousepad("clip", "write", text) == {"written": len(text)}
    assert mousepad("clip", "read") == {"text": text}


def test_image_group(mousepad, tmp_path):
    from PIL import Image

    shot = mousepad("shot", str(tmp_path / "base.png"))["path"]
    boxed = mousepad(
        "image",
        "annotate",
        shot,
        "--op",
        json.dumps({"type": "box", "at": "10,10,50,30"}),
        "--op",
        json.dumps({"type": "badge", "at": "app//Edit", "text": "1"}),
    )
    assert [a["type"] for a in boxed["annotations"]] == ["box", "badge"]
    assert Path(boxed["path"]).name == "base-annotated.png"
    cropped = mousepad("image", "crop", shot, "0,0,100,80")
    assert cropped["image_size"] == [100, 80]
    resized = mousepad("image", "resize", shot, "--width", "200")
    assert resized["image_size"][0] == 200
    padded = mousepad("image", "pad", shot, "--margin", "10")
    with Image.open(shot) as base:
        assert padded["image_size"] == [base.width + 20, base.height + 20]
    joined = mousepad(
        "image", "join", cropped["path"], resized["path"], "--out", str(tmp_path / "j.png")
    )
    assert joined["image_size"][0] == 100 + 200
    stacked = mousepad(
        "image",
        "join",
        cropped["path"],
        resized["path"],
        "--out",
        str(tmp_path / "v.png"),
        "--vertical",
    )
    assert stacked["image_size"][1] == 80 + resized["image_size"][1]
    inline = mousepad(
        "image",
        "shot",
        str(tmp_path / "is.png"),
        "--op",
        json.dumps({"type": "box", "at": "app//Edit"}),
    )
    assert inline["annotations"][0]["type"] == "box"


def test_events_mark_and_dumps_shapes(mousepad):
    listed = mousepad("events", "list", "--since", "5m")
    assert listed["image"] == "mousepad" and listed["count"] == len(listed["records"])
    marked = mousepad("events", "mark")
    assert Path(marked["mark"]).exists()
    after = mousepad("events", "list", "--since-mark")
    assert after["since"] is None and after["count"] == 0
    dumps = mousepad("events", "dumps")
    assert dumps["image"] == "mousepad" and isinstance(dumps["dumps"], list)


@activates
def test_type_keys_click_and_read_copy(mousepad):
    mousepad.focus()
    mousepad("ui", "click", "app//Edit")
    mousepad.focus()
    mousepad("ui", "keys", "{Ctrl}{End}")
    mousepad.focus()
    typed = mousepad("ui", "type", "added é ✓")
    assert typed["typed"] == "added é ✓" and typed["via"] == "keys"
    assert edit_text(mousepad).endswith("added é ✓")
    mousepad.focus()
    copied = mousepad("ui", "read", "app//Edit", "--copy")
    assert copied["via"] == "clipboard" and copied["text"].endswith("added é ✓")
    mousepad.focus()
    mousepad("ui", "type", "Z", "--into", "app//Edit", "--clear")
    assert edit_text(mousepad) == "Z"
    mousepad.focus()
    mousepad("ui", "keys", "{Ctrl}a{Delete}", "--into", "app//Edit")
    assert edit_text(mousepad) == ""


@activates
def test_click_at_a_point_and_relative(mousepad, tmp_path):
    rect = mousepad("ui", "find", "app//Edit")["element"]["rect"]
    x, y = rect[0] + 20, rect[1] + 20
    mousepad.focus()
    clicked = mousepad("ui", "click", "--at", f"{x},{y}")
    assert clicked["clicked"] is None and clicked["point"] == [x, y]
    assert clicked["via"] == "mouse" and clicked["relative"] is False
    mousepad.focus()
    origin = mousepad("shot", str(tmp_path / "frame.png"))["origin"]
    rel = mousepad("ui", "click", "--at", f"{x - origin[0]},{y - origin[1]}", "--relative")
    assert rel["relative"] is True and rel["point"] == [x, y]


@activates
def test_menu_invoke_select_expand_and_activate(mousepad):
    assert mousepad("ui", "activate")["activated"] == "app"
    mousepad.focus()
    try:
        opened = mousepad("ui", "click", "app//MenuItem[name='Document']", "--mode", "invoke")
        assert opened["clicked"] == "app//MenuItem[name='Document']"
        wait_until(lambda: len(mousepad.windows()) == 2, timeout=5, what="the popup")
        popup, main = sorted(mousepad.windows(), key=lambda w: w["is_main"])
        assert popup["is_main"] is False and main["hwnd"] == mousepad.hwnd
        mousepad.focus()
        mousepad("ui", "keys", "{Esc}")
        wait_until(mousepad.menu_gone, timeout=5, what="the menu to close")
        mousepad.focus()
        chosen = mousepad("ui", "select", "app//MenuItem[name='Document']", "--item", "Word Wrap")
        assert chosen["selected"] == "Word Wrap"
        wait_until(mousepad.menu_gone, timeout=5, what="the menu to close")
        mousepad.focus()
        mousepad("ui", "select", "app//MenuItem[name='Document']", "--item", "Word Wrap")  # back
    finally:
        mousepad.focus()
        mousepad("ui", "keys", "{Esc}")
        wait_until(mousepad.menu_gone, timeout=5, what="the menu to close")


@activates
def test_dialog_listing_and_close(mousepad):
    mousepad.focus()
    mousepad("ui", "keys", "{Ctrl}o")
    shown = wait_until(lambda: mousepad("dialogs")["dialogs"], timeout=15, what="the file chooser")
    assert shown[0]["hwnd"] and shown[0]["title"]
    no_policy = mousepad("dialogs", "--dismiss")
    assert no_policy["dismissed"] == [] and "no prompt policy" in no_policy["note"]
    closed = mousepad("dialogs", "--close")
    assert closed["closed"][0]["answered"] in {"Cancel", "Close", "No", "OK"}
    assert closed["remaining"] == []
    wait_until(lambda: not mousepad("dialogs")["dialogs"], timeout=10, what="the dialog to close")


@activates
def test_record_mp4_and_gif(mousepad, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    movie = tmp_path / "rec.mp4"
    started = mousepad("record", "start", str(movie))
    assert started["target"]["kind"] == "window" and started["pid"]
    assert mousepad("record", "status")["recording"] is True
    wait_until(lambda: mousepad("record", "status")["elapsed_s"] >= 2, timeout=10, what="2 s")
    done = mousepad("record", "stop")
    assert done["video"]["codec"] == "h264" and done["duration_s"] >= 2
    assert mousepad("record", "status") == {"recording": False}
    gif = mousepad("record", "gif", str(movie))
    assert Path(gif["path"]).suffix == ".gif" and Path(gif["path"]).stat().st_size > 0


def test_kill_and_the_session_afterwards(tmp_path_factory):
    """A throwaway Mousepad of its own: `app kill` ends it, `status` says so."""
    exe = shutil.which("mousepad")
    if exe is None:
        pytest.skip("needs mousepad")
    root = tmp_path_factory.mktemp("killme")
    name = f"pilotkill{os.getpid()}"
    config = profile(
        root,
        name,
        exe=[exe],
        args=["--disable-server"],
        window_class=["Org.xfce.mousepad"],
        launch="fresh",
        env={"GSETTINGS_BACKEND": "memory", "XDG_DATA_HOME": str(root)},
    )
    pilot = Pilot(name, config)
    info = launched(pilot)
    try:
        killed = pilot("app", "kill")
        assert killed["killed"] == info["pids"] and killed["quit_errors"] == []
        status = pilot("app", "status")
        assert status["running"] is False and status["window"] is None
    finally:
        stop(pilot)


# ------------------------------------------------------------ GTK3 radio items

GTK3_RADIO = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
win = Gtk.Window(title=sys.argv[1])
win.set_default_size(420, 300)
win.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
bar, view, menu = Gtk.MenuBar(), Gtk.MenuItem(label="View"), Gtk.Menu()
view.set_submenu(menu)
group = None
for label in ("Alpha", "Beta", "Gamma"):
    item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
    group = group or item
    menu.append(item)
menu.append(Gtk.CheckMenuItem(label="Check me"))
bar.append(view)
box.pack_start(bar, False, False, 0)
entry = Gtk.Entry()
box.pack_start(entry, False, False, 0)
label = Gtk.Label(label="a label text")
box.pack_start(label, False, False, 0)
combo = Gtk.ComboBoxText()
for text in ("one", "two", "three"):
    combo.append_text(text)
combo.set_active(0)
box.pack_start(combo, False, False, 0)
rows = Gtk.ListBox()
for text in ("row a", "row b", "row c"):
    rows.add(Gtk.Label(label=text))
box.pack_start(rows, False, False, 0)
button = Gtk.Button(label="Press")
button.connect("clicked", lambda b: label.set_text("pressed"))
box.pack_start(button, False, False, 0)
win.add(box)
win.show_all()
Gtk.main()
"""


@pytest.fixture(scope="module")
def radio(tmp_path_factory):
    """A GTK3 window with radio and check menu items, entry, combo, list and
    button, run by a copy of the system python (its own image name)."""
    pytest.importorskip("gi")
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip("needs the system python with GTK")
    root = tmp_path_factory.mktemp("radio")
    name = f"pilotradio{os.getpid()}"
    title = f"PilotRadio{os.getpid()}"
    exe = root / name
    shutil.copy2(os.path.realpath(SYSTEM_PYTHON), exe)
    script = root / "radio.py"
    script.write_text(GTK3_RADIO)
    config = profile(
        root,
        name,
        exe=[str(exe)],
        args=[str(script), title],
        window_class=["radio.py", "Radio.py"],
        launch="fresh",
        dialogs="explicit",
        env={"PYTHONHOME": "/usr"},
    )
    pilot = Pilot(name, config)
    launched(pilot)
    try:
        yield pilot
    finally:
        stop(pilot)


def checked(pilot: Pilot) -> list[str]:
    """The names of the radio and check items that are checked, by AT-SPI."""
    from desktoppilot.linux import auto

    found = []

    def walk(node, depth=0):
        if depth > 8:
            return
        for child in node.GetChildren():
            if (
                child._role() in ("radio menu item", "check menu item")
                and "checked" in child._states()
            ):
                found.append(child.Name)
            walk(child, depth + 1)

    walk(auto.ControlFromHandle(pilot.hwnd))
    return found


def test_radio_items_expose_selection_item_and_toggle_patterns(radio):
    items = radio("ui", "find", "app//MenuItem", "--all", "--hidden")["elements"]
    by_name = {e["name"]: e for e in items}
    assert by_name["Beta"]["patterns"] == ["SelectionItem"]
    assert by_name["Check me"]["patterns"] == ["Toggle"]
    assert by_name["View"]["patterns"] == ["ExpandCollapse"]


@activates
def test_select_of_a_radio_menu_item_succeeds_when_the_menu_closes(radio):
    radio.focus()
    try:
        done = radio("ui", "select", "app//MenuItem[name='View']", "--item", "Beta")
        assert done["selected"] == "Beta" and done["via"] == "SelectionItem"
        wait_until(radio.menu_gone, timeout=5, what="the menu to close")
        assert "Beta" in checked(radio)
        # the same item again, and another one: the group keeps exactly one
        radio.focus()
        radio("ui", "select", "app//MenuItem[name='View']", "--item", "Beta")
        radio.focus()
        radio("ui", "select", "app//MenuItem[name='View']", "--item", "Gamma")
        wait_until(radio.menu_gone, timeout=5, what="the menu to close")
        radios = [n for n in checked(radio) if n in {"Alpha", "Beta", "Gamma"}]
        assert radios == ["Gamma"]
    finally:
        radio.focus()
        radio("ui", "keys", "{Esc}")


@activates
def test_toggle_a_check_menu_item_through_its_pattern(radio):
    radio.focus()
    try:
        radio("ui", "click", "app//MenuItem[name='View']", "--mode", "invoke")
        done = radio("ui", "click", "app//MenuItem[name='Check me']", "--mode", "invoke")
        assert done["via"] == "Toggle"
        assert "Check me" in checked(radio)
        wait_until(radio.menu_gone, timeout=5, what="the menu to close")
        radio.focus()
        radio("ui", "click", "app//MenuItem[name='View']", "--mode", "invoke")
        radio("ui", "click", "app//MenuItem[name='Check me']", "--mode", "invoke")
        assert "Check me" not in checked(radio)
    finally:
        radio.focus()
        radio("ui", "keys", "{Esc}")
        wait_until(radio.menu_gone, timeout=5, what="the menu to close")


def test_combo_list_entry_and_button_patterns(radio):
    chosen = radio("ui", "select", "app//ComboBox", "--item", "two")
    assert chosen["selected"] == "two"
    assert radio("ui", "read", "app//ComboBox")["text"] == "two"
    picked = radio("ui", "select", "app//List", "--item", "row b")
    assert picked["selected"] == "row b" and picked["via"] == "SelectionItem"
    counted = radio("ui", "count", "app//List")
    assert counted["count"] == 3 and counted["names"] == ["row a", "row b", "row c"]
    radio("ui", "set-value", "app//Edit", "entry set")
    assert radio("ui", "read", "app//Edit")["text"] == "entry set"
    radio("ui", "click", "app//Button[name='Press']", "--mode", "invoke")
    wait_until(
        lambda: radio("ui", "find", "app//Text[name='pressed']", "--timeout", "1", ok=None).get(
            "element"
        ),
        timeout=10,
        what="the label to change",
    )


def test_select_without_item_reports_label_via_and_element(radio):
    done = radio("ui", "select", "app//ListItem[name='row c']")
    assert done["selected"] == "row c" and done["via"] == "SelectionItem"
    assert done["element"]["name"] == "row c"


def test_a_label_reads_as_no_text_and_scroll_has_no_pattern(radio):
    # known limits (README, Linux): a label's text is its name, not a value
    assert radio("ui", "read", "app//Text[name='pressed']", ok=None).get("text") in (
        None,
        "pressed",
    )
    failed = radio.fail("ui", "scroll", "app//List", "--vertical", "10")
    assert "no Scroll pattern" in failed["message"]
    expand = radio.fail("ui", "expand", "app//MenuBar", "--all")
    assert "not a tree" in expand["message"]


@activates
def test_expand_reports_a_bool_and_a_state_name(radio):
    radio.focus()
    try:
        opened = radio("ui", "expand", "app//MenuItem[name='View']")
        assert opened["expanded"] is True and isinstance(opened["state"], str)
    finally:
        radio.focus()
        radio("ui", "keys", "{Esc}")
        wait_until(radio.menu_gone, timeout=5, what="the menu to close")


def test_wait_and_gone(radio):
    assert radio("ui", "wait", "app//Button[name='Press']")["found"] is True
    assert radio("ui", "wait", "app//Button[name='Nope']", "--gone")["gone"] is True
    timed = radio.fail("ui", "wait", "app//Button[name='Nope']", "--timeout", "1")
    assert timed["error"] == "Timeout"


# ------------------------------------------------------------ zenity (GTK4)


def zenity_profile(root: Path, name: str, *args: str) -> Pilot:
    exe = shutil.which("zenity")
    if exe is None:
        pytest.skip("needs zenity")
    config = profile(
        root,
        name,
        exe=[exe],
        args=list(args),
        window_class=["zenity"],
        launch="fresh",
        dialogs="explicit",
    )
    return Pilot(name, config)


def test_zenity_entry_dialog_is_listed_read_and_set(tmp_path):
    name = f"pilotzen{os.getpid()}"
    pilot = zenity_profile(
        tmp_path, name, "--entry", "--title=PilotZen", "--text=Your name?", "--entry-text=start"
    )
    launched(pilot)
    try:
        listed = pilot("dialogs")["dialogs"]
        assert listed[0]["title"] == "PilotZen" and "Your name?" in listed[0]["text"]
        assert pilot("ui", "read", "app//Edit")["text"] == "start"
        done = pilot("ui", "set-value", "app//Edit", "from set-value")
        assert done["verified"] is True
        assert pilot("ui", "read", "app//Edit")["text"] == "from set-value"
        buttons = pilot("ui", "find", "app//Button", "--all")
        assert [b["name"] for b in buttons["elements"]] == ["Cancel", "OK"]
        pilot("ui", "click", "app//Button[name='OK']", "--mode", "invoke")
        wait_until(lambda: pilot("app", "status")["running"] is False, timeout=10, what="the exit")
        log = Path.home() / ".local/state/desktoppilot/sessions/zenity.log"
        assert "from set-value" in log.read_text()
    finally:
        stop(pilot)


@activates
def test_zenity_type_into_a_gtk4_entry(tmp_path):
    name = f"pilotzen{os.getpid()}"
    pilot = zenity_profile(tmp_path, name, "--entry", "--title=PilotZenType", "--entry-text=start")
    launched(pilot)
    try:
        pilot.focus()
        pilot("ui", "type", "Zq", "--into", "app//Edit", "--clear")
        assert pilot("ui", "read", "app//Edit")["text"] == "Zq"
        pilot.focus()
        assert pilot("ui", "read", "app//Edit", "--copy")["text"] == "Zq"
        pilot.focus()
        pilot("ui", "keys", "{Ctrl}a{Delete}", "--into", "app//Edit")
        assert pilot("ui", "read", "app//Edit")["text"] == ""
    finally:
        stop(pilot)


@activates
def test_zenity_type_without_into_and_click_at(tmp_path):
    name = f"pilotzen{os.getpid()}"
    pilot = zenity_profile(tmp_path, name, "--entry", "--title=PilotZenKeys", "--entry-text=start")
    launched(pilot)
    try:
        pilot.focus()
        pilot("ui", "keys", "{Ctrl}a")
        pilot.focus()
        pilot("ui", "type", "Zq")
        assert pilot("ui", "read", "app//Edit")["text"] == "Zq"
        pilot.focus()
        pilot("ui", "keys", "{End}!!")
        assert pilot("ui", "read", "app//Edit")["text"] == "Zq!!"
    finally:
        stop(pilot)


def test_zenity_question_closes_with_the_preferred_button(tmp_path):
    name = f"pilotzenq{os.getpid()}"
    pilot = zenity_profile(
        tmp_path, name, "--question", "--title=PilotQ", "--text=Proceed with it?"
    )
    launched(pilot)
    try:
        assert pilot("dialogs")["dialogs"][0]["title"] == "PilotQ"
        closed = pilot("dialogs", "--close")
        assert closed["closed"][0]["answered"] == "No" and closed["remaining"] == []
        wait_until(lambda: pilot("dialogs")["dialogs"] == [], timeout=10, what="the dialog to go")
    finally:
        stop(pilot)


# ------------------------------------------------------------ GNOME Calculator (GTK4)


@pytest.fixture(scope="module")
def calculator(tmp_path_factory):
    exe = shutil.which("gnome-calculator")
    if exe is None:
        pytest.skip("needs gnome-calculator")
    root = tmp_path_factory.mktemp("calc")
    name = f"pilotcalc{os.getpid()}"
    config = profile(
        root,
        name,
        exe=[exe],
        window_class=["gnome-calculator"],
        launch="fresh",
        dialogs="explicit",
        depth=30,
        env={"GSETTINGS_BACKEND": "memory"},
    )
    pilot = Pilot(name, config)
    launched(pilot)
    try:
        yield pilot
    finally:
        stop(pilot)


def test_calculator_buttons_toggle_and_disabled(calculator):
    for digit in "78":
        done = calculator(
            "ui", "click", f"app//Button[id='calc_{digit}_button']", "--mode", "invoke"
        )
        assert done["via"] == "Invoke"
    assert calculator("ui", "read", "app//Edit[id='source_view']")["text"] == "78"
    assert calculator("ui", "set-value", "app//Edit[id='source_view']", "6*7")["verified"] is True
    toggled = calculator("ui", "click", "app//Button[name='↑n']", "--mode", "invoke")
    assert toggled["via"] == "Toggle"
    calculator("ui", "click", "app//Button[name='↑n']", "--mode", "invoke")
    disabled = calculator.fail("ui", "click", "app//Button[name='Redo']", "--timeout", "1")
    assert disabled["error"] == "Disabled"
    forced = calculator("ui", "click", "app//Button[name='Redo']", "--allow-disabled")
    assert forced["element"]["enabled"] is False
    calculator("ui", "click", "app//Button[name='C']", "--mode", "invoke")


@activates
def test_calculator_radio_menu_item_is_selected_by_position(calculator):
    """GTK4 popover radio items have no accessible name, and sit deeper than
    a default depth: pick by class and index, with --depth."""
    calculator.focus()
    try:
        calculator("ui", "click", "app//Button[id='menu_button']")
        calculator.focus()
        picked = calculator("ui", "select", "app//MenuItem[class~='radio']#2")
        assert picked["selected"] == "" and picked["via"] == "SelectionItem"
        assert picked["element"]["class"] == "radio menu item"
        wait_until(calculator.menu_gone, timeout=5, what="the menu to close")
        mode = calculator("ui", "find", "app//Button[id='menu_button']")["element"]["name"]
        assert mode == "Advanced"
    finally:
        calculator.focus()
        calculator("ui", "click", "app//Button[id='menu_button']")
        calculator.focus()
        calculator("ui", "select", "app//MenuItem[class~='radio']#1")
        wait_until(calculator.menu_gone, timeout=5, what="the menu to close")


# ------------------------------------------------------------ Chrome (Chromium)

PAGE = """<!doctype html><title>{title}</title>
<input id=name aria-label=Name>
<textarea id=notes aria-label=Notes></textarea>
<div id=rich contenteditable=true role=textbox aria-label=Rich>rich start</div>
<select id=pick aria-label=Pick><option value=a>A</option><option value=b>B</option></select>
<button id=go onclick="out.textContent = 'hello ' + document.getElementById('name').value">Go</button>
<p id=out>idle para</p>
"""


def copy_chrome(root: Path) -> Path:
    """Chrome under the image name chrome-pilotcmd, the rest linked beside it."""
    folder = root / "chrome"
    folder.mkdir()
    for item in CHROME_DIR.iterdir():
        if item.name != "chrome":
            (folder / item.name).symlink_to(item)
    shutil.copy2(CHROME_DIR / "chrome", folder / "chrome-pilotcmd")
    return folder / "chrome-pilotcmd"


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    if not (CHROME_DIR / "chrome").exists():
        pytest.skip("needs Chrome in /opt/google/chrome")
    root = tmp_path_factory.mktemp("chromecmd")
    title = f"PilotCmd{os.getpid()}"
    page = root / "page.html"
    page.write_text(PAGE.format(title=title))
    port = free_port()
    name = f"pilotchrome{os.getpid()}"
    folder = root / "config" / "desktoppilot" / "profiles"
    folder.mkdir(parents=True)
    args = [
        "--force-renderer-accessibility",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={root / 'data'}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--new-window",
        page.as_uri(),
    ]
    (folder / f"{name}.toml").write_text(
        f'extends = "chrome"\nlaunch = "fresh"\ntitle_strip = [" - Google Chrome"]\n'
        f"args = {json.dumps(args)}\n"
        f"[linux]\nexe = [{json.dumps(str(copy_chrome(root)))}]\n"
        'window_class = ["Google-chrome", "chrome-pilotcmd", "Chromium"]\n'
    )
    pilot = Pilot(name, root / "config")
    pilot.port, pilot.title, pilot.root = port, title, root
    info = launched(pilot)
    pilot.pid = info["pids"][0]
    try:
        wait_until(
            lambda: pilot("ui", "find", "app//Button[name='Go']", "--timeout", "2", ok=None).get(
                "element"
            ),
            timeout=60,
            what="the page",
        )
        yield pilot
    finally:
        stop(pilot)


def cdp(chrome: Pilot, *args: str, ok: bool | None = True) -> dict:
    return chrome("cdp", *args, "--port", str(chrome.port), "--url", "page.html", ok=ok)


def test_chrome_client_rect_stays_inside_the_window_rect(chrome):
    from desktoppilot.linux import win32gui

    left, top, right, bottom = win32gui.GetWindowRect(chrome.hwnd)
    cl, ct, cr, cb = x11.client_rect(chrome.hwnd)
    assert left <= cl < cr <= right and top <= ct < cb <= bottom


def test_chrome_launch_env_and_image(chrome):
    assert "ELECTRON_RUN_AS_NODE" not in environ_of(chrome.pid)
    assert chrome("app", "status")["windows"][0]["title_stripped"] == chrome.title


def test_chrome_ui_read_of_a_contenteditable_and_a_paragraph(chrome):
    rich = chrome("ui", "read", "app//Edit[name='Rich']")
    assert rich["text"] == "rich start"  # not U+FFFC
    assert "￼" not in rich["text"]
    paragraph = chrome("ui", "read", "app//Group[id='out']")
    assert paragraph["text"] == "idle para"


def test_chrome_ui_set_value_points_to_cdp_and_cdp_set_value_works(chrome):
    failed = chrome.fail("ui", "set-value", "app//Edit[name='Name']", "x")
    assert "cdp set-value" in failed["message"]
    done = cdp(chrome, "set-value", "#name", "Zoë 日本")
    assert done["verified"] is True
    assert chrome("ui", "read", "app//Edit[name='Name']")["text"] == "Zoë 日本"


def test_chrome_cdp_group(chrome, tmp_path):
    targets = chrome("cdp", "targets", "--port", str(chrome.port))
    assert targets["port"] == chrome.port and targets["browser"].startswith("Chrome/")
    target = cdp(chrome, "target")
    assert target["type"] == "page" and target["title"] == chrome.title
    assert cdp(chrome, "eval", "document.title")["value"] == chrome.title
    assert cdp(chrome, "eval", "Promise.resolve({a: [1, 2]})")["value"] == {"a": [1, 2]}
    thrown = cdp(chrome, "eval", "throw new Error('x')", ok=False)
    assert "script threw" in thrown["message"]
    assert cdp(chrome, "type", "#notes", "one")["value"] == "one"
    assert cdp(chrome, "type", "#notes", " two")["value"] == "one two"
    assert cdp(chrome, "set-value", "#rich", "cdp rich")["verified"] is True
    assert cdp(chrome, "set-value", "#pick", "b")["value"] == "b"
    assert cdp(chrome, "set-value", "#name", "Ann")["verified"] is True
    clicked = cdp(chrome, "click", "#go")
    assert set(clicked) == {"x", "y"}
    assert cdp(chrome, "read", "#out")["text"] == "hello Ann"
    assert cdp(chrome, "read", "#name") == {"selector": "#name", "text": "Ann", "via": "value"}
    assert cdp(chrome, "wait", "#out")["found"] is True
    assert cdp(chrome, "wait", "#nothere", "--gone")["gone"] is True
    assert cdp(chrome, "wait", "#nothere", "--wait", "1", ok=False)["error"] == "Timeout"
    assert cdp(chrome, "click", "#nothere", ok=False)["error"] == "NoTarget"
    shot = cdp(chrome, "shot", str(tmp_path / "page.png"))
    assert Path(shot["path"]).stat().st_size > 0
    nothing = chrome.fail("cdp", "read", "#out", "--port", "1")
    assert nothing["error"] == "NoTarget" and "nothing is listening" in nothing["message"]
    lost = chrome.fail("cdp", "read", "#out", "--port", str(chrome.port), "--url", "nomatch")
    assert lost["error"] == "NoTarget"
    many = chrome.fail("cdp", "read", "#out", "--port", str(chrome.port), "--type", "browser_ui")
    assert many["error"] in {"Ambiguous", "NoTarget"}


def test_chrome_patterns_select_and_count(chrome):
    done = chrome("ui", "click", "app//Button[name='Go']", "--mode", "invoke")
    assert done["via"] == "Invoke"
    picked = chrome("ui", "select", "app//ComboBox[name='Pick']", "--item", "A")
    assert picked["selected"] == "A"
    assert cdp(chrome, "eval", "document.getElementById('pick').value")["value"] == "a"
    counted = chrome("ui", "count", "app//ComboBox[name='Pick']")
    assert counted["count"] == 2 and counted["names"] == ["A", "B"]


@activates
def test_chrome_click_type_keys_and_at(chrome):
    cdp(chrome, "set-value", "#name", "AtClick")
    cdp(chrome, "eval", "out.textContent = 'reset'")
    chrome("ui", "activate")
    chrome.focus()
    chrome("ui", "type", "typed via kbd", "--into", "app//Edit[name='Name']", "--clear")
    assert chrome("ui", "read", "app//Edit[name='Name']")["text"] == "typed via kbd"
    chrome.focus()
    chrome("ui", "keys", "{Ctrl}a{Delete}", "--into", "app//Edit[name='Name']")
    assert chrome("ui", "read", "app//Edit[name='Name']")["text"] == ""
    cdp(chrome, "set-value", "#name", "AtClick")
    rect = chrome("ui", "find", "app//Button[name='Go']")["element"]["rect"]
    chrome.focus()
    chrome("ui", "click", "--at", f"{rect[0] + rect[2] // 2},{rect[1] + rect[3] // 2}")
    wait_until(
        lambda: cdp(chrome, "read", "#out")["text"] == "hello AtClick", timeout=5, what="the click"
    )


@activates
def test_chrome_ui_hover_moves_the_real_pointer_and_stays(chrome):
    hovering = "document.getElementById('go').matches(':hover')"
    chrome.focus()
    done = chrome("ui", "hover", "app//Button[name='Go']", "--dwell", "0.3")
    assert done["hovered"] == "app//Button[name='Go']" and done["held"] is True
    assert x11.pointer() == (done["x"], done["y"])
    wait_until(lambda: cdp(chrome, "eval", hovering)["value"], timeout=5, what="the hover")
    rect = chrome("ui", "find", "app//Edit[name='Name']")["element"]["rect"]
    chrome.focus()
    at = chrome("ui", "hover", "--at", f"{rect[0] + 5},{rect[1] + 5}")
    assert at["hovered"] is None and at["fallback"] == "coordinates"
    wait_until(lambda: not cdp(chrome, "eval", hovering)["value"], timeout=5, what="the move")


# ------------------------------------------------------------ VS Code (Electron)


@pytest.fixture(scope="module")
def electron(tmp_path_factory):
    """A scratch VS Code (the snap's binary under its own image name)."""
    if not (SNAP_CODE / "code").exists():
        pytest.skip("needs the VS Code snap")
    root = tmp_path_factory.mktemp("codecmd")
    code = root / "usr" / "share" / "code"
    code.mkdir(parents=True)
    for item in SNAP_CODE.iterdir():
        if item.name != "code":
            (code / item.name).symlink_to(item)
    (root / "usr" / "lib").symlink_to(SNAP_CODE.parent.parent / "lib")
    shutil.copy2(SNAP_CODE / "code", code / "code-pilotcmd")
    user = root / "data" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(
        json.dumps(
            {
                "security.workspace.trust.enabled": False,
                "workbench.startupEditor": "none",
                "workbench.welcomePage.experimentalOnboarding": False,
                "telemetry.telemetryLevel": "off",
                "update.mode": "none",
                "chat.disableAIFeatures": True,
            }
        )
    )
    (root / "doc.txt").write_text("hello scratch\n")
    port = free_port()
    name = f"pilotcode{os.getpid()}"
    args = [
        "--force-renderer-accessibility",
        f"--remote-debugging-port={port}",
        "--no-sandbox",
        "--skip-welcome",
        "--disable-workspace-trust",
        "--disable-gpu",
        f"--user-data-dir={root / 'data'}",
        f"--extensions-dir={root / 'extensions'}",
        "--new-window",
        str(root / "doc.txt"),
    ]
    folder = root / "config" / "desktoppilot" / "profiles"
    folder.mkdir(parents=True)
    (folder / f"{name}.toml").write_text(
        f'extends = "chrome"\nlaunch = "fresh"\ntitle_strip = [" - Visual Studio Code"]\n'
        f"depth = 25\nargs = {json.dumps(args)}\n"
        f"[linux]\nexe = [{json.dumps(str(code / 'code-pilotcmd'))}]\n"
        'window_class = ["Code", "code-pilotcmd", "code_code"]\n'
    )
    pilot = Pilot(name, root / "config")
    pilot.port = port
    info = launched(pilot)
    pilot.pid = info["pids"][0]
    try:
        wait_until(
            lambda: pilot(
                "ui", "find", "app//MenuItem[name='File']", "--timeout", "2", ok=None
            ).get("element"),
            timeout=90,
            what="the workbench",
        )
        yield pilot
    finally:
        stop(pilot)


def test_electron_launch_drops_electron_run_as_node(electron):
    env = environ_of(electron.pid)
    assert "ELECTRON_RUN_AS_NODE" not in env and "NO_AT_BRIDGE" not in env
    assert not any(k.startswith("SNAP") for k in env)


def test_electron_tree_find_and_cdp(electron):
    menu = electron("ui", "find", "app//MenuItem[name='File']")["element"]
    assert menu["patterns"] == ["Invoke"]
    assert electron("ui", "find", "app//Document")["element"]["type"] == "Document"
    assert electron("cdp", "eval", "document.title")["value"].endswith("Visual Studio Code")
    assert electron("cdp", "wait", ".monaco-workbench")["found"] is True
    assert electron("ui", "tree", "--flat")["controls"][0]["type"] == "Window"


@activates
def test_electron_palette_read_cdp_set_value_and_ui_set_value_fails(electron):
    electron.focus()
    electron("ui", "keys", "{Ctrl}{Shift}p")
    try:
        wait_until(
            lambda: electron("ui", "find", "app//Edit", "--timeout", "1", ok=None).get("element"),
            timeout=15,
            what="the palette",
        )
        electron.focus()
        electron("ui", "type", "Toggle Word Wrap")
        assert electron("ui", "read", "app//Edit")["text"] == ">Toggle Word Wrap"
        done = electron("cdp", "set-value", ".quick-input-box input", ">cdp set")
        assert done["verified"] is True
        assert electron("ui", "read", "app//Edit")["text"] == ">cdp set"
        failed = electron.fail("ui", "set-value", "app//Edit", "x")
        assert "cdp set-value" in failed["message"]
        items = electron("ui", "find", "app//List//ListItem", "--all")
        assert items["count"] >= 1
        counted = electron("ui", "count", "app//List[id='quickInput_list']")
        assert counted["count"] == items["count"], (
            counted,
            items["count"],
        )  # rows, not the wrapper
    finally:
        electron.focus()
        electron("ui", "keys", "{Esc}")
    wait_until(
        lambda: electron(
            "ui", "wait", "app//List[id='quickInput_list']", "--gone", "--timeout", "2", ok=None
        ).get("gone"),
        timeout=10,
        what="the palette to close",
    )


@activates
def test_electron_read_copy_and_follow_recording(electron, tmp_path):
    electron.focus()
    electron("ui", "keys", "{Ctrl}{Shift}p")
    try:
        wait_until(
            lambda: electron("ui", "find", "app//Edit", "--timeout", "1", ok=None).get("element"),
            timeout=15,
            what="the palette",
        )
        electron.focus()
        electron("ui", "type", "copy me")
        electron.focus()
        copied = electron("ui", "read", "focus", "--copy")
        assert copied["via"] == "clipboard" and copied["text"].endswith("copy me")
        if shutil.which("ffmpeg"):
            movie = tmp_path / "follow.mp4"
            started = electron("record", "start", str(movie), "--follow")
            assert started["target"]["follow"] is True
            wait_until(
                lambda: electron("record", "status")["elapsed_s"] >= 1, timeout=10, what="1 s"
            )
            done = electron("record", "stop")
            assert done["video"]["codec"] == "h264"
    finally:
        electron.focus()
        electron("ui", "keys", "{Esc}")
