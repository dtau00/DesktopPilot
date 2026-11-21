"""The CDP client (core/cdp.py) and the `cdp` group, over a fake socket and a
fake HTTP endpoint: no browser."""

from __future__ import annotations

import io
import json
import ntpath
import re
import subprocess
import sys
import urllib.error

import pytest
from typer.testing import CliRunner

from desktoppilot.core import cdp, cmd_cdp, commands, procs
from desktoppilot.core.errors import Ambiguous, NoTarget, PilotError, Timeout
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import Registry

PAGE = {
    "id": "p1",
    "type": "page",
    "title": "Inbox",
    "url": "https://mail/x",
    "webSocketDebuggerUrl": "ws://p1",
}
OTHER = {
    "id": "p2",
    "type": "page",
    "title": "Docs",
    "url": "https://docs/y",
    "webSocketDebuggerUrl": "ws://p2",
}
FRAME = {
    "id": "f1",
    "type": "iframe",
    "title": "",
    "url": "https://mail/frame",
    "webSocketDebuggerUrl": "ws://f1",
}


# ------------------------------------------------------------ ports, targets


def test_the_port_comes_from_the_flag_wherever_it_sits():
    assert cdp.port_in(["--a", "--remote-debugging-port=9333"]) == 9333
    assert cdp.port_in(["--x --remote-debugging-port=9444 --y"]) == 9444
    assert cdp.port_in(["--remote-debugging-pipe", ""]) is None


def test_pick_takes_the_one_page_the_filters_name():
    targets = [PAGE, OTHER, FRAME]
    assert cdp.pick(targets, url="mail") is PAGE
    assert cdp.pick(targets, title="Docs") is OTHER
    assert cdp.pick(targets, url="frame", kind="iframe") is FRAME
    assert cdp.pick([PAGE, FRAME]) is PAGE


def test_pick_never_guesses_between_two():
    with pytest.raises(Ambiguous, match="narrow with --url or --title"):
        cdp.pick([PAGE, OTHER])


def test_pick_names_what_is_there_when_nothing_matches():
    with pytest.raises(NoTarget, match=r"no page target with url ~ 'calendar'.*Inbox"):
        cdp.pick([PAGE], url="calendar")


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serving(monkeypatch, routes: dict) -> list:
    asked = []

    def urlopen(url, timeout):
        path = re.sub(r"^http://127\.0\.0\.1:\d+", "", url)
        asked.append(path)
        body = routes[path]
        if isinstance(body, Exception):
            raise body
        return Answer(body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(cdp.urllib.request, "urlopen", urlopen)
    return asked


def test_list_targets_reads_json(monkeypatch):
    asked = serving(monkeypatch, {"/json": [PAGE]})
    assert cdp.list_targets(9222) == [PAGE]
    assert asked == ["/json"]


def test_nothing_listening_is_no_target_with_the_hint(monkeypatch):
    serving(monkeypatch, {"/json": urllib.error.URLError("refused")})
    with pytest.raises(NoTarget, match="nothing is listening on CDP port 9222. Start it so"):
        cdp.list_targets(9222, hint="Start it so")


def test_an_answer_that_is_not_cdp_says_so(monkeypatch):
    serving(monkeypatch, {"/json": b"<html>", "/json/version": [1]})
    with pytest.raises(PilotError, match="not CDP JSON"):
        cdp.list_targets(9222)
    with pytest.raises(PilotError, match="not CDP"):
        cdp.version(9222)


# ------------------------------------------------------------ a page


class FakeSocket:
    """Answers each call with `reply(message)`, after an unrelated event."""

    def __init__(self, reply):
        self.reply = reply
        self.sent: list[dict] = []
        self.inbox: list[str] = []

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        self.inbox += [
            json.dumps({"method": "Page.loadEventFired"}),
            json.dumps(self.reply(message)),
        ]

    def recv(self):
        return self.inbox.pop(0)

    def close(self):
        pass


class FakeDom:
    """Runtime.evaluate answered by the script it runs: which one shows in
    its source, and the element's state lives here."""

    def __init__(self, kind="field", text=""):
        self.kind, self.text, self.inserted = kind, text, []

    def __call__(self, message):
        method, params = message["method"], message["params"]
        value = None
        if method == "Input.insertText":
            self.text += params["text"]
            self.inserted.append(params["text"])
        elif method == "Runtime.evaluate":
            source = params["expression"]
            if "element.focus()" in source:
                value = self.kind
                if self.kind in ("field", "editable") and ", true)" in source:
                    self.text = ""  # select all, then the insert replaces it
            elif "setter.call" in source:
                self.text = json.loads(source.rsplit(",", 1)[1].strip(" )\n"))
                value = self.text
            elif "innerText" in source:
                via = "innerText" if self.kind == "editable" else "value"
                value = None if self.kind == "missing" else {"text": self.text, "via": via}
            elif "getComputedStyle" in source:
                value = self.kind != "missing"
            else:
                value = 7
        return {"id": message["id"], "result": {"result": {"value": value}}}


def page_with(monkeypatch, reply) -> tuple[cdp.Page, FakeSocket]:
    socket = FakeSocket(reply)
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    return cdp.Page(PAGE), socket


def test_send_skips_events_until_its_own_reply(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {"n": m["id"]}})
    assert page.send("A") == {"n": 1}
    assert page.send("B") == {"n": 2}
    assert [m["method"] for m in socket.sent] == ["A", "B"]


def test_a_silent_page_is_a_timeout(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {})

    def silent():
        raise cdp.websocket.WebSocketTimeoutException("timed out")

    socket.recv = silent
    with pytest.raises(Timeout, match="the page did not answer"):
        page.send("X")


def test_type_inserts_at_the_end_and_reads_back(monkeypatch):
    dom = FakeDom("field", "ab")
    page, socket = page_with(monkeypatch, dom)
    assert page.type("#q", "cd é") == {"typed": "cd é", "into": "#q", "value": "abcd é"}
    assert dom.inserted == ["cd é"]
    focus = socket.sent[0]["params"]["expression"]
    assert '"#q", false)' in focus


def test_set_value_writes_a_field_through_its_setter(monkeypatch):
    dom = FakeDom("field", "old")
    page, socket = page_with(monkeypatch, dom)
    assert page.set_value("#q", "new") == {"set": "#q", "value": "new", "verified": True}
    assert dom.inserted == []
    assert '"#q", true)' in socket.sent[0]["params"]["expression"]


def test_set_value_replaces_an_editable_by_inserting(monkeypatch):
    dom = FakeDom("editable", "old text")
    page, _ = page_with(monkeypatch, dom)
    assert page.set_value("#rich", "new")["verified"] is True
    assert dom.inserted == ["new"]


def test_set_value_refuses_what_holds_no_value(monkeypatch):
    page, _ = page_with(monkeypatch, FakeDom("other"))
    with pytest.raises(PilotError, match="not an input"):
        page.set_value("#div", "x")


@pytest.mark.parametrize(
    ("kind", "error", "message"),
    [
        ("missing", NoTarget, "no element matches"),
        ("unfocusable", PilotError, "does not take focus"),
    ],
)
def test_type_into_nothing_raises(monkeypatch, kind, error, message):
    page, _ = page_with(monkeypatch, FakeDom(kind))
    with pytest.raises(error, match=message):
        page.type("#x", "a")


def test_read_and_wait(monkeypatch):
    page, _ = page_with(monkeypatch, FakeDom("field", "v"))
    assert page.read("#q") == {"text": "v", "via": "value"}
    assert page.wait("#q", timeout=0.1) == {"selector": "#q", "found": True}
    gone, _ = page_with(monkeypatch, FakeDom("missing"))
    assert gone.wait("#q", gone=True, timeout=0.1) == {"selector": "#q", "gone": True}
    with pytest.raises(Timeout):
        gone.wait("#q", timeout=0.1)


# ------------------------------------------------------------ the group

runner = CliRunner()


def tree_for(profile: Profile):
    reg = Registry()
    reg.group("cdp", cmd_cdp.cdp_group(), for_app=profile.name)
    return commands.build_app_cli(profile.name, profile, reg)


def test_the_group_takes_the_port_from_the_profile_args(monkeypatch):
    monkeypatch.delenv("PILOT_CHROMEISH_PID", raising=False)
    monkeypatch.delenv("PILOT_PLAIN_PID", raising=False)
    profile = Profile(name="chromeish", args=["--remote-debugging-port=9555"])
    assert cmd_cdp.port_of(profile, None) == 9555
    assert cmd_cdp.port_of(profile, 9000) == 9000
    with pytest.raises(PilotError, match="opens no debugging port.*PILOT_PLAIN_PID is not set"):
        cmd_cdp.port_of(Profile(name="plain"), None)


def command_lines(monkeypatch, lines: dict) -> list[int]:
    """procs.command_line answers from `lines` (None for any other pid);
    returns the pids it was asked for."""
    asked: list[int] = []

    def command_line(pid):
        asked.append(pid)
        return lines.get(pid)

    monkeypatch.setattr(cmd_cdp.procs, "command_line", command_line)
    return asked


def test_a_bound_pid_gives_its_own_port_before_the_profile_args(monkeypatch):
    monkeypatch.setenv("PILOT_CHROME_ISH_PID", "4242")
    line = '"C:\\chrome.exe" --user-data-dir=x --remote-debugging-port=9333 about:blank'
    asked = command_lines(monkeypatch, {4242: line})
    profile = Profile(name="chrome-ish", args=["--remote-debugging-port=9555"])
    assert cmd_cdp.port_of(profile, None) == 9333
    assert asked == [4242]
    assert cmd_cdp.port_of(Profile(name="chrome-ish"), None) == 9333
    assert cmd_cdp.port_of(profile, 9000) == 9000
    assert asked == [4242, 4242]


@pytest.mark.parametrize("line", [None, "chrome.exe --no-first-run"], ids=["gone", "no flag"])
def test_a_bound_pid_without_a_port_falls_back_then_names_both_ways(monkeypatch, line):
    monkeypatch.setenv("PILOT_CHROMEISH_PID", "4242")
    command_lines(monkeypatch, {4242: line})
    with_args = Profile(name="chromeish", args=["--remote-debugging-port=9555"])
    assert cmd_cdp.port_of(with_args, None) == 9555
    with pytest.raises(PilotError) as raised:
        cmd_cdp.port_of(Profile(name="chromeish"), None)
    message = str(raised.value)
    assert "pass --port" in message
    assert "PILOT_CHROMEISH_PID=4242 has no --remote-debugging-port=N" in message


def test_the_command_line_of_a_live_process_names_its_port():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()", "--remote-debugging-port=9444"],
        stdin=subprocess.PIPE,
    )
    try:
        assert cdp.port_in([procs.command_line(child.pid) or ""]) == 9444
    finally:
        child.communicate(timeout=10)
    assert procs.command_line(child.pid) is None


def test_the_group_says_how_to_open_the_port(monkeypatch):
    serving(monkeypatch, {"/json": urllib.error.URLError("refused")})
    tree = tree_for(Profile(name="chromeish", args=["--remote-debugging-port=9555"]))
    result = runner.invoke(tree, ["--no-shot", "cdp", "eval", "1"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "NoTarget"
    assert "--remote-debugging-port=N" in payload["message"]


# ------------------------------------------------------------ hover, console, color scheme


def test_hover_moves_the_page_pointer_only_and_click_hovers_first(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {}})
    monkeypatch.setattr(cdp.Page, "_script", lambda self, template, *a: {"x": 3, "y": 4})
    assert page.hover("#h") == {"x": 3, "y": 4}
    assert [m["params"]["type"] for m in socket.sent] == ["mouseMoved"]
    socket.sent.clear()
    page.click("#h")
    kinds = [m["params"]["type"] for m in socket.sent]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_hover_of_nothing_is_no_target(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {}})
    monkeypatch.setattr(cdp.Page, "_script", lambda self, template, *a: None)
    with pytest.raises(NoTarget, match="no visible element"):
        page.hover("#none")
    assert socket.sent == []


def logged(kind, text, ts, line=4):
    return {
        "method": "Runtime.consoleAPICalled",
        "params": {
            "type": kind,
            "args": [{"type": "string", "value": text}, {"type": "number", "value": 42}],
            "timestamp": ts,
            "stackTrace": {"callFrames": [{"url": "file:///p.html", "lineNumber": line}]},
        },
    }


THROWN = {
    "method": "Runtime.exceptionThrown",
    "params": {
        "timestamp": 30.0,
        "exceptionDetails": {
            "text": "Uncaught",
            "url": "file:///p.html",
            "lineNumber": 9,
            "exception": {"description": "Error: boom"},
        },
    },
}
FAILED_LOAD = {
    "method": "Log.entryAdded",
    "params": {
        "entry": {
            "source": "network",
            "level": "error",
            "text": "Failed to load resource",
            "url": "http://x/a.png",
            "timestamp": 5.0,
        }
    },
}


class ConsoleSocket:
    """A page that logged before the connection: the enables replay it
    before their reply, as Chrome does; a reload logs anew, then loads.
    A read with nothing left waits out the socket's timeout."""

    def __init__(self, before: list[dict], after: list[dict]):
        self.before, self.after = before, after
        self.sent: list[dict] = []
        self.inbox: list[str] = []
        self.timeouts: list[float | None] = []

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        method = message["method"]
        replay = {
            "Runtime.enable": [e for e in self.before if e["method"].startswith("Runtime.")],
            "Log.enable": [e for e in self.before if e["method"].startswith("Log.")],
        }.get(method, [])
        self.inbox += [json.dumps(e) for e in replay]
        self.inbox.append(json.dumps({"id": message["id"], "result": {"result": {"value": 0}}}))
        if method == "Page.reload":
            self.inbox += [json.dumps(e) for e in self.after]
            self.inbox.append(json.dumps({"method": "Page.loadEventFired", "params": {}}))

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def recv(self):
        if not self.inbox:
            raise cdp.websocket.WebSocketTimeoutException("timed out")
        return self.inbox.pop(0)

    def close(self):
        pass


def console_page(monkeypatch, before, after=()):
    socket = ConsoleSocket(list(before), list(after))
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    return cdp.Page(PAGE, timeout=2.0), socket


def test_console_reads_what_the_page_logged_before_the_connection(monkeypatch):
    before = [logged("log", "hi", 10.0), logged("error", "bad", 20.0), THROWN, FAILED_LOAD]
    page, socket = console_page(monkeypatch, before)
    found = page.console()
    assert [m["method"] for m in socket.sent] == [
        "Runtime.enable",
        "Log.enable",
        "Runtime.evaluate",
    ]
    assert found["count"] == 4 and found["errors"] == 3
    assert [e["text"] for e in found["entries"]] == [
        "Failed to load resource",
        "hi 42",
        "bad 42",
        "Error: boom",
    ]
    first, _, bad, thrown = found["entries"]
    assert first == {
        "level": "error",
        "source": "network",
        "text": "Failed to load resource",
        "url": "http://x/a.png",
        "line": None,
        "timestamp": 5.0,
    }
    assert (bad["level"], bad["source"], bad["line"]) == ("error", "console", 5)
    assert (thrown["source"], thrown["line"]) == ("exception", 10)


def test_console_level_filters_the_entries_but_not_the_error_count(monkeypatch):
    before = [logged("log", "hi", 1.0), logged("warning", "careful", 2.0), THROWN]
    page, _ = console_page(monkeypatch, before)
    found = page.console(level="warning")
    assert [e["level"] for e in found["entries"]] == ["warning", "error"]
    assert found["errors"] == 1
    page, _ = console_page(monkeypatch, before)
    assert page.console(level="error")["count"] == 1
    with pytest.raises(PilotError, match="--level is error"):
        page.console(level="loud")


def test_console_reload_drops_the_old_document_and_reads_the_new_one(monkeypatch):
    page, socket = console_page(monkeypatch, [logged("error", "old", 1.0)], [THROWN])
    found = page.console(reload=True)
    methods = [m["method"] for m in socket.sent]
    assert methods == ["Page.enable", "Runtime.enable", "Log.enable", "Page.reload"]
    assert [e["text"] for e in found["entries"]] == ["Error: boom"]
    assert socket.timeouts[-1] == 2.0  # the socket's own timeout is back


def test_console_listen_reads_until_its_seconds_pass(monkeypatch):
    page, socket = console_page(monkeypatch, [])
    page.keep("Log.entryAdded")
    socket.inbox.append(json.dumps(FAILED_LOAD))
    assert page.pump(lambda: False, 0.2) is False
    assert len(page.events) == 1 and 0 < socket.timeouts[0] <= 0.2


def test_send_keeps_only_the_events_it_was_told_to(monkeypatch):
    page, _ = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {}})
    page.send("A")
    assert page.events == []
    page.keep("Page.loadEventFired")
    page.send("B")
    assert page.events == [{"method": "Page.loadEventFired"}]


def test_a_color_scheme_is_set_for_the_block_and_cleared_after(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {}})
    with pytest.raises(PilotError, match="inside"), page.emulating("dark"):
        raise PilotError("inside")
    values = [m["params"]["features"][0]["value"] for m in socket.sent]
    assert [m["method"] for m in socket.sent] == ["Emulation.setEmulatedMedia"] * 2
    assert values == ["dark", ""]
    socket.sent.clear()
    with page.emulating(None):
        pass
    assert socket.sent == []
    with pytest.raises(PilotError, match="dark or light"):
        page.color_scheme("blue")


def test_the_group_refuses_an_unknown_color_scheme_before_connecting(monkeypatch):
    monkeypatch.setattr(cdp, "list_targets", lambda *a, **k: pytest.fail("connected"))
    tree = tree_for(Profile(name="chromeish", args=["--remote-debugging-port=9555"]))
    result = runner.invoke(tree, ["--no-shot", "cdp", "eval", "1", "--color-scheme", "blue"])
    assert result.exit_code == 1
    assert "dark or light" in json.loads(result.output)["message"]


# ------------------------------------------------------------ upload


class FileInputDom:
    """The DOM calls `upload` makes: one element under the root, a file input
    unless told otherwise; setFileInputFiles fills it."""

    def __init__(self, node_name="INPUT", attributes=("type", "file"), found=True):
        self.node_name, self.attributes, self.found = node_name, list(attributes), found
        self.names: list[str] = []
        self.clears = False  # the page's change handler empties the input (GitHub)
        self.fires = True  # change fires (setting the files it holds fires none)
        self.slot: dict | None = None
        self.scripts: list[str] = []

    def __call__(self, message):
        method, params = message["method"], message["params"]
        result: dict = {}
        if method == "DOM.getDocument":
            result = {"root": {"nodeId": 1}}
        elif method == "DOM.querySelector":
            result = {"nodeId": 5 if self.found else 0}
        elif method == "DOM.describeNode":
            result = {"node": {"nodeName": self.node_name, "attributes": self.attributes}}
        elif method == "DOM.setFileInputFiles":
            self.names = [ntpath.basename(f) for f in params["files"]]
            if self.fires and self.slot is not None:
                self.slot = {"changed": True, "names": list(self.names)}
            if self.clears:
                self.names = []
        elif method == "Runtime.evaluate":
            result = {"result": {"value": self.evaluate(params["expression"])}}
        return {"id": message["id"], "result": result}

    def evaluate(self, expression):
        """The page side of the scripts `upload` runs: the watch, its slot
        (taken down with stop=true), the input's names."""
        name = next(
            n
            for n in ("_WATCH_CHANGE", "_SEEN", "_FILE_NAMES")
            if expression.startswith(getattr(cdp, n).split("%s")[0])
        )
        self.scripts.append(name)
        if name == "_WATCH_CHANGE":
            self.slot = {"changed": False, "names": None}
            return True
        if name == "_SEEN":
            seen = self.slot
            if expression.rstrip().endswith("true)"):
                self.slot = None
            return seen
        return self.names


def test_upload_sets_the_files_by_absolute_path_and_reads_them_back(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.png").write_bytes(b"a")
    (tmp_path / "b.txt").write_text("b")
    dom = FileInputDom(attributes=["class", "x", "TYPE", "File"])
    page, socket = page_with(monkeypatch, dom)
    found = page.upload("file-attachment input[type=file]", ["a.png", tmp_path / "b.txt"])
    absolute = [str((tmp_path / "a.png").resolve()), str((tmp_path / "b.txt").resolve())]
    assert found == {
        "selector": "file-attachment input[type=file]",
        "files": absolute,
        "count": 2,
        "changed": True,
        "names": ["a.png", "b.txt"],
        "names_after": ["a.png", "b.txt"],
        "verified": True,
    }
    methods = [m["method"] for m in socket.sent]
    assert methods == [
        "DOM.getDocument",
        "DOM.querySelector",
        "DOM.describeNode",
        "Runtime.evaluate",
        "DOM.setFileInputFiles",
        "Runtime.evaluate",
        "Runtime.evaluate",
        "Runtime.evaluate",
    ]
    # Watch before the files go in; one look (change fires inside the call),
    # the watch comes down, then the input is read.
    assert dom.scripts == ["_WATCH_CHANGE", "_SEEN", "_SEEN", "_FILE_NAMES"]
    assert dom.slot is None
    assert socket.sent[1]["params"] == {"nodeId": 1, "selector": "file-attachment input[type=file]"}
    assert socket.sent[4]["params"] == {"files": absolute, "nodeId": 5}


def test_upload_is_verified_by_the_change_event_when_the_page_clears_the_input(
    monkeypatch, tmp_path
):
    (tmp_path / "a.png").write_bytes(b"a")
    dom = FileInputDom()
    dom.clears = True
    page, _ = page_with(monkeypatch, dom)
    found = page.upload("#f", [tmp_path / "a.png"])
    assert found["changed"] is True
    assert found["names"] == ["a.png"] and found["count"] == 1
    assert found["names_after"] == []
    assert found["verified"] is True
    assert dom.slot is None


def test_upload_without_a_change_event_reads_the_input_after(monkeypatch, tmp_path):
    (tmp_path / "a.png").write_bytes(b"a")
    monkeypatch.setattr(cdp, "CHANGE_TIMEOUT", 0)
    dom = FileInputDom()
    dom.fires = False
    page, _ = page_with(monkeypatch, dom)
    found = page.upload("#f", [tmp_path / "a.png"])
    assert found["changed"] is False
    assert found["names"] == found["names_after"] == ["a.png"]
    assert found["verified"] is True
    assert dom.slot is None
    dom.clears = True
    assert page.upload("#f", [tmp_path / "a.png"])["verified"] is False


def test_upload_of_a_missing_file_sends_nothing(monkeypatch, tmp_path):
    page, socket = page_with(monkeypatch, FileInputDom())
    with pytest.raises(PilotError, match="no such file: .*gone.png"):
        page.upload("#f", [tmp_path / "gone.png"])
    assert socket.sent == []


def test_upload_to_nothing_is_no_target(monkeypatch, tmp_path):
    (tmp_path / "a.png").write_bytes(b"a")
    page, socket = page_with(monkeypatch, FileInputDom(found=False))
    with pytest.raises(NoTarget, match="no element matches '#f'"):
        page.upload("#f", [tmp_path / "a.png"])
    assert "DOM.setFileInputFiles" not in [m["method"] for m in socket.sent]


@pytest.mark.parametrize(
    ("node_name", "attributes", "what"),
    [("INPUT", ["type", "text"], "input"), ("DIV", [], "div")],
)
def test_upload_refuses_what_is_no_file_input(monkeypatch, tmp_path, node_name, attributes, what):
    (tmp_path / "a.png").write_bytes(b"a")
    page, socket = page_with(monkeypatch, FileInputDom(node_name, attributes))
    with pytest.raises(PilotError, match=rf"is <{what}>, not an input\[type=file\]"):
        page.upload("#f", [tmp_path / "a.png"])
    assert "DOM.setFileInputFiles" not in [m["method"] for m in socket.sent]


# ------------------------------------------------------------ keys


def test_keys_type_text_and_shift_a_letter():
    assert cdp.key_events("a{Shift}b") == [
        {"type": "keyDown", "modifiers": 0, "key": "a", "code": "KeyA",
         "windowsVirtualKeyCode": 65, "text": "a", "unmodifiedText": "a"},
        {"type": "keyUp", "modifiers": 0, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
        {"type": "rawKeyDown", "modifiers": 8, "key": "Shift", "code": "ShiftLeft",
         "windowsVirtualKeyCode": 16, "location": 1},
        {"type": "keyDown", "modifiers": 8, "key": "B", "code": "KeyB",
         "windowsVirtualKeyCode": 66, "text": "B", "unmodifiedText": "B"},
        {"type": "keyUp", "modifiers": 8, "key": "B", "code": "KeyB", "windowsVirtualKeyCode": 66},
        {"type": "keyUp", "modifiers": 0, "key": "Shift", "code": "ShiftLeft",
         "windowsVirtualKeyCode": 16, "location": 1},
    ]  # fmt: skip


def test_a_named_key_without_text_is_a_raw_key_down():
    down, up = cdp.key_events("{Left}")
    assert down == {"type": "rawKeyDown", "modifiers": 0, "key": "ArrowLeft",
                    "code": "ArrowLeft", "windowsVirtualKeyCode": 37}  # fmt: skip
    assert up["type"] == "keyUp" and up["key"] == "ArrowLeft"
    enter = cdp.key_events("{Enter}")[0]
    assert enter["type"] == "keyDown" and enter["text"] == "\r" and enter["code"] == "Enter"
    assert cdp.key_events("{F5}")[0]["windowsVirtualKeyCode"] == 116
    assert cdp.key_events("{PageDown}")[0]["key"] == "PageDown"


def test_ctrl_shortcuts_type_nothing_and_name_their_editing_command():
    events = cdp.key_events("{Ctrl}A")
    assert [e["type"] for e in events] == ["rawKeyDown", "rawKeyDown", "keyUp", "keyUp"]
    press = events[1]
    assert press["key"] == "a" and press["modifiers"] == 2 and "text" not in press
    assert press["commands"] == ["selectAll"]
    assert "commands" not in events[2]
    assert cdp.key_events("{Ctrl}v")[1]["commands"] == ["paste"]
    assert cdp.key_events("{Ctrl}{Shift}z")[2]["commands"] == ["redo"]
    assert "commands" not in cdp.key_events("{Ctrl}l")[1]


def test_a_modifier_holds_for_a_parenthesised_group_and_nests():
    events = cdp.key_events("{Ctrl}(ac)x")
    kinds = [(e["type"], e["key"], e["modifiers"]) for e in events]
    assert kinds == [
        ("rawKeyDown", "Control", 2), ("rawKeyDown", "a", 2), ("keyUp", "a", 2),
        ("rawKeyDown", "c", 2), ("keyUp", "c", 2), ("keyUp", "Control", 0),
        ("keyDown", "x", 0), ("keyUp", "x", 0),
    ]  # fmt: skip
    nested = cdp.key_events("{Ctrl}{Shift}p")
    assert [e["modifiers"] for e in nested] == [2, 10, 10, 10, 2, 0]


def test_repeats_aliases_and_literal_characters():
    assert len(cdp.key_events("{Backspace 3}")) == 6
    assert cdp.key_events("{Esc}")[0]["key"] == "Escape"
    assert cdp.key_events("{Win}r")[0]["key"] == "Meta"
    assert cdp.key_events("^")[0]["text"] == "^"
    assert cdp.key_events("{Shift}1")[1]["text"] == "!"
    assert cdp.key_events("é")[0] == {"type": "keyDown", "modifiers": 0, "key": "é", "code": "",
                                      "windowsVirtualKeyCode": 0, "text": "é",
                                      "unmodifiedText": "é"}  # fmt: skip


def test_an_unknown_key_name_raises():
    with pytest.raises(PilotError, match="not a key cdp knows"):
        cdp.key_events("{Bogus}")


def test_keys_focus_the_element_send_every_event_and_read_back(monkeypatch):
    dom = FakeDom("field", "ab")
    page, socket = page_with(monkeypatch, dom)
    found = page.keys("x{Enter}", into="#q")
    assert found == {"keys": "x{Enter}", "into": "#q", "events": 4, "value": "ab"}
    methods = [m["method"] for m in socket.sent]
    assert methods == ["Runtime.evaluate", *["Input.dispatchKeyEvent"] * 4, "Runtime.evaluate"]
    assert '"#q", false)' in socket.sent[0]["params"]["expression"]
    assert socket.sent[1]["params"] == cdp.key_events("x")[0]


def test_keys_without_into_go_to_the_focused_element(monkeypatch):
    page, socket = page_with(monkeypatch, lambda m: {"id": m["id"], "result": {}})
    assert page.keys("{Tab}")["value"] is None
    assert [m["method"] for m in socket.sent] == ["Input.dispatchKeyEvent"] * 2


def test_a_bad_key_string_sends_nothing(monkeypatch):
    page, socket = page_with(monkeypatch, FakeDom())
    with pytest.raises(PilotError):
        page.keys("{Nope}", into="#q")
    assert socket.sent == []


# ------------------------------------------------------------ scroll


class ScrollDom:
    """A scroll box at 0 that a wheel moves by its deltaY, within 0..max_top."""

    def __init__(self, max_top=500, found=True):
        self.top, self.max_top, self.found = 0, max_top, found

    def __call__(self, message):
        method, params = message["method"], message["params"]
        value = None
        if method == "Input.dispatchMouseEvent":
            self.top = min(max(self.top + params["deltaY"], 0), self.max_top)
        elif method == "Runtime.evaluate":
            source = params["expression"]
            if "scrollIntoView" in source or "innerWidth" in source:
                value = {"x": 10, "y": 20} if self.found else None
            elif self.found:
                value = {"scroller": "element", "top": self.top, "left": 0,
                         "max_top": self.max_top, "max_left": 0}  # fmt: skip
        return {"id": message["id"], "result": {"result": {"value": value}}}


@pytest.fixture
def quick_settle(monkeypatch):
    monkeypatch.setattr(cdp, "SETTLE", 0.001)


def test_scroll_wheels_at_the_element_and_reports_where_it_settled(monkeypatch, quick_settle):
    page, socket = page_with(monkeypatch, ScrollDom())
    found = page.scroll("#box", by=(0, 120))
    assert found == {"selector": "#box", "x": 10, "y": 20, "scroller": "element", "top": 120,
                     "left": 0, "max_top": 500, "max_left": 0}  # fmt: skip
    wheels = [m["params"] for m in socket.sent if m["method"] == "Input.dispatchMouseEvent"]
    assert wheels == [{"type": "mouseWheel", "x": 10, "y": 20, "deltaX": 0, "deltaY": 120}]
    assert '"#box"' in socket.sent[0]["params"]["expression"]


def test_scroll_without_a_selector_wheels_at_the_viewport_centre(monkeypatch, quick_settle):
    page, socket = page_with(monkeypatch, ScrollDom())
    page.scroll()
    assert socket.sent[0]["params"]["expression"] == cdp._VIEWPORT_CENTRE


def test_scroll_to_an_end_wheels_until_it_is_there(monkeypatch, quick_settle):
    page, socket = page_with(monkeypatch, ScrollDom(max_top=900))
    assert page.scroll("#box", to="bottom")["top"] == 900
    assert page.scroll("#box", to="top")["top"] == 0
    deltas = [m["params"]["deltaY"] for m in socket.sent if m["method"].startswith("Input.")]
    assert deltas == [1900, -1900]


def test_scroll_to_the_end_it_is_at_sends_no_wheel(monkeypatch, quick_settle):
    page, socket = page_with(monkeypatch, ScrollDom())
    assert page.scroll("#box", to="top")["top"] == 0
    assert "Input.dispatchMouseEvent" not in [m["method"] for m in socket.sent]


def test_scroll_of_nothing_and_bad_options(monkeypatch, quick_settle):
    page, _ = page_with(monkeypatch, ScrollDom(found=False))
    with pytest.raises(NoTarget, match="no visible element"):
        page.scroll("#none")
    with pytest.raises(PilotError, match="--to is top or bottom"):
        page.scroll(to="middle")
    assert cdp.parse_delta("-5,300") == (-5.0, 300.0)
    with pytest.raises(PilotError, match="--by is DX,DY"):
        cdp.parse_delta("300")


# ------------------------------------------------------------ drag, navigate


class EventSocket(ConsoleSocket):
    """Answers each call from `result` (by method); the events `then` names
    for a method, or for a mouse event's type, arrive before its reply."""

    def __init__(self, then: dict, result: dict):
        super().__init__([], [])
        self.then, self.result = then, result

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        method = message["method"]
        key = message["params"]["type"] if method == "Input.dispatchMouseEvent" else method
        self.inbox += [json.dumps(e) for e in self.then.get(key, [])]
        result = self.result.get(method, {})
        self.inbox.append(json.dumps({"id": message["id"], "result": result}))


def event_page(monkeypatch, then, result):
    socket = EventSocket(then, result)
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda url, **kw: socket)
    return cdp.Page(PAGE, timeout=0.5), socket


CENTRES = {"Runtime.evaluate": {"result": {"value": [{"x": 0, "y": 0}, {"x": 40, "y": 20}]}}}


def test_drag_presses_moves_in_steps_and_releases_on_the_target(monkeypatch):
    page, socket = event_page(monkeypatch, {}, CENTRES)
    assert page.drag("#a", "#b", steps=2) == {
        "from": {"x": 0, "y": 0},
        "to": {"x": 40, "y": 20},
        "mode": "mouse",
    }
    calls = [(m["method"], m["params"].get("type"), m["params"].get("x")) for m in socket.sent]
    assert calls == [
        ("Runtime.evaluate", None, None),
        ("Input.setInterceptDrags", None, None),
        ("Input.dispatchMouseEvent", "mouseMoved", 0),
        ("Input.dispatchMouseEvent", "mousePressed", 0),
        ("Input.dispatchMouseEvent", "mouseMoved", 20),
        ("Input.dispatchMouseEvent", "mouseMoved", 40),
        ("Input.dispatchMouseEvent", "mouseReleased", 40),
        ("Input.setInterceptDrags", None, None),
    ]
    intercepts = [m["params"] for m in socket.sent if m["method"] == "Input.setInterceptDrags"]
    assert intercepts == [{"enabled": True}, {"enabled": False}]
    assert socket.sent[4]["params"]["buttons"] == 1
    assert '"#a", "#b")' in socket.sent[0]["params"]["expression"]


def test_an_intercepted_html5_drag_is_dropped_with_its_data(monkeypatch):
    data = {"items": [{"mimeType": "text/plain", "data": "payload"}], "dragOperationsMask": 1}
    intercepted = [{"method": "Input.dragIntercepted", "params": {"data": data}}]
    page, socket = event_page(monkeypatch, {"mouseMoved": intercepted}, CENTRES)
    assert page.drag("#a", "#b", steps=1)["mode"] == "html5"
    drags = [m["params"] for m in socket.sent if m["method"] == "Input.dispatchDragEvent"]
    kinds = ("dragEnter", "dragOver", "drop")
    assert drags == [{"type": kind, "x": 40, "y": 20, "data": data} for kind in kinds]
    tail = [(m["method"], m["params"].get("type")) for m in socket.sent[-5:]]
    assert tail == [
        ("Input.dispatchDragEvent", "dragEnter"),
        ("Input.dispatchDragEvent", "dragOver"),
        ("Input.dispatchDragEvent", "drop"),
        ("Input.dispatchMouseEvent", "mouseReleased"),
        ("Input.setInterceptDrags", None),
    ]


def test_drag_of_nothing_is_no_target_and_sends_no_input(monkeypatch):
    page, socket = event_page(monkeypatch, {}, {"Runtime.evaluate": {"result": {"value": "#a"}}})
    with pytest.raises(NoTarget, match="'#a'"):
        page.drag("#a", "#b")
    assert [m["method"] for m in socket.sent] == ["Runtime.evaluate"]


LANDED = {
    "Page.navigate": {"frameId": "f", "loaderId": "l"},
    "Runtime.evaluate": {"result": {"value": {"url": "https://x/y", "title": "Y"}}},
}
LOADED = [{"method": "Page.loadEventFired", "params": {}}]


def test_navigate_waits_for_the_load_event_and_reads_where_it_is(monkeypatch):
    page, socket = event_page(monkeypatch, {"Page.navigate": LOADED}, LANDED)
    assert page.navigate("https://x/") == {"url": "https://x/y", "title": "Y"}
    assert [m["method"] for m in socket.sent] == [
        "Page.enable",
        "Page.navigate",
        "Runtime.evaluate",
    ]
    assert socket.sent[1]["params"] == {"url": "https://x/"}


def test_navigate_without_its_event_times_out(monkeypatch):
    page, _ = event_page(monkeypatch, {"Page.navigate": LOADED}, LANDED)
    with pytest.raises(Timeout, match="did not finish loading"):
        page.navigate("https://x/", wait="domcontentloaded")
    # A load event before the navigation is the old document's.
    page, _ = event_page(monkeypatch, {"Page.enable": LOADED}, LANDED)
    with pytest.raises(Timeout, match="did not finish loading"):
        page.navigate("https://x/")


def test_navigate_with_wait_none_or_to_a_fragment_does_not_wait(monkeypatch):
    page, socket = event_page(monkeypatch, {}, LANDED)
    assert page.navigate("https://x/", wait="none")["url"] == "https://x/y"
    assert [m["method"] for m in socket.sent] == ["Page.navigate", "Runtime.evaluate"]
    same_document = {**LANDED, "Page.navigate": {"frameId": "f"}}
    page, _ = event_page(monkeypatch, {}, same_document)
    assert page.navigate("https://x/#part")["title"] == "Y"


def test_a_failed_navigation_raises_with_chromes_reason(monkeypatch):
    failed = {"Page.navigate": {"frameId": "f", "errorText": "net::ERR_NAME_NOT_RESOLVED"}}
    page, _ = event_page(monkeypatch, {}, failed)
    with pytest.raises(PilotError, match="ERR_NAME_NOT_RESOLVED"):
        page.navigate("http://nowhere.invalid/")
    with pytest.raises(PilotError, match="--wait is load"):
        page.navigate("https://x/", wait="idle")
