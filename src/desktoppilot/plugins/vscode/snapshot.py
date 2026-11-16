"""One walk of the Codex panel into `panel.Node`s, every read guarded."""

from __future__ import annotations

from desktoppilot.core import selectors, ui

from . import codex
from . import panel as P
from .common import ancestors
from .common import read as read_attr

PANEL_DEPTH = 40
MAX_NODES = 8000


def _state(pattern, attr: str):
    """A pattern's state (ToggleState 0 is a real answer), None if unreadable."""
    try:
        return getattr(pattern, attr) if pattern is not None else None
    except Exception:  # noqa: BLE001 - element went stale mid-read
        return None


def _node(control, depth: int) -> P.Node | None:
    try:
        kind = selectors.normalize_type(control.ControlTypeName)
    except Exception:  # noqa: BLE001 - element went stale mid-read
        return None
    node = P.Node(
        type=kind,
        name=read_attr(control, "Name"),
        id=read_attr(control, "AutomationId"),
        cls=read_attr(control, "ClassName"),
        depth=depth,
        control=control,
    )
    if kind == "CheckBox":
        node.toggle = _state(ui._pattern(control, "GetTogglePattern"), "ToggleState")
    elif kind in ("RadioButton", "MenuItem"):
        # A model in the picker's list: a radio (UIA, build 26.917) or a radio
        # menu item (AT-SPI, build 26.1007), checked through SelectionItem,
        # else Toggle. A plain menu item has neither and stays None.
        selected = _state(ui._pattern(control, "GetSelectionItemPattern"), "IsSelected")
        if selected is None and kind == "MenuItem":
            toggle = _state(ui._pattern(control, "GetTogglePattern"), "ToggleState")
            selected = None if toggle not in (0, 1) else toggle == 1
        node.selected = None if selected is None else bool(selected)
    return node


def snapshot(root) -> list[P.Node]:
    """The panel in document order, depth-first, every read guarded."""
    nodes: list[P.Node] = []

    def walk(control, depth: int) -> None:
        if len(nodes) >= MAX_NODES:
            return
        node = _node(control, depth)
        if node is None:
            return
        nodes.append(node)
        if depth >= PANEL_DEPTH:
            return
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return nodes


def _contains(root, control) -> bool:
    """Is `control` inside `root`? Walks up from it, comparing runtime ids."""
    target = ui.identity(root)
    return target is not None and any(
        ui.identity(c) == target for c in ancestors(control, PANEL_DEPTH + codex.FRAME_DEPTH)
    )
