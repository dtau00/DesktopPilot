"""`pilot chrome cdp` on a real Chrome: a scratch one on its own user data
folder and debugging port, started and closed here. CDP input is made inside
the renderer, so no key or pointer reaches the desktop; the value it writes
is then read back through AT-SPI, which cannot write it."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Chrome on Linux"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs an X display"),
]

if sys.platform.startswith("linux"):
    from desktoppilot.core import cdp, selectors, ui
    from desktoppilot.core.util import wait_until
    from desktoppilot.linux import auto, procs, x11

PAGE = """<!doctype html><title>{title}</title>
<input id=name aria-label=Name>
<textarea id=notes></textarea>
<div id=rich contenteditable=true></div>
<select id=pick><option value=a>A</option><option value=b>B</option></select>
<button id=go onclick="out.textContent = 'hello ' + document.getElementById('name').value">Go</button>
<p id=out>idle</p>
<div id=hov style="width: 80px; height: 30px" onmouseover="tip.textContent = 'tip shown'">hover</div>
<p id=tip>no tip</p>
<input id=attach type=file multiple hidden onchange="got.textContent = 'change ' + this.files.length">
<p id=got>no files</p>
<input id=keyed onkeydown="keylog.textContent += e2k(event)">
<p id=keylog></p>
<div id=box style="width: 100px; height: 60px; overflow: auto"
  onwheel="wheeled.textContent = 'wheeled'"><div style="height: 600px"></div></div>
<p id=wheeled>no wheel</p>
<div id=native draggable=true style="width: 40px; height: 20px"
  ondragstart="event.dataTransfer.setData('text/plain', 'payload')">drag</div>
<div id=drop style="width: 60px; height: 30px" ondragover="event.preventDefault()"
  ondrop="event.preventDefault(); dropped.textContent = event.dataTransfer.getData('text/plain')"></div>
<p id=dropped>nothing</p>
<style>@media (prefers-color-scheme: dark) {{ body {{ background: rgb(0, 0, 0); }} }}</style>
<script>
const e2k = (e) => (e.ctrlKey ? "C-" : "") + e.key + " ";
console.error("pilot logged", 7);
setTimeout(() => {{ throw new Error("pilot thrown"); }}, 0);
</script>
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    """(port, title) of a scratch Chrome showing PAGE; closed afterwards."""
    exe = shutil.which("google-chrome") or shutil.which("chromium")
    if exe is None:
        pytest.skip("needs Chrome or Chromium")
    root = tmp_path_factory.mktemp("chrome")
    title = f"PilotCdp{os.getpid()}"
    page = root / "page.html"
    page.write_text(PAGE.format(title=title))
    port = free_port()
    proc = subprocess.Popen(
        [
            exe,
            f"--user-data-dir={root / 'profile'}",
            f"--remote-debugging-port={port}",
            "--force-renderer-accessibility",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            page.as_uri(),
        ],
        env=procs.app_env(dict(os.environ)),
        stdout=subprocess.DEVNULL,
        stderr=(root / "chrome.log").open("w"),
        start_new_session=True,
    )
    try:
        wait_until(
            lambda: cdp.matching(cdp.list_targets(port), title=title),
            timeout=30,
            what="the page target",
        )
        yield port, title
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(proc.pid, signal.SIGKILL)


def pilot(port: int, *args: str) -> dict:
    ran = subprocess.run(
        [str(Path(sys.executable).parent / "pilot"), "chrome", "--no-shot", "cdp", *args,
         "--port", str(port), "--url", "page.html"],
        capture_output=True,
        text=True,
        timeout=60,
    )  # fmt: skip
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return json.loads(ran.stdout)


def test_targets_and_eval(chrome):
    port, title = chrome
    listed = subprocess.run(
        [str(Path(sys.executable).parent / "pilot"), "chrome", "cdp", "targets", "--port", str(port)],
        capture_output=True,
        text=True,
        check=True,
    )  # fmt: skip
    assert any(t["title"] == title for t in json.loads(listed.stdout)["targets"])
    assert pilot(port, "eval", "document.title")["value"] == title


def test_set_value_type_click_and_read(chrome):
    port, _ = chrome
    assert pilot(port, "set-value", "#name", "Zoë 日本")["verified"] is True
    assert pilot(port, "type", "#notes", "one")["value"] == "one"
    assert pilot(port, "type", "#notes", " two")["value"] == "one two"
    assert pilot(port, "set-value", "#rich", "rich text")["verified"] is True
    assert pilot(port, "set-value", "#pick", "b")["value"] == "b"
    pilot(port, "click", "#go")
    assert pilot(port, "wait", "#out")["found"] is True
    assert pilot(port, "read", "#out")["text"] == "hello Zoë 日本"


def test_atspi_reads_what_cdp_wrote(chrome):
    port, title = chrome
    pilot(port, "set-value", "#name", "seen by AT-SPI")
    handle = wait_until(
        lambda: next((w for w in x11.client_windows() if x11.title(w).startswith(title)), 0),
        timeout=15,
        what="the window",
    )
    window = auto.ControlFromHandle(handle)
    step = selectors.parse("desktop//Edit[id='name']").steps[0]
    edit = wait_until(lambda: ui.search(window, step), timeout=15, what="the input")[0]
    assert ui.value_of(edit) == "seen by AT-SPI"


def test_shot(chrome, tmp_path):
    port, _ = chrome
    shot = Path(pilot(port, "shot", str(tmp_path / "page.png"))["path"])
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_hover_opens_the_tip_and_the_page_keeps_it(chrome):
    port, _ = chrome
    pilot(port, "eval", "tip.textContent = 'no tip'")
    assert set(pilot(port, "hover", "#hov")) == {"x", "y"}
    assert pilot(port, "read", "#tip")["text"] == "tip shown"
    assert pilot(port, "eval", "hov.matches(':hover')")["value"] is True


def test_upload_fills_a_hidden_file_input_and_fires_change(chrome, tmp_path):
    port, _ = chrome
    first, second = tmp_path / "one.png", tmp_path / "two.txt"
    first.write_bytes(b"png")
    second.write_text("two")
    found = pilot(port, "upload", "#attach", str(first), str(second))
    assert found["verified"] is True and found["count"] == 2
    assert found["names"] == ["one.png", "two.txt"]
    assert pilot(port, "read", "#got")["text"] == "change 2"


def test_console_replays_what_the_page_logged_and_reload_logs_anew(chrome):
    port, _ = chrome
    found = pilot(port, "console", "--level", "error")
    texts = [e["text"] for e in found["entries"]]
    assert "pilot logged 7" in texts and any("pilot thrown" in t for t in texts)
    assert found["errors"] >= 2 and all(e["level"] == "error" for e in found["entries"])
    pilot(port, "eval", "console.warn('after the load')")
    assert "after the load" in [e["text"] for e in pilot(port, "console")["entries"]]
    reloaded = pilot(port, "console", "--reload", "--for", "0.5")
    texts = [e["text"] for e in reloaded["entries"]]
    assert "after the load" not in texts and texts.count("pilot logged 7") == 1
    assert any("pilot thrown" in t for t in texts)


def test_color_scheme_lasts_one_command(chrome, tmp_path):
    port, _ = chrome
    dark = "matchMedia('(prefers-color-scheme: dark)').matches"
    assert pilot(port, "eval", dark, "--color-scheme", "dark")["value"] is True
    assert pilot(port, "eval", dark)["value"] is False
    assert pilot(port, "eval", dark, "--color-scheme", "light")["value"] is False
    shot = Path(pilot(port, "shot", str(tmp_path / "dark.png"), "--color-scheme", "dark")["path"])
    from PIL import Image

    assert Image.open(shot).convert("RGB").getpixel((5, 5)) == (0, 0, 0)
    assert pilot(port, "eval", dark)["value"] is False


def run_pilot(*args: str, env: dict | None = None) -> dict:
    ran = subprocess.run(
        [str(Path(sys.executable).parent / "pilot"), "chrome", "--no-shot", *args],
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, **(env or {})},
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return json.loads(ran.stdout)


def test_scratch_start_bind_resize_and_stop(monkeypatch):
    """`scratch start` beside whatever Chrome the user has open; bound, the
    app commands see only the scratch one; `stop` takes only its tree."""
    if not Path("/opt/google/chrome/chrome").exists():
        pytest.skip("needs Google Chrome")
    monkeypatch.delenv("PILOT_CHROME_PID", raising=False)
    others = {w["hwnd"]: w["rect"] for w in run_pilot("app", "status")["windows"]}
    title = f"PilotScratch{os.getpid()}"
    started = run_pilot("scratch", "start", "--url", f"data:text/html,<title>{title}</title>")
    pid = started["pid"]
    try:
        assert started["bind"] == f"PILOT_CHROME_PID={pid}"
        targets = run_pilot("cdp", "targets", "--port", str(started["port"]))["targets"]
        assert any(t["title"] == title for t in targets)

        bound = {"PILOT_CHROME_PID": str(pid)}
        status = run_pilot("app", "status", env=bound)
        assert status["pids"] == [pid] and status["bound_pid"] == pid
        assert {w["pid"] for w in status["windows"]} == {pid}

        resized = run_pilot("app", "resize", "640", "480", "--pid", str(pid))
        assert resized["hwnd"] in {w["hwnd"] for w in status["windows"]}
        assert resized["dips"] == [640, 480]
        after = {w["hwnd"]: w["rect"] for w in run_pilot("app", "status")["windows"]}
        moved = [h for h, rect in others.items() if after.get(h, rect) != rect]
        assert moved == [], "resized a window of the user's Chrome"
    finally:
        stopped = run_pilot("scratch", "stop", str(pid))
    assert pid in stopped["killed"] and stopped["removed"] is True
    assert not Path(started["user_data_dir"]).exists()
    assert not procs.alive(pid)
    assert run_pilot("app", "status", env={"PILOT_CHROME_PID": str(pid)})["pids"] == []


def test_keys_press_named_keys_and_editing_shortcuts(chrome):
    port, _ = chrome
    pilot(port, "eval", "keyed.value = ''; keylog.textContent = ''")
    found = pilot(port, "keys", "abc{Left}{Backspace}X{Enter}", "--into", "#keyed")
    assert found["value"] == "aXc"
    assert pilot(port, "read", "#keylog")["text"].split() == [
        "a",
        "b",
        "c",
        "ArrowLeft",
        "Backspace",
        "X",
        "Enter",
    ]
    assert pilot(port, "keys", "{Ctrl}a{Delete}", "--into", "#keyed")["value"] == ""


def test_scroll_wheels_a_box_and_reports_its_position(chrome):
    port, _ = chrome
    found = pilot(port, "scroll", "#box", "--by", "0,100")
    assert found["scroller"] == "element" and found["top"] == 100
    assert pilot(port, "read", "#wheeled")["text"] == "wheeled"
    bottom = pilot(port, "scroll", "#box", "--to", "bottom")
    assert bottom["top"] == bottom["max_top"]
    assert pilot(port, "scroll", "#box", "--to", "top")["top"] == 0


def test_drag_drops_a_native_draggable(chrome):
    port, _ = chrome
    assert pilot(port, "drag", "#native", "#drop")["mode"] == "html5"
    assert pilot(port, "read", "#dropped")["text"] == "payload"


def test_navigate_loads_and_reports_where_it_landed(chrome):
    port, title = chrome
    here = pilot(port, "eval", "location.href")["value"]
    assert pilot(port, "navigate", here + "#again")["title"] == title
    assert pilot(port, "navigate", here)["url"] == here
