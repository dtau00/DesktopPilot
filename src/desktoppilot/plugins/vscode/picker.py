"""The model picker: read the model, effort and fast mode it shows, and set
them, with a read-back that must match."""

from __future__ import annotations

from desktoppilot.core import ui
from desktoppilot.core.errors import NotPresent, PilotError, Timeout
from desktoppilot.core.registry import AppContext

from . import codex, palette
from . import panel as P
from .locate import Panel


class ModelMismatch(PilotError):
    """The picker's read-back differs from what was asked for. `details`
    holds wanted, got and mismatches, for the CLI to print."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def _menu_open(nodes: list[P.Node]) -> bool:
    return any(n.type == "MenuItem" and n.name in P.POWER for n in nodes)


def _models_open(nodes: list[P.Node]) -> bool:
    return bool(P.model_items(nodes))


def _menu_shown(nodes: list[P.Node]) -> bool:
    return _menu_open(nodes) or _models_open(nodes)


def _open_menu(pan: Panel) -> list[P.Node]:
    """A click that lands while the menu is still animating shut is lost, so
    it is retried."""
    for _ in range(3):
        nodes = pan.nodes()
        if _menu_open(nodes):
            return nodes
        pick = P.picker(nodes)
        if pick is None:
            raise NotPresent("no model picker in the Codex panel")
        codex._click(pan, pick)
        try:
            return codex._wait_for(pan, _menu_open, "the model picker menu", 3.0)
        except Timeout:
            continue
    raise PilotError("the model picker menu would not open")


def _close_menu(pan: Panel, ctx: AppContext) -> list[P.Node]:
    nodes = pan.nodes()
    for _ in range(3):
        if not _menu_shown(nodes):
            return nodes
        if pan.owns_focus():
            codex._keys(pan, "{Esc}")
        else:
            pick = P.picker(nodes)
            if pick is None:
                break
            palette.bring_to_front(pan.window)
            ui.click(pick.control, ctx=ctx, mode="mouse", activate=False)
        try:
            nodes = codex._wait_for(
                pan, lambda n: not _menu_shown(n), "the model picker menu to close", 1.0
            )
        except Timeout:
            nodes = pan.nodes()
    if _menu_shown(nodes):
        raise PilotError("the model picker menu would not close")
    return nodes


def _closed_label(pan: Panel, ctx: AppContext) -> dict:
    nodes = codex._close_menu(pan, ctx)
    pick = P.picker(nodes)
    if pick is None or pick.name in P.PICKER_OPEN:
        raise PilotError("cannot read the model picker's label")
    return P.parse_label(pick.name)


def _model_list(pan: Panel) -> list[P.Node]:
    nodes = codex._open_menu(pan)
    codex._click(pan, codex._one(nodes, P.SELECT_MODEL, "MenuItem"))
    nodes = codex._wait_for(pan, _models_open, "the model list")
    return P.model_items(nodes)


def _choose(pan: Panel, item: P.Node) -> None:
    """Click a model; the list closes once the picker took it. On AT-SPI a
    radio menu item's Select waits for the item to read as checked, but the
    closing menu takes the item along, so the click raises after it landed:
    a list that closed is the answer, not the click's word."""
    try:
        codex._click(pan, item)
    except PilotError:
        if _models_open(pan.nodes()):
            raise
    codex._wait_for(pan, lambda n: not _models_open(n), "the picker to take the model")


def _set_fast(pan: Panel, fast: bool) -> None:
    nodes = codex._open_menu(pan)
    state = P.fast_state(nodes)
    if state is None:
        raise PilotError("the fast-mode checkbox is missing or its state is unreadable")
    if state != fast:
        codex._click(pan, P.fast_box(nodes))
        codex._wait_for(pan, lambda n: P.fast_state(n) == fast, "fast mode to change")


def _slider_text(nodes: list[P.Node]) -> str:
    for n in nodes:
        if n.type == "Text" and P.slider_position(n.name):
            return n.name
    return ""


def _mismatch(wanted: dict, got: dict) -> ModelMismatch | None:
    wrong = P.mismatches(wanted, got)
    if not wrong:
        return None
    return ModelMismatch(
        "the picker does not show what was asked for: "
        + "; ".join(f"{m['field']} is {m['got']!r}, wanted {m['wanted']!r}" for m in wrong),
        {"wanted": wanted, "got": got, "mismatches": wrong},
    )


def _set_effort(pan: Panel, ctx: AppContext, target: str) -> None:
    """The slider only moves by arrow key, and while its menu is open nothing
    names the effort in words the label uses - so step once, close, read the
    label, repeat. An end stop that does not move means the model does not
    offer the effort."""
    for _ in range(len(P.EFFORT_ORDER) + 1):
        label = codex._closed_label(pan, ctx)
        current = label["effort"]
        if current == target:
            return
        if current is None:
            raise _mismatch({"effort": target}, {"effort": None, "label": label["label"]})
        key = P.effort_step(current, target)
        nodes = codex._open_menu(pan)
        codex._focus(pan, codex._one(nodes, P.POWER, "MenuItem"))
        before = _slider_text(pan.nodes())
        codex._keys(pan, key)
        try:
            codex._wait_for(
                pan, lambda n, was=before: _slider_text(n) != was, "the effort slider to move", 3.0
            )
        except Timeout as exc:
            raise PilotError(
                f"effort {target!r} is not offered: the slider stopped at {current!r} ({before})"
            ) from exc
    raise PilotError(f"effort did not reach {target!r}")


def _read_model(pan: Panel, ctx: AppContext) -> dict:
    """What the picker says now: model (the checked radio in the list), the
    effort (the closed label), fast (the checkbox's ToggleState). The label's
    model is named as the radio it stands for, by the --name rule."""
    label = codex._closed_label(pan, ctx)
    fast = P.fast_state(codex._open_menu(pan))
    radios = codex._model_list(pan)
    offered = [r.name for r in radios]
    checked = [r.name for r in radios if r.selected]
    codex._close_menu(pan, ctx)
    return {
        "model": checked[0] if len(checked) == 1 else None,
        "label_model": P.label_model_as_offered(label["model"], offered) or label["model"],
        "effort": label["effort"],
        "fast": fast,
        "label": label["label"],
        "offered": offered,
    }


def read_model(ctx: AppContext, window: str | None = None) -> dict:
    return _read_model(codex.find_panel(ctx, window), ctx)


def set_model(
    ctx: AppContext, *, name: str, effort: str, fast: bool, window: str | None = None
) -> dict:
    target_effort = P.effort_key(effort)
    pan = codex.find_panel(ctx, window)
    if P.generating(pan.nodes()):
        raise codex.CodexBusy("Codex is working on a turn; change the model between turns")

    try:
        radios = codex._model_list(pan)
        model = P.match_model(name, [r.name for r in radios])
        radio = next(r for r in radios if r.name == model)
        if radio.selected:
            # Select() on the checked radio is a no-op: nothing to take, just leave.
            codex._close_menu(pan, ctx)
        else:
            _choose(pan, radio)
        codex._set_fast(pan, fast)
        codex._set_effort(pan, ctx, target_effort)
    finally:
        codex._close_menu(pan, ctx)

    got = _read_model(pan, ctx)
    wanted = {"model": model, "label_model": model, "effort": target_effort, "fast": fast}
    error = _mismatch(wanted, got)
    if error is not None:
        raise error
    return {"verified": True, **{k: got[k] for k in ("model", "effort", "fast", "label")}}
