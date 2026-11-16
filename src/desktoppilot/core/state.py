"""One JSON blob describing everything an agent needs to decide the next step."""

from __future__ import annotations

from . import apps, fallback, ui
from . import dialogs as dialogs_mod
from .native import win32gui, win32process
from .registry import AppContext


def snapshot(ctx: AppContext, *, controls: int = 0) -> dict:
    if ctx.profile is None:
        return {
            "app": ctx.name,
            "profile": None,
            "foreground": _foreground([]),
            "fallback_used": fallback.used(ctx.name),
        }

    state: dict = apps.status(ctx)
    pids = state["pids"]  # status already ran tasklist; once per call
    state["dialogs"] = dialogs_mod.dialog_windows(
        pids, classes=dialogs_mod.dialog_classes(ctx.profile)
    )
    state["foreground"] = _foreground(pids)
    state["fallback_used"] = fallback.used(ctx.name)

    if pids and controls:
        try:
            walk: dict = {}
            state["controls"] = ui.flatten(
                ui.main_window(ctx, timeout=5.0), limit=controls, report=walk
            )
            state["controls_complete"] = walk["complete"]
        except Exception as exc:  # noqa: BLE001 - a state dump never raises
            state["controls_error"] = str(exc)
    return state


def _foreground(pids: list[int]) -> dict:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return {"hwnd": None}
    _, owner = win32process.GetWindowThreadProcessId(hwnd)
    return {
        "hwnd": hwnd,
        "pid": owner,
        "title": win32gui.GetWindowText(hwnd),
        "class": win32gui.GetClassName(hwnd),
        "is_target": owner in set(pids),
    }
