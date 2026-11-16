"""The registry: what a plugin adds, and which app sees it."""

from __future__ import annotations

import json
import sys
import textwrap
from importlib import metadata

import pytest
from typer.testing import CliRunner

from desktoppilot.core import profiles, selectors, ui
from desktoppilot.core.errors import PilotError
from desktoppilot.core.registry import AppContext, Registry, discover

from .compat import PLAIN_APP


@pytest.fixture
def reg():
    registry = Registry()
    registry.group("doc", "doc-group", for_app="a")
    registry.root("ribbon", lambda arg, timeout: ("a-ribbon", arg), for_app="a")
    registry.hook("ready", _a_ready, for_app="a")
    registry.fixtures("pkg.a.fixtures", for_app="a")
    return registry


def _a_ready(ctx):
    return True


def _b_ready(ctx):
    return True


def test_registrations_are_scoped_to_their_app(reg):
    assert list(reg.groups_for("a")) == ["doc"]
    assert list(reg.roots_for("a")) == ["ribbon"]
    assert reg.hooks_for("a", "ready") == [_a_ready]
    assert reg.fixture_modules_for("a") == ["pkg.a.fixtures"]

    assert reg.groups_for("other") == {}
    assert reg.roots_for("other") == {}
    assert reg.hooks_for("other", "ready") == []


def test_a_child_inherits_everything_from_its_parent(reg):
    reg.extend("b", "a")

    assert list(reg.groups_for("b")) == ["doc"]
    assert list(reg.roots_for("b")) == ["ribbon"]
    assert reg.hooks_for("b", "ready") == [_a_ready]
    assert reg.fixture_modules_for("b") == ["pkg.a.fixtures"]


def test_a_child_overrides_what_it_sets_and_adds_its_own(reg):
    reg.extend("b", "a")
    reg.group("doc", "b-doc-group", for_app="b")
    reg.group("logs", "b-logs", for_app="b")
    reg.root("ribbon", lambda arg, timeout: ("b-ribbon", arg), for_app="b")
    reg.hook("ready", _b_ready, for_app="b")
    reg.fixtures("pkg.b.fixtures", for_app="b")

    assert reg.groups_for("b") == {"doc": "b-doc-group", "logs": "b-logs"}
    assert reg.roots_for("b")["ribbon"](None, 1.0) == ("b-ribbon", None)
    # The parent's hooks run first: its pre-launch work sets the child's up.
    assert reg.hooks_for("b", "ready") == [_a_ready, _b_ready]
    assert reg.fixture_modules_for("b") == ["pkg.a.fixtures", "pkg.b.fixtures"]
    # The parent is untouched.
    assert reg.groups_for("a") == {"doc": "doc-group"}


def test_an_unknown_hook_event_is_refused(reg):
    with pytest.raises(PilotError, match="unknown hook event"):
        reg.hook("whenever", _b_ready, for_app="a")


def test_a_circular_extends_chain_is_refused(reg):
    reg.extend("a", "b")
    reg.extend("b", "a")
    with pytest.raises(PilotError, match="circular"):
        reg.lineage("a")


def test_a_plugin_root_resolves_for_its_app(reg):
    ctx = AppContext(name="a", profile=None, registry=reg)
    assert ui.resolve_root(selectors.parse("ribbon//Button"), ctx) == ("a-ribbon", None)


def test_an_unknown_root_names_the_valid_ones(reg):
    ctx = AppContext(name="a", profile=None, registry=reg)
    with pytest.raises(PilotError) as err:
        ui.resolve_root(selectors.parse("nosuchroot//Button"), ctx)

    message = str(err.value)
    assert "nosuchroot" in message
    for root in ("win", "focus", "dialog", "desktop", "ribbon"):
        assert root in message
    # No profile means no window of its own, so 'app' is not on offer.
    assert "'app'" not in message


FAKE_PLUGIN = textwrap.dedent(
    """
    def register(reg, profile):
        reg.group("widget", "widget-group", for_app=profile.name)
        reg.root("panel", lambda arg, timeout: ("panel", arg), for_app=profile.name)
        reg.fixtures("fakeplugins.gadget.fixtures", for_app=profile.name)
    """
)


def test_discovery_imports_every_plugin_and_registers_it(tmp_path, monkeypatch):
    package = tmp_path / "fakeplugins"
    plugin = package / "gadget"
    plugin.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "plugin.py").write_text(FAKE_PLUGIN, encoding="utf-8")
    (plugin / "profile.toml").write_text('exe = ["gadget.exe"]', encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(profiles, "PLUGINS_DIR", package)
    monkeypatch.delitem(sys.modules, "fakeplugins", raising=False)

    registry = Registry()
    assert discover(registry, package="fakeplugins") == ["gadget"]
    assert list(registry.groups_for("gadget")) == ["widget"]
    assert registry.roots_for("gadget")["panel"](None, 1.0) == ("panel", None)
    assert registry.fixture_modules_for("gadget") == ["fakeplugins.gadget.fixtures"]


def _user_profile(tmp_path, monkeypatch, name: str, body: str):
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    (user / f"{name}.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(profiles, "USER_DIR", user)


@pytest.mark.windows("the word plugin")
def test_a_user_profile_that_extends_a_plugin_gets_its_hooks_roots_and_groups(
    tmp_path, monkeypatch
):
    from desktoppilot.plugins.word import session

    _user_profile(tmp_path, monkeypatch, "myword", 'extends = "word"\n')
    registry = Registry()
    discover(registry)

    assert registry.lineage("myword") == ["myword", "word"]
    assert session.refuse_foreign_word in registry.hooks_for("myword", "pre_kill")
    assert "doc" in registry.groups_for("myword")
    assert "ribbon" in registry.roots_for("myword")


def test_discover_twice_registers_each_hook_once():
    registry = Registry()
    first = discover(registry)
    hooks = list(registry.hooks_for("word", "pre_kill"))

    assert discover(registry) == first
    assert registry.hooks_for("word", "pre_kill") == hooks


def test_a_malformed_profile_is_recorded_as_broken_not_raised(tmp_path, monkeypatch):
    _user_profile(tmp_path, monkeypatch, "bad", "exe = [\n")
    registry = Registry()
    discover(registry)

    assert "bad" in registry.broken and "bad.toml" in registry.broken["bad"]
    assert "bad" not in registry.profiles
    assert PLAIN_APP in registry.profiles


def test_groups_registers_a_whole_mapping_for_one_app():
    registry = Registry()
    registry.groups({"doc": "d", "ribbon": "r"}, for_app="x")
    assert registry.groups_for("x") == {"doc": "d", "ribbon": "r"}


# -------------------------------------------------------------- platforms

OTHER = "linux" if sys.platform == "win32" else "windows"
# Importing it would fail the run: discovery must not import an unsupported plugin.
REFUSING_PLUGIN = 'raise ImportError("an unsupported plugin was imported")\n'


def _fake_plugins(tmp_path, monkeypatch, plugins: dict[str, tuple[str, str]]) -> str:
    """A plugin package `fakeplatform` with one plugin per (profile, plugin.py)."""
    package = tmp_path / "fakeplatform"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, (profile, code) in plugins.items():
        plugin = package / name
        plugin.mkdir()
        (plugin / "__init__.py").write_text("", encoding="utf-8")
        (plugin / "plugin.py").write_text(code, encoding="utf-8")
        (plugin / "profile.toml").write_text(profile, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(profiles, "PLUGINS_DIR", package)
    monkeypatch.setattr(profiles, "USER_DIR", tmp_path / "user")
    monkeypatch.delitem(sys.modules, "fakeplatform", raising=False)
    return "fakeplatform"


def test_a_plugin_for_another_platform_is_listed_unsupported_and_never_imported(
    tmp_path, monkeypatch
):
    package = _fake_plugins(
        tmp_path,
        monkeypatch,
        {
            "gadget": ('exe = ["gadget"]', FAKE_PLUGIN.replace("fakeplugins", "fakeplatform")),
            "elsewhere": (f'exe = ["e"]\nplatforms = ["{OTHER}"]', REFUSING_PLUGIN),
        },
    )
    registry = Registry()

    assert discover(registry, package=package) == ["gadget"]
    assert "for " + OTHER + " only" in registry.unsupported["elsewhere"]
    assert "elsewhere" not in registry.profiles and "elsewhere" not in registry.broken
    assert "gadget" in registry.profiles


def test_a_profile_extending_an_unsupported_app_is_unsupported_too(tmp_path, monkeypatch):
    package = _fake_plugins(
        tmp_path,
        monkeypatch,
        {"elsewhere": (f'exe = ["e"]\nplatforms = ["{OTHER}"]', REFUSING_PLUGIN)},
    )
    user = tmp_path / "user"
    user.mkdir()
    (user / "mine.toml").write_text('extends = "elsewhere"\n', encoding="utf-8")
    (user / "here.toml").write_text(f'exe = ["h"]\n[{OTHER}]\nexe = ["x"]\n', encoding="utf-8")
    registry = Registry()
    discover(registry, package=package)

    assert {"elsewhere", "mine"} <= set(registry.unsupported)
    assert "here" not in registry.unsupported
    assert registry.profiles["here"].exe == ["h"]  # the other platform's table is not applied


def test_the_shipped_windows_only_apps_are_unsupported_off_windows():
    registry = Registry()
    discover(registry)
    windows_only = {"word", "notepad"}
    if sys.platform == "win32":
        assert registry.unsupported == {}
        assert windows_only <= set(registry.profiles)
    else:
        assert set(registry.unsupported) == windows_only
        assert not windows_only & set(registry.profiles)
        assert not windows_only & set(registry.plugins)
        assert {"code", "vscode"} <= set(registry.profiles)


# ------------------------------------------------------- external plugins

EXTERNAL_PLUGIN = textwrap.dedent(
    """
    import typer

    widget = typer.Typer()


    @widget.command()
    def ping():
        print("pong")


    def register(reg, profile):
        reg.group("widget", widget, for_app=profile.name)
        reg.fixtures("extpkg.gadget.fixtures", for_app=profile.name)
    """
)


# The real reader, before tests/unit/conftest.py stubs it out.
REAL_ENTRY_POINTS = profiles._entry_points


@pytest.fixture
def external(tmp_path, monkeypatch):
    """Declare external plugins: {entry name: (package, profile, plugin.py or
    None for no folder)}. Each package lives under tmp_path/extpkg."""
    root = tmp_path / "ext"
    package = root / "extpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(profiles, "USER_DIR", tmp_path / "user")
    for module in [m for m in sys.modules if m == "extpkg" or m.startswith("extpkg.")]:
        monkeypatch.delitem(sys.modules, module)
    profiles._external_plugins.cache_clear()

    def declare(plugins: dict[str, tuple[str, str | None, str | None]]):
        entries = []
        for name, (value, profile, code) in plugins.items():
            entries.append(metadata.EntryPoint(name, value, profiles.ENTRY_POINT_GROUP))
            folder = root / value.replace(".", "/")
            if code is None:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "__init__.py").write_text("", encoding="utf-8")
            (folder / "plugin.py").write_text(code, encoding="utf-8")
            if profile is not None:
                (folder / "profile.toml").write_text(profile, encoding="utf-8")

        def fake_entry_points(**kwargs):
            assert kwargs == {"group": profiles.ENTRY_POINT_GROUP}
            return entries

        monkeypatch.setattr(profiles, "_entry_points", REAL_ENTRY_POINTS)
        monkeypatch.setattr(profiles.metadata, "entry_points", fake_entry_points)
        profiles._external_plugins.cache_clear()
        return root

    yield declare
    profiles._external_plugins.cache_clear()


def test_an_external_plugin_is_discovered_and_gets_its_cli_group(external):
    from desktoppilot.cli import build

    root = external({"gadget": ("extpkg.gadget", 'extends = "code"\n', EXTERNAL_PLUGIN)})
    registry = Registry()
    discover(registry)

    assert "gadget" in registry.plugins and "gadget" in registry.profiles
    assert registry.lineage("gadget") == ["gadget", "code"]
    assert list(registry.groups_for("gadget")) == ["widget"]
    assert registry.fixture_modules_for("gadget") == ["extpkg.gadget.fixtures"]
    assert profiles.plugin_folders()["gadget"] == root / "extpkg" / "gadget"

    result = CliRunner().invoke(build(["gadget"]), ["gadget", "widget", "ping"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.output
    notes = CliRunner().invoke(build(["--help"]), ["--help"], env={"COLUMNS": "400"}).output
    assert "NOTES.md" in notes and "gadget" in notes


def test_an_external_plugin_for_another_platform_is_never_imported(external):
    external(
        {
            "elsewhere": (
                "extpkg.elsewhere",
                f'exe = ["e"]\nplatforms = ["{OTHER}"]',
                REFUSING_PLUGIN,
            )
        }
    )
    registry = Registry()
    discover(registry)

    assert "for " + OTHER + " only" in registry.unsupported["elsewhere"]
    assert "elsewhere" not in registry.broken and "elsewhere" not in registry.plugins
    assert "extpkg.elsewhere.plugin" not in sys.modules


@pytest.mark.parametrize(
    ("value", "profile", "code", "why"),
    [
        ("extpkg.gadget", 'exe = ["g"]\n', 'raise RuntimeError("boom")\n', "RuntimeError: boom"),
        ("extpkg.gadget", 'exe = ["g"]\n', "x = 1\n", "has no attribute 'register'"),
        ("extpkg.gadget", None, EXTERNAL_PLUGIN, "no profile.toml"),
        ("extpkg.missing", None, None, "not an importable package"),
        ("nosuchpkg.gadget", None, None, "ModuleNotFoundError"),
    ],
)
def test_a_broken_external_plugin_is_recorded_not_raised(external, value, profile, code, why):
    external({"gadget": (value, profile, code)})
    registry = Registry()
    discover(registry)

    assert why in registry.broken[f"gadget={value}"]
    assert "gadget" not in registry.broken
    assert "gadget" not in registry.plugins and "gadget" not in registry.profiles
    assert PLAIN_APP in registry.profiles


def test_an_external_plugin_with_a_malformed_profile_is_a_broken_profile(external):
    external({"gadget": ("extpkg.gadget", "exe = [\n", EXTERNAL_PLUGIN)})
    registry = Registry()
    discover(registry)

    assert "profile.toml" in registry.broken["gadget"]
    assert "gadget" not in registry.plugins
    assert "extpkg.gadget.plugin" not in sys.modules


def test_a_parent_package_that_does_not_import_is_broken_not_raised(tmp_path, external):
    root = external({})
    parent = root / "badparent"
    (parent / "gadget").mkdir(parents=True)
    (parent / "__init__.py").write_text('raise ImportError("needs Windows")\n', encoding="utf-8")
    (parent / "gadget" / "__init__.py").write_text("", encoding="utf-8")
    (parent / "gadget" / "profile.toml").write_text('exe = ["g"]\n', encoding="utf-8")
    entry = metadata.EntryPoint("gadget", "badparent.gadget", profiles.ENTRY_POINT_GROUP)
    profiles._external_plugins.cache_clear()
    registry = Registry()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(profiles.metadata, "entry_points", lambda **kwargs: [entry])
        discover(registry)

    assert "ImportError: needs Windows" in registry.broken["gadget=badparent.gadget"]
    assert "gadget" not in registry.profiles


def test_a_duplicate_external_name_keeps_the_first_and_breaks_the_second(external):
    root = external({"gadget": ("extpkg.gadget", 'exe = ["first"]\n', EXTERNAL_PLUGIN)})
    other = root / "extpkg" / "other"
    other.mkdir()
    (other / "__init__.py").write_text("", encoding="utf-8")
    (other / "plugin.py").write_text(REFUSING_PLUGIN, encoding="utf-8")
    (other / "profile.toml").write_text('exe = ["second"]\n', encoding="utf-8")
    entries = list(profiles.metadata.entry_points(group=profiles.ENTRY_POINT_GROUP))
    entries.append(metadata.EntryPoint("gadget", "extpkg.other", profiles.ENTRY_POINT_GROUP))
    profiles._external_plugins.cache_clear()
    registry = Registry()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(profiles.metadata, "entry_points", lambda **kwargs: entries)
        discover(registry)
        assert profiles.load("gadget").exe == ["first"]

    assert "already taken by 'extpkg.gadget'" in registry.broken["gadget=extpkg.other"]
    assert "gadget" in registry.plugins and "gadget" not in registry.broken


def test_a_user_profile_still_loads_when_its_external_plugin_is_broken(tmp_path, external):
    external({"gadget": ("extpkg.gadget", 'exe = ["external"]\n', 'raise RuntimeError("x")\n')})
    user = tmp_path / "user"
    user.mkdir()
    (user / "gadget.toml").write_text('exe = ["user"]\n', encoding="utf-8")
    registry = Registry()
    discover(registry)

    assert "gadget=extpkg.gadget" in registry.broken
    assert registry.profiles["gadget"].exe == ["user"]
    assert "gadget" not in registry.plugins


def test_a_broken_external_plugin_has_a_cli_command_that_explains_it(external):
    from desktoppilot.cli import build

    external({"gadget": ("extpkg.gadget", 'exe = ["g"]\n', 'raise RuntimeError("boom")\n')})
    tree = build(["apps"])

    listed = CliRunner().invoke(tree, ["apps"])
    assert listed.exit_code == 0, listed.output
    assert "gadget=extpkg.gadget" in json.loads(listed.output)["broken"]
    explained = CliRunner().invoke(tree, ["gadget=extpkg.gadget", "app", "launch"])
    assert explained.exit_code == 1
    assert "RuntimeError: boom" in explained.output


def test_no_external_plugins_changes_nothing(external):
    from desktoppilot import cli

    before = Registry()
    discover(before)
    external({})
    after = Registry()
    discover(after)

    assert after.plugins == before.plugins
    assert set(after.profiles) == set(before.profiles)
    assert after.broken == before.broken
    assert cli.epilog(after) == cli.EPILOG


@pytest.mark.parametrize(
    ("shipped", "value"),
    [("code", "extpkg.code"), ("notepad", "extpkg.notepad"), ("vscode", "vscode")],
)
def test_an_external_plugin_named_like_a_shipped_app_is_broken(external, shipped, value):
    external({shipped: (value, 'exe = ["external"]\n', REFUSING_PLUGIN)})
    registry = Registry()
    discover(registry)

    message = registry.broken[f"{shipped}={value}"]
    assert f"name {shipped!r} already shipped by desktoppilot" in message
    assert shipped not in registry.broken
    assert profiles.load(shipped).exe != ["external"]
    assert profiles.plugin_folders().get(shipped) in (None, profiles.PLUGINS_DIR / shipped)


def test_a_user_profile_overrides_an_external_one(tmp_path, external):
    external({"gadget": ("extpkg.gadget", 'exe = ["external"]\n', EXTERNAL_PLUGIN)})
    assert profiles.load("gadget").exe == ["external"]
    user = tmp_path / "user"
    user.mkdir()
    (user / "gadget.toml").write_text('exe = ["user"]\n', encoding="utf-8")

    assert profiles.load("gadget").exe == ["user"]
