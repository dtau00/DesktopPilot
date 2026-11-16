"""The core selector roots (`app`, `win:NAME`, `focus`, `dialog`, `desktop`) and
how a root name becomes a control, plugin roots first."""

from __future__ import annotations

from . import apps, selectors, ui
from . import dialogs as dialogs_mod
from .errors import NoApp, NotPresent, PilotError, Timeout
from .native import auto, win32gui
from .registry import AppContext, takes_probe
from .selectors import Selector
from .util import wait_until

DEFAULT_TIMEOUT = 15.0
CORE_ROOTS = ("app", "win", "focus", "dialog", "desktop")
# The budget of one presence probe (is_gone). A core root looks once - a
# window enumeration, no wait - and a plugin root that takes `probe` spends it
# on its own walks; either way the walk under the root is cut at it.
PROBE_TIMEOUT = 5.0
# A resolver that cannot be told it is probing waits to appear for its whole
# timeout, so a probe gives it only this much.
LEGACY_PROBE_TIMEOUT = 1.0
# How long a selector wait goes on while the app is not running: a window
# that is restarting gets it; an app that is gone fails then, not at timeout.
APP_GRACE = 2.0


def present(fn, *, timeout: float, what: str, probe: bool = False):
    """wait_until for a root: not appearing in time means not there. A probe
    looks once instead of waiting. No app ends it at once: the selector wait
    above it owns the grace."""
    if probe:
        found = fn()
        if not found:
            raise NotPresent(what + " is not there")
        return found
    try:
        return wait_until(fn, timeout=timeout, what=what, fatal=(NoApp,))
    except Timeout as exc:
        raise NotPresent(str(exc)) from exc


def app_window(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT, probe: bool = False):
    profile = apps.profile_of(ctx)
    apps.require_running(profile)
    if probe:
        candidates = apps.windows(profile, main_only=True)
        if not candidates:
            raise NotPresent(profile.name + " has no main window")
        hwnd = max(candidates, key=lambda w: w["rect"][2] * w["rect"][3])["hwnd"]
    else:
        try:
            hwnd = apps.main_hwnd(profile, timeout=timeout)
        except Timeout as exc:
            raise NotPresent(str(exc)) from exc
    control = auto.ControlFromHandle(hwnd)
    if control is None:
        raise PilotError(profile.name + " main window has no UIA element")
    return control


def named_window(
    ctx: AppContext,
    title: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe: bool = False,
):
    """Any top-level window of the app, matched on title."""

    def find_it():
        if ctx.profile is not None:
            apps.require_running(ctx.profile)
        needle = (title or "").casefold()
        for win in apps.windows(ctx.profile):
            if not win["title"]:
                continue
            if needle and needle not in win["title"].casefold():
                continue
            return auto.ControlFromHandle(win["hwnd"])
        return None

    return present(find_it, timeout=timeout, what="window " + repr(title or "any"), probe=probe)


def focused_window(*, timeout: float = DEFAULT_TIMEOUT, probe: bool = False):
    def find_it():
        hwnd = win32gui.GetForegroundWindow()
        return auto.ControlFromHandle(hwnd) if hwnd else None

    return present(find_it, timeout=timeout, what="the foreground window", probe=probe)


def dialog_window(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT, probe: bool = False):
    classes = dialogs_mod.dialog_classes(ctx.profile)
    pids = apps.pids(ctx.profile) if ctx.profile else None

    def find_dialog():
        if ctx.profile is not None:
            apps.require_running(ctx.profile)
        found = dialogs_mod.dialog_windows(pids, classes=classes)
        return auto.ControlFromHandle(found[0]["hwnd"]) if found else None

    return present(find_dialog, timeout=timeout, what="a dialog", probe=probe)


def valid_roots(ctx: AppContext) -> list[str]:
    """Which roots this app accepts - core ones plus whatever its plugin adds."""
    core = [r for r in CORE_ROOTS if r != "app" or (ctx and ctx.profile)]
    plugin = sorted(ctx.registry.roots_for(ctx.name)) if ctx else []
    return core + [r for r in plugin if r not in core]


def resolve_root(
    sel: Selector, ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT, probe: bool = False
):
    """The root control. Normally it waits up to `timeout` for the root to
    appear. `probe=True` asks only whether it is there now: the root looks
    once and raises NotPresent if it is not (WalkCut if a plugin root's walk
    was cut). A core root's look is a window enumeration and needs no budget.

    A plugin resolver opts in by taking a `probe` keyword; one that does not
    waits to appear, so a probe gives it LEGACY_PROBE_TIMEOUT at most."""
    # A plugin's root is consulted FIRST, so an app can override a core one:
    # `word`'s "app" is the OpusApp frame - not simply the largest window its
    # process owns - and its "win:NAME" must exclude that frame.
    resolver = ctx.registry.roots_for(ctx.name).get(sel.root) if ctx else None
    if resolver is not None:
        if not probe:
            return resolver(sel.root_arg, timeout)
        if takes_probe(resolver):
            return resolver(sel.root_arg, timeout, probe=True)
        return resolver(sel.root_arg, min(timeout, LEGACY_PROBE_TIMEOUT))

    if sel.root == "app":
        return ui.app_window(ctx, timeout=timeout, probe=probe)
    if sel.root == "win":
        return ui.named_window(ctx, sel.root_arg, timeout=timeout, probe=probe)
    if sel.root == "focus":
        return ui.focused_window(timeout=timeout, probe=probe)
    if sel.root == "dialog":
        return dialog_window(ctx, timeout=timeout, probe=probe)
    if sel.root == "desktop":
        return auto.GetRootControl()

    raise _unknown_root(sel, ctx)


def _unknown_root(sel: Selector, ctx: AppContext) -> PilotError:
    message = (
        f"unknown selector root {sel.root!r} for app "
        f"{getattr(ctx, 'name', '?')!r}; valid roots: {valid_roots(ctx)}"
    )
    if ctx is not None and not ctx.registry.discovered and not ctx.registry.roots_for(ctx.name):
        message += (
            "; plugin roots are not loaded; call discover(REGISTRY) first "
            "(desktoppilot.core.registry.discover - the CLI and the pytest plugin do)"
        )
    return PilotError(message)


def desktop_window():
    return auto.GetRootControl()


def _check_root(sel: Selector, ctx: AppContext) -> None:
    """Refuse an unknown root before any waiting, so a typo fails at once."""
    if sel.root in CORE_ROOTS or (ctx and sel.root in ctx.registry.roots_for(ctx.name)):
        if sel.root == "app":
            apps.profile_of(ctx)
        return
    raise _unknown_root(sel, ctx)


def main_window(ctx: AppContext | None, *, timeout: float = DEFAULT_TIMEOUT):
    """The app's own window, through the `app` root - so an app that overrides
    that root is screenshotted, focused and restored through its own
    definition too."""
    return ui.resolve_root(selectors.parse("app"), ctx, timeout=timeout)
