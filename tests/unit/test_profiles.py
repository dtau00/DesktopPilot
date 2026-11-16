"""Profiles: the TOML data that keeps app specifics out of the code."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from desktoppilot.core import paths, profiles
from desktoppilot.core.errors import PilotError


def test_builtin_profiles_parse():
    for name in profiles.available():
        profile = profiles.load(name)
        assert profile.name == name
        assert profile.exe


def test_code_profile_forces_renderer_accessibility():
    # Without it an Electron window is one opaque UIA node.
    assert "--force-renderer-accessibility" in profiles.load("code").args


def test_new_keys_have_defaults():
    profile = profiles.Profile(name="ghost")
    assert (profile.launch, profile.dialogs) == ("adopt", "explicit")
    assert (profile.depth, profile.root_depth) == (12, 20)
    assert (profile.extends, profile.env) == (None, {})


def test_shipped_profiles_set_the_launch_policy():
    notepad = profiles.load("notepad")
    assert (notepad.launch, notepad.dialogs) == ("adopt", "explicit")
    assert profiles.load("code").depth == 25  # a webview document sits near depth 20


@pytest.mark.windows("a Windows exe path")
def test_image_name_does_not_need_the_exe_to_exist():
    profile = profiles.Profile(name="ghost", exe=[r"%SystemRoot%\System32\nope.exe"])
    assert profile.image_name == "nope.exe"
    with pytest.raises(PilotError):
        profile.resolve_exe()


def test_unknown_profile_lists_the_known_ones():
    with pytest.raises(PilotError, match="available"):
        profiles.load("definitely-not-a-profile")


def test_unknown_key_is_refused(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('name = "bad"\nwidget = 1\n', encoding="utf-8")
    with pytest.raises(PilotError, match="unknown keys"):
        profiles._read(path)


def _plugin_profile(tmp_path, monkeypatch, name: str, body: str):
    plugin = tmp_path / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "profile.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(profiles, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(profiles, "USER_DIR", tmp_path / "nope")
    return plugin


def test_a_plugin_carries_its_own_profile(tmp_path, monkeypatch):
    _plugin_profile(tmp_path, monkeypatch, "widget", 'exe = ["widget.exe"]\ndepth = 7\n')
    assert "widget" in profiles.available()
    profile = profiles.load("widget")
    assert (profile.name, profile.depth) == ("widget", 7)


def test_extends_inherits_and_overrides(tmp_path, monkeypatch):
    _plugin_profile(
        tmp_path,
        monkeypatch,
        "parent",
        'exe = ["parent.exe"]\nlaunch = "fresh"\ndepth = 9\ndialog_classes = ["X"]\n',
    )
    _plugin_profile(tmp_path, monkeypatch, "child", 'extends = "parent"\ndepth = 3\n')

    child = profiles.load("child")
    assert (child.name, child.extends) == ("child", "parent")
    assert child.exe == ["parent.exe"]  # inherited
    assert child.launch == "fresh"  # inherited
    assert child.dialog_classes == ["X"]  # inherited
    assert child.depth == 3  # overridden


def test_options_merge_over_the_parents_key_by_key(tmp_path, monkeypatch):
    _plugin_profile(
        tmp_path, monkeypatch, "parent", 'exe = ["parent.exe"]\n[options]\na = 1\nb = [2]\n'
    )
    _plugin_profile(
        tmp_path, monkeypatch, "child", 'extends = "parent"\n[options]\nb = ["MyAddin"]\n'
    )
    assert profiles.load("child").options == {"a": 1, "b": ["MyAddin"]}
    assert profiles.load("parent").options == {"a": 1, "b": [2]}
    assert profiles.Profile(name="ghost").options == {}


def test_options_must_be_a_table(tmp_path, monkeypatch):
    _plugin_profile(tmp_path, monkeypatch, "widget", 'exe = ["w.exe"]\noptions = 3\n')
    with pytest.raises(PilotError, match=r"\[options\] must be a table"):
        profiles.load("widget")


def test_a_circular_extends_chain_is_refused(tmp_path, monkeypatch):
    _plugin_profile(tmp_path, monkeypatch, "a", 'extends = "b"\n')
    _plugin_profile(tmp_path, monkeypatch, "b", 'extends = "a"\n')
    with pytest.raises(PilotError, match="too deep or circular"):
        profiles.load("a")


@pytest.mark.parametrize(("key", "value"), [("launch", "fersh"), ("dialogs", "always")])
def test_a_bad_policy_value_is_refused(key, value):
    with pytest.raises(PilotError, match=key):
        profiles.Profile(name="ghost", **{key: value})


def test_the_root_depth_default_has_one_source():
    assert profiles.Profile(name="ghost").root_depth == profiles.DEFAULT_ROOT_DEPTH


# -------------------------------------------------------------- platforms


def test_a_profile_runs_on_both_platforms_by_default():
    assert profiles.Profile(name="ghost").platforms == ["windows", "linux"]
    assert profiles.Profile(name="ghost").supported


@pytest.mark.parametrize("platforms", [[], ["macos"], ["windows", "win32"]])
def test_a_bad_platforms_list_is_refused(platforms):
    with pytest.raises(PilotError, match="platforms"):
        profiles.Profile(name="ghost", platforms=platforms)


def test_supported_is_this_platform_in_platforms(monkeypatch):
    monkeypatch.setattr(profiles, "PLATFORM", "linux")
    assert not profiles.Profile(name="ghost", platforms=["windows"]).supported
    assert profiles.Profile(name="ghost", platforms=["linux"]).supported


def test_word_and_notepad_are_windows_only_and_code_runs_on_both():
    for name in ("word", "notepad"):
        assert profiles.load(name).platforms == ["windows"], name
    assert profiles.load("code").platforms == ["windows", "linux"]


OVERRIDDEN = """
exe = ['C:\\App\\app.exe']
window_class = ["AppWindow"]
depth = 9

[linux]
exe = ["/usr/bin/app"]
window_class = ["App"]

[windows]
title_strip = [" - App"]
"""


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_the_platform_table_replaces_its_keys_on_that_platform_only(
    tmp_path, monkeypatch, platform
):
    _plugin_profile(tmp_path, monkeypatch, "app", OVERRIDDEN)
    monkeypatch.setattr(profiles, "PLATFORM", platform)

    app = profiles.load("app")

    assert app.depth == 9  # set once, for both
    if platform == "linux":
        assert (app.exe, app.window_class, app.title_strip) == (["/usr/bin/app"], ["App"], [])
    else:
        assert app.exe == ["C:\\App\\app.exe"] and app.window_class == ["AppWindow"]
        assert app.title_strip == [" - App"]


def test_a_child_inherits_its_parents_platform_table(tmp_path, monkeypatch):
    _plugin_profile(tmp_path, monkeypatch, "parent", OVERRIDDEN)
    _plugin_profile(tmp_path, monkeypatch, "child", 'extends = "parent"\ndepth = 3\n')
    monkeypatch.setattr(profiles, "PLATFORM", "linux")

    child = profiles.load("child")

    assert (child.exe, child.depth) == (["/usr/bin/app"], 3)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[windows]\nwidget = 1\n", r"unknown keys in \[windows\]"),
        ('[linux]\nname = "other"\n', r"unknown keys in \[linux\]"),
        ('[linux]\n[linux.linux]\nexe = ["x"]\n', r"unknown keys in \[linux\]"),
        ('linux = "x"\n', r"\[linux\] must be a table"),
    ],
)
def test_a_bad_platform_table_is_refused_on_every_platform(tmp_path, body, match):
    # The other platform's table too: a typo under [linux] fails on Windows.
    path = tmp_path / "bad.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PilotError, match=match):
        profiles._read(path)


def test_code_on_linux_is_the_snap_the_deb_or_the_binary(monkeypatch):
    monkeypatch.setattr(profiles, "PLATFORM", "linux")
    code = profiles.load("code")
    assert code.exe == ["/snap/bin/code", "/usr/bin/code", "/usr/share/code/code"]
    assert code.window_class == ["Code", "code_code"]
    assert "--force-renderer-accessibility" in code.args
    assert code.image_name == "code"


def test_on_linux_a_bare_exe_name_is_looked_up_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "WINDOWS", False)
    monkeypatch.setattr(profiles.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert profiles.Profile(name="ghost", exe=["gedit"]).resolve_exe() == Path("/opt/bin/gedit")

    monkeypatch.setattr(profiles.shutil, "which", lambda name: None)
    with pytest.raises(PilotError, match="none of its exe candidates"):
        profiles.Profile(name="ghost", exe=["gedit"]).resolve_exe()


def test_on_linux_any_executable_file_is_an_adhoc_app(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "WINDOWS", False)
    exe = tmp_path / "python3.12"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    if sys.platform != "win32":  # Windows has no executable bit to leave off
        assert profiles.adhoc_exe(str(exe)) is None

    exe.chmod(0o755)
    profile = profiles.load(str(exe))

    assert (profile.name, profile.exe, profile.image_name) == (
        "python3.12",
        [str(exe)],
        "python3.12",
    )
    assert profiles.adhoc_exe(str(tmp_path)) is None  # a directory


def test_an_xdg_dir_is_the_variable_when_absolute_else_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths._xdg("XDG_STATE_HOME", ".local/state") == tmp_path
    for unusable in ("", "relative/dir"):
        monkeypatch.setenv("XDG_STATE_HOME", unusable)
        assert paths._xdg("XDG_STATE_HOME", ".local/state") == Path.home() / ".local/state"


def test_the_user_profiles_live_in_this_platforms_dir():
    assert profiles.USER_DIR == paths.PROFILES_DIR
    assert paths.PROFILES_DIR.parts[-2:] == ("desktoppilot", "profiles")
