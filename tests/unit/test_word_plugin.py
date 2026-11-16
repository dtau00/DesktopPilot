"""The word plugin as the core sees it: discovered, its groups on the CLI, its
roots overriding the core ones, its profile, and the dialog commands.

No Word is started here.
"""

from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from desktoppilot.cli import app as cli
from desktoppilot.core import dialogs as dialogs_mod
from desktoppilot.core import profiles, selectors, ui
from desktoppilot.core.registry import AppContext, Registry, discover
from desktoppilot.plugins.word import roots, session

runner = CliRunner()


def invoke(*args):
    return runner.invoke(cli, list(args))


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    discover(reg)
    return reg


@pytest.fixture
def word_ctx(registry) -> AppContext:
    return AppContext(name="word", profile=profiles.load("word"), registry=registry)


def test_the_plugin_is_discovered_as_an_app():
    result = invoke("apps")
    assert result.exit_code == 0
    assert json.loads(result.output)["apps"]["word"] == "plugin"


def test_word_carries_the_core_groups_and_its_own():
    result = invoke("word", "--help")
    assert result.exit_code == 0
    for group in (
        "app",
        "ui",
        "doc",
        "ribbon",
        "pane",
        "backstage",
        "addin",
        "dialogs",
        "state",
        "shot",
        "capture",
        "clip",
    ):
        assert group in result.output


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        (
            "doc",
            (
                "open",
                "close",
                "save-as",
                "read",
                "find",
                "select",
                "new",
                "save",
                "insert",
                "comments",
                "revisions",
            ),
        ),
        ("ribbon", ("tabs", "controls", "click")),
        ("pane", ("read",)),
        ("backstage", ("show", "open", "close")),
        ("addin", ("windows", "close")),
    ],
)
def test_each_word_group_has_its_commands(group, commands):
    result = invoke("word", group, "--help")
    assert result.exit_code == 0
    for command in commands:
        assert command in result.output


@pytest.mark.parametrize(
    ("code", "name"),
    [(1, "Insert"), (2, "Delete"), (14, "MovedFrom"), (18, "CellMerge"), (42, "Type42")],
)
def test_revision_type_names(code, name):
    from desktoppilot.plugins.word import docs

    assert docs.revision_type(code) == name


def test_insert_refuses_an_unknown_position():
    from desktoppilot.core.errors import PilotError
    from desktoppilot.plugins.word import docs

    with pytest.raises(PilotError, match="--at"):
        docs.insert_text(object(), "x", at="middle")


def test_word_registers_its_roots(registry):
    assert {"app", "win", "ribbon", "pane", "backstage"} <= set(registry.roots_for("word"))


def test_the_profile_carries_words_policy():
    profile = profiles.load("word")
    assert profile.launch == "fresh"
    assert profile.dialogs == "auto"
    assert (profile.depth, profile.root_depth) == (12, 20)
    assert profile.window_class == ["OpusApp"]
    assert profile.window_size == [1600, 1000]
    assert profile.image_name.upper() == "WINWORD.EXE"


def test_the_plugins_app_root_overrides_the_core_one(monkeypatch, word_ctx):
    """Word's frame is the OpusApp window, not the largest window the process
    owns, so `app` must resolve through the plugin."""
    monkeypatch.setattr(roots, "word_window", lambda ctx, timeout=0: "the-word-frame")
    monkeypatch.setattr(ui, "app_window", _never_called)

    assert ui.resolve_root(selectors.parse("app//Button"), word_ctx) == "the-word-frame"


def test_the_plugins_win_root_overrides_the_core_one(monkeypatch, word_ctx):
    monkeypatch.setattr(
        roots, "addin_window", lambda ctx, title, timeout=0, probe=False: ("addin", title)
    )
    monkeypatch.setattr(ui, "named_window", _never_called)

    selector = selectors.parse("win:Inspector//Button")
    assert ui.resolve_root(selector, word_ctx) == ("addin", "Inspector")


def test_a_core_root_word_does_not_override_still_resolves(monkeypatch, word_ctx):
    monkeypatch.setattr(ui, "focused_window", lambda timeout=0, probe=False: "the-focused-window")
    assert ui.resolve_root(selectors.parse("focus//Button"), word_ctx) == "the-focused-window"


def _never_called(*args, **kwargs):
    raise AssertionError("the core root ran although the plugin overrode it")


# ----------------------------------------------------------------- dialogs


@pytest.fixture
def fake_dialogs(monkeypatch):
    """Two open dialogs, one of them Word's safe-mode prompt."""
    open_dialogs = [
        {"hwnd": 1, "title": "Microsoft Word", "text": "start in safe mode?"},
        {"hwnd": 2, "title": "Font", "text": ""},
    ]
    calls: dict = {}

    monkeypatch.setattr("desktoppilot.core.apps.pids", lambda profile: [4242])
    monkeypatch.setattr(dialogs_mod, "dialog_windows", lambda pids, **_: list(open_dialogs))

    def close_all(pids, **_):
        calls["close_all"] = True
        return [
            {"title": d["title"], "text": d["text"], "answered": "Cancel"} for d in open_dialogs
        ]

    def reap(pids, prompts, **_):
        calls["reap"] = prompts
        return [{"title": "Microsoft Word", "text": "start in safe mode?", "answered": "No"}]

    monkeypatch.setattr(dialogs_mod, "close_all", close_all)
    monkeypatch.setattr(dialogs_mod, "reap", reap)
    return calls


def test_dialogs_lists_what_is_open(fake_dialogs):
    payload = json.loads(invoke("word", "dialogs").output)
    assert [d["title"] for d in payload["dialogs"]] == ["Microsoft Word", "Font"]


def test_dialogs_close_reports_closed_and_remaining(fake_dialogs):
    payload = json.loads(invoke("word", "dialogs", "--close").output)
    assert fake_dialogs["close_all"]
    assert [d["answered"] for d in payload["closed"]] == ["Cancel", "Cancel"]
    assert "remaining" in payload


def test_dialogs_dismiss_answers_only_the_prompts_word_knows(fake_dialogs):
    payload = json.loads(invoke("word", "dialogs", "--dismiss").output)
    assert fake_dialogs["reap"] == session.KNOWN_PROMPTS
    assert "close_all" not in fake_dialogs
    assert payload["dismissed"][0]["answered"] == "No"


def test_an_app_without_a_policy_reports_on_dismiss_and_clicks_nothing(fake_dialogs):
    """notepad names no prompts: there is nothing to recognize, so nothing is
    guessed at - the dialogs are reported and left open."""
    payload = json.loads(invoke("notepad", "dialogs", "--dismiss").output)
    assert "close_all" not in fake_dialogs and "reap" not in fake_dialogs
    assert payload["dismissed"] == []
    assert len(payload["remaining"]) == 2 and "--close" in payload["note"]


def test_the_null_app_refuses_to_close_every_dialog_on_the_desktop(fake_dialogs):
    result = invoke("desktop", "dialogs", "--close")
    assert result.exit_code == 1
    assert "pilot <app> dialogs --close" in json.loads(result.output)["message"]
    assert "close_all" not in fake_dialogs


# --------------------------------------------------------------- busy COM


@pytest.mark.parametrize(
    "text",
    [
        "Word.Application.COMAddIns",  # a property get on a Word still starting
        "<unknown>.Count",  # the same, on a sub-object pywin32 could not name
        "Word.Document.Paragraphs can not be set",
    ],
)
def test_a_busy_word_is_retried(text):
    from desktoppilot.plugins.word.com import _is_busy

    assert _is_busy(AttributeError(text))


def test_a_real_typo_is_not_retried():
    from desktoppilot.plugins.word.com import _is_busy

    assert not _is_busy(AttributeError("Doccuments"))


# ------------------------------------------------------- foreign Word guard


@pytest.fixture
def foreign_word(monkeypatch):
    """One Word this tool never started, holding a document."""
    monkeypatch.setattr(session, "documents_open_in", lambda pid: ["Contract"] if pid == 99 else [])
    monkeypatch.setattr("desktoppilot.core.apps.Session.load", lambda profile: None)
    return AppContext(name="word", profile=profiles.load("word"))


def test_killing_a_foreign_word_with_documents_is_refused(foreign_word):
    with pytest.raises(session.ForeignWordOpen) as err:
        session.refuse_foreign_word(foreign_word, pids=[99], force=False)

    assert "pid 99: Contract" in str(err.value)
    assert "--force" in str(err.value)


def test_force_kills_it_anyway(foreign_word):
    assert session.refuse_foreign_word(foreign_word, pids=[99], force=True) is None


def test_an_instance_this_tool_launched_is_ours_to_kill(monkeypatch, foreign_word):
    ours = session.apps.Session(app="word", pid=99, started_at=1.0, launched=True)
    monkeypatch.setattr("desktoppilot.core.apps.Session.load", lambda profile: ours)
    assert session.refuse_foreign_word(foreign_word, pids=[99], force=False) is None


@pytest.fixture
def saved_session(monkeypatch, tmp_path, foreign_word):
    """A real session file for pid 99, adopted or launched; `docs` per pid."""
    from desktoppilot.core import apps, paths

    monkeypatch.undo()
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(apps, "created", {99: 0.5}.get)

    def given(*, launched: bool, docs: dict):
        monkeypatch.setattr(session, "documents_open_in", lambda pid: docs.get(pid, []))
        apps.Session(
            app="word",
            pid=99 if launched else None,
            started_at=1.0,
            launched=launched,
            pids=None if launched else [99],
        ).save(foreign_word.profile)
        return foreign_word

    return given


def test_an_adopted_word_with_a_document_open_is_not_killed_without_force(saved_session):
    ctx = saved_session(launched=False, docs={99: ["Contract"]})
    with pytest.raises(session.ForeignWordOpen) as err:
        session.refuse_foreign_word(ctx, pids=[99], force=False)
    assert "pid 99 (adopted): Contract" in str(err.value) and "--force" in str(err.value)
    assert session.refuse_foreign_word(ctx, pids=[99], force=True) is None


def test_an_adopted_word_with_nothing_open_is_killed(saved_session):
    ctx = saved_session(launched=False, docs={})
    assert session.refuse_foreign_word(ctx, pids=[99], force=False) is None


def test_a_launched_word_with_a_document_open_is_killed(saved_session):
    ctx = saved_session(launched=True, docs={99: ["Contract"]})
    assert session.refuse_foreign_word(ctx, pids=[99], force=False) is None


def test_a_word_launched_through_a_child_profile_is_ours_to_kill_through_word(
    monkeypatch, tmp_path, foreign_word
):
    """One session per image: either app name finds the instance it launched."""
    from desktoppilot.core import apps, paths

    monkeypatch.undo()
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    (user / "wordchild.toml").write_text('extends = "word"\n', encoding="utf-8")
    monkeypatch.setattr(profiles, "USER_DIR", user)
    monkeypatch.setattr(session, "documents_open_in", lambda pid: ["Contract"])
    apps.Session(app="wordchild", pid=99, started_at=1.0, launched=True).save(
        profiles.load("wordchild")
    )
    assert session.refuse_foreign_word(foreign_word, pids=[99], force=False) is None


def test_a_word_with_nothing_open_is_killed_without_a_prompt(monkeypatch, foreign_word):
    monkeypatch.setattr(session, "documents_open_in", lambda pid: [])
    assert session.refuse_foreign_word(foreign_word, pids=[77], force=False) is None


def test_kill_runs_the_pre_kill_hooks_before_anything_else(monkeypatch):
    from desktoppilot.core import apps
    from desktoppilot.core.errors import PilotError
    from desktoppilot.core.registry import Registry

    def refuse(ctx, *, pids, force):
        raise PilotError("no: " + repr((pids, force)))

    reg = Registry()
    reg.hook("pre_kill", refuse, for_app="word")
    ctx = AppContext(name="word", profile=profiles.load("word"), registry=reg)
    monkeypatch.setattr(apps, "pids", lambda profile: [7])
    monkeypatch.setattr(apps.subprocess, "run", _never_called)

    with pytest.raises(PilotError, match=r"no: \(\[7\], False\)"):
        apps.kill(ctx)


def test_a_server_fault_is_not_retried_so_open_or_insert_never_runs_twice():
    import pythoncom

    from desktoppilot.plugins.word.com import _is_busy

    assert not _is_busy(
        pythoncom.com_error(-2147417851, "The server threw an exception.", None, None)
    )
    assert _is_busy(pythoncom.com_error(-2147418111, "Call was rejected by callee.", None, None))


# --------------------------------------------------------------- attach


@pytest.fixture
def no_rot(monkeypatch):
    from desktoppilot.plugins.word import com

    calls = []

    def get_active(progid):
        calls.append(progid)
        raise com.pythoncom.com_error(-2147221021, "Operation unavailable", None, None)

    monkeypatch.setattr(com.win32com.client, "GetActiveObject", get_active)
    return calls


def test_attach_with_no_word_running_fails_at_once_not_after_60_s(monkeypatch, no_rot):
    from desktoppilot.core.errors import AppNotRunning
    from desktoppilot.plugins.word import com

    monkeypatch.setattr(session.apps, "pids", lambda profile: [])
    start = time.monotonic()
    with pytest.raises(AppNotRunning):
        com.attach(profiles.load("word"))
    assert time.monotonic() - start < 1 and no_rot == []


def test_attach_to_a_word_this_tool_did_not_start_is_refused(monkeypatch, no_rot):
    from desktoppilot.plugins.word import com

    monkeypatch.setattr(session.apps, "pids", lambda profile: [99])
    monkeypatch.setattr("desktoppilot.core.apps.Session.load", lambda profile: None)
    with pytest.raises(session.NotOurWord, match="pilot word app launch"):
        com.attach(profiles.load("word"))
    assert no_rot == []


def test_attach_checks_whose_com_object_it_got(monkeypatch):
    from desktoppilot.plugins.word import com

    word = object()
    seen = []
    monkeypatch.setattr(session.apps, "pids", lambda profile: [7, 99])
    monkeypatch.setattr(session, "require_owned", lambda profile: [7])
    monkeypatch.setattr(com.win32com.client, "GetActiveObject", lambda progid: word)
    monkeypatch.setattr(
        session, "check_com_owner", lambda app, owned, running: seen.append((app, owned, running))
    )
    assert com.attach(profiles.load("word")) is word
    assert seen == [(word, [7], [7, 99])]
