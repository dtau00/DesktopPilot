"""The raw IUIAutomation seam: one cross-process call fetches a node's
TreeItem children with their type, name and ExpandCollapse state cached.

The uiautomation wrapper reads every property with its own call, which on a
busy WPF tree is ~25 calls per node. Here a node costs one call, the state is
read from the cache, and the expand goes through the cached pattern. Kept
small so tests can fake it.

Windows only: on Linux `raw` finds no element on a Control (AT-SPI has no
cache requests), so the callers take their per-property path.
"""

from __future__ import annotations

import threading

from .errors import is_uia_timeout
from .native import WINDOWS, auto

if WINDOWS:
    import comtypes

# UIA_TreeItemControlTypeId (UIA_TreeControlTypeId is 50023).
TREE_ITEM = 50024
CONTROL_TYPE = 30003
NAME = 30005
EC_STATE = 30070
EC_PATTERN = 10005
SCOPE_ELEMENT = 1
SCOPE_CHILDREN = 2
# ExpandCollapseState.LeafNode: no children, so no call is made for it.
LEAF = 3

# (condition, cache request) per thread: COM objects belong to their apartment.
_requests: dict[int, tuple] = {}


def client():
    from uiautomation import uiautomation as _ua

    return _ua._AutomationClient.instance()


def automation():
    return client().IUIAutomation


def tree_item_request() -> tuple:
    """(TreeItem condition, cache request), built once per thread. The
    request caches the found elements themselves (TreeScope_Element) - the
    children scope belongs to the FindAll call, not to the cache."""
    key = threading.get_ident()
    if key not in _requests:
        uia = automation()
        condition = uia.CreatePropertyCondition(CONTROL_TYPE, TREE_ITEM)
        request = uia.CreateCacheRequest()
        for prop in (CONTROL_TYPE, NAME, EC_STATE):
            request.AddProperty(prop)
        request.AddPattern(EC_PATTERN)
        request.TreeScope = SCOPE_ELEMENT
        _requests[key] = (condition, request)
    return _requests[key]


def raw(control):
    """The control's IUIAutomationElement, or None (a fake has none)."""
    try:
        return getattr(control, "Element", None) or None
    except Exception:  # noqa: BLE001 - a stale wrapper refinds and may fail
        return None


def tree_item_children(element) -> list:
    """The element's TreeItem children, cached, in one call. A UIA timeout
    propagates - the subtree is unknown, not empty; any other COM error
    (an element gone mid-walk) is no children."""
    condition, request = tree_item_request()
    try:
        found = element.FindAllBuildCache(SCOPE_CHILDREN, condition, request)
    except comtypes.COMError as exc:
        if is_uia_timeout(exc):
            raise
        return []
    if not found:
        return []
    return [found.GetElement(i) for i in range(found.Length)]


def cached_state(element) -> int:
    return element.GetCachedPropertyValue(EC_STATE)


def expand_cached(element) -> None:
    iface = client().UIAutomationCore.IUIAutomationExpandCollapsePattern
    element.GetCachedPattern(EC_PATTERN).QueryInterface(iface).Expand()


def wrap(element):
    """A uiautomation Control for the element - for reading labels only."""
    return auto.Control.CreateControlFromElement(element)
