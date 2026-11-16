"""What the Codex panel shows, read from one walk of its UIA tree.

Pure logic: every function here takes a list of `Node`s (the panel flattened
in document order) and answers a question about it. No UIA call happens here,
so all of it runs in the unit tests.

Every name matched below is a display string of the `openai.chatgpt`
extension (build 26.917) in English. The webview gives almost nothing an
AutomationId - only the radix drop-downs carry one, and it is a generated
`radix-_r_NN_` that changes on every render - so display strings are what
there is. Each constant is a tuple of accepted texts, matched by membership,
so a localized text goes next to the English one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from desktoppilot.core.errors import Ambiguous, PilotError
from desktoppilot.core.util import collapse_whitespace

# --------------------------------------------------------------- the strings

SEND = ("Send",)
STOP = ("Stop",)
COPY = ("Copy",)  # an assistant reply's copy button; a user message has USER_COPY
USER_COPY = ("Copy message",)
REPLY_HEADING = ("ChatGPT said:",)  # sr-only heading before every assistant reply
USER_HEADING = ("You said:",)
PICKER_OPEN = ("Select effort",)  # the model picker button's name while its menu is open
SELECT_MODEL = ("Select model",)  # menu item that opens the model list
POWER = ("Power",)  # the effort slider: a menu item driven by Left / Right
# The fast-mode CheckBox is named for what a click would do: "Enable fast
# mode" (ToggleState 0) while off, "Enable standard mode" (1) while on.
FAST = ("Enable fast mode",)
STANDARD = ("Enable standard mode",)
PERMISSIONS = ("Change permissions",)
WHERE = ("Select where to run the chat",)
CHAT_ACTIONS = ("Chat actions",)
BACK = ("Back",)  # the first-run tour's only two buttons
NEXT = ("Next",)
BROKEN = ("Codex could not start",)
CODEX_TAB = ("Codex",)  # the editor tab's title; "Codex, Editor Group 2" when split

# A radix drop-down button that is not the model picker.
OTHER_DROPDOWNS = frozenset(PERMISSIONS + WHERE + CHAT_ACTIONS)

# Buttons that exist only while Codex waits on the user: an exec / patch / MCP
# approval card, and the request-user-input panel.
QUESTION_BUTTONS = frozenset(
    {
        "Allow once",
        "Always allow",
        "Allow this conversation",
        "Allow similar commands",
        "Allow all edits",
        "Deny",
        "Decline",
        "Submit",
        "Skip",
    }
)
QUESTION_TEXTS = (
    "to run this command?",
    "to connect to",
    "Waiting for your answer",
    "Approval options",
    "Review this data change",
)

# A turn that ends in one of these has no reply heading after it: it failed.
FAILURE_TEXTS = (
    "usage limit",
    "credit limit",
    "out of credits",
    "Something went wrong",
)
FAILURE_BUTTONS = frozenset({"Retry", "Try again", "Request credits"})

# The picker's closed label is "<model> <effort label>", e.g. "GPT-6 Sol High".
# Keys are what the extension logs (`newEffort=xhigh`); values what it draws.
# The slider runs low to high in this order: which way Left / Right moves.
# "persistent" is last because the extension lists it after "ultra"; not seen live.
EFFORT_LABELS = {
    "none": ("None",),
    "minimal": ("Minimal",),
    "low": ("Low",),
    "medium": ("Medium",),
    "high": ("High",),
    "xhigh": ("Extra High",),
    "max": ("Max",),
    "ultra": ("Ultra",),
    "persistent": ("Persistent",),
}
EFFORT_ORDER = tuple(EFFORT_LABELS)

_POSITION = re.compile(r"(\d+)\s+of\s+(\d+)")


@dataclass
class Node:
    """One control, as far as the panel logic cares. `control` is the live
    UIA element (None in tests); everything else was read once, guarded."""

    type: str
    name: str = ""
    id: str = ""
    cls: str = ""
    depth: int = 0
    toggle: int | None = None
    selected: bool | None = None
    control: Any = field(default=None, repr=False, compare=False)


def norm(text: str | None) -> str:
    return collapse_whitespace(text).casefold()


def buttons(nodes: list[Node]) -> list[Node]:
    return [n for n in nodes if n.type == "Button"]


def named(nodes: list[Node], names: tuple[str, ...], type_: str | None = None) -> list[Node]:
    if isinstance(names, str):  # a bare str would match by substring
        names = (names,)
    return [n for n in nodes if n.name in names and (type_ is None or n.type == type_)]


# ------------------------------------------------------------- panel states


def is_broken(nodes: list[Node]) -> bool:
    return any(n.name in BROKEN for n in nodes if n.type in ("Document", "Text"))


def composer(nodes: list[Node]) -> Node | None:
    """The ProseMirror editor. There is one per panel."""
    for n in nodes:
        if n.type == "Edit" and "ProseMirror" in n.cls:
            return n
    return None


def in_onboarding(nodes: list[Node]) -> bool:
    names = {n.name for n in buttons(nodes)}
    return composer(nodes) is None and bool(names & set(BACK)) and bool(names & set(NEXT))


def is_new_chat(nodes: list[Node]) -> bool:
    """The home screen: a composer and no turn yet."""
    edit = composer(nodes)
    return edit is not None and not named(nodes, USER_HEADING, "Text")


def generating(nodes: list[Node]) -> bool:
    """Codex is working while the composer's Send button is a Stop button."""
    return any(n.name in STOP for n in buttons(nodes))


def user_turns(nodes: list[Node]) -> int:
    return len(named(nodes, USER_HEADING, "Text"))


def _text_after(nodes: list[Node], button: tuple[str, ...]) -> str | None:
    """A drop-down shows its value as the first named Text under it."""
    for i, n in enumerate(nodes):
        if n.type == "Button" and n.name in button:
            for m in nodes[i + 1 :]:
                if m.depth <= n.depth:
                    return None
                if m.type == "Text" and m.name.strip():
                    return m.name
    return None


def permissions(nodes: list[Node]) -> str | None:
    return _text_after(nodes, PERMISSIONS)


def where(nodes: list[Node]) -> str | None:
    return _text_after(nodes, WHERE)


def picker(nodes: list[Node]) -> Node | None:
    """The model picker: the radix drop-down that is none of the others.
    Its name is the closed label, or PICKER_OPEN while its menu is open."""
    found = [
        n
        for n in buttons(nodes)
        if n.id.startswith("radix-") and n.name not in OTHER_DROPDOWNS and n.name
    ]
    if len(found) > 1:
        raise Ambiguous(f"{len(found)} model pickers in one panel: {[n.name for n in found]}")
    return found[0] if found else None


# ------------------------------------------------------------ the picker


def model_items(nodes: list[Node]) -> list[Node]:
    """The model list's entries: RadioButtons, or MenuItems with a checked
    state (radio menu items; how AT-SPI shows them). A plain menu item of
    the effort view has no state (`selected` None) and is not one."""
    return [
        n
        for n in nodes
        if n.type == "RadioButton" or (n.type == "MenuItem" and n.selected is not None)
    ]


def fast_box(nodes: list[Node]) -> Node | None:
    boxes = [n for n in nodes if n.type == "CheckBox" and n.name in FAST + STANDARD]
    return boxes[0] if boxes else None


def fast_state(nodes: list[Node]) -> bool | None:
    """Fast mode on / off, or None when unreadable - including a name that
    disagrees with the ToggleState, which is not a state to trust."""
    box = fast_box(nodes)
    if box is None or box.toggle not in (0, 1):
        return None
    on = box.toggle == 1
    return on if (box.name in STANDARD) == on else None


def parse_label(label: str) -> dict:
    """ "GPT-6 Sol Extra High" -> model "GPT-6 Sol", effort "xhigh".

    The effort is the label's tail, longest match first ("Extra High" before
    "High"), so no model list is needed to split it. An unknown tail (e.g.
    "Standard") gives effort None and leaves the whole label as the model."""
    text = collapse_whitespace(label)
    shown = [(key, s) for key, labels in EFFORT_LABELS.items() for s in labels]
    for key, s in sorted(shown, key=lambda ks: -len(ks[1])):
        if text.casefold().endswith(" " + s.casefold()):
            return {"model": text[: -len(s) - 1].strip(), "effort": key, "label": text}
    return {"model": text, "effort": None, "label": text}


def effort_key(wanted: str) -> str:
    """Accept the key ("xhigh") or the label ("Extra High"), any case."""
    w = norm(wanted).replace("-", " ").replace("_", " ")
    for key, labels in EFFORT_LABELS.items():
        if w == key or any(w in (norm(s), norm(s).replace(" ", "")) for s in labels):
            return key
    every_label = ", ".join(s for labels in EFFORT_LABELS.values() for s in labels)
    raise PilotError(
        f"unknown effort {wanted!r}; known: {', '.join(EFFORT_LABELS)} (or their labels: {every_label})"
    )


def effort_step(current: str | None, target: str) -> str:
    """Which arrow key moves the slider from `current` towards `target`."""
    if current not in EFFORT_ORDER or target not in EFFORT_ORDER:
        raise PilotError(f"cannot order efforts {current!r} -> {target!r}")
    return "{Right}" if EFFORT_ORDER.index(target) > EFFORT_ORDER.index(current) else "{Left}"


def slider_position(text: str) -> tuple[int, int] | None:
    """ "GPT-6 Sol Standard, 2 of 5." -> (2, 5): the slider's live region."""
    found = _POSITION.search(text or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


class ModelNotFound(PilotError):
    pass


def match_model(wanted: str, offered: list[str]) -> str:
    """The one offered model `wanted` names.

    An exact name wins; otherwise every word of `wanted` must be a word of the
    model's name ("Sol" -> "GPT-6 Sol"). Two such models is Ambiguous, never
    a guess: this build offers both "GPT-6 Sol" and "GPT-5.6 Sol"."""
    target = norm(wanted)
    for name in offered:
        if norm(name) == target:
            return name
    words = set(target.split())
    hits = [name for name in offered if words and words <= set(norm(name).split())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ModelNotFound(f"no model {wanted!r} in the picker; it offers {offered}")
    raise Ambiguous(
        f"model {wanted!r} matches {hits}; name one exactly. The picker offers {offered}"
    )


def label_model_as_offered(label_model: str | None, offered: list[str]) -> str | None:
    """The offered model the closed label names, by the same rule as --name:
    the label may abbreviate the radio's name. None if it names none or two."""
    if not label_model:
        return None
    try:
        return match_model(label_model, offered)
    except PilotError:
        return None


def mismatches(wanted: dict, got: dict) -> list[dict]:
    """Every field of `wanted` (model, effort, fast) the read-back differs on.
    A field read as None (unreadable) is a mismatch too: unverified is not
    verified."""
    out = []
    for key, value in wanted.items():
        if value is None:
            continue
        actual = got.get(key)
        same = norm(actual) == norm(value) if isinstance(value, str) else actual == value
        if actual is None or not same:
            out.append({"field": key, "wanted": value, "got": actual})
    return out


# ---------------------------------------------------------------- replies


def last_reply(nodes: list[Node]) -> dict | None:
    """The newest assistant reply: its visible text and its Copy button.

    Replies are delimited by the sr-only REPLY_HEADING, so the last heading
    starts the last reply. None when the panel shows no reply at all (a new
    chat, or the reply scrolled out of the virtualized list)."""
    starts = [i for i, n in enumerate(nodes) if n.type == "Text" and n.name in REPLY_HEADING]
    if not starts:
        return None
    start = starts[-1]
    copy = None
    texts: list[str] = []
    for n in nodes[start + 1 :]:
        if n.type == "Text" and n.name in USER_HEADING:
            break
        if n.type == "Button" and n.name in COPY:
            copy = n
            break
        if n.type == "Edit":
            break
        if n.type == "Text" and n.name.strip() and n.name not in REPLY_HEADING:
            texts.append(n.name)
    return {"texts": _dedupe_nested(texts), "copy": copy}


def _dedupe_nested(texts: list[str]) -> list[str]:
    """Chromium reports a paragraph and its inline runs as nested Texts, so the
    same words come twice; keep a text only when the previous one kept does
    not already contain it."""
    out: list[str] = []
    for text in texts:
        if out and text in out[-1]:
            continue
        out.append(text)
    return out


def asks_question(reply_text: str) -> bool:
    """A reply that ends by asking something: its last line ends with '?'."""
    lines = [line.strip() for line in (reply_text or "").splitlines() if line.strip()]
    return bool(lines) and lines[-1].rstrip("*_`) ").endswith("?")


def _last_index(nodes: list[Node], heading: tuple[str, ...]) -> int:
    hits = [i for i, n in enumerate(nodes) if n.type == "Text" and n.name in heading]
    return hits[-1] if hits else -1


def _after_prompt(nodes: list[Node]) -> int:
    """Where the newest turn's own output starts: past the last user heading
    and, when it shows, the user message's Copy button, so the prompt's words
    are not read as the panel's."""
    start = _last_index(nodes, USER_HEADING)
    if start < 0:
        return 0
    for i in range(start + 1, len(nodes)):
        n = nodes[i]
        if n.type == "Text" and n.name in REPLY_HEADING:
            break
        if n.type == "Button" and n.name in USER_COPY:
            return i + 1
    return start + 1


def notice(nodes: list[Node], start: int = 0) -> str | None:
    """A usage-limit or error notice at or after `start`, if the panel shows one."""
    for n in nodes[start:]:
        if n.type == "Text" and any(t.casefold() in n.name.casefold() for t in FAILURE_TEXTS):
            return n.name
    for n in nodes[start:]:
        if n.type == "Button" and n.name in FAILURE_BUTTONS:
            return n.name
    return None


def _question(nodes: list[Node], start: int) -> str | None:
    """An approval card or input request at or after `start`."""
    for n in nodes[start:]:
        if n.type == "Button" and n.name in QUESTION_BUTTONS:
            return n.name
    for n in nodes[start:]:
        if n.type == "Text" and any(t.casefold() in n.name.casefold() for t in QUESTION_TEXTS):
            return n.name
    return None


def _unanswered(nodes: list[Node]) -> bool:
    """The newest user message has no reply after it yet: the turn is still
    starting, even if Stop has not appeared."""
    return _last_index(nodes, USER_HEADING) > _last_index(nodes, REPLY_HEADING)


def turn_state(nodes: list[Node]) -> dict:
    """generating / question / failed / done, and why. Only the newest turn
    counts: an old card's text or the prompt's own words are history."""
    asked = _question(nodes, _after_prompt(nodes))
    if asked is not None:
        return {"state": "question", "reason": "approval", "prompt": asked}
    if generating(nodes):
        return {"state": "generating"}
    if _unanswered(nodes):
        failure = notice(nodes, _last_index(nodes, USER_HEADING))
        if failure is not None:
            return {"state": "failed", "message": failure}
        return {"state": "generating"}
    reply = last_reply(nodes)
    if reply is not None and reply["texts"] and asks_question("\n".join(reply["texts"])):
        return {"state": "question", "reason": "reply_asks", "prompt": reply["texts"][-1]}
    return {"state": "done"}


def is_codex_tab(name: str) -> bool:
    """The editor tab holding the Codex panel: "Codex", or "Codex, Editor
    Group 2" with a split editor. A chat opened from history is its own tab,
    titled after the chat."""
    return name in CODEX_TAB or any(name.startswith(t + ",") for t in CODEX_TAB)
