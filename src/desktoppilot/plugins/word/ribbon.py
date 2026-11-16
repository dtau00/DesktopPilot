"""The ribbon: its tabs and controls, the open drop-down, and a click on
'Tab>Control'."""

from __future__ import annotations

import time

from desktoppilot.core import ui
from desktoppilot.core.errors import AppNotRunning, PilotError, Timeout, WalkCut
from desktoppilot.core.registry import AppContext
from desktoppilot.core.selectors import Predicate, Step
from desktoppilot.core.util import collapse_whitespace, wait_until

from . import roots
from .frame import DEFAULT_TIMEOUT
from .session import NotOurWord

TAB_RETRY = 10.0
# A COM error cuts a walk short, so one empty walk proves nothing.
TAB_MIN_WALKS = 3
TAB_POLL = 0.25
POPUP_CLASS = "NetUIToolWindow"
# The Lower Ribbon pane, which holds the selected tab's controls. Its name is
# localized; its class is not (the tab strip shares it, but is a Tab).
LOWER_RIBBON_CLASS = "NetUIPanViewer"
# Waiting cannot fix these, so the tab walks stop on them at once.
NO_WORD = (AppNotRunning, NotOurWord)


def ribbon(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT):
    """The ribbon pane. A busy Word can answer the walk with nothing for a
    while, so it is polled rather than looked up once. See _poll_frame_walk
    for when it says "not found" and when WalkCut."""
    hit = roots._poll_frame_walk(ctx, "Ribbon", depth=6, what="the ribbon", timeout=timeout)
    if hit is None:
        raise PilotError("ribbon not found in the Word window")
    return hit


def popup_roots(ctx: AppContext, *, deadline: float) -> list:
    """Windows a ribbon drop-down can live in: Word hangs one in its own
    top-level NetUIToolWindow, and another inside the frame. A frame walk
    past `deadline` raises WalkCut: an unread popup is not an absent one."""
    found = ui.popup_roots(deadline, classes=(POPUP_CLASS,))
    try:
        frame = roots.word_window(ctx, timeout=roots._left(deadline))
    except WalkCut:
        raise
    except Timeout as exc:
        raise WalkCut("no Word frame in time to look for a ribbon popup; Word is busy") from exc
    step = Step("*", True, predicates=[Predicate("class_name", "~=", POPUP_CLASS)])
    return found + ui.search(frame, step, max_depth=4, deadline=deadline)


def popup(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT, probe: bool = False):
    """The open ribbon drop-down, as a selector root."""
    deadline = time.monotonic() + timeout
    return ui.present(
        lambda: ui.first(popup_roots(ctx, deadline=deadline)),
        timeout=timeout,
        what="an open ribbon popup",
        probe=probe,
    )


def send_keys(ctx: AppContext, keys: str) -> None:
    """Keystrokes only reach Word when Word has the foreground."""
    ui.activate_app(ctx)
    ui.send_keys(keys)


def ribbon_tabs(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """The ribbon's tab names, from the first walk that lists any. A busy Word
    answers a cut walk or a ribbon with no tabs built, so those are walked
    again; none listing tabs by `timeout` raises WalkCut, never []."""
    start = time.monotonic()
    deadline = start + timeout
    walks = 0
    busy = ""
    while walks == 0 or time.monotonic() < deadline:
        walks += 1
        try:
            root = roots.ribbon(ctx, timeout=roots._left(deadline))
            names = [
                n for n in (collapse_whitespace(t.Name) for t in _tab_items(root, deadline)) if n
            ]
            if names:
                return names
            busy = "the ribbon listed no tabs"
        except NO_WORD:
            raise
        except Exception as exc:  # noqa: BLE001 - a cut walk or a busy Word; walk again
            busy = str(exc)
        time.sleep(min(roots.TAB_POLL, roots._left(deadline)))
    raise WalkCut(
        f"no ribbon walk listed tabs in {time.monotonic() - start:.1f}s (budget {timeout:.1f}s): "
        f"Word is busy ({busy or 'no walk left'}; {walks} walks)"
    )


def ribbon_controls(
    ctx: AppContext, tab: str | None = None, *, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """Labels of the controls on the active ribbon, or on the named tab's
    panel once it is built. An unknown tab raises with the tabs Word has."""
    deadline = time.monotonic() + timeout
    if not tab:
        return _labels(roots.ribbon(ctx, timeout=timeout), deadline)
    root, tabs = roots._find_tab(ctx, tab, timeout=roots._left(deadline))
    roots.select_tab(ctx, tabs[0])

    def built():
        panel = _tab_panel(root, tab, deadline=deadline)
        return _labels(panel, deadline) if panel is not None else None

    return wait_until(
        built, timeout=roots._left(deadline), what="the controls of ribbon tab " + repr(tab)
    )


def _labels(scope, deadline: float | None = None) -> list[str]:
    return sorted(
        {
            collapse_whitespace(c.Name)
            for c in ui.search(scope, Step("*", True), deadline=deadline)
            if collapse_whitespace(c.Name)
        }
    )


def _tab_panel(root, tab_name: str, *, deadline: float | None = None):
    """The selected tab's controls: Word holds them in a Group named after the
    tab, in the Lower Ribbon pane. None while that group is not built yet;
    the whole ribbon when there is no Lower Ribbon (a collapsed ribbon)."""
    lower = ui.search(
        root,
        Step("Pane", True, predicates=[Predicate("class_name", "=", LOWER_RIBBON_CLASS)]),
        max_depth=8,
        limit=1,
        deadline=deadline,
    )
    if not lower:
        return root
    target = collapse_whitespace(tab_name).casefold()
    return ui.first(
        [
            group
            for group in ui.search(lower[0], Step("Group", False), deadline=deadline)
            if collapse_whitespace(group.Name).casefold() == target
        ]
    )


def select_tab(ctx: AppContext, tab, *, mode: str = "auto") -> str:
    """Switch to a ribbon tab. Invoke does nothing on a TabItem - it takes
    SelectionItem.Select() - so tab switching never rides on the caller's
    click mode, only the command click does."""
    try:
        tab.GetSelectionItemPattern().Select()
        return "select"
    except Exception:  # noqa: BLE001 - no pattern, or it refused; fall through to a real click
        pass
    ui.activate_app(ctx)
    return ui.click(tab, ctx=ctx, mode="mouse" if mode == "mouse" else "auto", activate=False)


def _tab_items(root, deadline: float | None = None) -> list:
    """The ribbon's tabs, from its tab strip alone: the whole ribbon is a long
    walk on a busy Word, and every extra UIA call is one more that can fail."""
    strip = ui.search(root, Step("Tab", True), max_depth=8, limit=1, deadline=deadline)
    if strip:
        return ui.search(strip[0], Step("TabItem", True), max_depth=2, deadline=deadline)
    return ui.search(root, Step("TabItem", True), deadline=deadline)


def _find_tab(ctx: AppContext, tab_name: str, *, timeout: float) -> tuple:
    """The ribbon and the tab in it, within `timeout`. Only a walk that lists
    tabs is evidence: "not found" needs TAB_MIN_WALKS of them and TAB_RETRY
    seconds, and names the tabs the last one listed. A cut walk, no ribbon or
    no tabs is a busy Word, and running out of time on those raises WalkCut."""
    target = collapse_whitespace(tab_name).casefold()
    start = time.monotonic()
    deadline = start + timeout
    retry_until = start + min(timeout, roots.TAB_RETRY)
    names: list[str] = []
    walks = 0
    listed = 0
    busy: str = ""
    while walks == 0 or (
        (listed < TAB_MIN_WALKS or time.monotonic() < retry_until) and time.monotonic() < deadline
    ):
        walks += 1
        try:
            root = roots.ribbon(ctx, timeout=roots._left(deadline))
            pairs = [(t, collapse_whitespace(t.Name)) for t in _tab_items(root, deadline)]
            busy = "" if pairs else "the ribbon listed no tabs"
        except NO_WORD:
            raise
        except Exception as exc:  # noqa: BLE001 - a cut walk or a busy Word; walk again
            busy, pairs = str(exc), []
        if pairs:
            listed += 1
            names = [name for _, name in pairs]
        hits = [t for t, name in pairs if name.casefold() == target]
        if hits:
            return root, hits
        time.sleep(min(roots.TAB_POLL, roots._left(deadline)))
    if listed >= TAB_MIN_WALKS:
        raise PilotError(f"ribbon tab not found: {tab_name!r}; tabs are {names!r} ({walks} walks)")
    raise WalkCut(
        f"ribbon tab {tab_name!r} not seen after {time.monotonic() - start:.1f}s "
        f"(budget {timeout:.1f}s): Word is busy ({busy or 'no walk left'}); "
        f"tabs seen are {names!r} ({listed} of {walks} walks listed tabs)"
    )


def open_tab(ctx: AppContext, tab_name: str, *, mode: str = "auto", deadline: float):
    """Select a ribbon tab and return the panel holding its controls."""
    root, tabs = roots._find_tab(ctx, tab_name, timeout=roots._left(deadline))
    roots.select_tab(ctx, tabs[0], mode=mode)

    # A tab's contents are built lazily on first activation, so poll for them -
    # in its own panel: the rest of the ribbon can still hold the last tab's.
    return wait_until(
        lambda: _tab_panel(root, tab_name, deadline=deadline),
        timeout=roots._left(deadline),
        what="the panel of ribbon tab " + repr(tab_name),
    )


def ribbon_click(
    ctx: AppContext,
    path: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    mode: str = "auto",
    allow_disabled: bool = False,
) -> dict:
    """Click 'Tab>Control', or 'Tab>Group>Control' to disambiguate.

    `timeout` bounds the whole click: the tab, group and control lookups share
    one deadline, so it overruns by blocked UIA calls only (the tab select and
    the click itself are single calls that cannot be cut)."""
    deadline = time.monotonic() + timeout
    parts = [p.strip() for p in path.split(">") if p.strip()]
    if len(parts) < 2:
        raise PilotError("ribbon path needs at least 'Tab>Control'")

    tab_name, control_name = parts[0], parts[-1]
    group_name = parts[1] if len(parts) > 2 else None

    panel = open_tab(ctx, tab_name, mode=mode, deadline=deadline)
    scope = panel
    if group_name:
        scope = wait_until(
            lambda: ui.first(
                ui.search(
                    panel,
                    Step("*", True, predicates=[Predicate("name", "~=", group_name)]),
                    deadline=deadline,
                )
            ),
            timeout=roots._left(deadline),
            what="ribbon group " + repr(group_name),
        )

    def find_target():
        return ui.first(ui.by_label(scope, control_name, deadline=deadline))

    target = wait_until(
        find_target, timeout=roots._left(deadline), what="ribbon control " + repr(control_name)
    )
    how = ui.click(
        target,
        ctx=ctx,
        mode=mode,
        activate=False,
        allow_disabled=allow_disabled,
        timeout=roots._left(deadline),
        refind=find_target,
    )
    return {
        "tab": tab_name,
        "group": group_name,
        "control": control_name,
        "clicked_via": how,
        "element": ui.describe(target),
    }
