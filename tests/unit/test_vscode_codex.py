"""The Codex driver's flows over fakes: no VS Code, no clipboard, no keys.

A FakePanel hands out one node list per walk; the clipboard, SendKeys and
clicks are replaced, so each test sees exactly what the driver did.
"""

from __future__ import annotations

import pytest

from desktoppilot.core.errors import Ambiguous, PilotError, Timeout
from desktoppilot.plugins.vscode import codex, git
from desktoppilot.plugins.vscode import panel as P
from desktoppilot.plugins.vscode.panel import Node


def T(name: str, depth: int = 5) -> Node:
    return Node("Text", name, depth=depth)


def B(name: str, depth: int = 5, id: str = "") -> Node:
    return Node("Button", name, id=id, depth=depth, control=FakeControl(name))


class FakeControl:
    def __init__(self, name: str = ""):
        self.Name = name

    def SetFocus(self):
        pass


def composer(placeholder: str = "Ask for follow-up changes") -> list[Node]:
    edit = Node("Edit", placeholder, cls="ProseMirror", depth=12, control=FakeControl(placeholder))
    return [edit, B("GPT-6 Sol High", 13, "radix-_r_90_"), B(P.SEND[0], 12)]


def turn(prompt: str, reply: list[str] | None) -> list[Node]:
    nodes = [T(P.USER_HEADING[0], 10), T(prompt, 12), B(P.USER_COPY[0], 11)]
    if reply is not None:
        nodes += [T(P.REPLY_HEADING[0], 10), *[T(t, 12) for t in reply], B(P.COPY[0], 11)]
    return nodes


class FakePanel:
    """One node list per walk; the last one repeats."""

    def __init__(self, *frames, focus: bool = True):
        self.frames = list(frames)
        self.walks = 0
        self.focus = focus
        self.window = FakeControl("proj - Visual Studio Code")
        self.root = FakeControl("ChatGPT")
        self.sidebar = False

    def nodes(self):
        self.walks += 1
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def title(self):
        return "ChatGPT"

    def owns_focus(self):
        return self.focus


class FakeClipboard:
    def __init__(self, text: str = "", formats: int | None = None):
        self.text = text
        self.seq = 0
        self.formats = (1 if text else 0) if formats is None else formats

    def read(self):
        return self.text

    def write(self, text):
        self.text, self.seq = text, self.seq + 1
        return len(text)

    def clear(self):
        self.text, self.seq = "", self.seq + 1
        return True


@pytest.fixture
def clipboard(monkeypatch):
    def make(text: str = "", formats: int | None = None) -> FakeClipboard:
        fake = FakeClipboard(text, formats)
        monkeypatch.setattr(codex.clip, "read", fake.read)
        monkeypatch.setattr(codex.clip, "write", fake.write)
        monkeypatch.setattr(codex.clip, "clear", fake.clear)
        monkeypatch.setattr(codex.win32clipboard, "GetClipboardSequenceNumber", lambda: fake.seq)
        monkeypatch.setattr(codex.win32clipboard, "CountClipboardFormats", lambda: fake.formats)
        return fake

    return make


@pytest.fixture
def keys(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(codex.auto, "SendKeys", lambda k, **_: sent.append(k))
    return sent


def use(monkeypatch, pan):
    monkeypatch.setattr(codex, "find_panel", lambda ctx, window=None: pan)


# ------------------------------------------------------------------ read


REPLY = ["The answer is 42, see the docs.", "Second paragraph."]


def test_read_returns_the_copied_reply_and_puts_the_clipboard_back(monkeypatch, clipboard):
    clip = clipboard("the user's text")
    use(monkeypatch, FakePanel(turn("q", REPLY) + composer()))
    copied = "The answer is **42**, see [the docs](https://x.test).\n\nSecond paragraph."
    monkeypatch.setattr(codex.ui, "click", lambda control, **_: clip.write(copied))
    result = codex.read(None)
    assert result["text"] == copied
    assert result["verified"] is True
    assert result["clipboard_restored"] == "text"
    assert clip.text == "the user's text"


def test_read_refuses_text_another_copy_put_on_the_clipboard(monkeypatch, clipboard):
    clip = clipboard("the user's text")
    use(monkeypatch, FakePanel(turn("q", REPLY) + composer()))
    monkeypatch.setattr(codex.ui, "click", lambda control, **_: clip.write("a password"))
    with pytest.raises(PilotError, match="not the last reply"):
        codex.read(None)
    assert clip.text == "the user's text"


def test_read_says_when_the_clipboard_held_something_other_than_text(monkeypatch, clipboard):
    clip = clipboard("", formats=2)  # an image, say
    use(monkeypatch, FakePanel(turn("q", REPLY) + composer()))
    monkeypatch.setattr(codex.ui, "click", lambda control, **_: clip.write(REPLY[0]))
    assert codex.read(None)["clipboard_restored"] is False


def test_is_reply_wants_the_visible_words_in_order():
    assert codex.is_reply("# Title\n\nUse `pilot` now", ["Title"])
    assert not codex.is_reply("now Use pilot", ["Use pilot now"])
    assert codex.is_reply("anything", [])
    assert codex.is_reply("これは**強調**です。_italic_ text", ["これは強調です。italic text"])


# ------------------------------------------------------------------ send


def typing_panel(monkeypatch, clip, draft: str = ""):
    """A panel whose composer value follows the keys: Ctrl+A Delete clears
    it, Ctrl+V pastes the clipboard; after Send, Stop shows."""
    box = {"value": draft}
    nodes = turn("old", ["done"]) + composer()
    pan = FakePanel(nodes)
    use(monkeypatch, pan)
    monkeypatch.setattr(codex, "_composer_text", lambda node: box["value"])

    def key(k, **_):
        if k == "{Ctrl}a{Delete}":
            box["value"] = ""
        elif k == "{Ctrl}v":
            box["value"] += clip.text

    monkeypatch.setattr(codex.auto, "SendKeys", key)
    monkeypatch.setattr(
        codex.ui, "click", lambda control, **_: pan.frames.append(nodes + [B(P.STOP[0])])
    )
    return pan, box


def test_send_pastes_sends_and_restores_the_clipboard(monkeypatch, clipboard):
    clip = clipboard("the user's text")
    typing_panel(monkeypatch, clip)
    result = codex.send(None, "Review this branch.")
    assert result == {
        "sent": True,
        "chars": 19,
        "generating": True,
        "clipboard_restored": "text",
    }
    assert clip.text == "the user's text"


def test_send_refuses_to_discard_a_draft(monkeypatch, clipboard, keys):
    clip = clipboard("the user's text")
    use(monkeypatch, FakePanel(turn("old", ["done"]) + composer()))
    monkeypatch.setattr(codex, "_composer_text", lambda node: "half a thought")
    with pytest.raises(PilotError, match="unsent draft.*--replace-draft"):
        codex.send(None, "Review this branch.")
    assert keys == []
    assert clip.seq == 0


def test_send_replaces_a_draft_when_asked(monkeypatch, clipboard):
    clip = clipboard("")
    _, box = typing_panel(monkeypatch, clip, draft="half a thought")
    codex.send(None, "Review this branch.", replace_draft=True)
    assert box["value"] == "Review this branch."


def test_the_placeholder_is_not_a_draft(monkeypatch):
    edit = composer("Do anything")[0]
    monkeypatch.setattr(codex, "_composer_text", lambda node: "Do anything")
    assert codex._draft(edit) == ""


def test_send_puts_the_clipboard_back_when_the_paste_fails(monkeypatch, clipboard):
    clip = clipboard("the user's text")
    use(monkeypatch, FakePanel(turn("old", ["done"]) + composer()))
    monkeypatch.setattr(codex, "_composer_text", lambda node: "")

    def key(k, **_):
        if k == "{Ctrl}v":
            raise RuntimeError("SendInput failed")

    monkeypatch.setattr(codex.auto, "SendKeys", key)
    with pytest.raises(RuntimeError):
        codex.send(None, "Review this branch.")
    assert clip.text == "the user's text"


# ------------------------------------------------------------------ wait


DONE = turn("q", ["Done."]) + composer()
WORKING = turn("q", None) + [B(P.STOP[0])]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(codex.time, "sleep", lambda s: None)


def test_wait_wants_done_twice_in_a_row(monkeypatch, no_sleep):
    pan = FakePanel(DONE, WORKING, DONE, DONE)
    use(monkeypatch, pan)
    assert codex.wait(None, timeout=60) == {"state": "done"}
    assert pan.walks == 4


def test_wait_settle_polls_sets_how_many(monkeypatch, no_sleep):
    pan = FakePanel(DONE)
    use(monkeypatch, pan)
    codex.wait(None, timeout=60, settle_polls=3)
    assert pan.walks == 3


def test_wait_raises_on_a_failed_turn(monkeypatch, no_sleep):
    failed = turn("q", None) + [T("You've hit your usage limit.")] + composer()
    use(monkeypatch, FakePanel(WORKING, failed))
    with pytest.raises(codex.CodexFailed, match="usage limit"):
        codex.wait(None, timeout=60)


def test_wait_times_out(monkeypatch, no_sleep):
    use(monkeypatch, FakePanel(WORKING))
    with pytest.raises(Timeout, match="still working"):
        codex.wait(None, timeout=0)


# ------------------------------------------------------------------ model


def test_set_effort_stops_at_an_end_stop(monkeypatch, keys):
    pan = FakePanel([Node("MenuItem", P.POWER[0], control=FakeControl())])
    monkeypatch.setattr(
        codex, "_closed_label", lambda p, c: {"effort": "high", "label": "GPT-6 Sol High"}
    )
    monkeypatch.setattr(codex, "_open_menu", lambda p: p.nodes())
    monkeypatch.setattr(codex, "_focus", lambda p, n: None)

    def wait_for(p, test, what, timeout=0):
        raise Timeout(what)

    monkeypatch.setattr(codex, "_wait_for", wait_for)
    with pytest.raises(PilotError, match="'ultra' is not offered: the slider stopped at 'high'"):
        codex._set_effort(pan, None, "ultra")
    assert keys == ["{Right}"]


def test_an_unknown_effort_label_is_a_model_mismatch(monkeypatch):
    label = P.parse_label("GPT-6 Sol Standard")
    monkeypatch.setattr(codex, "_closed_label", lambda p, c: label)
    with pytest.raises(codex.ModelMismatch) as exc:
        codex._set_effort(FakePanel([]), None, "high")
    assert exc.value.details["got"] == {"effort": None, "label": "GPT-6 Sol Standard"}
    assert exc.value.details["mismatches"] == [{"field": "effort", "wanted": "high", "got": None}]


RADIOS = [
    Node("RadioButton", "Default Recommended set of models", selected=False),
    Node("RadioButton", "GPT-6 Sol", selected=True),
    Node("RadioButton", "GPT-5.6 Sol", selected=False),
]


def reading(monkeypatch, label: str, fast_toggle: int = 0):
    monkeypatch.setattr(codex, "_closed_label", lambda p, c: P.parse_label(label))
    box = Node("CheckBox", (P.FAST if fast_toggle == 0 else P.STANDARD)[0], toggle=fast_toggle)
    monkeypatch.setattr(codex, "_open_menu", lambda p: [box])
    monkeypatch.setattr(codex, "_model_list", lambda p: RADIOS)
    monkeypatch.setattr(codex, "_close_menu", lambda p, c: [])


def test_read_model_names_the_label_model_as_the_radio_it_stands_for(monkeypatch):
    reading(monkeypatch, "Default High")
    got = codex._read_model(FakePanel([]), None)
    assert got["label_model"] == "Default Recommended set of models"
    assert (got["model"], got["effort"], got["fast"]) == ("GPT-6 Sol", "high", False)


def model_set_up(monkeypatch, label: str):
    reading(monkeypatch, label)
    finds = []
    pan = FakePanel(composer())
    monkeypatch.setattr(codex, "find_panel", lambda ctx, window=None: finds.append(1) or pan)
    monkeypatch.setattr(codex, "_set_fast", lambda p, f: None)
    monkeypatch.setattr(codex, "_set_effort", lambda p, c, t: None)
    return finds


def test_set_model_verifies_on_the_same_panel(monkeypatch):
    finds = model_set_up(monkeypatch, "GPT-6 Sol High")
    result = codex.set_model(None, name="GPT-6 Sol", effort="high", fast=False)
    assert result["verified"] is True
    assert finds == [1]


def test_set_model_raises_model_mismatch_with_the_read_back(monkeypatch):
    model_set_up(monkeypatch, "GPT-6 Sol Medium")
    with pytest.raises(codex.ModelMismatch, match="effort is 'medium', wanted 'high'") as exc:
        codex.set_model(None, name="GPT-6 Sol", effort="high", fast=False)
    assert exc.value.details["got"]["label"] == "GPT-6 Sol Medium"
    assert [m["field"] for m in exc.value.details["mismatches"]] == ["effort"]


# ------------------------------------------------------------------ open


def test_open_reuses_a_panel_on_a_new_chat(monkeypatch):
    pan = FakePanel(composer("Do anything"))
    monkeypatch.setattr(codex, "_locate", lambda ctx, window: (pan.window, pan))
    result = codex.open_panel(None)
    assert (result["opened"], result["reused"], result["new_chat"]) == (False, True, True)


def test_open_refuses_while_codex_works(monkeypatch):
    pan = FakePanel(WORKING)
    monkeypatch.setattr(codex, "_locate", lambda ctx, window: (pan.window, pan))
    monkeypatch.setattr(codex, "_codex_tabs", lambda w: pytest.fail("must not close tabs"))
    with pytest.raises(codex.CodexBusy):
        codex.open_panel(None)


def test_open_needs_window_when_no_window_has_a_panel(monkeypatch):
    monkeypatch.setattr(codex, "_locate", lambda ctx, window: (None, None))
    with pytest.raises(Ambiguous, match="--window"):
        codex.open_panel(None)


def test_until_composer_pages_through_the_tour(monkeypatch):
    tour = [T("Codex in your IDE"), B(P.BACK[0]), B(P.NEXT[0])]
    pan = FakePanel(tour, tour, composer())
    clicked = []
    monkeypatch.setattr(codex, "_click", lambda p, node: clicked.append(node.name))
    nodes, pages = codex._until_composer(pan, 5)
    assert pages == 2 and clicked == ["Next", "Next"]
    assert P.composer(nodes) is not None


# ------------------------------------------------------------ which panel


def panel(sidebar: bool = False) -> codex.Panel:
    return codex.Panel(window=FakeControl("w"), root=FakeControl("ChatGPT"), sidebar=sidebar)


def test_pick_prefers_the_editor_and_refuses_two_editors():
    editor, side = panel(), panel(sidebar=True)
    assert codex._pick([side, editor]) is editor
    assert codex._pick([side]) is side
    assert codex._pick([]) is None
    with pytest.raises(Ambiguous, match="2 Codex panels"):
        codex._pick([panel(), panel()])


def windows_with(monkeypatch, panels: dict):
    """Windows by title, each with the panels it holds; --window filters titles."""

    def main_windows(ctx, window=None):
        names = [n for n in panels if window is None or window.casefold() in n.casefold()]
        return [FakeControl(n) for n in names]

    monkeypatch.setattr(codex, "main_windows", main_windows)
    monkeypatch.setattr(codex, "panels_in", lambda w: panels[w.Name])


def test_two_windows_with_a_panel_need_window(monkeypatch):
    a, b = panel(), panel()
    windows_with(monkeypatch, {"MyRepo": [a], "OtherRepo": [b], "notes": []})
    with pytest.raises(Ambiguous, match="2 VS Code windows have a Codex panel"):
        codex.find_panel(None)
    assert codex.find_panel(None, "other") is b


def test_the_one_window_with_a_panel_is_used(monkeypatch):
    a = panel()
    windows_with(monkeypatch, {"MyRepo": [a], "notes": []})
    assert codex.find_panel(None) is a
    window, pan = codex._locate(None, None)
    assert (window.Name, pan) == ("MyRepo", a)


def test_several_windows_and_no_panel_name_no_window(monkeypatch):
    windows_with(monkeypatch, {"a": [], "b": []})
    assert codex._locate(None, None) == (None, None)
    window, pan = codex._locate(None, "b")
    assert (window.Name, pan) == ("b", None)


# -------------------------------------------------------------------- git


@pytest.fixture
def checkout(monkeypatch):
    """git.checkout with the palette replaced; returns the rows the picker shows."""
    monkeypatch.setattr(git, "pick_window", lambda ctx, window: FakeControl("proj"))
    monkeypatch.setattr(git.palette, "run", lambda *a, **k: {"ran": "x"})
    monkeypatch.setattr(git.palette, "wait_row", lambda *a, **k: "Select a branch")
    monkeypatch.setattr(git.palette, "type_filter", lambda text: None)
    state = {"rows": [], "notices": [[]], "status": []}

    def pick(match, what, *, timeout, seen):
        for row in state["rows"]:
            seen.append(row)
            if match(row):
                return row
        raise git.palette.NoRow("no row")

    monkeypatch.setattr(git.palette, "pick", pick)
    monkeypatch.setattr(
        git,
        "notices",
        lambda ctx, w: (
            state["notices"].pop(0) if len(state["notices"]) > 1 else state["notices"][0]
        ),
    )
    monkeypatch.setattr(git, "status_items", lambda w: state["status"])
    return state


def test_checkout_fails_with_the_notice_vs_code_raised(checkout):
    checkout["rows"] = ["git-branch feat, now, branches"]
    checkout["notices"] = [[], ["Your local changes would be overwritten by checkout."]]
    with pytest.raises(PilotError, match="it says: Your local changes"):
        git.checkout(None, "feat")


def test_checkout_of_a_remote_only_branch_hints_at_remote(checkout):
    checkout["rows"] = ["plus Create new branch...", "cloud origin/feat, now, remote branches"]
    with pytest.raises(PilotError, match="exists only as a remote branch; pass --remote"):
        git.checkout(None, "feat")


def test_checkout_is_verified_by_the_status_bar(checkout):
    checkout["rows"] = ["git-branch feat, now, branches"]
    checkout["status"] = ["proj (Git) - feat*, Checkout Branch/Tag..."]
    result = git.checkout(None, "feat")
    assert (result["checked_out"], result["window"]) == ("feat", "proj")


def test_wait_for_ends_when_the_test_passes_on_no_nodes():
    class Pan:
        calls = 0

        def nodes(self):
            Pan.calls += 1
            return []

    assert codex._wait_for(Pan(), lambda nodes: not nodes, "no nodes", timeout=5.0) == []
    assert Pan.calls == 1
