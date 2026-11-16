"""The Chrome DevTools Protocol: list a Chromium's targets over HTTP, and
drive one of them over its WebSocket.

Every Chromium-based app serves it on the port its `--remote-debugging-port`
flag names: Chrome, an Electron app (VS Code), WebView2. It fills what
the accessibility tree cannot do there: AT-SPI on Linux has no EditableText
on a Chromium input, so `ui set-value` cannot write one, and `Page.type` and
`Page.set_value` write the DOM instead.

The port has no authentication: anything running as the user can drive the
app for as long as it is up.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import websocket

from . import keysyntax
from .errors import Ambiguous, NoTarget, PilotError, Timeout
from .util import wait_until

TIMEOUT = 30.0
LIST_TIMEOUT = 5.0
PORT_FLAG = re.compile(r"--remote-debugging-port=(\d+)")

# The element's centre after scrolling it into view, or null when nothing
# matches or it has no size. Clicks and hovers go through the input pipeline
# rather than element.click(), which skips hit-testing and so "succeeds" on a
# covered, disabled or scrolled-away control.
_CENTRE_OF = """
(() => {
  const element = document.querySelector(%s);
  if (element === null) { return null; }
  element.scrollIntoView({ block: "center" });
  const rect = element.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) { return null; }
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
})()
"""

# Focus the element and put the caret at its end; "missing", "unfocusable",
# or the element's kind: "field" (input, textarea), "editable"
# (contenteditable), "select".
_FOCUS = """
((selector, selectAll) => {
  const element = document.querySelector(selector);
  if (element === null) { return "missing"; }
  element.scrollIntoView({ block: "center" });
  element.focus();
  const active = document.activeElement;
  if (active !== element && !element.contains(active)) { return "unfocusable"; }
  if (element instanceof HTMLSelectElement) { return "select"; }
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
    const end = element.value.length;
    try { element.setSelectionRange(selectAll ? 0 : end, end); } catch (e) {}
    return "field";
  }
  if (element.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(element);
    if (!selectAll) { range.collapse(false); }
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return "editable";
  }
  return "other";
})(%s, %s)
"""

# A field's value set the way a person's typing sets it: through the
# prototype's setter (React tracks the instance's own), then input and
# change, so a framework's state follows.
_SET_FIELD = """
((selector, value) => {
  const element = document.querySelector(selector);
  if (element === null) { return null; }
  const prototype = Object.getPrototypeOf(element);
  const setter = Object.getOwnPropertyDescriptor(prototype, "value").set;
  setter.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
  return element.value;
})(%s, %s)
"""

# What the element holds: a field's value, else its rendered text.
_READ = """
((selector) => {
  const element = document.querySelector(selector);
  if (element === null) { return null; }
  if ("value" in element && (element instanceof HTMLInputElement
      || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement)) {
    return { text: element.value, via: "value" };
  }
  return { text: element.innerText, via: "innerText" };
})(%s)
"""

# Whether a visible element matches: in the DOM, with a size, not hidden.
_VISIBLE = """
((selector) => {
  const element = document.querySelector(selector);
  if (element === null) { return false; }
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden";
})(%s)
"""


# The names of the files a file input holds, or null when nothing matches or
# it is no file input.
_FILE_NAMES = """
((selector) => {
  const element = document.querySelector(selector);
  if (element === null || !element.files) { return null; }
  return Array.from(element.files, (file) => file.name);
})(%s)
"""


# Both centres after scrolling each into view, or the selector that matches
# nothing, has no size, or is outside the viewport once the other is in it.
_TWO_CENTRES = """
((first, second) => {
  const selectors = [first, second];
  const elements = selectors.map((s) => document.querySelector(s));
  for (const [i, element] of elements.entries()) {
    if (element === null) { return selectors[i]; }
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  const centres = [];
  for (const [i, element] of elements.entries()) {
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    if (rect.width === 0 || rect.height === 0 || x < 0 || y < 0
        || x > innerWidth || y > innerHeight) { return selectors[i]; }
    centres.push({ x, y });
  }
  return centres;
})(%s, %s)
"""

_VIEWPORT_CENTRE = "({ x: innerWidth / 2, y: innerHeight / 2 })"

# What the wheel at `selector` scrolls: the element or its nearest scrollable
# ancestor, else the window; its position and how far it can go. Null when
# the selector matches nothing.
_SCROLLER = """
((selector) => {
  let element = selector === null ? null : document.querySelector(selector);
  if (selector !== null && element === null) { return null; }
  const root = document.scrollingElement || document.documentElement;
  const scrolls = (e) => {
    const style = getComputedStyle(e);
    return /(auto|scroll|overlay)/.test(style.overflowY + style.overflowX)
      && (e.scrollHeight > e.clientHeight || e.scrollWidth > e.clientWidth);
  };
  while (element && element !== root && element !== document.body && !scrolls(element)) {
    element = element.parentElement;
  }
  const page = !element || element === root || element === document.body;
  const box = page ? root : element;
  return {
    scroller: page ? "window" : "element",
    top: Math.round(box.scrollTop), left: Math.round(box.scrollLeft),
    max_top: box.scrollHeight - box.clientHeight, max_left: box.scrollWidth - box.clientWidth,
  };
})(%s)
"""

SCROLL_ENDS = ("top", "bottom")
WAITS = ("load", "domcontentloaded", "none")
_LOADED = {"load": "Page.loadEventFired", "domcontentloaded": "Page.domContentEventFired"}
SETTLE = 0.1  # seconds a scroll position must hold to count as settled

# Keys as Chrome's US layout gives them: key, code, Windows virtual key,
# and the modifier bit or the text the key types (None: none).
_MODIFIERS = {
    "ALT": ("Alt", "AltLeft", 18, 1),
    "CTRL": ("Control", "ControlLeft", 17, 2),
    "META": ("Meta", "MetaLeft", 91, 4),
    "SHIFT": ("Shift", "ShiftLeft", 16, 8),
}
_MODIFIER_ALIASES = {
    "LALT": "ALT", "RALT": "ALT", "CONTROL": "CTRL", "LCTRL": "CTRL", "RCTRL": "CTRL",
    "LCONTROL": "CTRL", "RCONTROL": "CTRL", "LSHIFT": "SHIFT", "RSHIFT": "SHIFT",
    "WIN": "META", "LWIN": "META", "RWIN": "META", "CMD": "META", "COMMAND": "META",
}  # fmt: skip
_NAMED = {
    "ENTER": ("Enter", "Enter", 13, "\r"),
    "TAB": ("Tab", "Tab", 9, None),
    "ESCAPE": ("Escape", "Escape", 27, None),
    "BACKSPACE": ("Backspace", "Backspace", 8, None),
    "DELETE": ("Delete", "Delete", 46, None),
    "INSERT": ("Insert", "Insert", 45, None),
    "SPACE": (" ", "Space", 32, " "),
    "LEFT": ("ArrowLeft", "ArrowLeft", 37, None),
    "UP": ("ArrowUp", "ArrowUp", 38, None),
    "RIGHT": ("ArrowRight", "ArrowRight", 39, None),
    "DOWN": ("ArrowDown", "ArrowDown", 40, None),
    "HOME": ("Home", "Home", 36, None),
    "END": ("End", "End", 35, None),
    "PAGEUP": ("PageUp", "PageUp", 33, None),
    "PAGEDOWN": ("PageDown", "PageDown", 34, None),
    **{f"F{n}": (f"F{n}", f"F{n}", 111 + n, None) for n in range(1, 13)},
}
_NAMED_ALIASES = {
    "RETURN": "ENTER", "ESC": "ESCAPE", "BACK": "BACKSPACE", "DEL": "DELETE",
    "INS": "INSERT", "PRIOR": "PAGEUP", "NEXT": "PAGEDOWN", "ARROWLEFT": "LEFT",
    "ARROWUP": "UP", "ARROWRIGHT": "RIGHT", "ARROWDOWN": "DOWN",
}  # fmt: skip
# US punctuation: the unshifted and shifted character, code, virtual key.
_PUNCTUATION = [
    ("`", "~", "Backquote", 192), ("-", "_", "Minus", 189), ("=", "+", "Equal", 187),
    ("[", "{", "BracketLeft", 219), ("]", "}", "BracketRight", 221),
    ("\\", "|", "Backslash", 220), (";", ":", "Semicolon", 186), ("'", '"', "Quote", 222),
    (",", "<", "Comma", 188), (".", ">", "Period", 190), ("/", "?", "Slash", 191),
]  # fmt: skip
_DIGIT_SHIFTED = ")!@#$%^&*("
# A headful Chrome runs no editing shortcut for a synthetic key event, so
# the keyDown names the editor command. Ctrl only: Windows and Linux.
_EDIT_COMMANDS = {
    ("a", 2): "selectAll", ("c", 2): "copy", ("x", 2): "cut", ("v", 2): "paste",
    ("z", 2): "undo", ("y", 2): "redo", ("z", 10): "redo",
}  # fmt: skip


def _with_text(params: dict, text: str | None) -> dict:
    if text is not None:
        params["text"] = params["unmodifiedText"] = text
    return params


def _char_key(ch: str, modifiers: int) -> dict:
    """keyDown params for a character. Under Ctrl, Alt or Meta it types
    nothing (a shortcut); under Shift alone, its shifted form."""
    lower = ch.lower()
    letter = lower.isascii() and lower.isalpha()
    code, vk, shifted = "", 0, ch
    if letter:
        code, vk, shifted = f"Key{lower.upper()}", ord(lower.upper()), lower.upper()
    elif ch.isascii() and ch.isdigit():
        code, vk, shifted = f"Digit{ch}", ord(ch), _DIGIT_SHIFTED[int(ch)]
    elif ch in _DIGIT_SHIFTED:
        digit = str(_DIGIT_SHIFTED.index(ch))
        code, vk = f"Digit{digit}", ord(digit)
    else:
        for plain, shift, pcode, pvk in _PUNCTUATION:
            if ch in (plain, shift):
                code, vk, shifted = pcode, pvk, shift
    if modifiers & 7:
        key, text = (lower if letter else ch), None
    elif modifiers & 8:
        key = text = shifted
    else:
        key = text = ch
    params = _with_text({"key": key, "code": code, "windowsVirtualKeyCode": vk}, text)
    command = _EDIT_COMMANDS.get((lower, modifiers))
    if command is not None:
        params["commands"] = [command]
    return params


def _named_key(name: str) -> dict:
    key, code, vk, text = _NAMED[name]
    return _with_text({"key": key, "code": code, "windowsVirtualKeyCode": vk}, text)


def key_events(keys: str) -> list[dict]:
    """`ui keys` syntax as Input.dispatchKeyEvent params, in order. A
    modifier ({Ctrl}, {Alt}, {Shift}, {Meta} or {Win}) is held for the next
    key or a parenthesised group after it; they nest ({Ctrl}{Shift}p). A key
    that types is a keyDown with its text (Chrome makes keypress and input of
    it), one that does not a rawKeyDown; each ends in a keyUp. {Key N}
    presses it N times. An unknown {Name} raises."""
    out: list[dict] = []
    held: list[str] = []
    group = False

    def modifiers() -> int:
        return sum(_MODIFIERS[name][3] for name in held)

    def modifier_event(kind: str, name: str) -> dict:
        key, code, vk, _ = _MODIFIERS[name]
        return {"type": kind, "modifiers": modifiers(), "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "location": 1}  # fmt: skip

    def hold(name: str) -> None:
        if name not in held:
            held.append(name)
            out.append(modifier_event("rawKeyDown", name))

    def release() -> None:
        while held:
            name = held.pop()
            out.append(modifier_event("keyUp", name))

    def press(params: dict) -> None:
        down = "keyDown" if "text" in params else "rawKeyDown"
        out.append({"type": down, "modifiers": modifiers(), **params})
        up = {k: v for k, v in params.items() if k not in ("text", "unmodifiedText", "commands")}
        out.append({"type": "keyUp", "modifiers": modifiers(), **up})
        if not group:
            release()

    for token in keysyntax.tokens(keys):
        if token[0] == "open":
            group = bool(held)
            if not held:
                press(_char_key("(", 0))
        elif token[0] == "close":
            if held:
                group = False
                release()
            else:
                press(_char_key(")", 0))
        elif token[0] == "char":
            press(_char_key(token[1], modifiers()))
        else:
            _, name, count = token
            upper = name.upper()
            modifier = _MODIFIER_ALIASES.get(upper, upper)
            named = _NAMED_ALIASES.get(upper, upper)
            for _ in range(count):
                if modifier in _MODIFIERS:
                    hold(modifier)
                elif named in _NAMED:
                    press(_named_key(named))
                elif len(name) == 1:
                    press(_char_key(name, modifiers()))
                else:
                    raise PilotError(
                        f"{{{name}}} is not a key cdp knows; e.g. {{Enter}}, {{Tab}}, {{Left}}, "
                        "{PageDown}, {F5}, {Ctrl}, {Shift}"
                    )
    release()
    return out


def parse_delta(text: str) -> tuple[float, float]:
    try:
        dx, dy = (float(part) for part in text.split(","))
    except ValueError as exc:
        raise PilotError(f"--by is DX,DY in pixels (e.g. 0,300), not {text!r}") from exc
    return dx, dy


# What `console` reads. The enables replay what the page logged before the
# connection (V8 keeps the console messages and exceptions of the current
# document, Log its entries), then report new ones live.
CONSOLE_EVENTS = ("Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded")
LEVELS = {"all": 0, "verbose": 0, "info": 1, "warning": 2, "error": 3}
_CONSOLE_LEVEL = {"error": "error", "assert": "error", "warning": "warning", "debug": "verbose"}
COLOR_SCHEMES = ("dark", "light")


def _arg_text(arg: dict) -> str:
    """One console argument as the DevTools console prints it, roughly."""
    if "value" in arg:
        value = arg["value"]
        return value if isinstance(value, str) else json.dumps(value)
    return str(arg.get("description") or arg.get("unserializableValue") or arg.get("type", ""))


def _line(number: int | None) -> int | None:
    """CDP lines count from 0; an editor's from 1."""
    return None if number is None else number + 1


def console_entry(event: dict) -> dict:
    """A console event as {level, source, text, url, line, timestamp}: level
    is error, warning, info or verbose; source is console (a console.* call),
    exception (uncaught), or Log's own (network, javascript, violation, ...);
    timestamp is ms since the epoch."""
    method, params = event["method"], event.get("params", {})
    if method == "Runtime.consoleAPICalled":
        frames = (params.get("stackTrace") or {}).get("callFrames") or [{}]
        return {
            "level": _CONSOLE_LEVEL.get(params.get("type"), "info"),
            "source": "console",
            "text": " ".join(_arg_text(a) for a in params.get("args", [])),
            "url": frames[0].get("url"),
            "line": _line(frames[0].get("lineNumber")),
            "timestamp": params.get("timestamp"),
        }
    if method == "Runtime.exceptionThrown":
        details = params.get("exceptionDetails", {})
        thrown = details.get("exception") or {}
        return {
            "level": "error",
            "source": "exception",
            "text": thrown.get("description") or details.get("text", ""),
            "url": details.get("url"),
            "line": _line(details.get("lineNumber")),
            "timestamp": params.get("timestamp"),
        }
    entry = params.get("entry", {})
    return {
        "level": entry.get("level", "info"),
        "source": entry.get("source", "other"),
        "text": entry.get("text", ""),
        "url": entry.get("url"),
        "line": _line(entry.get("lineNumber")),
        "timestamp": entry.get("timestamp"),
    }


def port_in(texts: Iterable[str]) -> int | None:
    """The port a `--remote-debugging-port=N` flag in `texts` names (app
    args, or a WebView2 arguments string), or None."""
    for text in texts:
        found = PORT_FLAG.search(text or "")
        if found:
            return int(found.group(1))
    return None


def get_json(port: int, path: str, timeout: float, *, hint: str = "") -> Any:
    """GET http://127.0.0.1:PORT/PATH as JSON. Nothing listening is NoTarget,
    with `hint` (how the port gets opened) after the message."""
    # 127.0.0.1, not localhost: WebView2 listens on IPv4 only, and a client
    # that tries ::1 first can wait out its whole timeout there.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as answer:
            return json.load(answer)
    except OSError as exc:
        message = f"nothing is listening on CDP port {port}"
        raise NoTarget(f"{message}. {hint}" if hint else message) from exc
    except ValueError as exc:
        raise PilotError(
            f"port {port} answered {path} with something that is not CDP JSON"
        ) from exc


def version(port: int, timeout: float = LIST_TIMEOUT, *, hint: str = "") -> dict:
    """/json/version: Browser, Protocol-Version, User-Agent, ..."""
    found = get_json(port, "/json/version", timeout, hint=hint)
    if not isinstance(found, dict):
        raise PilotError(f"port {port} answered /json/version with something that is not CDP")
    return found


def list_targets(port: int, timeout: float = LIST_TIMEOUT, *, hint: str = "") -> list[dict]:
    """Every target: pages, iframes, workers, the browser's own."""
    found = get_json(port, "/json", timeout, hint=hint)
    if not isinstance(found, list):
        raise PilotError(f"port {port} answered /json with something that is not a target list")
    return found


def matching(
    targets: list[dict],
    *,
    url: str | None = None,
    title: str | None = None,
    kind: str | None = "page",
) -> list[dict]:
    """The targets of type `kind` (None: any) whose URL and title hold `url`
    and `title`."""
    return [
        t
        for t in targets
        if (kind is None or t.get("type") == kind)
        and (url is None or url in t.get("url", ""))
        and (title is None or title in t.get("title", ""))
    ]


def pick(
    targets: list[dict],
    *,
    url: str | None = None,
    title: str | None = None,
    kind: str | None = "page",
) -> dict:
    """The one target `url`, `title` and `kind` name. None or two is an
    error, never a guess: a command meant for one tab must not land in another."""
    hits = matching(targets, url=url, title=title, kind=kind)
    wanted = ", ".join(
        f"{key} ~ {value!r}" for key, value in (("url", url), ("title", title)) if value
    )
    what = f"{kind or 'any'} target" + (f" with {wanted}" if wanted else "")
    if not hits:
        seen = [f"{t.get('type')}: {t.get('title')!r} {t.get('url')}" for t in targets]
        raise NoTarget(f"no {what}; the targets are {seen}")
    if len(hits) > 1:
        seen = [f"{t.get('title')!r} {t.get('url')}" for t in hits]
        raise Ambiguous(f"{len(hits)} targets match ({what}); narrow with --url or --title: {seen}")
    return hits[0]


class Page:
    """One connection to a target, shared by every call of one command.

    An Emulation override belongs to the connection that set it, so set, act
    and clear share a Page. Measured on Chrome: the emulated media ends when
    its connection closes; other overrides on other hosts (WebView2) may not.
    `what` names the target in errors ("the page", "the pane")."""

    what = "the page"

    def __init__(self, target: dict, *, timeout: float = TIMEOUT):
        self.target = target
        # Chromium refuses a DevTools socket that sends an Origin header.
        try:
            self._socket = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True
            )
        except (OSError, websocket.WebSocketException, KeyError) as exc:
            raise NoTarget(
                f"could not connect to {self.what} target {target.get('url')!r}: {exc}"
            ) from exc
        self._id = 0
        self._timeout = timeout
        # Events of the methods in `_kept` are kept in `events`, in order;
        # every other event is skipped.
        self._kept: set[str] = set()
        self.events: list[dict] = []

    def __enter__(self) -> Page:
        return self

    def __exit__(self, *exc) -> None:
        self._socket.close()

    def keep(self, *methods: str) -> None:
        """From now on keep the events of `methods` in `events`."""
        self._kept.update(methods)

    def _take(self, message: dict) -> None:
        if message.get("method") in self._kept:
            self.events.append(message)

    def send(self, method: str, params: dict | None = None) -> dict:
        """One CDP call; events that arrive meanwhile are skipped, unless
        `keep` named them."""
        self._id += 1
        try:
            self._socket.send(
                json.dumps({"id": self._id, "method": method, "params": params or {}})
            )
            while True:
                message = json.loads(self._socket.recv())
                if message.get("id") == self._id:
                    break
                self._take(message)
        except websocket.WebSocketTimeoutException as exc:
            raise Timeout(f"{method}: {self.what} did not answer") from exc
        except websocket.WebSocketConnectionClosedException as exc:
            raise PilotError(
                f"{method}: {self.what} closed the connection; is it still open?"
            ) from exc
        if "error" in message:
            raise PilotError(f"{method} failed: {message['error'].get('message')}")
        return message.get("result", {})

    def pump(self, until: Callable[[], bool], timeout: float) -> bool:
        """Read events, keeping the kept ones, until `until()` holds or
        `timeout` seconds pass; True when it held. A blocking read with the
        time left as the socket's timeout, not a poll: it wakes on each event,
        and with nothing to wait for but the clock (`console --for`) the read
        is the wait."""
        end = time.monotonic() + timeout
        try:
            while not until():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._socket.settimeout(left)
                try:
                    self._take(json.loads(self._socket.recv()))
                except websocket.WebSocketTimeoutException:
                    return until()
        except websocket.WebSocketConnectionClosedException as exc:
            raise PilotError(f"{self.what} closed the connection; is it still open?") from exc
        finally:
            self._socket.settimeout(self._timeout)
        return True

    def evaluate(self, expression: str) -> Any:
        """The expression's value as JSON; a returned promise is awaited."""
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        details = result.get("exceptionDetails")
        if details is not None:
            thrown = details.get("exception", {}).get("description") or details.get("text")
            raise PilotError(f"script threw: {thrown}")
        return result.get("result", {}).get("value")

    def _script(self, template: str, *args) -> Any:
        """`template` with each %s an argument as a JS literal."""
        return self.evaluate(template % tuple(json.dumps(a) for a in args))

    def hover(self, selector: str) -> dict:
        """Scroll the element into view and move the page's pointer to its
        centre (one mouseMoved): :hover, mouseover and mouseenter fire as for
        a person's pointer. The page keeps it there until a later move, a real
        pointer's included. Returns the point, in page pixels."""
        box = self._script(_CENTRE_OF, selector)
        if box is None:
            raise NoTarget(f"no visible element matches {selector!r}")
        self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **box})
        return box

    def click(self, selector: str) -> dict:
        """Hover, press and release at the element's centre, with real mouse
        events. Returns the point, in page pixels."""
        # Hover first: MUI mounts row actions and tooltips on mouseover.
        box = self.hover(selector)
        for kind in ("mousePressed", "mouseReleased"):
            self.send(
                "Input.dispatchMouseEvent",
                {"type": kind, **box, "button": "left", "buttons": 1, "clickCount": 1},
            )
        return box

    def focus(self, selector: str, *, select_all: bool = False) -> str:
        """Focus the element, the caret at its end (or all of it selected).
        Returns its kind: field, editable, select or other."""
        kind = self._script(_FOCUS, selector, select_all)
        if kind == "missing":
            raise NoTarget(f"no element matches {selector!r}")
        if kind == "unfocusable":
            raise PilotError(f"{selector!r} does not take focus; is it an input?")
        return kind

    def read(self, selector: str) -> dict:
        """{text, via}: a field's value, else the element's innerText."""
        found = self._script(_READ, selector)
        if found is None:
            raise NoTarget(f"no element matches {selector!r}")
        return found

    def type(self, selector: str, text: str) -> dict:
        """Focus the element and insert `text` at its end, as an IME commit
        does: one input event, any characters, no key mapping. JSON: typed,
        into, value (what the element holds after)."""
        self.focus(selector)
        self.send("Input.insertText", {"text": text})
        return {"typed": text, "into": selector, "value": self.read(selector)["text"]}

    def set_value(self, selector: str, value: str) -> dict:
        """Replace what the element holds: a field through its value setter
        and input/change events, a contenteditable by selecting all and
        inserting. Verified by reading it back."""
        kind = self.focus(selector, select_all=True)
        if kind in ("field", "select"):
            self._script(_SET_FIELD, selector, value)
        elif kind == "editable":
            if value:
                self.send("Input.insertText", {"text": value})
            else:
                self.evaluate("document.execCommand('delete')")
        else:
            raise PilotError(f"{selector!r} is not an input, a textarea, a select or editable")
        got = self.read(selector)["text"]
        # innerText ends a block with a newline the value never had.
        verified = got == value or (kind == "editable" and got.rstrip("\n") == value)
        return {"set": selector, "value": got, "verified": verified}

    def upload(self, selector: str, files: Iterable[str | Path]) -> dict:
        """Put `files` into the file input `selector`, as the OS file dialog
        does: DOM.setFileInputFiles, after which Chrome fires input and change
        itself. No dialog, no pointer, no window focus, and a hidden input
        (GitHub's attach box) takes them too: there is no visibility check.
        Every path must exist. JSON: selector, files (absolute paths), count,
        names (what the input holds after), verified (names are the files')."""
        paths = [Path(f).expanduser().resolve() for f in files]
        if not paths:
            raise PilotError("upload needs at least one file")
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise PilotError(f"no such file: {', '.join(missing)}")
        root = self.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node = self.send("DOM.querySelector", {"nodeId": root, "selector": selector})
        node_id = node.get("nodeId", 0)
        if not node_id:
            raise NoTarget(f"no element matches {selector!r}")
        described = self.send("DOM.describeNode", {"nodeId": node_id}).get("node", {})
        flat = described.get("attributes") or []
        attributes = {k.lower(): v for k, v in zip(flat[::2], flat[1::2], strict=False)}
        is_file = (
            described.get("nodeName", "").lower() == "input"
            and attributes.get("type", "").lower() == "file"
        )
        if not is_file:
            what = described.get("nodeName", "?").lower()
            raise PilotError(f"{selector!r} is <{what}>, not an input[type=file]")
        absolute = [str(p) for p in paths]
        self.send("DOM.setFileInputFiles", {"files": absolute, "nodeId": node_id})
        names = self._script(_FILE_NAMES, selector) or []
        return {
            "selector": selector,
            "files": absolute,
            "count": len(names),
            "names": names,
            "verified": names == [p.name for p in paths],
        }

    def keys(self, keys: str, *, into: str | None = None) -> dict:
        """Press `keys` (`ui keys` syntax) as key events in the page: keydown,
        keypress, input and keyup fire as for a keyboard, with no window focus
        and no real key. With `into`, focus that element first (caret at its
        end). Ctrl+A, C, X, V, Z, Y and Ctrl+Shift+Z run their editing command,
        which a synthetic key alone does not in a headful Chrome. JSON: keys,
        into, events (key events sent), value (what `into` holds after, else
        null)."""
        events = key_events(keys)
        if into is not None:
            self.focus(into)
        for params in events:
            self.send("Input.dispatchKeyEvent", params)
        value = None if into is None else self.read(into)["text"]
        return {"keys": keys, "into": into, "events": len(events), "value": value}

    def _scroller(self, selector: str | None) -> dict:
        found = self._script(_SCROLLER, selector)
        if found is None:
            raise NoTarget(f"no element matches {selector!r}")
        return found

    def _settled(self, selector: str | None) -> dict:
        """The scroll position once it holds for SETTLE seconds: a wheel
        scroll may animate."""
        last: list[dict | None] = [None]

        def held() -> dict | None:
            now = self._scroller(selector)
            same = now == last[0]
            last[0] = now
            return now if same else None

        return wait_until(held, timeout=self._timeout, interval=SETTLE, what="the scroll to settle")

    def scroll(
        self,
        selector: str | None = None,
        *,
        by: tuple[float, float] = (0, 100),
        to: str | None = None,
    ) -> dict:
        """Turn the page's wheel at the element's centre (the viewport's
        without `selector`): wheel handlers and virtual lists see a person's
        wheel. `by` is (dx, dy) in pixels; `to` (top | bottom) turns it until
        the scroller is at that end or stops moving. JSON: selector, x, y (the
        wheel's point), scroller (window | element), top, left, max_top,
        max_left (the scroller's position after, and its end)."""
        if to is not None and to not in SCROLL_ENDS:
            raise PilotError(f"--to is top or bottom, not {to!r}")
        if selector is None:
            point = self.evaluate(_VIEWPORT_CENTRE)
        else:
            point = self._script(_CENTRE_OF, selector)
            if point is None:
                raise NoTarget(f"no visible element matches {selector!r}")

        def wheel(dx: float, dy: float) -> dict:
            self.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", **point, "deltaX": dx, "deltaY": dy},
            )
            return self._settled(selector)

        if to is None:
            where = wheel(*by)
        else:
            where = self._scroller(selector)
            for _ in range(20):
                if (where["top"] <= 0) if to == "top" else (where["top"] >= where["max_top"]):
                    break
                step = where["max_top"] + 1000
                after = wheel(0, -step if to == "top" else step)
                if after == where:
                    break
                where = after
        return {"selector": selector, **point, **where}

    def drag(self, source: str, target: str, *, steps: int = 10) -> dict:
        """Press at `source`'s centre, move in `steps` to `target`'s, release
        there, with the page's pointer: pointer-event drag libraries follow.
        A native HTML5 drag (draggable=true) that starts on the way is
        intercepted and finished with drag events (dragenter, dragover, drop)
        carrying its data at `target`, since the OS drag loop would wait for
        a real pointer. Both elements are scrolled into view and must then
        both be in the viewport. JSON: from, to (page pixels), mode (mouse |
        html5)."""
        if steps < 1:
            raise PilotError("--steps is 1 or more")
        centres = self._script(_TWO_CENTRES, source, target)
        if not isinstance(centres, list):
            raise NoTarget(
                f"no visible element matches {centres!r}, or it is out of view with the other"
            )
        start, end = centres
        self.keep("Input.dragIntercepted")
        self.send("Input.setInterceptDrags", {"enabled": True})
        try:
            pressed = {"button": "left", "buttons": 1}
            self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **start})
            self.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", **start, **pressed, "clickCount": 1},
            )
            for i in range(1, steps + 1):
                point = {
                    "x": start["x"] + (end["x"] - start["x"]) * i / steps,
                    "y": start["y"] + (end["y"] - start["y"]) * i / steps,
                }
                self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **point, **pressed})

            def intercepted() -> list[dict]:
                return [e for e in self.events if e["method"] == "Input.dragIntercepted"]

            self.pump(lambda: bool(intercepted()), 0.2)
            dragged = intercepted()
            if dragged:
                data = dragged[0]["params"]["data"]
                for kind in ("dragEnter", "dragOver", "drop"):
                    self.send("Input.dispatchDragEvent", {"type": kind, **end, "data": data})
            self.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", **end, **pressed, "clickCount": 1},
            )
        finally:
            with contextlib.suppress(PilotError):  # never mask the drag's error
                self.send("Input.setInterceptDrags", {"enabled": False})
        return {"from": start, "to": end, "mode": "html5" if dragged else "mouse"}

    def navigate(self, url: str, *, wait: str = "load") -> dict:
        """Load `url` in the page and wait for its load event (`wait`: load,
        domcontentloaded, none). A failed load (DNS, refused, aborted) raises
        with Chrome's reason; an HTTP error page is a load. JSON: url (where
        the page is after, redirects followed), title."""
        if wait not in WAITS:
            raise PilotError(f"--wait is load, domcontentloaded or none, not {wait!r}")
        event = _LOADED.get(wait)
        if event is not None:
            self.keep(event)
            self.send("Page.enable")
            self.events.clear()
        result = self.send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise PilotError(f"navigation to {url!r} failed: {result['errorText']}")
        # A same-document navigation (a #fragment) has no loader and no load event.
        if event is not None and result.get("loaderId"):
            loaded = self.pump(
                lambda: any(e["method"] == event for e in self.events), self._timeout
            )
            if not loaded:
                raise Timeout(f"{url!r} did not finish loading in {self._timeout:g}s")
        return self.evaluate("({ url: location.href, title: document.title })")

    def visible(self, selector: str) -> bool:
        return bool(self._script(_VISIBLE, selector))

    def wait(self, selector: str, *, gone: bool = False, timeout: float = 10.0) -> dict:
        """Until a visible element matches (or, `gone`, none does)."""
        wait_until(
            lambda: self.visible(selector) != gone,
            timeout=timeout,
            interval=0.2,
            what=f"{selector!r} to {'go' if gone else 'show'}",
        )
        return {"selector": selector, "gone" if gone else "found": True}

    def screenshot(self, path: str | Path) -> Path:
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
        return target

    def color_scheme(self, scheme: str | None) -> None:
        """Emulate prefers-color-scheme (dark, light) on this connection;
        None clears it. It ends with the connection anyway (see the class)."""
        if scheme is not None and scheme not in COLOR_SCHEMES:
            raise PilotError(f"--color-scheme is dark or light, not {scheme!r}")
        self.send(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": scheme or ""}]},
        )

    @contextlib.contextmanager
    def emulating(self, scheme: str | None):
        """The page in `scheme` for the block, as it was after it; no-op for None."""
        if scheme is None:
            yield self
            return
        self.color_scheme(scheme)
        try:
            yield self
        finally:
            with contextlib.suppress(PilotError):  # never mask the block's error
                self.color_scheme(None)

    def console(self, *, level: str = "all", reload: bool = False, listen: float = 0.0) -> dict:
        """What the page logged: console calls, uncaught exceptions and Log
        entries (failed loads, CORS, violations), oldest first. Without
        `reload`, what the current document logged before now, replayed by
        the enables; with it, the page is reloaded and what the new document
        logs until its load event is read. Then `listen` more seconds of live
        entries. JSON: entries, count, errors (error entries at any level)."""
        if level not in LEVELS:
            raise PilotError(f"--level is error, warning, info or all, not {level!r}")
        self.keep(*CONSOLE_EVENTS)
        if reload:
            self.keep("Page.loadEventFired")
            self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Log.enable")
        if reload:
            self.events.clear()  # the old document's, replayed by the enables
            self.send("Page.reload")
            loaded = self.pump(
                lambda: any(e["method"] == "Page.loadEventFired" for e in self.events),
                self._timeout,
            )
            if not loaded:
                raise Timeout(f"{self.what} did not finish loading in {self._timeout:g}s")
        else:
            self.evaluate("0")  # answered after the replay: the buffer is in
        if listen > 0:
            self.pump(lambda: False, listen)
        found = [console_entry(e) for e in self.events if e["method"] in CONSOLE_EVENTS]
        found.sort(key=lambda e: e["timestamp"] or 0)
        floor = LEVELS[level]
        shown = [e for e in found if LEVELS.get(e["level"], 1) >= floor]
        errors = sum(e["level"] == "error" for e in found)
        return {"entries": shown, "count": len(shown), "errors": errors}
