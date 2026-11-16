"""The fallback rule: a failure leaves a screenshot, a coordinate click is
marked, and every run says whether it needed either.

No real app: the screenshot, the window and the pointer are all stubbed, so
what is under test is the bookkeeping and the JSON shape.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from desktoppilot.cli import app
from desktoppilot.core import apps, artifacts, fallback, paths, ui
from desktoppilot.core import state as state_mod
from desktoppilot.core.errors import PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

from .compat import PLAIN_APP

runner = CliRunner()

MISSING = "app//Button[name='Does Not Exist']"


@pytest.fixture(autouse=True)
def state_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fallback, "SHOT_DIR", tmp_path / "shots")
    return tmp_path


@pytest.fixture
def stub_window(monkeypatch):
    """A main window that screenshots to a real file."""
    shots: list[str] = []

    def screenshot(control, path):
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PNG")
        shots.append(str(target))
        return target

    monkeypatch.setattr(ui, "main_window", lambda ctx, timeout=15.0: object())
    monkeypatch.setattr(ui, "screenshot", screenshot)
    return shots


@pytest.fixture
def failing_find(monkeypatch):
    def explode(*args, **kwargs):
        raise PilotError("no element matched")

    monkeypatch.setattr(ui, "find", explode)


def invoke(*args):
    return runner.invoke(app, list(args))


def journal() -> list[dict]:
    return fallback.events(PLAIN_APP)


# ------------------------------------------------------- screenshot on failure


def test_a_failed_click_returns_a_screenshot_path_and_journals_it(stub_window, failing_find):
    result = invoke(PLAIN_APP, "ui", "click", MISSING)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "PilotError"
    assert payload["screenshot"] == stub_window[0]

    events = journal()
    assert [e["kind"] for e in events] == ["failure_screenshot"]
    assert events[0]["command"] == "ui click"
    assert events[0]["detail"] == stub_window[0]


def test_the_screenshot_lands_in_the_state_dir_and_not_the_cwd(stub_window, failing_find, tmp_path):
    invoke(PLAIN_APP, "ui", "find", MISSING)

    path = stub_window[0]
    assert str(tmp_path / "shots" / PLAIN_APP) in path
    assert path.endswith("-ui_find.png")


def test_no_shot_turns_the_rule_off(stub_window, failing_find):
    result = invoke(PLAIN_APP, "--no-shot", "ui", "click", MISSING)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "screenshot" not in payload
    assert stub_window == []
    assert journal() == []


def test_a_screenshot_that_fails_never_masks_the_original_error(monkeypatch, failing_find):
    monkeypatch.setattr(ui, "main_window", lambda ctx, timeout=15.0: object())
    monkeypatch.setattr(
        ui, "screenshot", lambda control, path: (_ for _ in ()).throw(PilotError("no display"))
    )

    result = invoke(PLAIN_APP, "ui", "click", MISSING)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["message"] == "no element matched"
    assert payload["screenshot"] is None
    assert payload["screenshot_error"] == "no display"
    assert journal() == []


# ------------------------------------------------------------ coordinate click


@pytest.fixture
def stub_pointer(monkeypatch):
    clicks: list[tuple] = []
    monkeypatch.setattr(ui, "activate_app", lambda ctx=None, control=None: None)
    monkeypatch.setattr(
        ui,
        "click_point",
        lambda x, y, button="left", double=False: clicks.append((x, y, button, double)) or "mouse",
    )
    return clicks


def test_a_coordinate_click_is_marked_and_journalled(stub_pointer):
    result = invoke(PLAIN_APP, "ui", "click", "--at", "10,20")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["clicked"] is None and payload["point"] == [10, 20]
    assert payload["fallback"] == "coordinates"
    assert stub_pointer == [(10, 20, "left", False)]

    events = journal()
    assert [e["kind"] for e in events] == ["coordinates"]
    assert events[0]["detail"] == "10,20"


def test_a_relative_point_is_offset_by_the_window(stub_pointer, monkeypatch):
    monkeypatch.setattr(ui, "window_origin", lambda ctx: (100, 200))

    result = invoke(PLAIN_APP, "ui", "click", "--at", "10,20", "--relative")

    assert result.exit_code == 0
    assert json.loads(result.output)["point"] == [110, 220]


def test_a_desktop_coordinate_click_needs_no_main_window(monkeypatch):
    clicks: list[tuple] = []
    monkeypatch.setattr(ui, "main_window", lambda *a, **k: pytest.fail("no main window"))
    monkeypatch.setattr(ui, "click_point", lambda x, y, **_: clicks.append((x, y)) or "mouse")

    result = invoke("desktop", "--no-shot", "ui", "click", "--at", "5,5")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["fallback"] == "coordinates"
    assert clicks == [(5, 5)]


def test_a_desktop_relative_click_is_refused_before_clicking(monkeypatch):
    clicks: list[tuple] = []
    monkeypatch.setattr(ui, "click_point", lambda x, y, **_: clicks.append((x, y)) or "mouse")

    result = invoke("desktop", "--no-shot", "ui", "click", "--at", "5,5", "--relative")

    assert result.exit_code == 1
    assert "--relative needs an app with a window" in json.loads(result.output)["message"]
    assert clicks == []


def test_at_and_a_selector_are_mutually_exclusive(stub_pointer):
    result = invoke(PLAIN_APP, "ui", "click", MISSING, "--at", "10,20")

    assert result.exit_code != 0
    assert stub_pointer == []


def test_a_click_with_neither_a_selector_nor_a_point_is_refused():
    assert invoke(PLAIN_APP, "ui", "click").exit_code != 0


# ------------------------------------------------------------------- shot


@pytest.fixture
def stub_screen(monkeypatch):
    """A window at (100, 50), 400x300, on a 1920x1080 screen; the capture is a
    grey image of whatever size was asked for."""
    from PIL import Image

    grabs: list[tuple] = []

    def capture_rect(left, top, width, height):
        grabs.append((left, top, width, height))
        return Image.new("RGB", (width, height), (128, 128, 128))

    monkeypatch.setattr(ui, "find", lambda selector, ctx, **kwargs: object())
    monkeypatch.setattr(ui, "capture_origin", lambda control: (100, 50, 400, 300))
    monkeypatch.setattr(ui, "virtual_screen_rect", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(ui, "capture_rect", capture_rect)
    monkeypatch.setattr(ui, "is_minimized", lambda control: False)
    monkeypatch.setattr(ui, "is_occluded", lambda control, rect: False)
    return grabs


def test_a_plain_shot_is_the_whole_window_at_scale_1(stub_screen, tmp_path):
    from PIL import Image

    target = tmp_path / "plain.png"
    result = invoke(PLAIN_APP, "shot", str(target))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "path": str(target.resolve()),
        "origin": [100, 50],
        "size": [400, 300],
        "scale": 1.0,
        "image_size": [400, 300],
        "resample": "none",
        "clipped": False,
        "occluded": False,
        "grid": None,
    }
    assert stub_screen == [(100, 50, 400, 300)]
    assert Image.open(target).size == (400, 300)


def test_a_region_shot_is_cropped_zoomed_and_journalled(stub_screen, tmp_path):
    from PIL import Image

    target = tmp_path / "z.png"
    result = invoke(PLAIN_APP, "shot", str(target), "--region", "10,20,100,50", "--grid", "20")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["origin"] == [110, 70] and payload["size"] == [100, 50]
    assert payload["scale"] == 15.0 and payload["resample"] == "nearest"
    assert payload["image_size"] == [1500, 750]
    assert payload["grid"] == {"step": 20, "label_every": 1}
    assert Image.open(target).size == (1500, 750)

    events = journal()
    assert [e["kind"] for e in events] == ["shot"]
    assert events[0]["detail"] == f"{target.resolve()} region=110,70,100,50 scale=15"


def test_a_region_past_the_window_is_clipped(stub_screen, tmp_path):
    result = invoke(PLAIN_APP, "shot", str(tmp_path / "c.png"), "--region", "350,250,100,100")

    payload = json.loads(result.output)
    assert payload["clipped"] is True and payload["size"] == [50, 50]


def test_a_bad_region_is_an_error_json(stub_screen, tmp_path):
    result = invoke(PLAIN_APP, "--no-shot", "shot", str(tmp_path / "x.png"), "--region", "1,2,3")

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "PilotError" and "--region" in payload["message"]
    assert stub_screen == [] and journal() == []


def test_a_minimized_target_is_refused(stub_screen, monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "is_minimized", lambda control: True)

    result = invoke(PLAIN_APP, "--no-shot", "shot", str(tmp_path / "m.png"))

    assert result.exit_code == 1
    assert "minimized" in json.loads(result.output)["message"]
    assert stub_screen == []


def test_an_occluded_region_says_so(stub_screen, monkeypatch, tmp_path):
    seen: list[tuple] = []
    monkeypatch.setattr(ui, "is_occluded", lambda control, rect: seen.append(rect) or True)

    result = invoke(PLAIN_APP, "shot", str(tmp_path / "o.png"), "--region", "0,0,40,30")

    assert json.loads(result.output)["occluded"] is True
    assert seen == [(100, 50, 40, 30)]


# ------------------------------------------------------------------ reporting


def test_capture_reports_the_fallbacks_of_this_run(monkeypatch, stub_window, tmp_path):
    ctx = AppContext(name=PLAIN_APP, profile=None, registry=Registry())
    fallback.record(ctx, "failure_screenshot", "ui click", "shot.png")
    fallback.record(ctx, "coordinates", "ui click", "10,20")

    monkeypatch.setattr(state_mod, "snapshot", lambda ctx, controls=0: {"app": PLAIN_APP})
    monkeypatch.setattr(ui, "tree", lambda root, **kwargs: {})
    monkeypatch.setattr(ui, "flatten", lambda root, **kwargs: [])

    result = artifacts.capture("step5", ctx, directory=tmp_path / "artifacts")

    assert result["fallback_used"] is True
    assert [e["kind"] for e in result["fallbacks"]] == ["failure_screenshot", "coordinates"]
    written = json.loads((tmp_path / "artifacts" / "step5" / "manifest.json").read_text("utf-8"))
    assert written["fallback_used"] is True


def test_state_says_whether_a_fallback_was_used(monkeypatch):
    ctx = AppContext(name="desktop", profile=None, registry=Registry())
    monkeypatch.setattr(state_mod, "_foreground", lambda pids: {})

    assert state_mod.snapshot(ctx)["fallback_used"] is False
    fallback.record(ctx, "shot", "shot", "out.png")
    assert state_mod.snapshot(ctx)["fallback_used"] is True


# ------------------------------------------------------------------- lifecycle


class FakeProcess:
    pid = 4242


def test_launch_truncates_the_journal(tmp_path, monkeypatch):
    exe = tmp_path / "fake.exe"
    exe.write_bytes(b"")
    profile = Profile(name=PLAIN_APP, exe=[str(exe)], ready_timeout=0.2)
    ctx = AppContext(name=PLAIN_APP, profile=profile, registry=Registry())
    fallback.record(ctx, "coordinates", "ui click", "1,2")
    assert journal()

    monkeypatch.setattr(apps, "pids", lambda profile: [])
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 99)
    monkeypatch.setattr(apps.win32process, "GetWindowThreadProcessId", lambda hwnd: (0, 4242))
    monkeypatch.setattr(apps, "status", lambda ctx: {"app": ctx.name})

    apps.launch(ctx, fresh=False, pin_size=False)

    assert journal() == []


@pytest.mark.windows("the word plugin")
@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["word", "doc", "read"], "desktoppilot.plugins.word.com.attach"),
        (["vscode", "codex", "read"], "desktoppilot.plugins.vscode.codex.read"),
    ],
)
def test_a_failing_plugin_command_leaves_a_screenshot(stub_window, monkeypatch, argv, target):
    def explode(*args, **kwargs):
        raise PilotError("Word is busy")

    monkeypatch.setattr(target, explode)
    result = invoke(*argv)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["message"] == "Word is busy"
    assert payload["screenshot"] == stub_window[0]
    assert fallback.events(argv[0])[0]["command"] == " ".join(argv[1:])


def test_a_clipboard_read_that_fails_takes_no_screenshot(stub_window, monkeypatch):
    """Nothing on screen explains a clipboard read, and a shot would mark the
    run as one that needed vision."""

    def explode(*args, **kwargs):
        raise PilotError("clipboard busy")

    monkeypatch.setattr("desktoppilot.core.clip.read", explode)
    result = invoke(PLAIN_APP, "clip", "read")

    assert result.exit_code == 1
    assert "screenshot" not in json.loads(result.output)
    assert stub_window == [] and fallback.events(PLAIN_APP) == []
