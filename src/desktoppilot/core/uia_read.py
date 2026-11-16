"""What one UIA element says about itself: description, value, text, patterns."""

from __future__ import annotations

from . import selectors
from .errors import is_uia_timeout


def describe(control, *, verbose: bool = False) -> dict:
    """The control's type, name, id, class, rect and state (plus patterns and
    value when `verbose`), or {"stale": True, "detail"} if it is gone.

    Never raises. A control that acted on the UI often destroys itself doing
    it - a Send button unmounts as the message posts - and losing the whole
    command to that is worse than reporting a stale element."""
    try:
        return _describe(control, verbose=verbose)
    except Exception as exc:  # noqa: BLE001 - element vanished after acting
        return {"stale": True, "detail": str(exc)}


def _describe(control, *, verbose: bool = False) -> dict:
    rect = control.BoundingRectangle
    info = {
        "type": selectors.normalize_type(control.ControlTypeName),
        "name": control.Name,
        "id": control.AutomationId or None,
        "class": control.ClassName or None,
        "rect": [rect.left, rect.top, rect.width(), rect.height()],
        "enabled": bool(control.IsEnabled),
        "offscreen": bool(control.IsOffscreen),
    }
    if verbose:
        info["patterns"] = available_patterns(control)
        try:
            value = value_of(control)
        except Exception as exc:  # noqa: BLE001 - a timed-out value read; keep the rest
            info["value_error"] = str(exc)
        else:
            if value is not None:
                info["value"] = value
    return info


def value_of(control) -> str | None:
    """The Value (or MSAA value) text, or None. A read the app timed out on
    raises: unknown is not empty."""
    for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
        pattern = _pattern(control, getter)
        if pattern is None:
            continue
        try:
            text = pattern.Value
        except Exception as exc:  # noqa: BLE001 - value read on a dead element
            if is_uia_timeout(exc):
                raise
            continue
        if text:
            return text
    return None


def text_of(control) -> str | None:
    """Value, else the whole TextPattern range - how a document-shaped control
    (an editor, a chat transcript) reports its contents."""
    value = value_of(control)
    if value:
        return value
    pattern = _pattern(control, "GetTextPattern")
    if pattern is not None:
        try:
            return pattern.DocumentRange.GetText(-1)
        except Exception:  # noqa: BLE001 - document went away mid-read
            return None
    return None


PATTERN_NAMES = ("Invoke", "Toggle", "Value", "Text", "SelectionItem", "ExpandCollapse", "Scroll")


def available_patterns(control) -> list[str]:
    return [name for name in PATTERN_NAMES if _pattern(control, "Get" + name + "Pattern")]


def _pattern(control, getter):
    """A UIA pattern, or None - uiautomation raises for a wrong control type."""
    try:
        return getattr(control, getter)()
    except Exception:  # noqa: BLE001 - pattern simply unsupported
        return None
