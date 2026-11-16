"""The AT-SPI backend on a real desktop: a GTK3 and a GTK4 window this test
builds, and a Chrome on a scratch profile, each started (and closed) here.

Only pattern actions (Invoke, Toggle, SetValue, Select, Expand): no pointer,
no keys, and no window is activated unless PILOT_TEST_ACTIVATE is set, so
another agent's input test on the same desktop is left alone.
"""

from __future__ import annotations

import itertools
import os
import shutil
import signal
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the AT-SPI backend"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs an X display"),
]

if sys.platform.startswith("linux"):
    from desktoppilot.core import selectors, ui
    from desktoppilot.core.errors import PilotError
    from desktoppilot.core.util import wait_until
    from desktoppilot.linux import auto, procs, x11

SYSTEM_PYTHON = "/usr/bin/python3"
_starts = itertools.count(1)

GTK3_APP = """
import sys, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
win = Gtk.Window(title=sys.argv[1])
win.set_default_size(500, 600)
win.connect("destroy", Gtk.main_quit)
box = Gtk.VBox()
win.add(box)
status = Gtk.Label(label="ready")
bar, filem, menu = Gtk.MenuBar(), Gtk.MenuItem(label="File"), Gtk.Menu()
filem.set_submenu(menu)
hello = Gtk.MenuItem(label="Say Hello")
hello.connect("activate", lambda *_: status.set_text("menu hello"))
menu.append(hello)
bar.append(filem)
box.pack_start(bar, False, False, 0)
count = [0]
def clicked(*_):
    count[0] += 1
    status.set_text(f"clicked {count[0]}")
button = Gtk.Button(label="Hello")
button.connect("clicked", clicked)
box.pack_start(button, False, False, 0)
off = Gtk.Button(label="Disabled")
off.set_sensitive(False)
box.pack_start(off, False, False, 0)
box.pack_start(status, False, False, 0)
entry = Gtk.Entry()
entry.set_text("abc")
box.pack_start(entry, False, False, 0)
fixed = Gtk.Entry()
fixed.set_text("fixed")
fixed.set_editable(False)
box.pack_start(fixed, False, False, 0)
box.pack_start(Gtk.CheckButton(label="Check me"), False, False, 0)
combo = Gtk.ComboBoxText()
for text in ("One", "Two", "Three"):
    combo.append_text(text)
combo.set_active(0)
box.pack_start(combo, False, False, 0)
store = Gtk.TreeStore(str)
for name in ("Alpha", "Beta"):
    row = store.append(None, [name])
    for leaf in ("one", "two"):
        store.append(row, [f"{name} {leaf}"])
tree = Gtk.TreeView(model=store)
tree.append_column(Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=0))
box.pack_start(tree, False, False, 0)
book = Gtk.Notebook()
book.append_page(Gtk.Label(label="page A"), Gtk.Label(label="Tab A"))
book.append_page(Gtk.Label(label="page B"), Gtk.Label(label="Tab B"))
box.pack_start(book, False, False, 0)
scroller = Gtk.ScrolledWindow()
scroller.set_size_request(-1, 120)
view = Gtk.TextView()
view.get_buffer().set_text("\\n".join(f"line {i}" for i in range(200)))
scroller.add(view)
box.pack_start(scroller, True, True, 0)
win.show_all()
Gtk.main()
"""

GTK4_APP = """
import sys, gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk
def build(app):
    win = Gtk.ApplicationWindow(application=app, title=sys.argv[1])
    win.set_default_size(420, 400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    win.set_child(box)
    status = Gtk.Label(label="ready")
    button = Gtk.Button(label="Hello")
    button.connect("clicked", lambda *_: status.set_label("clicked"))
    box.append(button)
    box.append(status)
    entry = Gtk.Entry()
    entry.set_text("abc")
    box.append(entry)
    box.append(Gtk.CheckButton(label="Check me"))
    box.append(Gtk.Switch())
    win.present()
app = Gtk.Application(application_id="org.desktoppilot.AtspiTest4", flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", build)
app.run([])
"""

PAGE = """<!doctype html><title>{title}</title>
<h1 id=hd class='big title'>Hello</h1>
<p>Some <a href='#x' id=l1>Link</a> text.</p>
<button id=b1 class=btn onclick="this.textContent='clicked'">Press</button>
<button id=b2 disabled>Off</button>
<input id=e1 type=text value=abc>
<input id=c1 type=checkbox><label for=c1>Check me</label>
<select id=s1><option>One</option><option>Two</option><option>Three</option></select>
"""


def _window_titled(title: str) -> int:
    return next((w for w in x11.client_windows() if x11.title(w).startswith(title)), 0)


def _start(args: list[str], title: str, tmp_path):
    """Start an app in a session of its own; (process, its window's control)."""
    proc = subprocess.Popen(
        args,
        env=procs.app_env(dict(os.environ)),
        stdout=subprocess.DEVNULL,
        stderr=(tmp_path / "app.log").open("w"),
        start_new_session=True,
    )
    try:
        handle = wait_until(lambda: _window_titled(title), timeout=30, what=title)
        control = wait_until(lambda: auto.ControlFromHandle(handle), timeout=15, what="its object")
    except Exception:
        _stop(proc)
        raise
    return proc, control


def _stop(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        os.killpg(proc.pid, signal.SIGKILL)


@pytest.fixture
def started(tmp_path):
    running = []

    def start(args, title):
        proc, control = _start(args, title, tmp_path)
        running.append(proc)
        return control

    yield start
    for proc in running:
        _stop(proc)


def gtk(started, version: int, tmp_path):
    pytest.importorskip("gi")
    if not os.path.exists(SYSTEM_PYTHON):
        pytest.skip("needs the system python with GTK")
    title = f"AtspiTest{version}-{os.getpid()}-{next(_starts)}"
    script = tmp_path / f"gtk{version}.py"
    script.write_text(GTK3_APP if version == 3 else GTK4_APP)
    return started([SYSTEM_PYTHON, str(script), title], title)


def find(root, path: str):
    """The first match of each step of `path` (a selector without its root)."""
    node = root
    for step in selectors.parse("desktop" + path).steps:
        hits = ui.search(node, step, limit=step.index)
        assert len(hits) >= step.index, path
        node = hits[step.index - 1]
    return node


def status(window) -> str:
    names = [c.Name for c in ui.search(window, selectors.parse("desktop//Text").steps[0])]
    return next(n for n in names if n.startswith(("ready", "clicked", "menu")))


def inside(inner, outer) -> bool:
    return (
        outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )


# ------------------------------------------------------------ GTK3


def test_gtk3_window_maps_both_ways(started, tmp_path):
    window = gtk(started, 3, tmp_path)
    handle = window.NativeWindowHandle
    assert handle and x11.title(handle) == window.Name
    assert window.BoundingRectangle == auto.Rect(*x11.frame_rect(handle))
    assert auto.ControlFromHandle(handle).GetRuntimeId() == window.GetRuntimeId()
    assert window.ClassName == x11.wm_class(handle)[1]
    assert any(
        w.GetRuntimeId() == window.GetRuntimeId() for w in ui._children(auto.GetRootControl())
    )


def test_gtk3_tree_reads_like_uia(started, tmp_path):
    window = gtk(started, 3, tmp_path)
    hello = find(window, "//Button[name='Hello']")
    assert ui.describe(hello)["type"] == "Button"
    assert inside(hello.BoundingRectangle, window.BoundingRectangle)
    assert find(window, "//MenuBar/MenuItem[name='File']").ControlTypeName == "MenuItemControl"
    assert hello.GetRuntimeId() == find(window, "//Button[name='Hello']").GetRuntimeId()
    assert hello.GetRuntimeId() != find(window, "//Button[name='Disabled']").GetRuntimeId()
    assert hello.GetTopLevelControl().GetRuntimeId() == window.GetRuntimeId()


def test_gtk3_invoke_and_disabled(started, tmp_path):
    window = gtk(started, 3, tmp_path)
    assert ui.click(find(window, "//Button[name='Hello']"), activate=False) == "Invoke"
    wait_until(lambda: status(window) == "clicked 1", timeout=5, what="the click")
    disabled = ui.search(window, selectors.parse("desktop//Button[name='Disabled']").steps[0])[0]
    assert not disabled.IsEnabled
    with pytest.raises(PilotError, match="disabled"):
        disabled.GetInvokePattern().Invoke()


def test_gtk3_set_value_toggle_and_read_only(started, tmp_path):
    window = gtk(started, 3, tmp_path)
    result = ui.set_value(find(window, "//Edit"), "typed by pilot", activate=False)
    assert result["verified"]
    with pytest.raises(Exception, match="read-only"):
        ui.set_value(find(window, "//Edit#2"), "x", activate=False)
    box = find(window, "//CheckBox[name='Check me']")
    assert ui.click(box, activate=False) == "Toggle"
    assert box.GetTogglePattern().ToggleState == auto.ToggleState.On


@pytest.mark.skipif(
    not os.environ.get("PILOT_TEST_ACTIVATE"),
    reason="activates its window (set PILOT_TEST_ACTIVATE=1 when no other agent drives the desktop)",
)
def test_gtk3_popups_open_once_the_window_is_active(started, tmp_path):
    """GTK3's combo list and menus (like GTK4's popovers and Chrome's
    <select>) open only in the active window, as select_item makes it."""
    window = gtk(started, 3, tmp_path)
    assert window.SetActive()
    combo = find(window, "//ComboBox")
    assert ui.select_item(combo, "Two")["selected"] == "Two"
    wait_until(lambda: ui.value_of(combo) == "Two", timeout=5, what="the combo's value")
    assert ui.select_item(find(window, "//MenuItem[name='File']"), "Say Hello")["selected"]
    wait_until(lambda: status(window) == "menu hello", timeout=5, what="the menu item")


def test_gtk3_tree_tabs_and_scroll(started, tmp_path):
    window = gtk(started, 3, tmp_path)
    counted = ui.count_items(find(window, "//Tree"), names=10)
    assert counted["count"] == 6 and counted["complete"]
    tab = find(window, "//TabItem[name='Tab B']")
    assert ui.click(tab, activate=False) == "SelectionItem"
    assert tab.GetSelectionItemPattern().IsSelected
    scroller = next(
        c
        for c in ui.search(window, selectors.parse("desktop//Pane").steps[0])
        if c.ClassName == "scroll pane"
    )
    assert ui.scroll(scroller, vertical=50)["vertical_percent"] == pytest.approx(50, abs=1)


# ------------------------------------------------------------ GTK4


def test_gtk4_rects_sit_in_the_window_and_patterns_act(started, tmp_path):
    window = gtk(started, 4, tmp_path)
    client = auto.Rect(*x11.client_rect(window.NativeWindowHandle))
    hello = find(window, "//Button[name='Hello']")
    assert inside(hello.BoundingRectangle, client)
    assert ui.click(hello, activate=False) == "Invoke"
    wait_until(lambda: status(window) == "clicked", timeout=5, what="the click")
    assert ui.set_value(find(window, "//Edit"), "gtk4 text", activate=False)["verified"]
    switch = next(
        c
        for c in ui.search(window, selectors.parse("desktop//Button").steps[0])
        if c.ClassName == "switch"
    )
    before = switch.GetTogglePattern().ToggleState
    switch.GetTogglePattern().Toggle()
    assert switch.GetTogglePattern().ToggleState != before
    with pytest.raises(Exception, match="no usable UIA pattern"):
        ui.click(find(window, "//CheckBox"), activate=False, mode="invoke")


# ------------------------------------------------------------ Chrome


@pytest.fixture
def chrome(started, tmp_path):
    exe = shutil.which("google-chrome") or shutil.which("chromium")
    if exe is None:
        pytest.skip("needs Chrome or Chromium")
    title = f"AtspiChrome{os.getpid()}-{next(_starts)}"
    page = tmp_path / "page.html"
    page.write_text(PAGE.format(title=title))
    args = [
        exe,
        f"--user-data-dir={tmp_path / 'profile'}",
        "--force-renderer-accessibility",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        page.as_uri(),
    ]
    window = started(args, title)
    wait_until(
        lambda: ui.search(window, selectors.parse("desktop//Button[id='b1']").steps[0]),
        timeout=15,
        what="the page",
    )
    return window


def test_chrome_finds_dom_ids_and_classes(chrome):
    button = find(chrome, "//Button[id='b1']")
    heading = find(chrome, "//Text[class='big title']")
    assert (button.Name, heading.Name) == ("Press", "Hello")
    assert inside(button.BoundingRectangle, chrome.BoundingRectangle)
    assert ui.text_of(find(chrome, "//Document")).startswith("Hello\nSome Link text.")


def test_chrome_patterns(chrome):
    button = find(chrome, "//Button[id='b1']")
    assert ui.click(button, activate=False) == "Invoke"
    wait_until(lambda: button.Name == "clicked", timeout=5, what="the click")
    with pytest.raises(Exception, match="disabled"):
        ui.click(find(chrome, "//Button[id='b2']"), activate=False, mode="invoke")
    box = find(chrome, "//CheckBox[id='c1']")
    assert ui.click(box, activate=False) == "Toggle"
    assert box.GetTogglePattern().ToggleState == auto.ToggleState.On
    combo = find(chrome, "//ComboBox[id='s1']")
    assert ui.value_of(combo) == "One"
    three = ui.search(
        combo, selectors.parse("desktop//MenuItem[name='Three']").steps[0], visible_only=False
    )[0]
    assert ui.click(three, activate=False) == "Invoke"
    wait_until(lambda: ui.value_of(combo) == "Three", timeout=5, what="the choice")
    with pytest.raises(Exception, match="EditableText"):
        ui.set_value(find(chrome, "//Edit[id='e1']"), "x", activate=False)


@pytest.mark.skipif(
    not os.environ.get("PILOT_TEST_ACTIVATE"),
    reason="activates its window (set PILOT_TEST_ACTIVATE=1 when no other agent drives the desktop)",
)
def test_chrome_select_item_opens_the_list(chrome):
    assert chrome.SetActive()
    combo = find(chrome, "//ComboBox[id='s1']")
    assert ui.select_item(combo, "Two")["selected"] == "Two"
    wait_until(lambda: ui.value_of(combo) == "Two", timeout=5, what="the choice")
