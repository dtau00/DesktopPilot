"""PILOT_<APP>_PID binds an app to one instance: that pid and its
descendants, never another process of the same image. Fakes only."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from desktoppilot.core import apps, commands, paths
from desktoppilot.core.errors import AppNotRunning, PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

# The user's Chrome (100, children 101 102) and a scratch one (200, child
# 201, and 202 under 201); 300 is another image under the scratch one.
TREE = {100: 1, 101: 100, 102: 100, 200: 1, 201: 200, 202: 201, 300: 200}
IMAGE = {100, 101, 102, 200, 201, 202}


def row(pid: int) -> list[str]:
    return ["chrome", str(pid), "", "1", "1024 K"]


@pytest.fixture
def chrome(monkeypatch, tmp_path):
    """A chrome profile over a fake process table; `alive` is what runs."""
    alive = set(IMAGE)
    killed: list[list[int]] = []

    def taskkill(pids):
        pids = sorted(pids)
        killed.append(pids)
        alive.difference_update(pids)

    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(apps, "_parents", lambda: dict(TREE))
    monkeypatch.setattr(
        apps.procs, "tasklist", lambda image: [row(pid) for pid in sorted(alive & IMAGE)]
    )
    monkeypatch.setattr(apps, "created", lambda pid: 1.0)
    monkeypatch.setattr(apps.procs, "taskkill", taskkill)
    monkeypatch.setattr(apps, "_spared", lambda profile: {1})
    monkeypatch.setattr(apps, "windows", lambda profile, **k: [])
    monkeypatch.delenv("PILOT_CHROME_PID", raising=False)
    profile = Profile(name="chrome", exe=["/opt/google/chrome/chrome"], ready_timeout=0.2)
    ctx = AppContext(name="chrome", profile=profile, registry=Registry())
    return ctx, alive, killed


def test_the_variable_is_named_after_the_profile():
    assert apps.binding_env(Profile(name="chrome")) == "PILOT_CHROME_PID"
    assert apps.binding_env(Profile(name="my-app")) == "PILOT_MY_APP_PID"


def test_unbound_the_app_is_every_process_of_the_image(chrome):
    ctx, _, _ = chrome
    assert apps.bound_pid(ctx.profile) is None
    assert apps.pids(ctx.profile) == [100, 200]
    assert apps.all_pids(ctx.profile) == sorted(IMAGE)


def test_bound_the_app_is_that_pid_and_its_descendants_of_the_image(chrome, monkeypatch):
    ctx, _, _ = chrome
    monkeypatch.setenv("PILOT_CHROME_PID", "200")
    assert apps.pids(ctx.profile) == [200]
    assert apps.all_pids(ctx.profile) == [200, 201, 202]


def test_bound_to_a_pid_that_is_gone_the_app_is_not_running(chrome, monkeypatch):
    ctx, _, _ = chrome
    monkeypatch.setenv("PILOT_CHROME_PID", "999")
    assert apps.pids(ctx.profile) == []
    with pytest.raises(AppNotRunning):
        apps.require_running(ctx.profile)


@pytest.mark.parametrize("value", ["abc", "-5", "0"])
def test_a_binding_that_is_not_a_pid_is_an_error(chrome, monkeypatch, value):
    ctx, _, _ = chrome
    monkeypatch.setenv("PILOT_CHROME_PID", value)
    with pytest.raises(PilotError, match="PILOT_CHROME_PID"):
        apps.pids(ctx.profile)


def test_status_says_where_the_binding_came_from(chrome, monkeypatch):
    ctx, _, _ = chrome
    assert (apps.status(ctx)["bound_pid"], apps.status(ctx)["bound_by"]) == (None, None)
    monkeypatch.setenv("PILOT_CHROME_PID", "200")
    status = apps.status(ctx)
    assert (status["bound_pid"], status["bound_by"]) == (200, "PILOT_CHROME_PID")
    assert status["pids"] == [200]


def test_a_bound_kill_takes_that_tree_only_and_keeps_the_other_session(chrome, monkeypatch):
    ctx, alive, killed = chrome
    apps.Session(app="chrome", pid=None, started_at=2.0, launched=False, pids=[100]).save(
        ctx.profile
    )
    monkeypatch.setenv("PILOT_CHROME_PID", "200")

    assert apps.kill(ctx, force=True)["killed"] == [200]
    assert killed == [[200, 201, 202]]
    assert alive == {100, 101, 102}
    assert apps.Session.path(ctx.profile).exists(), "the user's session was removed"


def test_a_bound_kill_of_the_session_instance_removes_the_session(chrome, monkeypatch):
    ctx, _, _ = chrome
    apps.Session(app="chrome", pid=200, started_at=2.0, launched=True).save(ctx.profile)
    monkeypatch.setenv("PILOT_CHROME_PID", "200")

    apps.kill(ctx, force=True)
    assert not apps.Session.path(ctx.profile).exists()


def test_a_bound_launch_adopts_the_bound_instance(chrome, monkeypatch):
    ctx, _, _ = chrome
    monkeypatch.setenv("PILOT_CHROME_PID", "200")
    monkeypatch.setattr(apps, "main_hwnd", lambda profile, **k: 7)
    result = apps.launch(ctx)
    assert result["adopted"] is True and result["pids"] == [200]


def test_a_bound_launch_refuses_fresh_and_a_binding_that_is_gone(chrome, monkeypatch):
    ctx, _, _ = chrome
    monkeypatch.setenv("PILOT_CHROME_PID", "200")
    with pytest.raises(PilotError, match="only adopts"):
        apps.launch(ctx, fresh=True)
    monkeypatch.setenv("PILOT_CHROME_PID", "999")
    with pytest.raises(AppNotRunning, match="PILOT_CHROME_PID=999"):
        apps.launch(ctx)


def two_windows(profile, *, main_only=False, owned=None):
    """The user's big window (pid 100) and the scratch one's small one (200)."""
    found = [
        {"hwnd": 1, "pid": 100, "is_main": True, "rect": [0, 0, 1000, 1000]},
        {"hwnd": 2, "pid": 200, "is_main": True, "rect": [0, 0, 300, 300]},
    ]
    owned = set(apps.all_pids(profile)) if owned is None else owned
    return [w for w in found if w["pid"] in owned]


def test_main_hwnd_of_a_pid_is_that_tree_s_window_not_the_largest(chrome, monkeypatch):
    ctx, _, _ = chrome
    monkeypatch.setattr(apps, "windows", two_windows)
    assert apps.main_hwnd(ctx.profile) == 1
    assert apps.main_hwnd(ctx.profile, pid=200) == 2


# ------------------------------------------------------------ app resize


@pytest.fixture
def resize_cli(chrome, monkeypatch):
    ctx, _, _ = chrome
    resized: list = []
    monkeypatch.setattr(apps, "windows", two_windows)
    monkeypatch.setattr(
        apps, "resize", lambda hwnd, w, h: resized.append((hwnd, w, h)) or {"dips": [w, h]}
    )
    from desktoppilot.core import cmd_app

    monkeypatch.setattr(cmd_app.win32gui, "IsWindow", lambda hwnd: hwnd in (1, 2))
    tree = commands.build_app_cli("chrome", ctx.profile, Registry())
    return tree, resized


def invoke(tree, *args):
    result = CliRunner().invoke(tree, list(args))
    return result.exit_code, json.loads(result.output)


def test_resize_takes_the_largest_window_by_default(resize_cli):
    tree, resized = resize_cli
    assert invoke(tree, "app", "resize", "800", "600")[0] == 0
    assert resized == [(1, 800, 600)]


def test_resize_pid_takes_the_main_window_of_that_tree(resize_cli):
    tree, resized = resize_cli
    code, payload = invoke(tree, "app", "resize", "800", "600", "--pid", "200")
    assert code == 0 and payload["hwnd"] == 2 and resized == [(2, 800, 600)]


def test_resize_follows_the_binding(resize_cli, monkeypatch):
    tree, resized = resize_cli
    monkeypatch.setenv("PILOT_CHROME_PID", "200")
    assert invoke(tree, "app", "resize", "800", "600")[1]["hwnd"] == 2


def test_resize_hwnd_takes_that_window(resize_cli):
    tree, resized = resize_cli
    assert invoke(tree, "app", "resize", "800", "600", "--hwnd", "2")[1]["hwnd"] == 2
    code, payload = invoke(tree, "app", "resize", "800", "600", "--hwnd", "9")
    assert code == 1 and "no window 9" in payload["message"]


def test_resize_pid_and_hwnd_are_exclusive(resize_cli):
    tree, resized = resize_cli
    code, payload = invoke(tree, "app", "resize", "8", "6", "--pid", "200", "--hwnd", "2")
    assert code == 1 and "not both" in payload["message"] and resized == []


def test_resize_takes_width_and_height_as_options_too(resize_cli):
    tree, resized = resize_cli
    assert invoke(tree, "app", "resize", "--width", "640", "--height", "480")[0] == 0
    assert resized == [(1, 640, 480)]
    code, payload = invoke(tree, "app", "resize", "800", "600", "--width", "640")
    assert code == 1 and "twice" in payload["message"]
    code, payload = invoke(tree, "app", "resize", "--width", "640")
    assert code == 1 and "both WIDTH and HEIGHT" in payload["message"]
