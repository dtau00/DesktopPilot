"""Acting on a control: activate, click, type, set a value, select an item,
scroll, close a window."""

from __future__ import annotations

import time

from . import clip, ui
from .errors import Ambiguous, Disabled, PilotError, Timeout, WalkCut, is_uia_timeout
from .native import WINDOWS, auto, win32api, win32con, win32gui
from .registry import AppContext
from .roots import DEFAULT_TIMEOUT
from .util import collapse_whitespace, wait_until


def close_window(control_or_hwnd, *, timeout: float = 10.0) -> bool:
    """Close by posting WM_CLOSE - many app windows have no reliable Close
    button in the UIA tree.

    An element with no window handle (a WPF or web control) is refused, not
    climbed from: its nearest windowed ancestor is often the app itself, and
    WM_CLOSE to handle 0 does nothing while `not IsWindow(0)` reads as closed."""
    hwnd = (
        control_or_hwnd if isinstance(control_or_hwnd, int) else control_or_hwnd.NativeWindowHandle
    )
    if not hwnd:
        raise PilotError(
            "the element has no window handle, so it is not a window to close; "
            "select the window itself (e.g. win:NAME) or click its Close button"
        )
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    wait_until(
        lambda: not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd),
        timeout=timeout,
        what="window to close",
    )
    return True


def activate(control) -> None:
    """Bring a window to the foreground; a background window ignores real clicks."""
    control.SetActive(waitTime=0.05)
    control.SetFocus()


def _target_window(ctx: AppContext | None, control):
    """Which window to bring forward: the app's main one - or, for an app with
    no profile (`desktop` owns no process, so it has no main window), the
    top-level window of the control being acted on. None: nothing to do."""
    if ctx is None:
        return None
    if ctx.profile is None:
        return control.GetTopLevelControl() if control is not None else None
    return ui.main_window(ctx)


def activate_app(ctx: AppContext | None, control=None) -> None:
    window = _target_window(ctx, control)
    if window is not None:
        activate(window)


def restore_app(ctx: AppContext | None, control=None) -> None:
    """Un-minimize, and nothing more. A UIA pattern acts on a window that is
    merely behind another one - measured: 0.54 s against 0.67 s for the same
    Invoke behind a full activate - but a MINIMIZED window has no toolbar to
    act on, and the click then fails silently."""
    window = _target_window(ctx, control)
    if window is not None and win32gui.IsIconic(window.NativeWindowHandle):
        window.SetActive(waitTime=0.05)


# Invoke / SelectionItem / Toggle say what they do; LegacyIAccessible is the
# catch-all MSAA bridge - it exists on almost every control and can be a
# silent no-op, so auto mode tries the pointer before it.
_STRONG_PATTERNS = (
    ("GetInvokePattern", lambda p: p.Invoke()),
    ("GetSelectionItemPattern", lambda p: p.Select()),
    ("GetTogglePattern", lambda p: p.Toggle()),
)
_WEAK_PATTERNS = (("GetLegacyIAccessiblePattern", lambda p: p.DoDefaultAction()),)


def _click_by_pattern(control, candidates, refused: list[Exception]) -> str | None:
    """Fire the first usable UIA pattern; None if the control offers none.
    A pattern that raises is skipped, and its error appended to `refused`."""
    for getter, call in candidates:
        pattern = ui._pattern(control, getter)
        if pattern is None:
            continue
        try:
            call(pattern)
            return getter.replace("Get", "").replace("Pattern", "")
        except Exception as exc:  # noqa: BLE001 - pattern present but refused the call
            refused.append(exc)
    return None


def _refusal(refused: list[Exception]) -> str:
    return f"; the last pattern refused with: {refused[-1]}" if refused else ""


def _centre(control) -> tuple[int, int]:
    """The screen point at the middle of the control's visible rect."""
    rect = ui._visible_rect(control)
    if rect is None:
        raise PilotError(
            "element is offscreen, has no size or cannot be read; the pointer cannot reach it"
        )
    return rect.left + rect.width() // 2, rect.top + rect.height() // 2


def _click_with_pointer(control, *, button: str, double: bool) -> str:
    x, y = _centre(control)
    return ui.click_point(x, y, button=button, double=double)


HOVER_POLL = 0.05


def hover_point(x: int, y: int, *, dwell: float = 0.0) -> dict:
    """Move the real pointer to (x, y) and leave it there. With `dwell`,
    hold until that many seconds pass, so a UI that opens on a hover delay
    opens before the command returns. The wait is on the pointer: it ends
    early when something else moves it away. Returns x, y, via and held
    (the pointer was still at the point at the end)."""
    how = ui.move_point(x, y)
    end = time.monotonic() + max(dwell, 0.0)

    def done() -> str:
        if tuple(win32api.GetCursorPos()) != (x, y):
            return "moved"
        return "held" if time.monotonic() >= end else ""

    ended = wait_until(
        done, timeout=max(dwell, 0.0) + 5.0, interval=HOVER_POLL, what="the hover dwell"
    )
    return {"x": x, "y": y, "via": how, "held": ended == "held"}


def hover(control, *, ctx: AppContext | None = None, dwell: float = 0.0) -> dict:
    """Bring the app forward and hover the middle of the control (see
    hover_point): a pointer over a background window shows nothing."""
    ui.activate_app(ctx, control)
    x, y = _centre(control)
    return hover_point(x, y, dwell=dwell)


def _is_enabled(control) -> bool:
    """IsEnabled; a control without the property counts as enabled. Raises
    when the read fails."""
    return bool(getattr(control, "IsEnabled", True))


def _enabled(control) -> bool:
    """True unless the control says it is disabled; an unreadable state is
    left to the click to find out."""
    try:
        return _is_enabled(control)
    except Exception:  # noqa: BLE001 - element vanished mid-read
        return True


ENABLED_POLL = 0.2


def _wait_enabled(control, *, timeout: float, refind=None):
    """The control once it reads as enabled: an app can build a control
    disabled and enable it seconds later (an add-in's ribbon tab, after a
    document opens). IsEnabled is re-read on the same element; an element
    that goes stale is found again through `refind`, when the caller has one.
    Disabled when `timeout` ends and it still reads as disabled."""
    if _enabled(control):
        return control
    start = time.monotonic()
    current = [control]

    def ready():
        try:
            if _is_enabled(current[0]):
                return current[0]
        except Exception:  # noqa: BLE001 - a stale element; find it again
            if refind is None:
                return current[0]  # unreadable: left to the click to find out
            current[0] = refind() or current[0]
        return None

    try:
        return wait_until(
            ready,
            timeout=max(timeout, 0.0),
            interval=ui.ENABLED_POLL,
            what="enabled",
            fatal=(Ambiguous,),
        )
    except Timeout:
        raise Disabled(
            f"the element is still disabled after {time.monotonic() - start:.1f}s; "
            "a click would do nothing (--allow-disabled)"
        ) from None


def click(
    control,
    *,
    ctx: AppContext | None = None,
    mode: str = "auto",
    button: str = "left",
    double: bool = False,
    activate: bool = True,
    allow_disabled: bool = False,
    timeout: float = 0.0,
    refind=None,
) -> str:
    """Click a control. mode: invoke (UIA patterns only), mouse (real pointer
    only), auto (a UIA pattern when the control offers one, else the pointer).
    auto is the default: a pattern click does not move the pointer, does not
    depend on window position, and cannot hit a neighbouring control.
    A right-click or a double-click has no pattern equivalent, so both always
    take the pointer.

    A disabled control is waited on for up to `timeout` seconds, then raises
    Disabled: the pointer "clicks" it without error and nothing happens.
    `refind` finds a stale element again. `allow_disabled` clicks it anyway."""
    if mode not in ("auto", "invoke", "mouse"):
        raise PilotError("mode must be auto, invoke or mouse; got " + repr(mode))
    if not allow_disabled:
        control = _wait_enabled(control, timeout=timeout, refind=refind)

    pointer_only = button == "right" or double
    if pointer_only and mode == "invoke":
        raise PilotError("a right-click or a double-click needs --mode mouse")

    if mode == "mouse" or pointer_only:
        if activate:
            ui.activate_app(ctx, control)
        return _click_with_pointer(control, button=button, double=double)

    # A pattern route needs the window restored, not focused: SetFocus costs
    # time, steals the caret, and buys a pattern click nothing.
    if activate:
        ui.restore_app(ctx, control)

    refused: list[Exception] = []
    how = _click_by_pattern(control, _STRONG_PATTERNS, refused)
    if how is not None:
        return how

    if mode == "invoke":
        how = _click_by_pattern(control, _WEAK_PATTERNS, refused)
        if how is not None:
            return how
        raise PilotError(
            "no usable UIA pattern on this element; try --mode mouse" + _refusal(refused)
        ) from (refused[-1] if refused else None)

    # Falling back to the pointer DOES need the foreground.
    if activate:
        ui.activate_app(ctx, control)
    try:
        return _click_with_pointer(control, button=button, double=double)
    except PilotError as err:
        how = _click_by_pattern(control, _WEAK_PATTERNS, refused)
        if how is not None:
            return how
        raise PilotError(
            f"no way to click this element (no usable pattern, no mouse target: {err})"
            + _refusal(refused)
        ) from err


def type_text(text: str, *, control=None, clear: bool = False, interval: float = 0.01) -> None:
    """Type `text` as a keyboard would: the layout's virtual key plus Shift for
    each character, not a Unicode packet. An app's keyboard hook that matches
    virtual keys (say, an autocomplete that watches Space, digits, '.' and
    Backspace) sees the same events a person produces. A character the layout
    cannot produce falls back to a Unicode packet; newline and tab become
    Enter and Tab.

    The text is literal: '{Ctrl}' types as braces. Keys and shortcuts go
    through send_keys."""
    if control is not None:
        control.SetFocus()
    if clear:
        auto.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
    for inputs in ui._keystrokes(text):
        ui._send_input(inputs)
        time.sleep(interval)
    time.sleep(0.05)


def send_keys(keys: str, *, control=None) -> None:
    """uiautomation key syntax, e.g. '{Ctrl}s', '{Enter}'.

    The caller names the focus target: always activating the main window
    silently sends a panel's shortcut to the wrong place.
    """
    if control is not None:
        control.SetFocus()
    auto.SendKeys(keys, waitTime=0.05)


def set_value(control, text: str, *, ctx: AppContext | None = None, activate: bool = True) -> dict:
    """Write a control's text through the Value pattern - no focus, no
    keystrokes, no caret. This is how a WPF TextBox gets cleared: `ui type
    --clear` sends Ctrl+A to whatever has focus, which is often the app's
    main document. `verified` says whether the value read back is `text`; a
    control that is gone after the write reads back as None."""
    if activate:
        ui.restore_app(ctx, control)
    pattern = ui._pattern(control, "GetValuePattern")
    if pattern is None:
        raise PilotError("no Value pattern on this element; use ui type")
    if pattern.IsReadOnly:
        raise PilotError("this element is read-only")
    pattern.SetValue(text, waitTime=0.05)
    try:
        value = pattern.Value
    except Exception:  # noqa: BLE001 - the write can tear the element down
        value = None
    return {"set": text, "value": value, "verified": value == text, "element": ui.describe(control)}


def copy_text(control=None) -> str:
    """Select-all then copy, and read the clipboard. The fallback for surfaces
    that expose no UIA value - Electron chat panes among them. The
    clipboard's text is put back afterwards (clip.preserved)."""
    if control is not None:
        control.SetFocus()
    with clip.preserved():
        clip.clear()
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Ctrl}c", waitTime=0.2)
        return wait_until(clip.read, timeout=5.0, interval=0.1, what="copied text")


# A drop-down item is an item, never a Group, a Pane or a Text label: a
# name-only hunt across the desktop matched a ribbon "Editing" GROUP and
# changed Word's editing mode.
_ITEM_TYPES = ("ListItem", "MenuItem", "TreeItem", "Button", "CheckBox", "RadioButton")
POPUP_CLASSES = ("Popup", "#32768")


def popup_roots(deadline: float | None = None, *, classes=POPUP_CLASSES) -> list:
    """Top-level windows a drop-down can live in: an app hangs its menu in a
    Win32 #32768 rather than inside its own window. On Linux `classes` is
    unused: a popup is told by its role and X window (`auto.is_popup`). A
    desktop read past `deadline`, or one UIA timed out on, raises WalkCut:
    an unread popup is not an absent one."""
    if deadline is not None and time.monotonic() > deadline:
        raise WalkCut("the popup lookup was cut at its deadline; the desktop was not read")
    found = []
    for window in ui._children(auto.GetRootControl()):
        try:
            popup = _has_class(window, classes) if WINDOWS else auto.is_popup(window)
        except Exception as exc:  # noqa: BLE001 - window gone mid-read
            if is_uia_timeout(exc):
                raise WalkCut("a popup's class read timed out; the app is busy") from exc
            continue
        if popup:
            found.append(window)
    return found


def _has_class(window, classes) -> bool:
    name = window.ClassName or ""
    return any(cls in name for cls in classes)


def _find_item(scope, item: str, *, deadline: float | None = None):
    """The first control labelled `item`, by _ITEM_TYPES rank and then walk
    order, in one walk of `scope`. A walk past `deadline` raises WalkCut."""
    rank = {control_type: i for i, control_type in enumerate(_ITEM_TYPES)}
    found = [c for c in ui.by_label(scope, item, deadline=deadline) if ui._type_of(c) in rank]
    return min(found, key=lambda c: rank[ui._type_of(c)], default=None)


def select_item(
    control,
    item: str | None = None,
    *,
    ctx: AppContext | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Select `control` itself, or expand it and select the named item inside.

    A combo box or a collapsed group builds its list only once open, and the
    app often hangs that list in a SEPARATE window, so the item is looked for
    in the control first and in the popup windows second."""
    ui.activate_app(ctx, control)
    if item is None:
        pattern = ui._pattern(control, "GetSelectionItemPattern")
        if pattern is None:
            raise PilotError("no SelectionItem pattern on this element; name an --item")
        # Read FIRST, as for --item: selecting a menu's radio item closes the
        # menu, and the item is dead afterwards. An unnamed item (a GTK4
        # popover radio) is told by `element`.
        label = collapse_whitespace(control.Name) or ui._label(control)
        element = ui.describe(control)
        pattern.Select()
        return {"selected": label, "expanded": False, "via": "SelectionItem", "element": element}

    expander = ui._pattern(control, "GetExpandCollapsePattern")
    expanded = False
    if expander is not None and expander.ExpandCollapseState != auto.ExpandCollapseState.Expanded:
        expander.Expand(waitTime=0.1)
        expanded = True

    deadline = time.monotonic() + timeout
    polls = {"complete": 0, "cut": 0}

    def hunt():
        try:
            for scope in (control, *ui.popup_roots(deadline)):
                found = ui._find_item(scope, item, deadline=deadline)
                if found is not None:
                    return found
        except WalkCut:
            polls["cut"] += 1
            raise
        polls["complete"] += 1
        return None

    what = "item " + repr(item)
    try:
        target = wait_until(hunt, timeout=timeout, what=what)
    except Timeout as exc:
        if polls["cut"] and not polls["complete"]:
            raise WalkCut(
                f"timed out after {timeout:g}s waiting for {what}: all {polls['cut']} "
                "walk(s) were cut; the app is busy, so whether it is there is unknown"
            ) from exc
        raise
    # Read the label FIRST: picking an item tears the popup down, and every
    # property read on the dead element then raises a COMError.
    label = collapse_whitespace(target.Name)
    how = ui.click(target, activate=False)
    return {"selected": label, "expanded": expanded, "via": how}


def scroll(control, *, vertical: float | None = None, horizontal: float | None = None) -> dict:
    """Scroll through the Scroll pattern - a percentage, not a wheel."""
    pattern = ui._pattern(control, "GetScrollPattern")
    if pattern is None:
        raise PilotError("no Scroll pattern on this element")
    if vertical is None and horizontal is None:
        raise PilotError("give --vertical and/or --horizontal, 0-100")
    if vertical is not None and not pattern.VerticallyScrollable:
        raise PilotError("this element does not scroll vertically")
    if horizontal is not None and not pattern.HorizontallyScrollable:
        raise PilotError("this element does not scroll horizontally")
    for percent in (vertical, horizontal):
        if percent is not None and not 0 <= percent <= 100:
            raise PilotError("a scroll percentage must be 0-100; got " + repr(percent))
    pattern.SetScrollPercent(
        pattern.NoScrollValue if horizontal is None else horizontal,
        pattern.NoScrollValue if vertical is None else vertical,
        waitTime=0.05,
    )
    return {
        "vertical_percent": pattern.VerticalScrollPercent,
        "horizontal_percent": pattern.HorizontalScrollPercent,
        "vertical_view_size": pattern.VerticalViewSize,
    }
