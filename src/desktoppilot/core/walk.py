"""The UIA tree walk under a root: one selector step's matches, element
identity and visibility, and the tree and flat dumps."""

from __future__ import annotations

import time
from collections import deque

from . import selectors, ui
from .errors import WalkCut, is_uia_timeout
from .registry import AppContext
from .selectors import Step
from .util import collapse_whitespace

DEFAULT_DEPTH = 12


def _matches(control, step: Step) -> bool:
    # Web content re-renders while the walk is in flight, so any property read
    # can raise on an element that just went away. A stale element is simply
    # not a match; a read the app timed out on raises, since it proves nothing.
    try:
        if step.control_type != "*" and (
            selectors.normalize_type(control.ControlTypeName) != step.control_type
        ):
            return False
        for pred in step.predicates:
            actual = {
                "name": control.Name,
                "automation_id": control.AutomationId,
                "class_name": control.ClassName,
                "value": ui.value_of(control),
            }[pred.key]
            if not pred.matches(actual):
                return False
    except Exception as exc:  # noqa: BLE001 - element vanished mid-walk
        if is_uia_timeout(exc):
            raise
        return False
    return True


def matches(control, step: Step) -> bool:
    """Whether `control` answers one selector step's type and predicates -
    for a plugin that walks once and sorts the hits itself."""
    return _matches(control, step)


def _children(control) -> list:
    """A vanished element has no children; one the app timed out on has
    unknown children, so that raises WalkCut instead of answering []."""
    try:
        return control.GetChildren()
    except Exception as exc:  # noqa: BLE001 - element vanished mid-walk
        if is_uia_timeout(exc):
            raise WalkCut("a UIA children read timed out; the app is busy") from exc
        return []


def _search(
    root,
    step: Step,
    *,
    max_depth: int = DEFAULT_DEPTH,
    visible_only: bool = True,
    limit: int | None = None,
    prune=None,
    deadline: float | None = None,
) -> list:
    """Breadth-first, so shallow visible matches win over deeply buried ones.

    Offscreen elements are skipped by default: a virtualized list, a collapsed
    panel or an inactive ribbon tab keeps its controls in the tree, so matching
    them would let a test "click" something no user can see, and would make
    wait --gone never fire.

    Every UIA call blocks on a busy app, so a caller that knows its answer can
    say so: `limit` stops the walk at that many hits, and `prune(control,
    depth)` returning true keeps the walk out of that control's subtree (the
    control itself is still matched).

    A limit or a prune does not bound the walk's wall time: a busy app blocks
    each call for seconds. `deadline`, a time.monotonic() value, does - it is
    checked before every node, and a walk past it raises WalkCut carrying the
    hits so far. It never returns a short list as if the tree ended there, so
    no caller can report "not found" from a walk that was cut. Any read the
    app timed out on (UIA_E_TIMEOUT) - children, a property, a prune check -
    is a cut too: that part of the tree is unknown.
    """
    hits: list = []

    def cut(why: str) -> WalkCut:
        return WalkCut(
            f"the UIA walk for {step.control_type!r} was cut {why} with {len(hits)} hit(s); "
            "the app is busy, so what it did not reach is unknown",
            hits,
        )

    def check() -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise cut("at its deadline")

    def children(control) -> list:
        try:
            return _children(control)
        except WalkCut as exc:
            raise cut("by a UIA timeout") from exc

    def keep(control) -> bool:
        try:
            return _matches(control, step) and not (visible_only and control.IsOffscreen)
        except Exception as exc:  # noqa: BLE001 - element vanished mid-walk
            if is_uia_timeout(exc):
                raise cut("by a UIA timeout") from exc
            return False

    def skip(control, depth: int) -> bool:
        try:
            return prune is not None and prune(control, depth)
        except Exception as exc:  # noqa: BLE001 - element vanished mid-walk
            if isinstance(exc, WalkCut) or is_uia_timeout(exc):
                raise cut("by a UIA timeout") from exc
            return True

    check()
    if not step.descendant:
        for child in children(root):
            check()
            if keep(child):
                hits.append(child)
                if limit is not None and len(hits) >= limit:
                    break
        return hits

    queue = deque((child, 1) for child in children(root))
    while queue:
        check()
        control, depth = queue.popleft()
        if keep(control):
            hits.append(control)
            if limit is not None and len(hits) >= limit:
                break
        if depth < max_depth and not skip(control, depth):
            queue.extend((child, depth + 1) for child in children(control))
    return hits


def search(
    root,
    step: Step,
    *,
    max_depth: int = DEFAULT_DEPTH,
    visible_only: bool = True,
    limit: int | None = None,
    prune=None,
    deadline: float | None = None,
) -> list:
    """One selector step under `root`: the walk, for plugins that resolve their
    own roots. See _search for `limit`, `prune` and `deadline`."""
    return _search(
        root,
        step,
        max_depth=max_depth,
        visible_only=visible_only,
        limit=limit,
        prune=prune,
        deadline=deadline,
    )


def by_label(scope, label: str, *, control_type: str = "*", deadline: float | None = None) -> list:
    """Every visible control under `scope` whose name, whitespace collapsed
    and case folded, is `label` (labels carry layout whitespace; see collapse_whitespace).
    A walk past `deadline` raises WalkCut (see _search)."""
    target = collapse_whitespace(label).casefold()
    return [
        c
        for c in _search(scope, Step(control_type, True), deadline=deadline)
        if collapse_whitespace(c.Name).casefold() == target
    ]


def first(hits: list):
    return hits[0] if hits else None


def default_depth(ctx: AppContext) -> int:
    return ctx.profile.depth if ctx is not None and ctx.profile else DEFAULT_DEPTH


def _unique(controls: list) -> list:
    seen: set = set()
    out = []
    for control in controls:
        # id() is safe inside one list, which keeps every wrapper alive.
        key = identity(control) or ("object", id(control))
        if key not in seen:
            seen.add(key)
            out.append(control)
    return out


def identity(control) -> tuple | None:
    """The element's RuntimeId, which names it across reads; None when it has
    none or is gone. Never id(): a freed wrapper passes its address on to the
    next one, so a later read would match an element it never saw."""
    try:
        runtime_id = control.GetRuntimeId()
    except Exception:  # noqa: BLE001 - element vanished or offers no id
        return None
    return tuple(runtime_id) if runtime_id else None


def _visible_rect(control):
    """Its bounding rectangle when it is on screen with a size; else None,
    an element that cannot be read included."""
    try:
        rect = control.BoundingRectangle
        offscreen = control.IsOffscreen
    except Exception:  # noqa: BLE001 - element vanished mid-read
        return None
    if offscreen or rect.width() <= 0 or rect.height() <= 0:
        return None
    return rect


def is_visible(control) -> bool:
    """On screen and with a size: what a pointer can reach."""
    return _visible_rect(control) is not None


def _walk_report(report: dict | None) -> dict:
    """Where tree and flatten say whether they covered the subtree: a node
    whose children read the app timed out is `cut`, and `complete` is False."""
    report = {} if report is None else report
    report.update(complete=True, cut=0)
    return report


def _children_or_cut(control, report: dict) -> list | None:
    try:
        return _children(control)
    except WalkCut:
        report["complete"] = False
        report["cut"] += 1
        return None


def tree(
    root, *, depth: int = 4, limit: int = 400, verbose: bool = False, report: dict | None = None
) -> dict:
    """Serialize a control subtree, node-capped so output stays readable.
    Pass `report` (a dict) to learn whether the walk was complete; a node
    whose children could not be read carries `cut: true`."""
    count = 0
    report = _walk_report(report)

    def walk(control, level: int) -> dict:
        nonlocal count
        node = ui.describe(control, verbose=verbose)
        count += 1
        children = _children_or_cut(control, report)
        if children is None:
            node["cut"] = True
            return node
        if level < depth and count < limit:
            kids = []
            for child in children:
                if count >= limit:
                    node["truncated"] = True
                    break
                kids.append(walk(child, level + 1))
            if kids:
                node["children"] = kids
        elif children:
            node["children_omitted"] = len(children)
        return node

    return walk(root, 0)


def flatten(
    root,
    *,
    depth: int = 8,
    limit: int = 500,
    only_named: bool = True,
    only_visible: bool = True,
    report: dict | None = None,
) -> list[dict]:
    """Flat list of controls - usually what an agent wants before clicking.
    Pass `report` (a dict) to learn whether the walk was complete."""
    out: list[dict] = []
    report = _walk_report(report)
    queue = deque([(root, 0)])
    while queue and len(out) < limit:
        control, level = queue.popleft()
        info = ui.describe(control)
        keep = (info.get("name") or not only_named) and (
            not only_visible or not info.get("offscreen")
        )
        if keep:
            info["depth"] = level
            out.append(info)
        if level < depth:
            queue.extend((child, level + 1) for child in _children_or_cut(control, report) or [])
    return out
