"""Finding the Codex panel: its webview frames in each VS Code window, the
editor panel before the sidebar, and the `codex` selector root."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from itertools import islice

from desktoppilot.core import native, selectors, ui
from desktoppilot.core.errors import Ambiguous, NotPresent
from desktoppilot.core.native import auto
from desktoppilot.core.registry import AppContext

from . import codex
from . import panel as P
from .common import ancestors
from .common import read as read_attr

# A display string, a tuple of accepted texts (see panel.py): the webview
# frame's name, which is the editor tab's or the view's title.
FRAME_NAME = ("Codex",)
FRAME_DEPTH = 25
# The webview's inner <iframe id="active-frame">: UIA makes it a Document;
# AT-SPI gives it the role `internal frame`, a Window to the selectors.
FRAME_TYPE = "Document" if native.WINDOWS else "Window"
_FRAME = selectors.parse(f"app//{FRAME_TYPE}[id='active-frame']").steps[0]
# A view's frame sits this many levels under its `pane-body`, at most. AT-SPI
# shows the <body> of each document as a level of its own.
SIDEBAR_LEVELS = 7 if native.WINDOWS else 9
OVERLAY = "webview-overlay-content"  # the class of the layer a webview is drawn in
_PANE_BODY = selectors.parse("app//*[class~='pane-body']").steps[0]


@dataclass
class Panel:
    window: object
    root: object  # the frame's RootWebArea
    sidebar: bool = False

    def nodes(self) -> list[P.Node]:
        return codex.snapshot(self.root)

    def title(self) -> str:
        return read_attr(self.root, "Name")

    def owns_focus(self) -> bool:
        return codex._contains(self.root, auto.GetFocusedControl())


def _in_sidebar(frame, bodies: Callable[[], list] | None = None) -> bool:
    """A view's webview sits in a VS Code `pane-body`; an editor's does not.
    Newer VS Code draws every webview in an overlay (`webview-overlay-content`)
    laid over its placeholder, outside the view: then the frame is a view's
    when its centre lies in one of `bodies()`, the pane-body rectangles."""
    parents = list(islice(ancestors(frame, SIDEBAR_LEVELS), 1, None))
    classes = [read_attr(c, "ClassName") for c in parents]
    if any("pane-body" in cls for cls in classes):
        return True
    if bodies is None or not any(OVERLAY in cls for cls in classes):
        return False
    centre = _centre(frame)
    return centre is not None and any(contains(rect, centre) for rect in bodies())


def _centre(control) -> tuple[int, int] | None:
    try:
        rect = control.BoundingRectangle
    except Exception:  # noqa: BLE001 - element went stale mid-read
        return None
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def contains(rect, point: tuple[int, int]) -> bool:
    """Is `point` inside `rect` (left, top, right, bottom; right and bottom
    exclusive)?"""
    left, top, right, bottom = rect
    return left <= point[0] < right and top <= point[1] < bottom


def _pane_bodies(window) -> list[tuple[int, int, int, int]]:
    """The rectangle of every visible view body (`pane-body`) in the window."""
    found = []
    for body in ui.search(window, _PANE_BODY, max_depth=FRAME_DEPTH):
        with suppress(Exception):  # element went stale mid-read
            rect = body.BoundingRectangle
            if rect.right > rect.left and rect.bottom > rect.top:
                found.append((rect.left, rect.top, rect.right, rect.bottom))
    return found


def is_web_root(control) -> bool:
    """The frame's own document: UIA names it RootWebArea; AT-SPI gives it
    the role `document web` and no id."""
    if native.WINDOWS:
        return read_attr(control, "AutomationId") == "RootWebArea"
    return read_attr(control, "LocalizedControlType") == "document web"


def panels_in(window) -> list[Panel]:
    """Every visible, working Codex frame in one VS Code window, editor panels
    first. A frame whose renderer never came up ("Codex could not start") is
    skipped: that is the sidebar when the extension timed its webview out at
    VS Code's start, and it stays that way until VS Code reloads."""
    found = []
    bodies = cache(lambda: _pane_bodies(window))
    for frame in ui.search(window, _FRAME, max_depth=FRAME_DEPTH):
        if read_attr(frame, "Name") not in FRAME_NAME or not ui.is_visible(frame):
            continue
        for child in frame.GetChildren():
            if is_web_root(child) and read_attr(child, "Name") not in P.BROKEN:
                found.append(Panel(window=window, root=child, sidebar=_in_sidebar(frame, bodies)))
    return sorted(found, key=lambda p: p.sidebar)


def _pick(found: list[Panel]) -> Panel | None:
    """The editor panel if there is one, else the sidebar; two of a kind is
    Ambiguous (a split editor)."""
    for kind in (False, True):
        same = [p for p in found if p.sidebar == kind]
        if len(same) > 1:
            raise Ambiguous(
                f"{len(same)} Codex panels are visible in one window; close or hide all but one"
            )
        if same:
            return same[0]
    return None


def _locate(ctx: AppContext, window: str | None) -> tuple[object | None, Panel | None]:
    """The window to work in and its Codex panel. Two windows with a panel is
    Ambiguous unless `window` names one: either could be another project's.
    With several windows and no panel, the window is None."""
    found = [(w, _pick(codex.panels_in(w))) for w in codex.main_windows(ctx, window)]
    with_panel = [(w, p) for w, p in found if p is not None]
    if len(with_panel) > 1:
        titles = [read_attr(w, "Name") for w, _ in with_panel]
        raise Ambiguous(
            f"{len(with_panel)} VS Code windows have a Codex panel: {titles}; pass --window "
            "with part of the one to use"
        )
    if with_panel:
        return with_panel[0]
    return found[0] if len(found) == 1 else (None, None)


def find_panel(ctx: AppContext, window: str | None = None) -> Panel:
    _, pan = codex._locate(ctx, window)
    if pan is None:
        raise NotPresent("no Codex panel is open; run `pilot vscode codex open`")
    return pan


def resolve_root(ctx: AppContext, timeout: float, probe: bool = False):
    """The `codex` selector root: the panel's RootWebArea."""
    return ui.present(
        lambda: codex.find_panel(ctx).root, timeout=timeout, what="the Codex panel", probe=probe
    )
