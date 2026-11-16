"""The shape of the command tree: one sub-tree per app, no hidden app state."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from desktoppilot.cli import app, build
from desktoppilot.core.registry import Registry, discover

from .compat import PLAIN_APP

runner = CliRunner()


def invoke(*args):
    return runner.invoke(app, list(args))


def test_the_root_lists_every_app():
    result = invoke("--help")
    assert result.exit_code == 0
    for name in (PLAIN_APP, "code", "desktop", "apps"):
        assert name in result.output


def test_an_app_carries_the_core_groups():
    result = invoke(PLAIN_APP, "--help")
    assert result.exit_code == 0
    for group in ("app", "ui", "dialogs", "state", "shot", "capture", "clip", "image", "record"):
        assert group in result.output


def test_the_null_app_has_no_lifecycle_group():
    """`desktop` owns no process, so launch/kill would have nothing to act on."""
    result = invoke("desktop", "--help")
    assert result.exit_code == 0
    assert "ui" in result.output
    assert "\n  app " not in result.output


def test_ui_group_commands():
    result = invoke(PLAIN_APP, "ui", "--help")
    assert result.exit_code == 0
    for command in (
        "tree",
        "find",
        "read",
        "click",
        "type",
        "set-value",
        "select",
        "scroll",
        "count",
        "expand",
        "keys",
        "wait",
        "windows",
        "close",
        "activate",
    ):
        assert command in result.output


def test_apps_command_reports_every_app_and_its_source():
    result = invoke("apps")
    assert result.exit_code == 0
    apps = json.loads(result.output)["apps"]
    assert apps[PLAIN_APP] == "profile"
    assert apps["code"] == "profile"
    assert apps["desktop"] == "builtin"


@pytest.mark.parametrize("command", [("ui", "click"), ("ui", "find")])
def test_an_unknown_root_fails_before_any_uia_work(command):
    """A typo is not something that becomes true after a 15 s wait, so it
    fails at resolve time with the roots this app does accept."""
    result = invoke(PLAIN_APP, *command, "ribbon//Button[name='x']")

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "PilotError"
    assert "ribbon" in payload["message"]
    for root in ("app", "win", "focus", "dialog", "desktop"):
        assert root in payload["message"]


def test_shot_takes_the_path_as_an_argument_or_an_option():
    result = invoke(PLAIN_APP, "shot", "--help")
    assert result.exit_code == 0
    assert "[PATH]" in result.output
    assert "--out" in result.output
    clash = invoke(PLAIN_APP, "shot", "a.png", "--out", "b.png")
    assert clash.exit_code != 0


def test_an_unknown_root_without_discover_says_to_discover():
    from desktoppilot.core import profiles, ui
    from desktoppilot.core.errors import PilotError
    from desktoppilot.core.registry import AppContext

    ctx = AppContext(name="word", profile=profiles.load("word"), registry=Registry())
    with pytest.raises(PilotError, match=r"call discover\(REGISTRY\) first"):
        ui.find("pane//List", ctx, timeout=0.1)

    reg = Registry()
    discover(reg)
    ctx = AppContext(name=PLAIN_APP, profile=profiles.load(PLAIN_APP), registry=reg)
    with pytest.raises(PilotError) as err:
        ui.find("pane//List", ctx, timeout=0.1)
    assert "discover" not in str(err.value)


@pytest.mark.windows("an ad-hoc .exe path")
def test_an_existing_exe_path_is_an_adhoc_app_named_by_its_stem(tmp_path):
    exe = str(tmp_path / "x.exe")
    (tmp_path / "x.exe").write_bytes(b"")
    tree = build([exe, "--help"])

    result = runner.invoke(tree, [exe, "--help"])

    assert result.exit_code == 0, result.output
    assert "Drive x." in result.output
    for group in ("app", "ui", "events"):
        assert group in result.output


def test_a_missing_exe_path_is_no_app(tmp_path):
    exe = str(tmp_path / "missing.exe")

    result = runner.invoke(build([exe, "app", "status"]), [exe, "app", "status"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_an_adhoc_app_is_not_listed_in_apps(tmp_path):
    exe = str(tmp_path / "x.exe")
    (tmp_path / "x.exe").write_bytes(b"")

    result = runner.invoke(build([exe, "app", "status"]), ["apps"])

    assert json.loads(result.output) == json.loads(invoke("apps").output)


def _user_dir(tmp_path, monkeypatch, files: dict[str, str]):
    from desktoppilot.core import profiles

    user = tmp_path / "user"
    user.mkdir()
    for name, body in files.items():
        (user / f"{name}.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(profiles, "USER_DIR", user)


def test_a_malformed_user_profile_does_not_break_the_other_commands(tmp_path, monkeypatch):
    _user_dir(tmp_path, monkeypatch, {"bad": "exe = [\n"})
    tree = build(["apps"])

    listed = runner.invoke(tree, ["apps"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert "bad" not in payload["apps"] and PLAIN_APP in payload["apps"]
    assert "bad.toml" in payload["broken"]["bad"]

    named = runner.invoke(tree, ["bad", "app", "status", "--force"])
    assert named.exit_code == 1
    error = json.loads(named.output)
    assert error["error"] == "PilotError" and "bad.toml" in error["message"]


def test_apps_reports_no_broken_profiles_as_an_empty_map():
    assert json.loads(invoke("apps").output)["broken"] == {}


@pytest.mark.windows("the word plugin")
def test_a_user_profile_that_extends_word_carries_words_groups(tmp_path, monkeypatch):
    _user_dir(tmp_path, monkeypatch, {"myword": 'extends = "word"\n'})
    tree = build(["myword", "--help"])

    result = runner.invoke(tree, ["myword", "--help"])

    assert result.exit_code == 0, result.output
    for group in ("doc", "ribbon"):
        assert group in result.output


def test_selector_help_names_no_plugin_root_under_a_plain_app():
    output = runner.invoke(app, [PLAIN_APP, "ui", "find", "--help"], env={"COLUMNS": "300"}).output
    assert "ribbon" not in output and "backstage" not in output
    assert "NOTES.md" in output


def test_ui_tree_takes_levels_not_depth():
    output = invoke(PLAIN_APP, "ui", "tree", "--help").output
    assert "--levels" in output
    assert "--depth" not in output


def test_ui_keys_help_says_caret_is_not_ctrl():
    output = runner.invoke(app, [PLAIN_APP, "ui", "keys", "--help"], env={"COLUMNS": "300"}).output
    assert "{Ctrl}" in output and "^" in output and "not modifiers" in output


def test_ui_activate_takes_root_as_selector(monkeypatch):
    from desktoppilot.core import ui

    seen: list[str] = []
    monkeypatch.setattr(ui, "find", lambda selector, ctx, **k: seen.append(selector) or object())
    monkeypatch.setattr(ui, "activate", lambda window: None)
    for name in ("--selector", "--root"):
        result = invoke(PLAIN_APP, "--no-shot", "ui", "activate", name, "win:Find")
        assert result.exit_code == 0, result.output
    assert seen == ["win:Find", "win:Find"]


@pytest.mark.parametrize("command", ["keys", "type", "close", "activate"])
def test_every_ui_command_that_finds_takes_a_timeout(command):
    assert "--timeout" in invoke(PLAIN_APP, "ui", command, "--help").output


def test_ui_wait_takes_a_depth():
    assert "--depth" in invoke(PLAIN_APP, "ui", "wait", "--help").output


def test_ui_tree_levels_sets_the_walk_depth_and_depth_is_gone(monkeypatch):
    from desktoppilot.core import ui

    seen: list[int] = []
    monkeypatch.setattr(ui, "find", lambda selector, ctx, **k: object())
    monkeypatch.setattr(ui, "tree", lambda node, depth, **k: seen.append(depth) or {})

    assert invoke(PLAIN_APP, "ui", "tree", "--levels", "2").exit_code == 0
    assert invoke(PLAIN_APP, "ui", "tree", "--depth", "7").exit_code != 0
    assert seen == [2]


def test_record_group_commands():
    result = invoke(PLAIN_APP, "record", "--help")
    assert result.exit_code == 0
    for command in ("start", "stop", "status", "gif"):
        assert command in result.output


def test_apps_lists_the_apps_for_another_platform_as_unsupported():
    payload = json.loads(invoke("apps").output)
    if sys.platform == "win32":
        assert payload["unsupported"] == {}
    else:
        assert set(payload["unsupported"]) == {"word", "notepad"}
        assert "word" not in payload["apps"]


def test_a_command_under_an_unsupported_app_is_an_error_json(tmp_path, monkeypatch):
    other = "linux" if sys.platform == "win32" else "windows"
    _user_dir(tmp_path, monkeypatch, {"elsewhere": f'exe = ["e"]\nplatforms = ["{other}"]\n'})
    tree = build(["elsewhere", "app", "status"])

    result = runner.invoke(tree, ["elsewhere", "app", "status"])

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error["error"] == "PilotError" and "not supported here" in error["message"]
    assert "elsewhere" in json.loads(runner.invoke(tree, ["apps"]).output)["unsupported"]


def test_on_linux_an_executable_path_is_an_adhoc_app_named_by_its_file_name(tmp_path, monkeypatch):
    from desktoppilot.core import profiles

    monkeypatch.setattr(profiles, "WINDOWS", False)
    exe = tmp_path / "tool.sh"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    tree = build([str(exe), "--help"])

    result = runner.invoke(tree, [str(exe), "--help"])

    assert result.exit_code == 0, result.output
    assert "Drive tool.sh." in result.output
