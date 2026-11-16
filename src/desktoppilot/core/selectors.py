"""Selector parsing for the UIA layer.

Grammar (borrowed from XPath, trimmed to what the UIA tree needs):

    root//Type[name='X'][id='Y']/Type[name~='partial']#2

  root      core roots are app (the app's main window), win:NAME (any
            top-level window of the app, matched on title), focus (the window
            that currently has focus), dialog, desktop. A plugin registers
            more (ribbon, pane:NAME, backstage). Any name parses here; which
            names an app accepts is decided by ui._check_root (and listed by
            ui.valid_roots), because the answer depends on the app. An argument holding '/' is quoted:
            win:"Find/Replace"//Button.
  //        descendant (any depth, breadth-first)
  /         direct child
  Type      control type without the 'Control' suffix (Button, TabItem, Edit)
            or * for any
  [k='v']   exact, case-insensitive match; k~='v' means "contains"
            keys: name, id (AutomationId), class (ClassName), value.
            Values take '...' or "..."; \\' \\" and \\\\ escape inside either.
  #N        take the Nth match (1-based) instead of the first
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import PilotError

_ROOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_KEY_RE = re.compile(r"\w+")
_TYPE_RE = re.compile(r"\*|[A-Za-z]+")
_OP_RE = re.compile(r"~?=")
_INDEX_RE = re.compile(r"#(-?\d+)")
_QUOTES = "'\""
_ESCAPABLE = "'\"\\"

_KEY_ALIASES = {
    "name": "name",
    "id": "automation_id",
    "automationid": "automation_id",
    "class": "class_name",
    "classname": "class_name",
    "value": "value",
}

# Every UIA control type. A step naming anything else would never match, and
# is most often a window title split at its '/' - so it is refused.
CONTROL_TYPES = (
    "AppBar", "Button", "Calendar", "CheckBox", "ComboBox", "Custom", "DataGrid",
    "DataItem", "Document", "Edit", "Group", "Header", "HeaderItem", "Hyperlink",
    "Image", "List", "ListItem", "Menu", "MenuBar", "MenuItem", "Pane",
    "ProgressBar", "RadioButton", "ScrollBar", "SemanticZoom", "Separator",
    "Slider", "Spinner", "SplitButton", "StatusBar", "Tab", "TabItem", "Table",
    "Text", "Thumb", "TitleBar", "ToolBar", "ToolTip", "Tree", "TreeItem", "Window",
)  # fmt: skip
_CANONICAL_TYPES = {name.casefold(): name for name in CONTROL_TYPES}


@dataclass
class Predicate:
    key: str
    op: str
    value: str

    def matches(self, actual: str | None) -> bool:
        actual = (actual or "").casefold()
        wanted = self.value.casefold()
        return wanted in actual if self.op == "~=" else actual == wanted


@dataclass
class Step:
    control_type: str  # normalized, e.g. "Button"; "*" matches anything
    descendant: bool  # True for //, False for /
    index: int = 1  # 1-based, from the #N suffix
    predicates: list[Predicate] = field(default_factory=list)
    indexed: bool = False  # the selector said #N; find_all then takes only the Nth


@dataclass
class Selector:
    root: str  # core or plugin root name; resolve_root decides if it is valid
    root_arg: str | None  # e.g. the NAME in pane:NAME
    steps: list[Step]


def parse(selector: str) -> Selector:
    text = selector.strip()
    if not text:
        raise PilotError("empty selector")
    return _Parser(text, selector).selector()


class _Parser:
    """One pass over the text, quote-aware: a '/' or ']' inside a quoted value
    is part of the value, and anything left unbalanced is an error rather
    than a selector that quietly means something else."""

    def __init__(self, text: str, original: str):
        self.text, self.original, self.pos = text, original, 0

    def fail(self, what: str) -> PilotError:
        return PilotError(f"{what} at offset {self.pos} in selector {self.original!r}")

    def peek(self, n: int = 1) -> str:
        return self.text[self.pos : self.pos + n]

    def skip_space(self) -> None:
        while self.peek().isspace():
            self.pos += 1

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def selector(self) -> Selector:
        root, root_arg = self.root()
        steps: list[Step] = []
        while not self.at_end():
            steps.append(self.step())
        return Selector(root=root, root_arg=root_arg, steps=steps)

    def root(self) -> tuple[str, str | None]:
        end = len(self.text)
        for stop in (":", "/"):
            found = self.text.find(stop)
            if found != -1:
                end = min(end, found)
        name = self.text[:end].strip().lower() or "app"
        if not _ROOT_RE.match(name):
            raise PilotError(f"cannot parse selector root {name!r} in {self.original!r}")
        self.pos = end
        if self.peek() != ":":
            return name, None
        self.pos += 1
        self.skip_space()
        if self.peek() in _QUOTES and not self.at_end():
            arg = self.quoted()
            self.skip_space()
            if not self.at_end() and self.peek() != "/":
                raise self.fail("expected / or // after the quoted root argument")
            return name, arg or None
        end = self.text.find("/", self.pos)
        end = len(self.text) if end == -1 else end
        arg = self.text[self.pos : end].strip()
        if any(ch in arg for ch in "[]" + _QUOTES):
            raise self.fail(f'quote a root argument holding brackets or quotes: {name}:"..."')
        self.pos = end
        return name, arg or None

    def quoted(self) -> str:
        quote = self.peek()
        self.pos += 1
        out: list[str] = []
        while not self.at_end():
            ch = self.peek()
            if ch == "\\" and self.peek(2)[1:] in tuple(_ESCAPABLE):
                out.append(self.text[self.pos + 1])
                self.pos += 2
                continue
            self.pos += 1
            if ch == quote:
                return "".join(out)
            out.append(ch)
        raise self.fail(f"unterminated {quote}-quoted string")

    def match(self, pattern: re.Pattern) -> str | None:
        found = pattern.match(self.text, self.pos)
        if found is None:
            return None
        self.pos = found.end()
        return found.group(0)

    def step(self) -> Step:
        if self.peek(2) == "//":
            descendant = True
            self.pos += 2
        elif self.peek() == "/":
            descendant = False
            self.pos += 1
        else:
            raise self.fail("expected / or //")
        self.skip_space()
        raw_type = self.match(_TYPE_RE)
        if raw_type is None:
            raise self.fail("expected a control type or *")
        predicates: list[Predicate] = []
        while self.peek() == "[":
            predicates.append(self.predicate())
        index, indexed = 1, False
        raw_index = self.match(_INDEX_RE)
        if raw_index is not None:
            index, indexed = int(raw_index[1:]), True
            if index < 1:
                raise self.fail(f"#N is 1-based; got #{index}")
        self.skip_space()
        if not self.at_end() and self.peek() != "/":
            raise self.fail(f"unexpected {self.peek()!r} in step")
        return Step(
            control_type=_control_type(raw_type, self.original),
            descendant=descendant,
            index=index,
            predicates=predicates,
            indexed=indexed,
        )

    def predicate(self) -> Predicate:
        self.pos += 1  # [
        self.skip_space()
        key = self.match(_KEY_RE)
        if key is None:
            raise self.fail("expected a predicate key")
        normalized = _KEY_ALIASES.get(key.lower())
        if normalized is None:
            raise self.fail(f"unknown predicate key {key!r}")
        self.skip_space()
        op = self.match(_OP_RE)
        if op is None:
            raise self.fail(f"expected = or ~= after {key!r}")
        self.skip_space()
        if self.peek() not in _QUOTES or self.at_end():
            raise self.fail("expected a quoted value")
        value = self.quoted()
        self.skip_space()
        if self.peek() != "]":
            raise self.fail("unbalanced [: expected ]")
        self.pos += 1
        return Predicate(key=normalized, op=op, value=value)


def _control_type(raw: str, selector: str) -> str:
    if raw == "*":
        return "*"
    name = normalize_type(raw)
    canonical = _CANONICAL_TYPES.get(name.casefold())
    if canonical is None:
        raise PilotError(
            f"unknown control type {raw!r} in selector {selector!r}; if it is part of a "
            "root argument holding '/', quote the argument: win:\"Find/Replace\"//Button"
        )
    return canonical


def normalize_type(name: str) -> str:
    """'ButtonControl', 'button', 'Button' all become 'Button'."""
    if name == "*":
        return "*"
    name = name.strip()
    if name.lower().endswith("control"):
        name = name[: -len("control")]
    return name[:1].upper() + name[1:]
