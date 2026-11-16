"""The `uiautomation` names the core calls, over AT-SPI (gi `Atspi` 2.0).

`Control` wraps one `Atspi.Accessible` with the uiautomation Control duck
type, so the walk, the selectors and the actions run unchanged. Properties
read live (only the role, which never changes, is kept), and a dead object
raises, as a stale UIA element does. gi is imported on first use, so the
module loads without a session bus.

Two AT-SPI habits are turned into UIA's:

- A call the app does not answer in AT_SPI_TIMEOUT raises AtspiTimeout,
  which `errors.is_uia_timeout` reads as UIA_E_TIMEOUT. libatspi reports some
  timed-out reads as an empty answer (a "" name, -1 children), so an empty
  answer that took the whole timeout counts as one too.
- The desktop's children are the top-level windows of every application, as
  on Windows; the application level is skipped, and a top-level window's
  parent is the desktop.
"""

from __future__ import annotations

import contextlib
import re
import time
import warnings
from collections import deque
from dataclasses import dataclass

from ..core.errors import UIA_E_TIMEOUT, PilotError, Timeout
from ..core.util import wait_until
from . import input as _input
from . import roles, x11

AT_SPI_TIMEOUT = 3.0
# A read that took this long and came back empty was a timeout.
_SILENT = AT_SPI_TIMEOUT * 0.9
# How long a pattern action waits for the state it set to show.
STATE_WAIT = 2.0
# How long SetFocus waits for FOCUSED when the app refused grab_focus.
FOCUS_WAIT = 0.5
FOCUS_SEARCH_CAP = 5000
# How long one read of states, attributes or extents is reused: within one
# describe, never across a poll.
MEMO_TTL = 0.02
INT_MIN = -(2**31)
# GtkBuilder's ids for objects the UI file left unnamed: not stable.
_BUILDER_ID = re.compile(r"___object_\d+___")

_atspi = None
_GLIB_ERROR: tuple = ()


def atspi():
    """`gi.repository.Atspi`, loaded and given AT_SPI_TIMEOUT on first use."""
    global _atspi, _GLIB_ERROR
    if _atspi is None:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, GLib

        # Both the call and the app-startup timeout: the startup one (15 s by
        # default) otherwise governs the first call to an app.
        Atspi.set_timeout(int(AT_SPI_TIMEOUT * 1000), int(AT_SPI_TIMEOUT * 1000))
        _GLIB_ERROR = (GLib.Error,)
        _atspi = Atspi
    return _atspi


class ElementGone(Exception):
    """The object is gone: its app quit, or it left the tree."""


class AtspiTimeout(Exception):
    """The app did not answer an AT-SPI call in time: a UIA timeout to the
    core (`errors.is_uia_timeout` reads `hresult` and args[0])."""

    hresult = UIA_E_TIMEOUT

    def __init__(self, what: str) -> None:
        super().__init__(
            UIA_E_TIMEOUT, f"AT-SPI {what}: the app did not answer in {AT_SPI_TIMEOUT:g}s"
        )


class AtspiError(PilotError):
    """The app answered an AT-SPI call with an error, and the object is still
    alive: the app refused the call (GTK4 refuses grab_focus on most
    widgets). Not ElementGone: the element is there."""


# What a read that skips one unreadable object catches: a gone object, and
# one whose app refused the read (before AtspiError, both were ElementGone).
_UNREADABLE = (ElementGone, AtspiError)

# Error texts that say the object or its app is gone.
_GONE_TEXTS = ("no longer exists", "unknown object", "defunct", "does not exist")


def _owner(fn, args):
    """The accessible a call is made on: a bound method's object, else the
    first argument (`Atspi.Text.get_text(acc, ...)`); None when neither is."""
    for candidate in (getattr(fn, "__self__", None), *args[:1]):
        if candidate is not None and hasattr(candidate, "get_state_set"):
            return candidate
    return None


def _is_alive(acc) -> bool:
    """Whether the object answers a state read and is not DEFUNCT."""
    try:
        states = acc.get_state_set().get_states()
    except Exception:  # noqa: BLE001 - any failure to answer is a dead object
        return False
    return "defunct" not in {state.value_nick for state in states}


def _call(what: str, fn, *args, empty=None):
    """fn(*args) with AT-SPI's errors as AtspiTimeout, ElementGone or
    AtspiError. An error is ElementGone only when the object is gone: the
    error says so, or the object no longer answers a state read; an object
    that answers refused the call (AtspiError).
    `empty(result)` says whether the answer is the empty one libatspi gives
    for a call that timed out."""
    start = time.monotonic()
    try:
        result = fn(*args)
    except _GLIB_ERROR as exc:
        text = str(exc).lower()
        if time.monotonic() - start >= _SILENT or "did not receive a reply" in text:
            raise AtspiTimeout(what) from exc
        owner = _owner(fn, args)
        if any(t in text for t in _GONE_TEXTS) or owner is None or not _is_alive(owner):
            raise ElementGone(f"AT-SPI {what}: {exc}") from exc
        raise AtspiError(f"AT-SPI {what}: the app refused the call ({exc})") from exc
    if empty is not None and empty(result) and time.monotonic() - start >= _SILENT:
        raise AtspiTimeout(what)
    return result


class PatternId:
    """uiautomation's pattern ids (UIA_*PatternId), for GetPattern."""

    InvokePattern = 10000
    SelectionPattern = 10001
    ValuePattern = 10002
    RangeValuePattern = 10003
    ScrollPattern = 10004
    ExpandCollapsePattern = 10005
    WindowPattern = 10009
    SelectionItemPattern = 10010
    TextPattern = 10014
    TogglePattern = 10015
    ScrollItemPattern = 10017
    LegacyIAccessiblePattern = 10018
    ItemContainerPattern = 10019


class ExpandCollapseState:
    Collapsed = 0
    Expanded = 1
    PartiallyExpanded = 2
    LeafNode = 3


class ToggleState:
    Off = 0
    On = 1
    Indeterminate = 2


@dataclass(frozen=True)
class Rect:
    """A BoundingRectangle: screen pixels, right and bottom exclusive."""

    left: int
    top: int
    right: int
    bottom: int

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


EMPTY = Rect(0, 0, 0, 0)

# Role sets the patterns are decided by. Chromium lists every interface on
# every object, so an interface alone never decides a pattern.
INVOKE_ROLES = frozenset(
    {"button", "push button", "menu item", "link", "push button menu", "tearoff menu item"}
    | {"icon", "desktop icon", "arrow"}
)
TOGGLE_ROLES = frozenset({"check box", "toggle button", "check menu item", "switch"})
RADIO_ROLES = frozenset({"radio button", "radio menu item"})
SELECT_ROLES = frozenset(
    {"list item", "tree item", "page tab", "table cell", "table row", "desktop icon"} | RADIO_ROLES
)
TEXT_ROLES = frozenset({"entry", "password text", "text", "spin button", "editbar", "date editor"})
NUMBER_ROLES = frozenset({"slider", "scroll bar", "progress bar", "level bar", "dial"})
READ_ONLY_NUMBERS = frozenset({"progress bar", "level bar"})
POPUP_ROLES = frozenset({"menu", "popup menu", "list", "list box", "window"})
# Not Invoke even with a click action: UIA gives these Toggle, SelectionItem,
# ExpandCollapse or Value instead, and an entry's "activate" is Enter.
NOT_INVOKE = (
    TOGGLE_ROLES
    | SELECT_ROLES
    | TEXT_ROLES
    | {"combo box", "menu", "frame", "window", "dialog", "alert", "label", "static"}
)
# Items a toolkit leaves unnamed and names through a label child (GTK4 lists).
LABELLED_BY_CHILD = frozenset(
    {"list item", "tree item", "menu item", "page tab", "radio menu item", "check menu item"}
)
LABEL_ROLES = frozenset({"label", "static", "text"})
INLINE_ROLES = frozenset({"link", "static", "label", "image", "text"})
# Blocks of text with no Value: `ui read` reads them through a TextPattern.
BLOCK_TEXT_ROLES = frozenset({"paragraph", "heading", "block quote"})

INVOKE_ACTIONS = ("click", "press", "activate", "jump", "open", "invoke")
TOGGLE_ACTIONS = ("toggle", "click", "press", "check", "uncheck", "activate")
SELECT_ACTIONS = ("select", "click", "press", "activate", "check")
EXPAND_ACTIONS = ("expand or contract", "expand", "toggle", "open", "click", "press", "activate")
COLLAPSE_ACTIONS = ("expand or contract", "collapse", "toggle", "close", "click", "press")
OPENERS = frozenset({"toggle button", "button", "push button"})

_UNKNOWN = object()


class Control:
    """One AT-SPI object as a uiautomation Control. `accessible` is the raw
    `Atspi.Accessible`. See the plan's `auto.py` contract for each property."""

    def __init__(self, accessible, parent=_UNKNOWN) -> None:
        self.accessible = accessible
        self._parent = parent
        self._role_name: str | None = None
        self._type: str | None = None
        self._memo: dict[str, tuple[float, object]] = {}

    @staticmethod
    def CreateControlFromElement(element) -> Control:
        return Control(element)

    @property
    def Element(self):
        """No raw UIA element: `uiacache.raw` takes the per-property path."""
        return None

    def __repr__(self) -> str:
        return f"<Control {self._role_name or '?'} {getattr(self.accessible, 'path', '')}>"

    # ------------------------------------------------------------ reads

    def _role(self) -> str:
        if self._role_name is None:
            self._role_name = _call("role", self.accessible.get_role_name)
        return self._role_name

    def _fresh(self, key: str, read):
        """`read()`, or its answer from the last MEMO_TTL: one describe reads
        the states, attributes and extents twice each."""
        now = time.monotonic()
        hit = self._memo.get(key)
        if hit is not None and now - hit[0] < MEMO_TTL:
            return hit[1]
        value = read()
        self._memo[key] = (now, value)
        return value

    def _states(self) -> set[str]:
        return self._fresh("states", self._read_states)

    def _read_states(self) -> set[str]:
        state_set = _call(
            "states", self.accessible.get_state_set, empty=lambda s: not s.get_states()
        )
        states = {state.value_nick for state in state_set.get_states()}
        if "defunct" in states:
            raise ElementGone("the AT-SPI object is defunct")
        return states

    def _attributes(self) -> dict:
        return self._fresh(
            "attributes", lambda: _call("attributes", self.accessible.get_attributes) or {}
        )

    def _actions(self) -> list[str]:
        if not self.accessible.is_action():
            return []
        count = _call("actions", self.accessible.get_n_actions)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # no undeprecated twin in gi
            return [
                (_call("action name", self.accessible.get_action_name, i) or "").casefold()
                for i in range(count)
            ]

    def _action(self, names, *, first: bool = False) -> int | None:
        """The index of the first action named in `names`, else action 0
        when `first` and it has any; None when it has none of them."""
        actions = self._actions()
        for name in names:
            if name in actions:
                return actions.index(name)
        return 0 if first and actions else None

    def _do(self, index: int, what: str) -> None:
        self._memo.clear()
        if not _call(what, self.accessible.do_action, index):
            raise PilotError(f"the app refused the {what} action")

    @property
    def Name(self) -> str:
        """The accessible name, layout padding stripped (GTK3 pads menu items
        where the accelerator goes). An unnamed item takes its label child's."""
        name = (_call("name", self.accessible.get_name, empty=lambda n: not n) or "").strip()
        if not name:
            self._states()  # a dead object answers "" too
            if self._role() in LABELLED_BY_CHILD:
                name = self._child_label()
        return name

    def _child_label(self) -> str:
        queue = deque((child, 1) for child in self.GetChildren())
        while queue:
            child, depth = queue.popleft()
            if child._role() in LABEL_ROLES:
                name = (_call("name", child.accessible.get_name) or "").strip()
                if name:
                    return name
            if depth < 3:
                queue.extend((grandchild, depth + 1) for grandchild in child.GetChildren())
        return ""

    @property
    def ControlTypeName(self) -> str:
        """`roles.control_type` of the role: "ButtonControl", ..."""
        if self._type is None:
            role = self._role()
            parent = self._parent_role() if role in roles.CONTEXT_ROLES else None
            first = True
            if role == "table cell" and parent == "tree table":
                first = _cell_column(self.accessible) == 0
            self._type = roles.control_type(role, parent, first_column=first)
        return self._type

    @property
    def LocalizedControlType(self) -> str:
        return self._role()

    @property
    def AutomationId(self) -> str:
        """`get_accessible_id()` (GtkBuilder's generated ids left out), else
        the `id` object attribute (Chromium's DOM id)."""
        found = _call("accessible id", self.accessible.get_accessible_id) or ""
        if _BUILDER_ID.fullmatch(found):
            found = ""
        return found or self._attributes().get("id", "")

    @property
    def ClassName(self) -> str:
        """The `class` object attribute (Chromium's DOM class), else for a
        top-level window its WM_CLASS class, else the role name."""
        found = self._attributes().get("class")
        if found:
            return found
        if self._is_top():
            handle = self.NativeWindowHandle
            if handle:
                found = x11.wm_class(handle)[1]
        return found or self._role()

    @property
    def ProcessId(self) -> int:
        return _call("process id", self.accessible.get_process_id)

    def GetRuntimeId(self) -> tuple:
        """Stable for one live object: (bus name, object path)."""
        return (self.accessible.app.bus_name, self.accessible.path)

    # ------------------------------------------------------------ geometry

    @property
    def BoundingRectangle(self) -> Rect:
        """Screen extents; a top-level window's visible X frame. Window-
        relative extents (GTK4 on X11) are moved by the window's client
        origin. An object with no place (GTK3's closed menus) is EMPTY."""
        if self._is_top():
            handle = self.NativeWindowHandle
            if handle:
                return Rect(*x11.frame_rect(handle))
        return self._extents()

    def _extents(self) -> Rect:
        return self._fresh("extents", self._read_extents)

    def _read_extents(self) -> Rect:
        if not self.accessible.is_component():
            return EMPTY
        atspi_ = atspi()
        relative = _window_relative(self.accessible)
        kind = atspi_.CoordType.WINDOW if relative else atspi_.CoordType.SCREEN
        box = _call("extents", self.accessible.get_extents, kind)
        if box.x <= INT_MIN or box.y <= INT_MIN:
            return EMPTY
        left, top = box.x, box.y
        if relative and not self._is_top():
            window = self.GetTopLevelControl()
            handle = window.NativeWindowHandle if window is not None else 0
            if handle:
                origin = x11.x_rect(handle)  # GTK4 counts from the X window's corner
                left, top = left + origin[0], top + origin[1]
        return Rect(left, top, left + box.width, top + box.height)

    @property
    def IsOffscreen(self) -> bool:
        """Not SHOWING, or no size; a top-level window also when it has no X
        window on the screen, or that window is minimized."""
        if "showing" not in self._states():
            return True
        if self._is_top():
            handle = _handle_of(self)
            if handle == 0 or (handle and x11.is_minimized(handle)):
                return True
        rect = self.BoundingRectangle
        return rect.width() <= 0 or rect.height() <= 0

    @property
    def IsEnabled(self) -> bool:
        """ENABLED or SENSITIVE."""
        return bool(self._states() & {"enabled", "sensitive"})

    @property
    def HasKeyboardFocus(self) -> bool:
        return "focused" in self._states()

    @property
    def NativeWindowHandle(self) -> int:
        """The X client window of a top-level window (matched by pid, title
        and rect), else 0."""
        return (_handle_of(self) or 0) if self._is_top() else 0

    # ------------------------------------------------------------ tree

    def GetChildren(self) -> list[Control]:
        count = _call("child count", self.accessible.get_child_count, empty=lambda n: n < 0)
        if count < 0:
            raise ElementGone("the AT-SPI object has no child count: it is gone")
        parent = GetRootControl() if self._role() == "application" else self
        children = []
        for index in range(count):
            child = _call("child", self.accessible.get_child_at_index, index)
            if child is not None:
                children.append(Control(child, parent))
        return children

    def GetParentControl(self) -> Control | None:
        """The parent; the desktop for a top-level window."""
        if self._parent is _UNKNOWN:
            raw = _call("parent", self.accessible.get_parent)
            if raw is None:
                self._states()  # a dead object has no parent either
                self._parent = None
            elif _call("role", raw.get_role_name) in ("application", "desktop frame"):
                self._parent = GetRootControl()
            else:
                self._parent = Control(raw)
        return self._parent

    def _parent_role(self) -> str | None:
        parent = self.GetParentControl()
        if parent is None:
            return None
        return "application" if isinstance(parent, _Desktop) else parent._role()

    def _is_top(self) -> bool:
        """A top-level window: the child of an application."""
        return isinstance(self.GetParentControl(), _Desktop) and self._role() != "application"

    def GetTopLevelControl(self) -> Control | None:
        """The frame/dialog/window ancestor-or-self (the child of the
        application); None for the desktop."""
        node = self
        for _ in range(256):
            if node is None or isinstance(node, _Desktop):
                return None
            if node._is_top():
                return node
            node = node.GetParentControl()
        return None

    # ------------------------------------------------------------ patterns

    def GetPattern(self, pattern_id: int):
        """The pattern for a PatternId, or None."""
        getter = _PATTERN_GETTERS.get(pattern_id)
        return getattr(self, getter)() if getter else None

    def GetInvokePattern(self):
        """An invokable role with an action (click/press/activate/jump/open,
        else action 0), or any other role with one of those named actions."""
        role = self._role()
        if role in NOT_INVOKE:
            return None
        index = self._action(INVOKE_ACTIONS, first=role in INVOKE_ROLES)
        return None if index is None else InvokePattern(self)

    def GetTogglePattern(self):
        """A check box, toggle button, check menu item or switch, or any
        CHECKABLE object but a radio; Toggle() raises when it has no action."""
        role = self._role()
        if role in RADIO_ROLES:
            return None
        if role in TOGGLE_ROLES or "checkable" in self._states():
            return TogglePattern(self)
        return None

    def GetValuePattern(self):
        """Text for an entry (EditableText to write), the Value interface for
        a number, the selection for a combo box."""
        role = self._role()
        if role in TEXT_ROLES and self.accessible.is_text():
            return ValuePattern(self, "text")
        if role in NUMBER_ROLES and self.accessible.is_value():
            return ValuePattern(self, "number")
        if role == "combo box":
            return ValuePattern(self, "choice")
        if self.accessible.is_text() and "editable" in self._states():
            return ValuePattern(self, "text")
        return None

    def GetTextPattern(self):
        """DocumentRange.GetText(-1) over the Text interface, for a Document,
        an Edit or a block of text (Chromium's <p>: its Text is one U+FFFC
        per child, the range reads the children's text)."""
        if not self.accessible.is_text():
            return None
        if self.ControlTypeName in ("DocumentControl", "EditControl"):
            return TextPattern(self)
        return TextPattern(self) if self._role() in BLOCK_TEXT_ROLES else None

    def GetSelectionItemPattern(self):
        """An item of a list, tree, table or tab list, a radio, or a combo
        box's choice."""
        role = self._role()
        if role in SELECT_ROLES or (role == "menu item" and self._is_choice()):
            return SelectionItemPattern(self)
        return None

    def _is_choice(self) -> bool:
        """A menu item of a combo box's list (GTK3, Chromium's <select>)."""
        if "selectable" not in self._states():
            return False
        menu = self.GetParentControl()
        if menu is None or isinstance(menu, _Desktop) or menu._role() not in POPUP_ROLES:
            return False
        owner = menu.GetParentControl()
        return (
            owner is not None and not isinstance(owner, _Desktop) and (owner._role() == "combo box")
        )

    def GetExpandCollapsePattern(self):
        """EXPANDABLE, a combo box, or a submenu item (GTK3 marks neither)."""
        role = self._role()
        if "expandable" in self._states() or role == "combo box":
            return ExpandCollapsePattern(self)
        if role == "menu" and self._parent_role() in roles.MENU_PARENTS:
            return ExpandCollapsePattern(self)
        return None

    def GetScrollPattern(self):
        """A scroll pane through its scroll bars' Value."""
        if self._role() not in ("scroll pane", "viewport"):
            return None
        bars = [
            c for c in self.GetChildren() if c._role() == "scroll bar" and c.accessible.is_value()
        ]
        return ScrollPattern(self, bars) if bars else None

    def GetScrollItemPattern(self):
        """Component.scroll_to."""
        return ScrollItemPattern(self) if self.accessible.is_component() else None

    def GetLegacyIAccessiblePattern(self):
        """DoDefaultAction (action 0), Value; on an object with an action."""
        return LegacyIAccessiblePattern(self) if self._actions() else None

    def GetSelectionPattern(self):
        return None

    def GetRangeValuePattern(self):
        return None

    def GetWindowPattern(self):
        return None

    def GetItemContainerPattern(self):
        return None

    # ------------------------------------------------------------ acting

    def _require_enabled(self) -> None:
        if not self.IsEnabled:
            raise PilotError("the element is disabled")

    def SetFocus(self) -> bool:
        """Activate its window, then Component.grab_focus. When the app
        refuses grab_focus or answers False (GTK4 does for most widgets),
        the element counts as focused if it shows FOCUSED within FOCUS_WAIT;
        otherwise this raises PilotError, as UIA's SetFocus raises: keys sent
        on would land in whatever has the focus. The pointer is never used
        here: a click can act (a button, a link), and only the caller can
        say that is safe."""
        window = self.GetTopLevelControl()
        handle = window.NativeWindowHandle if window is not None else 0
        if handle and x11.active_window() != handle and not x11.activate(handle):
            return False
        if window is self or not self.accessible.is_component():
            return bool(handle)
        refused = None
        try:
            if _call("grab focus", self.accessible.grab_focus):
                return True
        except AtspiError as exc:
            refused = exc
        try:
            wait_until(self._has_focus, timeout=FOCUS_WAIT, interval=0.05, what="focus")
        except Timeout:
            why = f" ({refused})" if refused else ""
            raise PilotError(
                "could not give the element the keyboard focus: the app refused AT-SPI "
                f"grab_focus{why}. Click it first (`ui click SELECTOR`), then run the "
                "command without --into (`ui type`, `ui keys`; `ui read focus --copy`)"
            ) from refused
        return True

    def _has_focus(self) -> bool:
        self._memo.clear()
        return "focused" in self._states()

    def SetActive(self, waitTime: float = 0.5) -> bool:
        """Activate the top-level window (`x11.activate`)."""
        window = self.GetTopLevelControl()
        handle = window.NativeWindowHandle if window is not None else 0
        return bool(handle) and x11.activate(handle)

    def CaptureToImage(
        self, savePath: str, x: int = 0, y: int = 0, width: int = 0, height: int = 0
    ) -> bool:
        """Grab the BoundingRectangle (or the part of it given) and save it."""
        rect = self.BoundingRectangle
        width = width or rect.width() - x
        height = height or rect.height() - y
        if width <= 0 or height <= 0:
            return False
        x11.grab(rect.left + x, rect.top + y, width, height).save(savePath)
        return True

    def Click(
        self,
        x: int | None = None,
        y: int | None = None,
        ratioX: float = 0.5,
        ratioY: float = 0.5,
        **_,
    ) -> None:
        """Pointer click inside the rect: at (x, y) from its top-left, else at
        the ratios (the centre)."""
        rect = self.BoundingRectangle
        left = rect.left + (x if x is not None else int(rect.width() * ratioX))
        top = rect.top + (y if y is not None else int(rect.height() * ratioY))
        _input.click_point(left, top)


class _Desktop(Control):
    """The AT-SPI desktop, whose children are every application's top-level
    windows."""

    def __init__(self, accessible) -> None:
        super().__init__(accessible, None)
        self._role_name = "desktop frame"

    Name = "Desktop"
    ControlTypeName = "PaneControl"
    AutomationId = ""
    ClassName = "desktop frame"
    IsOffscreen = False
    IsEnabled = True
    HasKeyboardFocus = False

    @property
    def BoundingRectangle(self) -> Rect:
        left, top, width, height = x11.screen_rect()
        return Rect(left, top, left + width, top + height)

    @property
    def NativeWindowHandle(self) -> int:
        from . import win32gui

        return win32gui.GetDesktopWindow()

    def GetRuntimeId(self) -> tuple:
        return ("desktop",)

    def GetChildren(self) -> list[Control]:
        """Every application's top-level windows; an app that quit mid-read
        has none."""
        windows = []
        for app in _apps():
            with contextlib.suppress(*_UNREADABLE):
                windows += Control(app, self).GetChildren()
        return windows

    def GetParentControl(self) -> None:
        return None

    def _is_top(self) -> bool:
        return False


# ---------------------------------------------------------------- patterns


class _Pattern:
    def __init__(self, control: Control) -> None:
        self.control = control


class InvokePattern(_Pattern):
    def Invoke(self, waitTime: float = 0) -> bool:
        """The click action; a disabled element or an action the app refuses
        raises, as UIA's Invoke does."""
        control = self.control
        control._require_enabled()
        index = control._action(INVOKE_ACTIONS, first=control._role() in INVOKE_ROLES)
        if index is None:
            raise PilotError("the element has no click action any more")
        control._do(index, "click")
        return True


class TogglePattern(_Pattern):
    @property
    def ToggleState(self) -> int:
        states = self.control._states()
        if "indeterminate" in states:
            return ToggleState.Indeterminate
        return ToggleState.On if states & {"checked", "pressed"} else ToggleState.Off

    def Toggle(self, waitTime: float = 0) -> bool:
        """Its toggle action, then wait for the state to change: an action
        that changes nothing raises."""
        control = self.control
        control._require_enabled()
        index = control._action(TOGGLE_ACTIONS, first=True)
        if index is None:
            raise PilotError("the app offers no action that toggles it; use the pointer")
        before = self.ToggleState
        control._do(index, "toggle")
        _wait_for(lambda: self.ToggleState != before, "the toggle state to change")
        return True


class ValuePattern(_Pattern):
    """`kind`: text (Text, written through EditableText), number (Value),
    choice (a combo box: its name or its selected item's)."""

    def __init__(self, control: Control, kind: str) -> None:
        super().__init__(control)
        self.kind = kind

    @property
    def Value(self) -> str:
        control = self.control
        if self.kind == "text":
            if control._role() == "password text":
                return ""
            return _deep_text(control.accessible)  # a contenteditable: its paragraphs
        if self.kind == "number":
            value = _call("value", control.accessible.get_current_value)
            return f"{value:g}"
        return control.Name or _selected_name(control)

    @property
    def IsReadOnly(self) -> bool:
        control = self.control
        states = control._states()
        if self.kind == "text":
            return "editable" not in states or "read-only" in states
        if self.kind == "number":
            return control._role() in READ_ONLY_NUMBERS or "read-only" in states
        return True

    def SetValue(self, value: str, waitTime: float = 0) -> bool:
        control = self.control
        if self.IsReadOnly:
            raise PilotError("this element is read-only")
        if self.kind == "number":
            ok = _call("set value", control.accessible.set_current_value, float(value))
        elif not control.accessible.is_editable_text():
            raise PilotError(
                "the app offers no EditableText on this element (Chromium does not); "
                "use `cdp set-value`, or `ui type` with the window in front"
            )
        else:
            ok = _call(
                "set text", atspi().EditableText.set_text_contents, control.accessible, value
            )
        if not ok:
            raise PilotError("the app refused the new value")
        return True


class _Range:
    def __init__(self, control: Control) -> None:
        self.control = control

    def GetText(self, maxLength: int = -1) -> str:
        text = _deep_text(self.control.accessible)
        return text if maxLength < 0 else text[:maxLength]


class TextPattern(_Pattern):
    @property
    def DocumentRange(self) -> _Range:
        return _Range(self.control)


class SelectionItemPattern(_Pattern):
    @property
    def IsSelected(self) -> bool:
        states = self.control._states()
        if self.control._role() in RADIO_ROLES:
            return "checked" in states
        return "selected" in states

    def Select(self, waitTime: float = 0) -> bool:
        """A radio or a combo box's choice through its action; any other item
        through its parent's Selection. A selection that does not show raises."""
        control = self.control
        control._require_enabled()
        role = control._role()
        parent = control.GetParentControl()
        if role == "radio menu item":
            # A menu closed already does not count: only one this action closed.
            was_open = not _menu_closed(control, parent)
            self._act(SELECT_ACTIONS)
            _wait_for(
                lambda: (was_open and _menu_closed(control, parent)) or self.IsSelected,
                "the item to read as selected or its menu to close",
            )
        elif role in RADIO_ROLES:
            self._act(SELECT_ACTIONS)
            _wait_for(lambda: self.IsSelected, "the item to read as selected")
        elif role == "menu item":
            self._act(SELECT_ACTIONS)  # the popup closes: nothing left to read
        elif (
            parent is not None
            and not isinstance(parent, _Desktop)
            and parent.accessible.is_selection()
        ):
            index = _call("index", control.accessible.get_index_in_parent)
            if not _call("select", parent.accessible.select_child, index):
                raise PilotError("the app refused the selection")
            _wait_for(lambda: self.IsSelected, "the item to read as selected")
        else:
            self._act(("select", "click", "press", "activate"), first=False)
        return True

    def _act(self, names, first: bool = True) -> None:
        index = self.control._action(names, first=first)
        if index is None:
            raise PilotError("the app offers no way to select this item; use the pointer")
        self.control._do(index, "select")


class ExpandCollapsePattern(_Pattern):
    @property
    def ExpandCollapseState(self) -> int:
        control = self.control
        states = control._states()
        if "expanded" in states:
            return ExpandCollapseState.Expanded
        if "expandable" in states or "collapsed" in states:
            return ExpandCollapseState.Collapsed
        if control._role() in ("combo box", "menu"):
            # GTK3 marks neither: its popup's items show while it is open.
            popup = _popup_child(control)
            if popup is not None and "showing" in popup._states():
                return ExpandCollapseState.Expanded
            return ExpandCollapseState.Collapsed
        return ExpandCollapseState.LeafNode

    def Expand(self, waitTime: float = 0) -> bool:
        if self.ExpandCollapseState != ExpandCollapseState.Expanded:
            self._toggle(EXPAND_ACTIONS)
            _wait_for(
                lambda: self.ExpandCollapseState == ExpandCollapseState.Expanded, "it to expand"
            )
        return True

    def Collapse(self, waitTime: float = 0) -> bool:
        if self.ExpandCollapseState == ExpandCollapseState.Expanded:
            self._toggle(COLLAPSE_ACTIONS)
            _wait_for(
                lambda: self.ExpandCollapseState != ExpandCollapseState.Expanded, "it to collapse"
            )
        return True

    def _toggle(self, names) -> None:
        """Its own action, else its opener child's (GTK4 drop-downs and menu
        buttons act through an inner toggle button)."""
        control = self.control
        control._require_enabled()
        index = control._action(names, first=True)
        if index is None:
            for child in control.GetChildren():
                if child._role() in OPENERS:
                    index = child._action(names, first=True)
                    if index is not None:
                        control = child
                        break
        if index is None:
            raise PilotError("the app offers no action that opens or closes it; use the pointer")
        control._do(index, "expand")


class ScrollPattern(_Pattern):
    """Over the scroll pane's scroll bars. View sizes assume a pixel-based
    range (GTK's scrolled windows): the bar's range plus the visible length."""

    NoScrollValue = -1

    def __init__(self, control: Control, bars: list[Control]) -> None:
        super().__init__(control)
        self.bars = bars

    def _bar(self, vertical: bool) -> Control | None:
        for bar in self.bars:
            states = bar._states()
            if "vertical" in states or "horizontal" in states:
                is_vertical = "vertical" in states
            else:
                rect = bar._extents()
                is_vertical = rect.height() > rect.width()
            if is_vertical == vertical and "showing" in states:
                return bar
        return None

    @staticmethod
    def _range(bar: Control) -> tuple[float, float, float]:
        acc = bar.accessible
        return (
            _call("value", acc.get_minimum_value),
            _call("value", acc.get_current_value),
            _call("value", acc.get_maximum_value),
        )

    def _scrollable(self, vertical: bool) -> bool:
        bar = self._bar(vertical)
        if bar is None:
            return False
        low, _, high = self._range(bar)
        return high > low

    def _percent(self, vertical: bool) -> float:
        bar = self._bar(vertical)
        if bar is None:
            return self.NoScrollValue
        low, value, high = self._range(bar)
        return 100.0 * (value - low) / (high - low) if high > low else self.NoScrollValue

    def _view(self, vertical: bool) -> float:
        bar = self._bar(vertical)
        if bar is None:
            return 100.0
        low, _, high = self._range(bar)
        rect = self.control._extents()
        page = rect.height() if vertical else rect.width()
        return 100.0 * page / (page + high - low) if page + high - low > 0 else 100.0

    @property
    def VerticallyScrollable(self) -> bool:
        return self._scrollable(True)

    @property
    def HorizontallyScrollable(self) -> bool:
        return self._scrollable(False)

    @property
    def VerticalScrollPercent(self) -> float:
        return self._percent(True)

    @property
    def HorizontalScrollPercent(self) -> float:
        return self._percent(False)

    @property
    def VerticalViewSize(self) -> float:
        return self._view(True)

    @property
    def HorizontalViewSize(self) -> float:
        return self._view(False)

    def SetScrollPercent(
        self, horizontalPercent: float, verticalPercent: float, waitTime: float = 0
    ) -> bool:
        for vertical, percent in ((False, horizontalPercent), (True, verticalPercent)):
            if percent == self.NoScrollValue:
                continue
            bar = self._bar(vertical)
            if bar is None:
                raise PilotError(
                    "it does not scroll " + ("vertically" if vertical else "horizontally")
                )
            low, _, high = self._range(bar)
            target = low + (high - low) * percent / 100.0
            if not _call("scroll", bar.accessible.set_current_value, target):
                raise PilotError("the app refused the scroll position")
        return True


class ScrollItemPattern(_Pattern):
    def ScrollIntoView(self, waitTime: float = 0) -> bool:
        if not _call("scroll to", self.control.accessible.scroll_to, atspi().ScrollType.ANYWHERE):
            raise PilotError("the app refused to scroll the element into view")
        return True


class LegacyIAccessiblePattern(_Pattern):
    @property
    def Name(self) -> str:
        return self.control.Name

    @property
    def Description(self) -> str:
        return _call("description", self.control.accessible.get_description) or ""

    @property
    def DefaultAction(self) -> str:
        actions = self.control._actions()
        return actions[0] if actions else ""

    @property
    def Value(self) -> str:
        pattern = self.control.GetValuePattern()
        return pattern.Value if pattern is not None else ""

    def DoDefaultAction(self) -> bool:
        control = self.control
        control._require_enabled()
        if not control._actions():
            raise PilotError("the element has no default action")
        control._do(0, "default")
        return True


_PATTERN_GETTERS = {
    PatternId.InvokePattern: "GetInvokePattern",
    PatternId.SelectionPattern: "GetSelectionPattern",
    PatternId.ValuePattern: "GetValuePattern",
    PatternId.RangeValuePattern: "GetRangeValuePattern",
    PatternId.ScrollPattern: "GetScrollPattern",
    PatternId.ExpandCollapsePattern: "GetExpandCollapsePattern",
    PatternId.WindowPattern: "GetWindowPattern",
    PatternId.SelectionItemPattern: "GetSelectionItemPattern",
    PatternId.TextPattern: "GetTextPattern",
    PatternId.TogglePattern: "GetTogglePattern",
    PatternId.ScrollItemPattern: "GetScrollItemPattern",
    PatternId.LegacyIAccessiblePattern: "GetLegacyIAccessiblePattern",
    PatternId.ItemContainerPattern: "GetItemContainerPattern",
}


def _wait_for(check, what: str) -> None:
    try:
        wait_until(check, timeout=STATE_WAIT, interval=0.05, what=what)
    except Timeout as exc:
        raise PilotError(f"the action did nothing: timed out waiting for {what}") from exc


def _menu_closed(item: Control, menu: Control | None) -> bool:
    """A menu item's popup closed after its action: the item, or the menu
    that held it, is gone or no longer showing. The choice took then."""
    try:
        if "showing" not in item._states():
            return True
        if menu is None or isinstance(menu, _Desktop):
            return False
        return "showing" not in menu._states()
    except _UNREADABLE:
        return True


def _popup_child(control: Control) -> Control | None:
    for child in control.GetChildren():
        if child._role() in POPUP_ROLES or child._role() in ("menu item", "check menu item"):
            return child
    return None


def _selected_name(combo: Control) -> str:
    """The SELECTED item of a combo box's list (Chromium's <select>)."""
    popup = _popup_child(combo)
    if popup is None:
        return ""
    for item in popup.GetChildren():
        if "selected" in item._states():
            return item.Name
    return ""


# ---------------------------------------------------------------- raw reads


def _text(acc) -> str:
    return _call("text", atspi().Text.get_text, acc, 0, -1) or ""


def _cell_column(acc) -> int:
    """A table cell's column (TableCell), 0 when it does not say."""
    try:
        return int(atspi().TableCell.get_position(acc)[2])
    except Exception:  # noqa: BLE001 - no TableCell: one column
        return 0


def _deep_text(acc, depth: int = 0) -> str:
    """The Text of `acc` with each embedded object (U+FFFC: Chromium's
    child elements) replaced by that object's own text; a block-level child
    on a line of its own."""
    text = _text(acc)
    if "￼" not in text or depth > 32 or not acc.is_hypertext():
        return text.replace("￼", "")
    atspi_ = atspi()
    chars = list(text)
    for index in reversed(range(_call("links", atspi_.Hypertext.get_n_links, acc))):
        link = _call("link", atspi_.Hypertext.get_link, acc, index)
        start = _call("link start", link.get_start_index)
        child = _call("link object", link.get_object, 0)
        if child is None or not 0 <= start < len(chars) or chars[start] != "￼":
            continue
        inner = _deep_text(child, depth + 1) if child.is_text() else ""
        if _call("role", child.get_role_name) not in INLINE_ROLES:
            inner = "\n" + inner.strip("\n") + "\n"
        chars[start] = inner
    lines = "".join(chars).replace("￼", "").split("\n")
    return "\n".join(line for line in lines if line.strip())


# Per app (bus name): whether it reports window-relative extents.
_RELATIVE: dict[str, bool] = {}


def _window_relative(acc) -> bool:
    """GTK4 on X11 knows no screen position: its SCREEN extents are (0, 0)
    and its WINDOW ones relative to the window."""
    bus = acc.app.bus_name
    if bus not in _RELATIVE:
        app = _call("application", acc.get_application) or acc  # GTK4 tells only there
        toolkit = (_call("toolkit", app.get_toolkit_name) or "").casefold()
        version = _call("toolkit version", app.get_toolkit_version) or ""
        _RELATIVE[bus] = toolkit == "gtk" and version.startswith("4.")
    return _RELATIVE[bus]


def _apps() -> list:
    desktop = atspi().get_desktop(0)
    count = _call("desktop", desktop.get_child_count)
    found = []
    for index in range(max(count, 0)):
        app = _call("app", desktop.get_child_at_index, index)
        if app is not None:
            found.append(app)
    return found


# ---------------------------------------------------------------- windows

# RuntimeId -> X window of a top-level object, and back; checked on reuse.
_HANDLES: dict[tuple, int] = {}
_CONTROLS: dict[int, Control] = {}
MATCH_FLOOR = 0.8


def _overlap(a: Rect, b: tuple[int, int, int, int]) -> float:
    """Intersection over union of two rects."""
    left, top = max(a.left, b[0]), max(a.top, b[1])
    right, bottom = min(a.right, b[2]), min(a.bottom, b[3])
    inter = max(0, right - left) * max(0, bottom - top)
    union = a.width() * a.height() + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _same_size(a: Rect, b: tuple[int, int, int, int]) -> float:
    return (
        1.0 if abs(a.width() - (b[2] - b[0])) <= 2 and abs(a.height() - (b[3] - b[1])) <= 2 else 0.0
    )


def _score(window: int, name: str, rect: Rect, relative: bool) -> float:
    """How well an X window fits a top-level object: 1 for the same title,
    plus how well the rects agree (only the size for window-relative
    extents, which have no position)."""
    titled = 1.0 if name and x11.title(window).strip() == name else 0.0
    if rect.width() <= 0 or rect.height() <= 0:
        return titled
    fit = _same_size if relative else _overlap
    return titled + max(fit(rect, x11.x_rect(window)), fit(rect, x11.frame_rect(window)))


def _match_window(control: Control) -> int:
    """The X client window of a top-level object: among the windows of its
    pid (all, when none carries it: then the title must agree), the best by
    title and rect, if it fits well enough. 0 when none does."""
    pid = control.ProcessId
    windows = x11.client_windows()
    mine = [w for w in windows if x11.pid(w) == pid]
    name = control.Name
    rect = control._extents()
    relative = _window_relative(control.accessible)
    best, best_score = 0, MATCH_FLOOR
    for window in mine or windows:
        score = _score(window, name, rect, relative)
        if not mine and score < 1.0:
            continue
        if score > best_score:
            best, best_score = window, score
    return best


def _handle_of(control: Control) -> int | None:
    """The X window of a top-level object, cached while both live; None when
    the X side cannot be asked (no x11 backend)."""
    key = control.GetRuntimeId()
    try:
        handle = _HANDLES.get(key)
        # X reuses a closed client's window ids: the pid tells a new window.
        if handle and x11.exists(handle) and x11.pid(handle) == control.ProcessId:
            return handle
        handle = _match_window(control)
    except NotImplementedError:
        return None
    if handle:
        _HANDLES[key] = handle
    return handle


def ControlFromHandle(hwnd: int) -> Control | None:
    """The AT-SPI top-level object of an X window: among the apps with the
    window's pid (every app when none has it), the top-level object that
    matches back to it. None when no object does."""
    if not hwnd or not x11.exists(hwnd):
        return None
    cached = _CONTROLS.pop(hwnd, None)
    if cached is not None:
        with contextlib.suppress(*_UNREADABLE):
            cached._states()  # a dead object raises
            if _handle_of(cached) == hwnd:
                _CONTROLS[hwnd] = cached
                return cached
    pid = x11.pid(hwnd)
    apps = _apps()
    mine = [app for app in apps if pid and _app_pid(app) == pid]
    root = GetRootControl()
    for app in mine or apps:
        try:
            windows = Control(app, root).GetChildren()
        except _UNREADABLE:
            continue
        for window in windows:
            with contextlib.suppress(*_UNREADABLE):
                if _handle_of(window) == hwnd:
                    _CONTROLS[hwnd] = window
                    return window
    return None


def _app_pid(app) -> int:
    try:
        return _call("process id", app.get_process_id)
    except _UNREADABLE:
        return 0


POPUP_WINDOW_TYPES = frozenset({"menu", "popup_menu", "dropdown_menu", "combo"})


def is_popup(window: Control) -> bool:
    """Whether a top-level object is a popup a drop-down's items can live in:
    a `window` or menu role (GTK3's combo lists and context menus), or an X
    window that is a menu or is override-redirect (Chromium's menus and
    <select> lists). GTK3 menu-bar menus and GTK4 popovers stay inside
    their owner instead, and are found there."""
    if window._role() in ("window", "menu", "popup menu"):
        return True
    handle = window.NativeWindowHandle
    if not handle:
        return False
    return bool(POPUP_WINDOW_TYPES & set(x11.window_type(handle))) or x11.is_override_redirect(
        handle
    )


def GetRootControl() -> Control:
    """The desktop: its children are every application's top-level windows."""
    return _Desktop(atspi().get_desktop(0))


def GetFocusedControl() -> Control | None:
    """The FOCUSED object in the active window; the window itself when none
    inside it is; None when the active window has no AT-SPI object."""
    handle = x11.active_window()
    window = ControlFromHandle(handle) if handle else None
    if window is None:
        return None
    return _focused_in(window) or window


def _focused_in(window: Control) -> Control | None:
    """Collection.get_matches when the app has it (one call), else a
    breadth-first search of the showing objects."""
    acc = window.accessible
    if acc.is_collection():
        atspi_ = atspi()
        match = atspi_.CollectionMatchType.ALL
        rule = atspi_.MatchRule.new(
            atspi_.StateSet.new([atspi_.StateType.FOCUSED]),
            match, {}, match, [], match, [], match, False,
        )  # fmt: skip
        found = _call(
            "focus",
            atspi_.Collection.get_matches,
            acc,
            rule,
            atspi_.CollectionSortOrder.CANONICAL,
            1,
            True,
        )
        if found:
            return Control(found[0])
    queue = deque(window.GetChildren())
    seen = 0
    while queue and seen < FOCUS_SEARCH_CAP:
        node = queue.popleft()
        seen += 1
        with contextlib.suppress(*_UNREADABLE):
            states = node._states()
            if "focused" in states:
                return node
            if "showing" in states:
                queue.extend(node.GetChildren())
    return None


def DwmIsCompositionEnabled() -> bool:
    """X11 frame rects already are the visible frame."""
    return True


def DwmGetWindowExtendFrameBounds(hwnd: int) -> Rect | None:
    """The visible frame (`x11.frame_rect`); the screen for the root window."""
    from . import win32gui

    if hwnd == win32gui.GetDesktopWindow():
        left, top, width, height = x11.screen_rect()
        return Rect(left, top, left + width, top + height)
    return Rect(*x11.frame_rect(hwnd))


class Bitmap:
    """A screen copy: Width, Height, GetAllPixelColors (BGRA bytes, as
    uiautomation's ARGB words lie in memory), a context manager. Linux's
    capture_rect grabs through `x11.grab` itself; this is for callers that
    still ask uiautomation's way."""

    def __init__(self, image) -> None:
        self.image = image.convert("RGBA")
        self.Width, self.Height = self.image.size

    def GetAllPixelColors(self) -> bytes:
        return self.image.tobytes("raw", "BGRA")

    def __enter__(self) -> Bitmap:
        return self

    def __exit__(self, *exc) -> None:
        return None

    @staticmethod
    def FromControl(control, x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> Bitmap:
        """A rect relative to the control (the root: the screen)."""
        rect = control.BoundingRectangle
        width = width or rect.width() - x
        height = height or rect.height() - y
        return Bitmap(x11.grab(rect.left + x, rect.top + y, width, height))


# COM apartments are a Windows matter.
UIAutomationInitializerInThread = contextlib.nullcontext


def SendKeys(keys: str, interval: float = 0.01, waitTime: float = 0.05) -> None:
    """uiautomation key syntax through XTest (`input.send_keys`). `interval`,
    uiautomation's delay between keys, is accepted and ignored: XTest sends
    the keys as one batch and the X server paces them."""
    _input.send_keys(keys, waitTime)


def MoveTo(x: int, y: int, waitTime: float = 0.05) -> None:
    _input.move_to(x, y)


def WheelDown(wheelTimes: int = 1, waitTime: float = 0.05) -> None:
    _input.wheel(-wheelTimes)
