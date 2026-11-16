"""UIA helpers that codex, git and palette share: guarded reads, the
ancestor walk, focus-guarded keys, and the choice of VS Code window."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from desktoppilot.core import apps
from desktoppilot.core.errors import Ambiguous, NotPresent, PilotError
from desktoppilot.core.native import auto
from desktoppilot.core.registry import AppContext


def read(control, attr: str) -> str:
    try:
        return getattr(control, attr) or ""
    except Exception:  # noqa: BLE001 - element went stale mid-read
        return ""


def ancestors(control, limit: int) -> Iterator:
    """`control`, then its parents, `limit` elements at most; a stale element
    ends the walk."""
    for _ in range(limit):
        if control is None:
            return
        yield control
        try:
            control = control.GetParentControl()
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            return


def send_keys_if(focus_ok: Callable[[], bool], keys: str, refusal: str, **send) -> None:
    """SendKeys goes to whatever has focus, and in the user's editor that is
    often another extension's composer: refuse unless `focus_ok()`."""
    if not focus_ok():
        raise PilotError(refusal)
    auto.SendKeys(keys, **send)


# ------------------------------------------------------------ the window


def _matching(ctx: AppContext, window: str | None) -> tuple[list[dict], list[str]]:
    """The main windows `window` names (all of them for None), and every title."""
    profile = apps.profile_of(ctx)
    apps.require_running(profile)
    listed = apps.windows(profile, main_only=True)
    if not listed:
        raise NotPresent(f"{profile.name} has no main window")
    titles = [apps.strip_title(profile, w["title"]) for w in listed]
    if window is None:
        return listed, titles
    hits = [w for w, t in zip(listed, titles, strict=True) if window.casefold() in t.casefold()]
    if not hits:
        raise NotPresent(f"no VS Code window title contains {window!r}; there are {titles}")
    if len(hits) > 1:
        matched = [t for t in titles if window.casefold() in t.casefold()]
        raise Ambiguous(f"{window!r} matches {len(hits)} windows: {matched}")
    return hits, titles


def main_windows(ctx: AppContext, window: str | None = None) -> list:
    """Every main window, or the one whose title contains `window`."""
    hits, _ = _matching(ctx, window)
    return [auto.ControlFromHandle(w["hwnd"]) for w in hits]


def pick_window(ctx: AppContext, window: str | None):
    """The one main window, or the one whose title contains `window`. Never
    the foreground one by default: with several open, that may be another
    project's."""
    hits, titles = _matching(ctx, window)
    if len(hits) > 1:
        raise Ambiguous(
            f"{len(hits)} VS Code windows are open: {titles}; pass --window "
            "with part of the one to use"
        )
    return auto.ControlFromHandle(hits[0]["hwnd"])
