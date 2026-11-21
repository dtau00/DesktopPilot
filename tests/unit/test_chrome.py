"""The chrome app: its profile on each platform, and the cdp group it adds."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from desktoppilot.cli import app
from desktoppilot.core import cmd_cdp, profiles


@pytest.mark.parametrize(
    ("platform", "image", "window_class"),
    [("windows", "chrome.exe", "Chrome_WidgetWin_1"), ("linux", "chrome", "Google-chrome")],
)
def test_the_profile_on_each_platform(monkeypatch, platform, image, window_class):
    monkeypatch.setattr(profiles, "PLATFORM", platform)
    profile = profiles.load("chrome")
    assert profile.supported
    assert profile.exe[0].endswith(("\\" + image, "/" + image))
    assert profile.window_class == [window_class]
    assert profile.launch == "adopt" and profile.dialogs == "explicit" and profile.depth == 25
    assert profile.args == ["--force-renderer-accessibility"]
    assert profile.title_strip == [" - Google Chrome"]


def test_chrome_opens_no_port_of_its_own(monkeypatch):
    monkeypatch.delenv("PILOT_CHROME_PID", raising=False)
    with pytest.raises(Exception, match="opens no debugging port"):
        cmd_cdp.port_of(profiles.load("chrome"), None)


def test_the_cdp_group_is_on_the_cli():
    result = CliRunner().invoke(app, ["chrome", "cdp", "--help"])
    assert result.exit_code == 0
    for command in (
        "targets",
        "target",
        "eval",
        "click",
        "type",
        "set-value",
        "read",
        "wait",
        "shot",
    ):
        assert command in result.output


# ------------------------------------------------------------ scratch


class FakeChrome:
    pid = 5150
    returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def fake_scratch(monkeypatch, tmp_path):
    """Chrome's start, port and kill stubbed; `seen` records what they got."""
    from desktoppilot.core import apps
    from desktoppilot.plugins.chrome import scratch

    seen: dict = {"killed": [], "alive": {5150, 5151}}
    chrome = FakeChrome()
    exe = tmp_path / "chrome"
    exe.write_bytes(b"")
    profile = profiles.Profile(
        name="chrome", exe=[str(exe)], args=["--force-renderer-accessibility"]
    )

    def start_process(args, env, log):
        seen["args"], seen["env"] = args, env
        return chrome

    def taskkill(pids):
        seen["killed"].append(list(pids))
        seen["alive"].clear()

    monkeypatch.setattr(scratch, "RECORDS", tmp_path / "records")
    monkeypatch.setattr(scratch.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "temp"))
    monkeypatch.setattr(apps, "start_process", start_process)
    monkeypatch.setattr(apps, "launch_env", lambda profile: {"CLEAN": "1"})
    monkeypatch.setattr(apps, "created", lambda pid: 10.0)
    monkeypatch.setattr(apps, "alive", lambda pid, started=None: pid in seen["alive"])
    monkeypatch.setattr(apps, "tree_of", lambda pid: {5150, 5151})
    monkeypatch.setattr(scratch.procs, "taskkill", taskkill)
    monkeypatch.setattr(scratch.cdp, "list_targets", lambda port, timeout: [{"type": "page"}])
    monkeypatch.setattr(scratch.cdp, "version", lambda port: {"Browser": "Chrome/1"})
    return scratch, profile, chrome, seen, tmp_path


def test_scratch_start_runs_chrome_on_its_own_folder_and_port(fake_scratch):
    scratch, profile, _, seen, tmp_path = fake_scratch
    (tmp_path / "temp").mkdir()
    started = scratch.start(profile, port=9444, url="http://x/")

    assert seen["args"][0].endswith("chrome") and seen["args"][-1] == "http://x/"
    flags = seen["args"][1:-1]
    assert flags.count("--force-renderer-accessibility") == 1
    for flag in ("--no-first-run", "--no-default-browser-check", "--remote-debugging-port=9444"):
        assert flag in flags
    assert f"--user-data-dir={tmp_path / 'temp'}" in flags
    assert seen["env"] == {"CLEAN": "1"}
    assert started["bind"] == "PILOT_CHROME_PID=5150"
    assert (started["pid"], started["port"], started["temporary"]) == (5150, 9444, True)
    assert started["browser"] == "Chrome/1"


def test_scratch_start_picks_a_free_port_and_keeps_a_given_folder(fake_scratch):
    scratch, profile, _, seen, tmp_path = fake_scratch
    started = scratch.start(profile, directory=str(tmp_path / "mine"))
    assert started["port"] > 0 and started["temporary"] is False
    assert started["user_data_dir"] == str(tmp_path / "mine") and started["url"] == "about:blank"


def test_a_scratch_chrome_that_exits_at_start_is_an_error_and_leaves_nothing(fake_scratch):
    from desktoppilot.core.errors import PilotError

    scratch, profile, chrome, seen, tmp_path = fake_scratch
    (tmp_path / "temp").mkdir()
    chrome.returncode = 0
    with pytest.raises(PilotError, match="exited at start"):
        scratch.start(profile)
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "records").exists()


def test_scratch_stop_kills_that_tree_and_removes_the_folder_start_made(fake_scratch):
    scratch, profile, _, seen, tmp_path = fake_scratch
    (tmp_path / "temp").mkdir()
    scratch.start(profile)

    stopped = scratch.stop(5150)
    assert seen["killed"] == [[5150]], "the kill must name the pid, never the image"
    assert stopped["killed"] == [5150, 5151] and stopped["removed"] is True
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "records" / "5150.json").exists()


def test_scratch_stop_keeps_a_folder_it_was_given(fake_scratch):
    scratch, profile, _, _, tmp_path = fake_scratch
    scratch.start(profile, directory=str(tmp_path / "mine"))
    assert scratch.stop(5150)["removed"] is False
    assert (tmp_path / "mine").exists()


def test_scratch_stop_refuses_a_pid_it_did_not_start(fake_scratch):
    from desktoppilot.core.errors import PilotError

    scratch, _, _, seen, _ = fake_scratch
    with pytest.raises(PilotError, match="left alone"):
        scratch.stop(16894)
    assert seen["killed"] == []


def test_scratch_stop_of_a_pid_reused_since_kills_nothing(fake_scratch):
    scratch, profile, _, seen, tmp_path = fake_scratch
    scratch.start(profile, directory=str(tmp_path / "mine"))
    seen["alive"].clear()
    assert scratch.stop(5150)["killed"] == [] and seen["killed"] == []


def test_the_scratch_json_keys_are_documented_and_emitted(fake_scratch, monkeypatch):
    import json

    _, profile, _, _, tmp_path = fake_scratch
    (tmp_path / "temp").mkdir()
    exe = profile.resolve_exe()
    monkeypatch.setattr(profiles.Profile, "resolve_exe", lambda self: exe)
    runner = CliRunner()

    def contract(args, keys):
        result = runner.invoke(app, ["chrome", "scratch", *args])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        help_text = runner.invoke(
            app, ["chrome", "scratch", args[0], "--help"], env={"COLUMNS": "400"}
        ).output
        assert [k for k in keys if k not in help_text] == []
        assert [k for k in keys if k not in payload] == []

    start_keys = ["pid", "created", "port", "user_data_dir", "temporary", "url", "browser"]
    contract(["start"], [*start_keys, "log", "bind"])
    contract(["stop", "5150"], ["pid", "killed", "user_data_dir", "removed"])
