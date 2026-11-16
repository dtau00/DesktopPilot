"""Selectors to controls: find one, find all, wait for one to appear or go."""

from __future__ import annotations

import time

from . import selectors, ui
from .errors import Ambiguous, AppNotRunning, NoApp, NotPresent, Timeout, WalkCut
from .registry import AppContext
from .roots import APP_GRACE, DEFAULT_TIMEOUT, PROBE_TIMEOUT
from .util import wait_until


def find(
    selector: str,
    ctx: AppContext,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_depth: int | None = None,
    visible_only: bool = True,
):
    """Resolve a selector to a single control, waiting for it to appear.

    The poll still walking at the deadline is always cut, so the timeout is
    told from the polls before it: any that walked the whole tree and found
    nothing make it "not found"; only when every poll was cut is the answer
    unknown - WalkCut, "the app is busy". An app not running (NoApp) for
    APP_GRACE, or at the timeout, raises that instead."""
    sel = selectors.parse(selector)
    depth = ui.default_depth(ctx) if max_depth is None else max_depth
    # An unknown root is a typo, not something that becomes true if you wait
    # fifteen seconds for it, so it fails before any polling.
    ui._check_root(sel, ctx)
    deadline = time.monotonic() + timeout
    polls = {"complete": 0, "cut": 0, "other": 0}

    def attempt():
        # What is left of the wait: a root whose walks stop at its timeout
        # (Word's ribbon, its panes) would never resolve in a fixed second.
        try:
            node = ui.resolve_root(sel, ctx, timeout=max(1.0, deadline - time.monotonic()))
            for step in sel.steps:
                hits = ui._search(
                    node,
                    step,
                    max_depth=depth,
                    visible_only=visible_only,
                    limit=step.index,
                    deadline=deadline,
                )
                if len(hits) < step.index:
                    polls["complete"] += 1
                    return None
                node = hits[step.index - 1]
        except Exception as exc:
            polls["cut" if isinstance(exc, WalkCut) else "other"] += 1
            raise
        return node

    what = "element " + repr(selector)
    try:
        return wait_until(
            attempt,
            timeout=timeout,
            what=what,
            fatal=(Ambiguous,),
            lasting=(NoApp,),
            grace=APP_GRACE,
        )
    except Timeout as exc:
        waited = f"timed out after {timeout:g}s waiting for {what}"
        if polls["complete"]:
            raise Timeout(
                f"{waited}: {polls['complete']} complete walk(s) found no match"
                + (f" ({polls['cut']} more cut)" if polls["cut"] else "")
            ) from exc
        if polls["cut"] and not polls["other"]:
            raise WalkCut(
                f"{waited}: all {polls['cut']} walk(s) were cut; the app is busy, "
                "so whether it is there is unknown"
            ) from exc
        raise


def find_all(
    selector: str,
    ctx: AppContext,
    *,
    max_depth: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    visible_only: bool = True,
    probe: bool = False,
) -> list:
    """Every match. A step with #N takes the Nth match under each parent, as
    find does; a control reached through two parents (nested matches of a
    `//` step) is listed once. `probe` is resolve_root's: look once, do not
    wait for the root to appear. The walks end `timeout` after the call
    starts, and one still walking then raises WalkCut."""
    sel = selectors.parse(selector)
    depth = ui.default_depth(ctx) if max_depth is None else max_depth
    deadline = time.monotonic() + timeout
    hits = [ui.resolve_root(sel, ctx, timeout=timeout, probe=probe)]
    for step in sel.steps:
        collected: list = []
        for parent in hits:
            found = ui._search(
                parent, step, max_depth=depth, visible_only=visible_only, deadline=deadline
            )
            if step.indexed:
                found = found[step.index - 1 : step.index]
            collected.extend(found)
        hits = ui._unique(collected)
    return hits


def is_gone(
    selector: str,
    ctx: AppContext,
    *,
    visible_only: bool = True,
    timeout: float = PROBE_TIMEOUT,
) -> bool:
    """Gone means "no longer visible" - see _search on why that is the useful
    definition for panels that keep hidden controls alive.

    One presence probe (resolve_root's `probe`): the root looks once and
    never waits to appear, so an absent root answers at once, not after
    `timeout`. `timeout` bounds the whole probe: a plugin root that needs
    several walks (Word's pane:TITLE, ~1.7 s on an idle Word) spends it on
    them, and the walk under the root is cut when it runs out.

    Only absence is gone: no match, a root that raises NotPresent, an app
    that is not running (but not a plugin's other NoApp, e.g. NotOurWord).
    A cut walk is not (False: poll again); any other error - a parse error,
    an unknown root, Ambiguous - propagates."""
    try:
        return not ui.find_all(
            selector, ctx, timeout=timeout, visible_only=visible_only, probe=True
        )
    except WalkCut:
        return False
    except (NotPresent, AppNotRunning):
        return True


def wait_for(
    selector: str,
    ctx: AppContext,
    *,
    gone: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    visible_only: bool = True,
) -> dict:
    if gone:
        # A broken selector fails at once; it never "disappears".
        ui._check_root(selectors.parse(selector), ctx)
        deadline = time.monotonic() + timeout

        def probe_budget() -> float:
            # One probe runs at most a little past the wait.
            left = deadline - time.monotonic()
            return max(ui.LEGACY_PROBE_TIMEOUT, min(ui.PROBE_TIMEOUT, left))

        wait_until(
            lambda: is_gone(selector, ctx, visible_only=visible_only, timeout=probe_budget()),
            timeout=timeout,
            what=repr(selector) + " to disappear",
            fatal=(Ambiguous,),
            lasting=(NoApp,),
            grace=APP_GRACE,
        )
        return {"selector": selector, "gone": True}
    control = ui.find(selector, ctx, timeout=timeout, visible_only=visible_only)
    return {"selector": selector, "found": True, "element": ui.describe(control, verbose=True)}
