"""What a plugin adds to the core, and for which app.

Everything a plugin contributes - CLI groups, selector roots, lifecycle hooks,
pytest fixture modules - is keyed by app name, so one process can carry several
apps without any of them leaking into another. `extends` makes a child app see
its parent's contributions; the child wins on a name clash.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import PilotError
from .native import PLATFORM
from .profiles import Profile

# Every hook is fn(ctx, **kwargs). pre_kill gets pids= and force= and may
# REFUSE by raising. The other four act on the app; dialog_policy and status
# answer a question - the prompts this app knows, extra JSON keys for `status`.
HOOK_EVENTS = (
    "pre_launch",
    "post_launch",
    "ready",
    "quit",
    "pre_kill",
    "dialog_policy",
    "status",
)

# resolver(root_arg, timeout) -> a UIA control; one that also takes a `probe`
# keyword can be asked to look once instead of waiting (ui.resolve_root).
RootResolver = Callable[..., Any]


def takes_probe(resolver: RootResolver) -> bool:
    try:
        return "probe" in inspect.signature(resolver).parameters
    except (TypeError, ValueError):
        return False


class Registry:
    def __init__(self) -> None:
        self._groups: dict[str, dict[str, Any]] = {}
        self._roots: dict[str, dict[str, RootResolver]] = {}
        self._hooks: dict[str, dict[str, list[Callable]]] = {}
        self._fixtures: dict[str, list[str]] = {}
        self._parents: dict[str, str] = {}
        self.discovered = False
        self.plugins: list[str] = []
        self.profiles: dict[str, Profile] = {}
        # name -> why it could not load: listed by `pilot apps`, never fatal
        self.broken: dict[str, str] = {}
        # name -> why it is not for this platform: listed by `pilot apps`
        self.unsupported: dict[str, str] = {}

    def extend(self, app: str, parent: str | None) -> None:
        if parent:
            self._parents[app] = parent

    def group(self, name: str, app, *, for_app: str) -> None:
        """A Typer sub-command group, e.g. `pilot word doc`."""
        self._groups.setdefault(for_app, {})[name] = app

    def groups(self, mapping: dict[str, Any], *, for_app: str) -> None:
        for name, app in mapping.items():
            self.group(name, app, for_app=for_app)

    def root(self, name: str, resolver: RootResolver, *, for_app: str) -> None:
        """A selector root: resolver(root_arg, timeout) -> a UIA control."""
        self._roots.setdefault(for_app, {})[name] = resolver

    def hook(self, event: str, fn: Callable, *, for_app: str) -> None:
        if event not in HOOK_EVENTS:
            raise PilotError(f"unknown hook event {event!r}; known: {list(HOOK_EVENTS)}")
        self._hooks.setdefault(for_app, {}).setdefault(event, []).append(fn)

    def fixtures(self, module: str, *, for_app: str) -> None:
        """A module name pytest should load as a plugin for this app."""
        self._fixtures.setdefault(for_app, []).append(module)

    def lineage(self, app: str) -> list[str]:
        """The app and its ancestors, parent last."""
        chain = [app]
        seen = {app}
        while chain[-1] in self._parents:
            parent = self._parents[chain[-1]]
            if parent in seen:
                raise PilotError(f"circular extends chain at {parent!r}")
            seen.add(parent)
            chain.append(parent)
        return chain

    def groups_for(self, app: str) -> dict[str, Any]:
        return self._merged(self._groups, app)

    def roots_for(self, app: str) -> dict[str, RootResolver]:
        return self._merged(self._roots, app)

    def hooks_for(self, app: str, event: str) -> list[Callable]:
        """Ancestors first: a parent's pre-launch work runs before the child's."""
        out: list[Callable] = []
        for name in reversed(self.lineage(app)):
            out += self._hooks.get(name, {}).get(event, [])
        return out

    def fixture_modules_for(self, app: str) -> list[str]:
        out: list[str] = []
        for name in reversed(self.lineage(app)):
            out += [m for m in self._fixtures.get(name, []) if m not in out]
        return out

    def _merged(self, store: dict[str, dict], app: str) -> dict:
        merged: dict = {}
        for name in reversed(self.lineage(app)):
            merged |= store.get(name, {})
        return merged


@dataclass
class AppContext:
    """Which app a command is acting on. Passed explicitly, never stored: the
    command line names the app, so there is no attached-app state to go stale."""

    name: str
    profile: Profile | None
    registry: Registry = field(default_factory=Registry)
    shot_on_failure: bool = True


def discover(reg: Registry, *, package: str = "desktoppilot.plugins") -> list[str]:
    """Import every plugin subpackage, and every plugin another package
    declares (see profiles.external_plugins), and let it register itself, then load
    every profile: the lineage comes from each one's `extends`, so a user
    profile that extends a plugin's app gets its hooks, roots and groups.
    A profile that does not load lands in `reg.broken` instead of raising;
    one whose `platforms` leave this platform out lands in `reg.unsupported`,
    and its plugin module is never imported (it may need Windows to import).
    Idempotent: a second call must not register every hook twice."""
    from . import profiles as profiles_mod

    if reg.discovered:
        return list(reg.plugins)
    plugins = importlib.import_module(package)
    for info in pkgutil.iter_modules(plugins.__path__):
        if not info.ispkg:
            continue
        try:
            profile = profiles_mod.load(info.name)
        except PilotError as exc:
            reg.broken[info.name] = str(exc)
            continue
        if not profile.supported:
            reg.unsupported[info.name] = _unsupported(profile)
            continue
        module = importlib.import_module(f"{package}.{info.name}.plugin")
        module.register(reg, profile)
        reg.plugins.append(info.name)
    failed = _discover_external(reg)
    for name in profiles_mod.available():
        if name in reg.broken or name in reg.unsupported:
            continue
        if name in failed and not _user_overrides(name):
            continue  # its own profile.toml, without the plugin it needs
        try:
            profile = profiles_mod.load(name)
        except PilotError as exc:
            reg.broken[name] = str(exc)
            continue
        if not profile.supported:
            reg.unsupported[name] = _unsupported(profile)
            continue
        reg.profiles[name] = profile
        reg.extend(name, profile.extends)
    reg.discovered = True
    return list(reg.plugins)


def _discover_external(reg: Registry) -> set[str]:
    """The plugins other installed packages declare in the `desktoppilot.plugins`
    entry-point group. Same rules as a built-in, but a failure to import or
    register lands in `reg.broken` too, under profiles.broken_key: someone
    else's package must not stop the tool. Returns the names that failed so,
    which keep no profile of their own (a user profile still loads)."""
    from . import profiles as profiles_mod

    found, broken = profiles_mod.external_plugins()
    reg.broken |= broken
    failed: set[str] = set()
    for name, plugin in found.items():
        try:
            profile = profiles_mod.load(name)
        except PilotError:
            continue  # the profile pass below records it under its name
        if not profile.supported:
            reg.unsupported[name] = _unsupported(profile)
            continue
        try:
            module = importlib.import_module(f"{plugin.package}.plugin")
            module.register(reg, profile)
        except Exception as exc:
            reg.broken[profiles_mod.broken_key(name, plugin.package)] = (
                f"external plugin {name!r} ({plugin.package}): {type(exc).__name__}: {exc}"
            )
            failed.add(name)
            continue
        reg.plugins.append(name)
    return failed


def _user_overrides(name: str) -> bool:
    from . import profiles as profiles_mod

    path = profiles_mod._path_of(name)
    return path is not None and path.parent == profiles_mod.USER_DIR


def _unsupported(profile: Profile) -> str:
    return f"for {', '.join(profile.platforms)} only; this is {PLATFORM}"
