"""Driving the Codex extension's panel in a running VS Code through the accessibility tree.

The panel is a webview: a React app in an iframe, reached through Chromium's
accessibility tree. It re-renders under every read, so each operation walks
the panel once into `panel.Node`s, decides from that snapshot, acts, and walks
again to see the result. panel.py holds the decisions; this module holds the
clicks and the keys of open, send, wait and read, snapshot.py the walk,
locate.py finding the panel, and picker.py the model picker.

This module is the import point. The other three call back through it
(`codex.find_panel`, not `find_panel`), so a patch on `codex.X` reaches every
caller of X.
"""

from __future__ import annotations

import contextlib
import re
import time

from desktoppilot.core import clip, selectors, ui
from desktoppilot.core.errors import Ambiguous, NotPresent, PilotError, Timeout
from desktoppilot.core.native import auto, win32clipboard, win32con, win32gui
from desktoppilot.core.registry import AppContext
from desktoppilot.core.util import collapse_whitespace, wait_until

from . import palette
from . import panel as P
from .common import main_windows, send_keys_if
from .common import read as read_attr
from .locate import (
    _FRAME,
    FRAME_DEPTH,
    FRAME_NAME,
    Panel,
    _in_sidebar,
    _locate,
    _pick,
    find_panel,
    panels_in,
    resolve_root,
)
from .picker import (
    ModelMismatch,
    _close_menu,
    _closed_label,
    _menu_open,
    _menu_shown,
    _mismatch,
    _model_list,
    _models_open,
    _open_menu,
    _read_model,
    _set_effort,
    _set_fast,
    _slider_text,
    read_model,
    set_model,
)
from .snapshot import (
    MAX_NODES,
    PANEL_DEPTH,
    _contains,
    _node,
    _state,
    snapshot,
)

# Display strings, each a tuple of accepted texts (see panel.py).
NEW_PANEL_COMMAND = ("Codex: New Codex Agent",)
CLOSE = ("Close",)  # contained in an editor tab's close button name
TAB_DEPTH = 32  # editor tabs sit deeper than the webview frames
UI_TIMEOUT = 10.0
OPEN_TIMEOUT = 180.0  # a new panel's web app has taken 121 s to boot
TURN_POLL = 1.0
SETTLE_POLLS = 2  # a turn briefly shows Send between tool calls
CHECKED_CHARS = 40  # of the visible reply, looked for in the copied text
_TAB = selectors.parse("app//TabItem[class~='tab ']").steps[0]
_BUTTON = selectors.parse("app//Button").steps[0]
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_NOT_WORD = re.compile(r"[\W_]+")


class CodexBusy(PilotError):
    """Codex is still working on a turn."""


class CodexFailed(PilotError):
    """The turn ended without a reply: a usage limit, an error notice."""


def _one(nodes: list[P.Node], names: tuple[str, ...], type_: str = "Button") -> P.Node:
    hits = P.named(nodes, names, type_)
    if not hits:
        raise NotPresent(f"no {type_} {names[0]!r} in the Codex panel")
    return hits[-1]


def _click(pan: Panel, node: P.Node) -> str:
    """A UIA pattern where the control has one; the radix drop-down triggers
    have only ExpandCollapse, which core click does not use, so those take the
    pointer at the element's own rectangle, with VS Code brought to front.
    Nothing checks that another window does not cover that point."""
    palette.bring_to_front(pan.window)
    return ui.click(node.control, activate=False)


def _wait_for(pan: Panel, test, what: str, timeout: float = UI_TIMEOUT) -> list[P.Node]:
    """The panel's nodes once `test` passes on them, even on none: the
    list goes in a tuple, which is truthy for `wait_until` when empty."""

    def attempt():
        nodes = pan.nodes()
        return (nodes,) if test(nodes) else None

    return wait_until(attempt, timeout=timeout, interval=0.2, what=what)[0]


def _keys(pan: Panel, keys: str) -> None:
    """Keys only ever go to the Codex panel (the Claude Code panel sits in
    the same window)."""
    send_keys_if(
        pan.owns_focus,
        keys,
        f"focus is not in the Codex panel; did not send {keys!r}",
        waitTime=0.1,
    )


def _focus(pan: Panel, node: P.Node) -> None:
    node.control.SetFocus()
    wait_until(pan.owns_focus, timeout=3.0, interval=0.1, what="focus in the Codex panel")


def _summary(pan: Panel, nodes: list[P.Node]) -> dict:
    pick = P.picker(nodes)
    return {
        "title": pan.title(),
        "window": read_attr(pan.window, "Name"),
        "new_chat": P.is_new_chat(nodes),
        "model_label": pick.name if pick else None,
        "permissions": P.permissions(nodes),
        "where": P.where(nodes),
        "generating": P.generating(nodes),
        "blocked": P.notice(nodes),
    }


def _until_composer(pan: Panel, timeout: float) -> tuple[list[P.Node], int]:
    """Wait for the composer, paging through the first-run tour (Back / Next,
    four pages) whenever it shows - it shows on every new panel.

    A new panel's web app can take minutes to boot (measured: 121 s after the
    extension's app-server connection reset); until then its document is
    empty - a spinner. A frame that renders "Codex could not start" never
    recovers without a VS Code reload, so that fails at once."""
    seen: dict = {"nodes": [], "pages": 0}

    def attempt():
        nodes = seen["nodes"] = pan.nodes()
        if P.composer(nodes) is not None:
            return nodes
        if P.is_broken(nodes) or pan.title() in P.BROKEN:
            raise CodexFailed(
                "the Codex panel shows 'Codex could not start' (the extension's webview "
                "timed out; see its log for renderer_ready_timeout). Only a VS Code reload fixes it."
            )
        if P.in_onboarding(nodes):
            _click(pan, _one(nodes, P.NEXT))
            seen["pages"] += 1
        return None

    try:
        nodes = wait_until(
            attempt, timeout=timeout, interval=0.5, what="the composer", fatal=(CodexFailed,)
        )
    except Timeout as exc:
        names = sorted({n.name for n in seen["nodes"] if n.name})
        shown = "an empty document (the extension is still loading its web app)"
        if any(n.name for n in seen["nodes"][1:]):
            shown = "no composer: " + ", ".join(names[:8])
        raise Timeout(
            f"no Codex composer after {timeout:g}s (tour pages clicked: {seen['pages']}); "
            f"the panel shows {shown}"
        ) from exc
    return nodes, seen["pages"]


def _codex_tabs(window) -> list:
    return [
        tab
        for tab in ui.search(window, _TAB, max_depth=TAB_DEPTH)
        if P.is_codex_tab(read_attr(tab, "Name"))
    ]


def _close_tab(window, tab) -> None:
    close = [
        b
        for b in ui.search(tab, _BUTTON, max_depth=4)
        if any(t in read_attr(b, "Name") for t in CLOSE)
    ]
    if not close:
        raise PilotError(f"the {read_attr(tab, 'Name')!r} tab has no Close button")
    target = ui.identity(tab)
    palette.bring_to_front(window)
    ui.click(close[0], activate=False)
    wait_until(
        lambda: target not in {ui.identity(t) for t in _codex_tabs(window)},
        timeout=UI_TIMEOUT,
        what="the Codex tab to close",
    )


def open_panel(
    ctx: AppContext, *, timeout: float = OPEN_TIMEOUT, window: str | None = None
) -> dict:
    """A Codex panel on a new chat.

    A visible panel already on a new chat is reused. Otherwise the editor tab
    titled "Codex" is closed - its chat stays in Recent chats - and "Codex: New
    Codex Agent" opens a fresh one. Closing is the only way: the panel's URI is
    fixed (/extension/panel/new), so the command, and the panel's own New chat
    button, only reveal the tab that already holds it."""
    target, pan = _locate(ctx, window)
    if target is None:
        raise Ambiguous(
            "several VS Code windows are open and none has a Codex panel; pass --window "
            "with part of the one to open it in"
        )

    closed = False
    if pan is not None:
        nodes = pan.nodes()
        if P.generating(nodes):
            raise CodexBusy("Codex is working in the open panel; `wait` for it first")
        if P.is_new_chat(nodes):
            return {
                "opened": False,
                "reused": True,
                "closed_previous": False,
                **_summary(pan, nodes),
            }
    for tab in _codex_tabs(target):
        _close_tab(target, tab)
        closed = True

    before = {ui.identity(p.root) for p in panels_in(target)}
    palette.run(target, NEW_PANEL_COMMAND)

    def fresh():
        new = [p for p in panels_in(target) if not p.sidebar and ui.identity(p.root) not in before]
        return new[0] if len(new) == 1 else None

    pan = wait_until(fresh, timeout=min(timeout, 30.0), interval=0.5, what="the new Codex panel")
    nodes, tour = _until_composer(pan, timeout)
    return {
        "opened": True,
        "reused": False,
        "closed_previous": closed,
        "tour_pages": tour,
        **_summary(pan, nodes),
    }


def _composer_text(node: P.Node) -> str:
    """What the composer holds: its Value (on AT-SPI, its paragraphs' text)."""
    return collapse_whitespace(ui.value_of(node.control) or "")


def _draft(edit: P.Node) -> str:
    """What the user left unsent in the composer; the placeholder is not."""
    text = _composer_text(edit)
    return "" if text == collapse_whitespace(edit.name) else text


def send(
    ctx: AppContext,
    text: str,
    *,
    timeout: float = 20.0,
    window: str | None = None,
    replace_draft: bool = False,
) -> dict:
    """Paste `text` into the composer through the clipboard (one keystroke
    for any length or number of lines), then Send. An unsent draft in the
    composer is refused unless `replace_draft`. The user's clipboard text is
    put back afterwards."""
    if not text.strip():
        raise PilotError("nothing to send")
    pan = find_panel(ctx, window)
    nodes = pan.nodes()
    if P.generating(nodes):
        raise CodexBusy("Codex is still working on the last turn; `wait` first")
    edit = P.composer(nodes)
    if edit is None:
        raise NotPresent("no composer in the Codex panel")
    draft = _draft(edit)
    if draft and not replace_draft:
        raise PilotError(
            f"the composer holds an unsent draft ({len(draft)} chars, starting {draft[:40]!r}); "
            "send or clear it, or pass --replace-draft"
        )
    turns = P.user_turns(nodes)

    with clip.preserved() as kept:
        _focus(pan, edit)
        _keys(pan, "{Ctrl}a{Delete}")
        clip.write(text)
        _keys(pan, "{Ctrl}v")
        head = collapse_whitespace(text)[:40]
        wait_until(
            lambda: head in _composer_text(edit),
            timeout=5.0,
            interval=0.1,
            what="the pasted prompt",
        )

    button = _one(pan.nodes(), P.SEND)
    ui.click(button.control, ctx=ctx, timeout=5.0, activate=False)
    nodes = _wait_for(
        pan,
        lambda n: P.generating(n) or P.user_turns(n) > turns,
        "Codex to take the message",
        timeout,
    )
    return {
        "sent": True,
        "chars": len(text),
        "generating": P.generating(nodes),
        "clipboard_restored": kept["restored"],
    }


def wait(
    ctx: AppContext,
    *,
    timeout: float,
    settle_polls: int = SETTLE_POLLS,
    window: str | None = None,
) -> dict:
    """Until the turn ends: done, or a question (an approval card, or a reply
    whose last line asks something). A turn that ends in a usage-limit or
    error notice instead of a reply raises CodexFailed. Done must hold for
    `settle_polls` walks in a row, since a turn briefly shows Send between
    tool calls - a counting loop, so not a wait_until."""
    pan = find_panel(ctx, window)
    deadline = time.monotonic() + timeout
    calm = 0
    while time.monotonic() < deadline:
        state = P.turn_state(pan.nodes())
        if state["state"] == "question":
            return state
        if state["state"] == "failed":
            raise CodexFailed(f"the turn ended without a reply: {state['message']}")
        if state["state"] == "done":
            calm += 1
            if calm >= settle_polls:
                return state
        else:
            calm = 0
        time.sleep(TURN_POLL)
    raise Timeout(f"Codex was still working after {timeout:g}s")


def _scroll_to_end(pan: Panel) -> None:
    """The transcript has shown no Scroll pattern, so the wheel - over the
    panel, once VS Code is in front and owns that point."""
    with contextlib.suppress(PilotError):
        ui.scroll(pan.root, vertical=100)
        return
    palette.bring_to_front(pan.window)
    rect = pan.root.BoundingRectangle
    x, y = rect.left + rect.width() // 2, rect.top + rect.height() // 2
    owner = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT)
    if owner != pan.window.NativeWindowHandle:
        raise PilotError("another window covers the Codex panel; did not scroll it")
    auto.MoveTo(x, y, waitTime=0.05)
    auto.WheelDown(wheelTimes=30, waitTime=0.2)


def _letters(text: str) -> str:
    return _NOT_WORD.sub("", text).casefold()


def is_reply(copied: str, shown: list[str]) -> bool:
    """Does the copied text start the way the reply shows? Copy gives
    markdown and the tree the rendered text, so both are cut down to word
    characters, after the markdown's link targets go."""
    if not shown:
        return True
    return _letters(shown[0])[:CHECKED_CHARS] in _letters(_LINK_TARGET.sub("", copied))


def read(ctx: AppContext, window: str | None = None) -> dict:
    """The last reply, whole, through its Copy button: the transcript is
    virtualized, so the text in the tree is only what is on screen. What
    lands on the clipboard is checked against the visible reply, since
    anything else that copies at that moment lands there too."""
    pan = find_panel(ctx, window)
    nodes = pan.nodes()
    if P.generating(nodes):
        raise CodexBusy("Codex is still generating; `wait` first")
    reply = P.last_reply(nodes)
    if reply is None or reply["copy"] is None:
        _scroll_to_end(pan)
        nodes = pan.nodes()
        reply = P.last_reply(nodes)
    if reply is None:
        raise NotPresent("no Codex reply in the panel")
    if reply["copy"] is None:
        raise NotPresent("the last reply has no Copy button yet (still streaming?)")

    with clip.preserved() as kept:
        clip.clear()
        before = win32clipboard.GetClipboardSequenceNumber()
        ui.click(reply["copy"].control, ctx=ctx, activate=False)
        text = wait_until(
            lambda: win32clipboard.GetClipboardSequenceNumber() != before and clip.read(),
            timeout=5.0,
            interval=0.1,
            what="the copied reply",
        )
    if not is_reply(text, reply["texts"]):
        raise PilotError(
            "the clipboard took text that is not the last reply (something else copied at "
            "the same moment?); run read again"
        )
    return {
        "text": text,
        "chars": len(text),
        "title": pan.title(),
        "verified": bool(reply["texts"]),
        "clipboard_restored": kept["restored"],
    }
