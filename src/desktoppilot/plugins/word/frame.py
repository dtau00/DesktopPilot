"""Word's frame window: found through Win32, its UIA element made once and
cached, a settle after an open, and the polled walk of the frame that the
ribbon and Backstage are looked up with."""

from __future__ import annotations

import functools
import threading
import time

import uiautomation as auto

from desktoppilot.core import ui
from desktoppilot.core.errors import Timeout, WalkCut
from desktoppilot.core.registry import AppContext
from desktoppilot.core.selectors import Predicate, Step
from desktoppilot.core.util import wait_until

from . import roots, session

SETTLE_TIMEOUT = 10.0
# s; an idle Word answers a frame children read in ~0.04 s, a post-open one in ~2 s
SETTLE_FAST = 0.3
# The quiet gap that lets Word finish its post-open work.
SETTLE_POLL = 1.0

DEFAULT_TIMEOUT = ui.DEFAULT_TIMEOUT


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


# Word frame elements by (hwnd, pid, thread): UIA elements are per thread. A
# few are kept, so two document windows taking turns are not remade each time.
_frames: dict[tuple, object] = {}
FRAMES_KEPT = 8
# How long each ControlFromHandle for a frame took, newest last: {"hwnd",
# "seconds"}. Product suites read it, so keep the name and shape.
FRAME_MAKES: list[dict] = []
FRAME_MAKES_KEPT = 50


def word_window(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT):
    """Our Word's frame. Matched on class and owner, not on size: Backstage and
    the add-in's WPF windows belong to the same process.

    Found through Win32, which asks Word nothing: the UIA desktop leaves out a
    Word too busy to answer. The UIA element is made once per frame: making it
    took 3-50 ms on a warm Word and ~274 ms cold (measured), but that one call
    cannot be cut, so a make that ends past `timeout` raises WalkCut and the
    retry gets the cached element at once. Each make is timed in FRAME_MAKES."""
    pids = session.require_owned(ctx)
    deadline = time.monotonic() + timeout

    def find_window():
        frame = ui.first(session.word_frames(pids))
        if frame is None:
            return None
        key = (frame["hwnd"], frame["pid"], threading.get_ident())
        if key not in roots._frames:
            took = _make_frame(key)
            if time.monotonic() > deadline:
                raise WalkCut(
                    f"making the Word frame's UIA element took {took:.1f}s, past the "
                    f"{timeout:.1f}s budget; Word is busy (the element is cached for the retry)"
                )
        return roots._frames[key]

    return wait_until(find_window, timeout=timeout, what="Word main window", fatal=(WalkCut,))


def _make_frame(key: tuple) -> float:
    """Cache the UIA element of the frame `key` names; the seconds it took."""
    began = time.monotonic()
    element = auto.ControlFromHandle(key[0])
    took = time.monotonic() - began
    while len(roots._frames) >= roots.FRAMES_KEPT:
        del roots._frames[next(iter(roots._frames))]
    roots._frames[key] = element
    roots.FRAME_MAKES.append({"hwnd": key[0], "seconds": round(took, 2)})
    del roots.FRAME_MAKES[:-FRAME_MAKES_KEPT]
    return took


def settle(ctx: AppContext, *, timeout: float = SETTLE_TIMEOUT) -> dict:
    """Wait until Word answers UIA quickly again after a document open.

    For ~3-4 s after an open Word answers UIA slowly, and a walk started in
    that window stays slow for as long as its calls keep coming; a quiet gap
    lets Word finish. So: time one cheap frame read, and while it is slow,
    keep quiet for SETTLE_POLL and read again. Never raises.
    Returns {"settled": bool, "seconds": float, "reads": int}."""
    start = time.monotonic()
    deadline = start + timeout
    reads = 0

    def done(settled: bool) -> dict:
        return {"settled": settled, "seconds": round(time.monotonic() - start, 2), "reads": reads}

    try:
        frame = roots.word_window(ctx, timeout=_left(deadline))
    except Exception:  # noqa: BLE001 - settling is best effort; never raise
        return done(False)
    while True:
        began = time.monotonic()
        try:
            ui.search(frame, Step("*", False), deadline=deadline)
        except Exception:  # noqa: BLE001 - a cut or failed read is not settled
            return done(False)
        reads += 1
        if time.monotonic() - began < roots.SETTLE_FAST:
            return done(True)
        if time.monotonic() >= deadline:
            return done(False)
        time.sleep(min(roots.SETTLE_POLL, _left(deadline)))


def frame_hwnd(ctx: AppContext) -> int | None:
    """Our Word's frame: the topmost visible OpusApp of a Word this tool owns,
    whatever other Word sits above it."""
    frame = ui.first(session.word_frames(session.require_owned(ctx)))
    return frame["hwnd"] if frame else None


def _poll_frame_walk(
    ctx: AppContext, name: str, *, depth: int, what: str, timeout: float, probe: bool = False
):
    """The first element whose name holds `name`, walking the frame until it
    turns up (once, for a probe). The add-in's WPF UI is pruned: it holds
    neither, and on a busy Word each of its controls blocks a read.

    None means a walk covered the tree without it. With no covering walk -
    no frame in time, or each walk cut at `timeout` or by a UIA timeout -
    it raises WalkCut, because on a busy Word "not there" would be a guess.
    Walks stop at `timeout`; the call can overrun it by one blocked UIA call,
    or by the frame element's make, which cannot be cut."""
    start = time.monotonic()
    deadline = start + timeout
    try:
        window = roots.word_window(ctx, timeout=timeout)
    except Timeout as exc:
        raise WalkCut(
            f"no Word frame after {time.monotonic() - start:.1f}s (budget {timeout:.1f}s) "
            f"to look for {what}; Word is busy"
        ) from exc
    step = Step("*", True, predicates=[Predicate("name", "~=", name)])
    prune = functools.partial(roots._addin_ui, hosts=roots.pane_hosts(ctx))
    covered = []

    def walk():
        hit = ui.first(
            ui.search(window, step, max_depth=depth, limit=1, prune=prune, deadline=deadline)
        )
        covered.append(True)
        return hit

    try:
        if probe:
            return walk()
        return wait_until(walk, timeout=_left(deadline), what=what, fatal=(WalkCut,))
    except Timeout as exc:  # WalkCut included
        if covered:
            return None
        spent = time.monotonic() - start
        if isinstance(exc, WalkCut):
            raise WalkCut(
                f"the {what} walk was cut after {spent:.1f}s (budget {timeout:.1f}s); "
                "Word is busy, so whether it is there is unknown"
            ) from exc
        raise WalkCut(
            f"no time left after {spent:.1f}s (budget {timeout:.1f}s) to look for {what}"
        ) from exc
