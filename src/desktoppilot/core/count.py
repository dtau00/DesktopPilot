"""Counting the real items of a list or tree, virtualized rows and collapsed
subtrees included, and expanding a tree."""

from __future__ import annotations

import contextlib
import time
from collections import deque

from . import selectors, ui, uiacache
from .errors import PilotError, WalkCut, is_uia_timeout
from .native import auto
from .util import collapse_whitespace

COUNT_CAP = 100_000
COUNT_TIMEOUT = 60.0
EXPAND_TIMEOUT = 180.0
EXPAND_PASSES = 4
DEFAULT_NAMES = 50
# ExpandCollapseState.Collapsed; PartiallyExpanded (2) opens the same way.
_COLLAPSED_STATES = (0, 2)
# ExpandCollapseState by name, for `ui expand`.
STATE_NAMES = {0: "collapsed", 1: "expanded", 2: "partial", 3: "leaf"}


def _item_container(control):
    """The raw IUIAutomationItemContainerPattern, or None.

    The uiautomation wrapper's FindItemByProperty(None, ...) raises on the
    None start (it dereferences `.Element`), so _enumerate_items calls the
    raw COM pattern instead."""
    try:
        wrapper = control.GetPattern(auto.PatternId.ItemContainerPattern)
    except Exception:  # noqa: BLE001 - pattern simply unsupported
        return None
    return getattr(wrapper, "pattern", None) if wrapper else None


def _raw_name(element) -> str:
    try:
        return collapse_whitespace(element.CurrentName)
    except Exception:  # noqa: BLE001 - a virtualized item may refuse property reads
        return ""


def _enumerate_items(raw, *, names: int, cap: int, deadline: float) -> dict:
    """Walk every item of an ItemsControl, virtualized ones included.

    The end of the walk is a NULL COM pointer, which is falsy but NOT None: an
    `is not None` loop spins forever on it. Hence `not current`, plus a cap
    and a deadline so a misbehaving provider cannot hang the caller.

    A walk stopped by either raises WalkCut (see its `count`)."""
    count, current, seen = 0, None, []
    while True:
        try:
            current = raw.FindItemByProperty(current, 0, 0)
        except Exception as exc:  # noqa: BLE001 - only a UIA timeout is a cut
            if is_uia_timeout(exc):
                raise WalkCut(
                    f"the list count was cut by a UIA timeout with {count} item(s) walked; "
                    f"the count is unknown, not {count}",
                    count=count,
                ) from exc
            raise
        if not current:
            break
        count += 1
        if len(seen) < names:
            seen.append(_raw_name(current))
        if count >= cap or time.monotonic() > deadline:
            why = f"hit the {cap} item cap" if count >= cap else "was cut at its deadline"
            raise WalkCut(
                f"the list count {why} with {count} item(s) walked; the count is unknown, "
                f"not {count}",
                count=count,
            )
    return {"count": count, "via": "ItemContainerPattern", "names": seen}


def _type_of(control) -> str:
    try:
        return selectors.normalize_type(control.ControlTypeName)
    except Exception:  # noqa: BLE001 - element vanished mid-walk
        return ""


def _label(control) -> str:
    """What a user reads on a row. WPF rows are named after their view-model
    class, so the first Text child wins over the row's own name."""
    with contextlib.suppress(WalkCut):  # a label is best effort; the row's name will do
        for child in ui._children(control):
            if _type_of(child) == "Text" and collapse_whitespace(child.Name):
                return collapse_whitespace(child.Name)
    try:
        return collapse_whitespace(control.Name)
    except Exception:  # noqa: BLE001
        return ""


# What a list's rows are. A list whose children include none of these, but
# that holds them inside unnamed wrappers, is counted through the wrappers.
_ROW_TYPES = ("ListItem", "MenuItem", "TreeItem", "DataItem", "TabItem")
# Rows whose children are their parts, not items: counting one is a mistake.
_ITEM_TYPES = ("ListItem", "DataItem", "TabItem")
# Layout a toolkit puts between a list and its rows: Chromium wraps a VS
# Code list's rows in an unnamed group, a <select>'s options in its menu list.
_WRAPPER_TYPES = ("Pane", "Group", "Custom")
_HOLDER_TYPES = ("List", "Menu")
_WRAPPER_DEPTH = 4


def _is_wrapper(control) -> bool:
    kind = _type_of(control)
    if kind in _HOLDER_TYPES:
        return True
    if kind not in _WRAPPER_TYPES:
        return False
    try:
        return not collapse_whitespace(control.Name)
    except Exception:  # noqa: BLE001 - an unreadable name is no wrapper to look through
        return False


def _list_items(control) -> list:
    """The rows of a list: its children, or, when none of them is a row,
    the rows inside its unnamed wrappers (a Pane, Group or Custom with no
    name, or a List or Menu: a combo box's drop-down), down to
    _WRAPPER_DEPTH levels. A flat list, and a list with no rows inside its
    wrappers either, count their children as before."""
    kids = ui._children(control)
    if any(_type_of(k) in _ROW_TYPES for k in kids):
        return kids
    rows: list = []
    level = [k for k in kids if _is_wrapper(k)]
    for _ in range(_WRAPPER_DEPTH):
        if not level:
            break
        below: list = []
        for wrapper in level:
            for child in ui._children(wrapper):
                if _type_of(child) in _ROW_TYPES:
                    rows.append(child)
                elif _is_wrapper(child):
                    below.append(child)
        if rows:
            return rows
        level = below
    return kids


def _tree_nodes(control) -> list:
    """The TreeItem children of `control`, and nothing else. A row of a tree
    is a TreeItem; a Button, SplitButton, MenuItem or ComboBox also offers
    ExpandCollapse, but expanding it opens a drop-down (the ribbon's Share
    menu), so offering the pattern never makes a child a tree row."""
    return [c for c in ui._children(control) if _type_of(c) == "TreeItem"]


def is_tree_like(control) -> bool:
    """A Tree, a TreeItem, or a control whose children are TreeItems. Only
    these are expanded to count; anything else counts as a list."""
    return _type_of(control) in ("Tree", "TreeItem") or bool(_tree_nodes(control))


def expand(control) -> dict:
    """Open one node through ExpandCollapse. `expanded`: whether this call
    opened it; `state`: its state after, by name."""
    pattern = ui._pattern(control, "GetExpandCollapsePattern")
    if pattern is None:
        raise PilotError("no ExpandCollapse pattern on this element; try --all on its tree")
    was_collapsed = pattern.ExpandCollapseState in _COLLAPSED_STATES
    if was_collapsed:
        pattern.Expand(waitTime=0.05)
    state = pattern.ExpandCollapseState
    return {"expanded": was_collapsed, "state": STATE_NAMES.get(state, str(state))}


def expand_all(control, deadline: float | None = None, *, names: int = 0) -> dict:
    """Expand every collapsed node under `control` and count the nodes. A WPF
    tree builds a collapsed node's children only when it opens, so a
    collapsed subtree is simply missing from the UIA tree until then.

    Breadth first over the raw element, with the state and ExpandCollapse
    pattern cached: each non-leaf costs ONE call that fetches its TreeItem
    children, a leaf costs none, and a collapsed node opens through its
    cached pattern. The wrapper walk cost ~25 calls a node. Breadth first
    because a node's children are built a beat after it opens: its siblings'
    fetches give it that beat. `names` come in breadth-first order.

    A control without a raw element, or whose root has no TreeItem children,
    is walked through the wrapper instead; `via` says which ran.

    `deadline` is a time.monotonic() value, checked before every fetch and
    every expand; past it the pass stops and says `complete: false`, as a
    fetch or an expand the app timed out on does. Such a pass is a floor (see
    WalkCut): what it did not reach is unknown, not empty.

    A control that is not tree-like raises PilotError: its ExpandCollapse
    children are drop-downs, and "0 nodes" would read as an empty tree."""
    if not is_tree_like(control):
        raise PilotError(
            f"not a tree: this {_type_of(control) or 'control'} has no TreeItem children, "
            "so there is nothing to expand; `ui count` counts it as a list, "
            "`ui expand` without --all opens one element"
        )
    deadline = time.monotonic() + EXPAND_TIMEOUT if deadline is None else deadline
    element = uiacache.raw(control)
    if element is None:
        return _expand_all_walk(control, deadline, names)
    stats = _expand_stats("cache")
    labels: list[str] = []
    queue: deque = deque()

    def fetch(el) -> list | None:
        """The children, or None when the pass is cut."""
        if time.monotonic() > deadline:
            return None
        try:
            return uiacache.tree_item_children(el)
        except Exception as exc:  # noqa: BLE001 - only a UIA timeout propagates
            if is_uia_timeout(exc):
                return None
            raise

    def take(kids: list, level: int) -> None:
        for el in kids:
            _tally(stats, level, labels, names, lambda el=el: _label(uiacache.wrap(el)))
            state = uiacache.cached_state(el)
            if state in _COLLAPSED_STATES:
                if time.monotonic() > deadline:
                    stats["complete"] = False
                    continue
                try:
                    uiacache.expand_cached(el)
                    stats["expanded"] += 1
                except Exception as exc:  # noqa: BLE001 - a node that refuses Expand
                    if is_uia_timeout(exc):
                        stats["complete"] = False
            if state != uiacache.LEAF:
                queue.append((el, level + 1))

    top = fetch(element)
    if top == []:
        return _expand_all_walk(control, deadline, names)
    if top is None:
        stats["complete"] = False
    else:
        take(top, 0)
    while queue and stats["complete"]:
        el, level = queue.popleft()
        kids = fetch(el)
        if kids is None:
            stats["complete"] = False
        else:
            take(kids, level)
    return _with_names(stats, labels, names)


def _expand_stats(via: str) -> dict:
    return {"expanded": 0, "nodes": 0, "depths": {}, "complete": True, "via": via}


def _tally(stats: dict, level: int, labels: list[str], names: int, label) -> None:
    """Count one node at `level`; read its `label()` while `names` wants more."""
    stats["nodes"] += 1
    stats["depths"][level] = stats["depths"].get(level, 0) + 1
    if len(labels) < names:
        labels.append(label())


def _with_names(stats: dict, labels: list[str], names: int) -> dict:
    if names:
        stats["names"] = labels
    return stats


def _expand_all_walk(control, deadline: float, names: int) -> dict:
    """expand_all through the wrapper, depth first: nodes found past
    `deadline` are counted but neither expanded nor walked into. A read or
    an expand the app timed out on leaves that subtree unknown:
    `complete: false`."""
    stats = _expand_stats("walk")
    labels: list[str] = []

    def walk(node, level: int) -> None:
        try:
            nodes = _tree_nodes(node)
        except WalkCut:
            stats["complete"] = False
            return
        for child in nodes:
            _tally(stats, level, labels, names, lambda child=child: _label(child))
            if time.monotonic() > deadline:
                stats["complete"] = False
                continue
            expander = ui._pattern(child, "GetExpandCollapsePattern")
            if expander is not None:
                try:
                    if expander.ExpandCollapseState in _COLLAPSED_STATES:
                        expander.Expand(waitTime=0)
                        stats["expanded"] += 1
                except Exception as exc:  # noqa: BLE001 - a leaf refuses Expand
                    if is_uia_timeout(exc):
                        stats["complete"] = False
                        continue
            walk(child, level + 1)

    walk(control, 0)
    return _with_names(stats, labels, names)


def count_items(
    control,
    *,
    names: int = DEFAULT_NAMES,
    expand: bool = True,
    timeout: float | None = None,
) -> dict:
    """The real number of items in a list or tree, not the realized ones.

    A virtualized WPF ListBox keeps only the rows on screen in the UIA tree
    (5 of 63 rows), so counting children counts the viewport; its
    ItemContainerPattern walks the virtualized items too. A tree hides every
    collapsed subtree, so it is counted after expand_all - repeatedly, until
    two passes agree, because a node's children are built a beat after it
    opens.

    For a tree, `count` is the largest expand pass that covered the whole
    tree, and `complete` means two such passes agreed. A cut pass is reported
    as `cut_pass` (a floor, see WalkCut) and is never the count. With no pass
    that covered the tree it raises WalkCut."""
    if expand and is_tree_like(control):
        return _count_tree(control, names, EXPAND_TIMEOUT if timeout is None else timeout)

    raw = _item_container(control)
    if raw is not None:
        deadline = time.monotonic() + (COUNT_TIMEOUT if timeout is None else timeout)
        return _enumerate_items(raw, names=names, cap=ui.COUNT_CAP, deadline=deadline)

    kids = _list_items(control)
    result = {"count": len(kids), "via": "children", "names": [_label(k) for k in kids[:names]]}
    kind = _type_of(control)
    if kind in _ITEM_TYPES:
        result["item_hint"] = (
            f"this is one {kind}: its own children were counted; "
            "select the list that holds it to count its items"
        )
    scroller = ui._pattern(control, "GetScrollPattern")
    if scroller is not None:
        try:
            view = scroller.VerticalViewSize
            if scroller.VerticallyScrollable and view < 100:
                result["virtualized_hint"] = (
                    f"only {view:.1f}% of the list is in view and it offers no "
                    "ItemContainerPattern: a virtualized list counts only realized rows"
                )
        except Exception:  # noqa: BLE001 - scroll state unreadable
            pass
    return result


def _count_tree(control, names: int, budget: float) -> dict:
    """count_items for a tree: expand passes until two complete ones agree."""
    deadline = time.monotonic() + budget
    passes: list[int] = []  # complete passes only
    best: dict | None = None  # the largest complete pass
    cut: dict | None = None  # the cut pass, if any
    expanded = 0
    while len(passes) < EXPAND_PASSES:
        stats = expand_all(control, deadline, names=names)
        expanded += stats["expanded"]
        if not stats["complete"]:
            cut = stats
            break
        passes.append(stats["nodes"])
        if best is None or stats["nodes"] >= best["nodes"]:
            best = stats
        if len(passes) >= 2 and passes[-1] == passes[-2]:
            break
        if time.monotonic() > deadline:
            break
    if best is None:
        reached = cut["nodes"] if cut else 0
        raise WalkCut(
            f"the tree count was cut before one expand pass covered the tree "
            f"({budget:g}s budget): {reached} node(s) reached, {expanded} expanded; "
            f"the count is unknown, not {reached}",
            count=reached,
        )
    result = {
        "count": best["nodes"],
        "via": "expand",
        "names": best.get("names", []),
        "expanded": expanded,
        "depths": best["depths"],
        "passes": passes,
        "complete": len(passes) >= 2 and passes[-1] == passes[-2],
    }
    if cut is not None:
        result["cut_pass"] = cut["nodes"]  # a floor, see WalkCut
    return result
