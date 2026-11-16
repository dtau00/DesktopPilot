"""Task panes: an add-in's WPF pane hosts by header, and Word's own panes by
name, from one walk of the frame.

A WPF add-in hosts each task pane in a window of its own, named by the add-in;
Word gives it no title, so name-based lookup would miss the pane. The names
are profile data: `[options] pane_hosts` (see pane_hosts)."""

from __future__ import annotations

import functools
import time

from desktoppilot.core import selectors, ui
from desktoppilot.core.errors import Ambiguous, NotPresent, PilotError
from desktoppilot.core.registry import AppContext
from desktoppilot.core.selectors import Predicate, Step
from desktoppilot.core.util import collapse_whitespace

from . import roots
from .frame import DEFAULT_TIMEOUT
from .session import WORD_WINDOW_CLASS
from .view import DOCUMENT_HOST_CLASS

CONTAINER_TYPES = ("Pane", "Window", "Custom", "Group")
# The profile option that lists the host-window names WPF add-ins give their
# task panes. Unset, `pane` finds Word's own panes only.
PANE_HOSTS_OPTION = "pane_hosts"
MIN_PANE_WIDTH = 120
# How deep a pane's header is looked for; the rows below it are deeper.
HEADER_DEPTH = 3
# How deep task_pane's one walk of the frame goes.
PANE_WALK_DEPTH = 10
# The UIA class of a WPF Window: the add-in's floating toolbar and its toasts.
WPF_WINDOW_UIA_CLASS = "Window"


def task_pane(
    ctx: AppContext,
    name: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe: bool = False,
):
    """Find a task pane. `pane:TITLE` is the add-in's WPF pane whose header
    reads TITLE; bare `pane` is the only one open. Failing a header match, a
    name means any pane holding that text (e.g. 'Navigation' for Word's own).

    One walk of the frame answers every question below, cut at `timeout`. It
    never waits for the pane to appear, so a probe resolves the same way."""
    deadline = time.monotonic() + timeout
    names = pane_hosts(ctx)
    tree = _pane_walk(roots.word_window(ctx, timeout=timeout), deadline, names)
    hosts = [c for c in tree if _is_host(c, names) and _visible(c)]

    if name:
        host = host_titled(hosts, name, deadline=deadline)
        if host is not None:
            return host
    elif len(hosts) == 1:
        return hosts[0]
    elif hosts:
        raise Ambiguous(
            f"{len(hosts)} add-in panes are open, headed {[host_header(h) for h in hosts]}; "
            "name one with pane:TITLE"
        )

    preds = [Predicate("name", "~=", name)] if name else []
    named = [
        c
        for c in tree
        if not _is_host(c, names)
        and ui.matches(c, Step("*", True, predicates=preds))
        and _visible(c)
    ]
    for control_type in CONTAINER_TYPES:
        for c in named:
            if ui.matches(c, Step(control_type, True)) and _wide(c):
                return c

    # Fall back: match anything by name, then climb to its pane container.
    if name:
        for control in named:
            pane = _pane_ancestor(control)
            if pane is not None:
                return pane

    raise NotPresent("task pane not found: " + (name or "any"))


def pane_hosts(ctx: AppContext | None) -> tuple[str, ...]:
    """The pane host-window names the app's profile lists in
    `[options] pane_hosts`; none without a profile."""
    profile = ctx.profile if ctx is not None else None
    names = profile.options.get(PANE_HOSTS_OPTION, []) if profile is not None else []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PilotError(
            f"profile {profile.name!r}: [options] {PANE_HOSTS_OPTION} must be a list of "
            f"strings, got {names!r}"
        )
    return tuple(names)


def _is_host(control, hosts: tuple[str, ...]) -> bool:
    if not hosts:
        return False
    try:
        return control.Name in hosts and ui.matches(control, Step("Pane", True))
    except Exception:  # noqa: BLE001 - element vanished mid-walk
        return False


def _visible(control) -> bool:
    try:
        return not control.IsOffscreen
    except Exception:  # noqa: BLE001 - element vanished mid-walk
        return False


def _wide(control) -> bool:
    try:
        return control.BoundingRectangle.width() >= MIN_PANE_WIDTH
    except Exception:  # noqa: BLE001 - element vanished mid-walk
        return False


def wpf_hosts(window, *, hosts: tuple[str, ...] = (), deadline: float | None = None) -> list:
    """The visible WPF pane hosts named in `hosts`. Past `deadline`
    (time.monotonic()) the walk raises WalkCut instead of answering a short list."""
    return [c for c in _pane_walk(window, deadline, hosts) if _is_host(c, hosts) and _visible(c)]


def _pane_walk(window, deadline: float | None, hosts: tuple[str, ...] = ()) -> list:
    """Every element under the frame that may be a task pane or a pane host,
    hidden ones included, in one walk that stays out of _not_a_pane."""
    return [
        c
        for c in ui.search(
            window,
            Step("*", True),
            max_depth=PANE_WALK_DEPTH,
            visible_only=False,
            prune=functools.partial(_not_a_pane, hosts=hosts),
            deadline=deadline,
        )
        if _is_host(c, hosts) or not _not_a_pane(c, 1, hosts)
    ]


def _not_a_pane(control, depth: int, hosts: tuple[str, ...] = ()) -> bool:
    """What never answers as `pane:TITLE`, nor holds a pane that does: the
    document host and its canvas, the ribbon, Backstage, and the add-in's own
    UI - a host already found (hosts do not nest), its toolbar and toasts."""
    try:
        kind = selectors.normalize_type(control.ControlTypeName)
        # ClassName on panes only: every read blocks on a busy Word.
        if kind == "Document" or (kind == "Pane" and control.ClassName == DOCUMENT_HOST_CLASS):
            return True
        name = control.Name or ""
        return (
            _addin_ui(control, depth, hosts)
            or (depth <= 4 and name == "Ribbon")
            or roots.BACKSTAGE_NAME in name
        )
    except Exception:  # noqa: BLE001 - a read that failed proves nothing; walk it
        return False


def _addin_ui(control, depth: int, hosts: tuple[str, ...] = ()) -> bool:
    """An add-in's WPF UI under the Word frame: a pane host named in `hosts`,
    or a WPF window (its toolbar, its toasts). Neither holds the ribbon or
    another pane, and on a busy Word each of their dozens of controls blocks
    a read for seconds."""
    try:
        return (bool(hosts) and control.Name in hosts) or (
            depth == 1 and control.ClassName == WPF_WINDOW_UIA_CLASS
        )
    except Exception:  # noqa: BLE001 - a read that failed proves nothing; walk it
        return False


def _header_texts(host, deadline: float | None = None) -> list[str]:
    return [
        collapse_whitespace(t.Name)
        for t in ui.search(host, Step("Text", True), max_depth=HEADER_DEPTH, deadline=deadline)
        if collapse_whitespace(t.Name)
    ]


def host_header(host) -> str | None:
    """The pane's title: its first visible text."""
    return ui.first(_header_texts(host))


def host_titled(hosts: list, title: str, *, deadline: float | None = None):
    """The host holding a text equal to `title`, else one containing it -
    whitespace collapsed, case ignored. Past `deadline` it raises WalkCut:
    a header not yet read is not a header that is missing."""
    wanted = collapse_whitespace(title).casefold()
    texts = [(host, [t.casefold() for t in _header_texts(host, deadline)]) for host in hosts]
    for host, found in texts:
        if wanted in found:
            return host
    for host, found in texts:
        if any(wanted in text for text in found):
            return host
    return None


def _pane_ancestor(control):
    """The nearest wide container above `control`, below the Word frame and
    outside the document host: a match in the document is in no pane."""
    node = control
    for _ in range(12):
        try:
            node = node.GetParentControl()
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            return None
        if node is None or node.ClassName in (WORD_WINDOW_CLASS, DOCUMENT_HOST_CLASS):
            return None
        if (
            selectors.normalize_type(node.ControlTypeName) in CONTAINER_TYPES
            and node.BoundingRectangle.width() >= MIN_PANE_WIDTH
        ):
            return node
    return None
