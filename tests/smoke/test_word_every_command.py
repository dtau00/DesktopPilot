"""Every `pilot word` leaf command against a real Word, each called at least
twice against the same state, through the real CLI (a subprocess: exit code
and JSON on stdout are the contract under test).

    pytest tests/smoke/test_word_every_command.py -m word -v

Needs no --app: Word is launched and killed through the CLI itself. The tests
run in file order and share one Word. A scratch .docx (heading, text, a
comment, a tracked insertion) is built in PILOT_SCRATCH (default: a pytest
temp dir). Nothing here needs an add-in: the pane, tree and window tests use
Word's own Navigation pane and Word Options.
PILOT_EVERY_COMMAND_LOG, if set, gets one JSON line per call.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
import typer
import win32clipboard

from desktoppilot import cli
from desktoppilot.core import recording, winevents
from desktoppilot.core.errors import PilotError
from desktoppilot.plugins.word import session

pytestmark = pytest.mark.word

PILOT = Path(sys.executable).with_name("pilot.exe")
CALL_TIMEOUT = 300
OPTIONS = "win:Word Options"
# Customize Ribbon's tree of tabs, groups and commands: Word's own, no add-in.
RIBBON_TREE = OPTIONS + "//Tree[name='Ribbon']/Group[name='Main Tabs']"
LOWER_RIBBON_GROUP = "ribbon//Pane[class='NetUIPanViewer']//Group[name='{}']"

PARAGRAPHS = [
    ("Heading 1", "Pilot Scratch Heading"),
    ("Normal", "Alpha bravo charlie delta echo."),
    ("Normal", "The commented clause ends here."),
    ("Normal", "Tracked: inserted words."),
]

CALLS: collections.Counter = collections.Counter()


# ------------------------------------------------------------------ the CLI


class Result:
    def __init__(self, args: list[str], code: int, data, stdout: str, stderr: str, seconds: float):
        self.args, self.code, self.data = args, code, data
        self.stdout, self.stderr, self.seconds = stdout, stderr, seconds

    def __repr__(self) -> str:
        return f"pilot word {' '.join(self.args)} -> exit {self.code} in {self.seconds:.1f}s: {self.stdout[:1500]}{self.stderr[-800:]}"


def leaf_of(args: list[str]) -> str:
    words = [a for a in args if not a.startswith("-")]
    return " ".join(words[:2]) if words and words[0] in GROUPS else words[0]


def call(*args: str, expect_ok: bool | None = True) -> Result:
    argv = list(args)
    began = time.monotonic()
    proc = subprocess.run(
        [str(PILOT), "word", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CALL_TIMEOUT,
        env=os.environ | {"PYTHONIOENCODING": "utf-8"},
    )
    seconds = time.monotonic() - began
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = None
    result = Result(argv, proc.returncode, data, proc.stdout, proc.stderr, seconds)
    CALLS[leaf_of(argv)] += 1
    _log(result)
    assert data is not None, f"stdout is not JSON: {result!r}"
    if expect_ok is None:
        return result
    if expect_ok:
        assert proc.returncode == 0 and "error" not in data, repr(result)
    else:
        assert proc.returncode == 1 and "error" in data and "message" in data, repr(result)
    return result


def ok(*args: str) -> dict:
    return call(*args).data


def _log(result: Result) -> None:
    path = os.environ.get("PILOT_EVERY_COMMAND_LOG")
    if not path:
        return
    line = {
        "test": os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0],
        "leaf": leaf_of(result.args),
        "args": result.args,
        "exit": result.code,
        "seconds": round(result.seconds, 2),
        "error": (result.data or {}).get("error") if isinstance(result.data, dict) else None,
        "message": (result.data or {}).get("message") if isinstance(result.data, dict) else None,
        "screenshot": (result.data or {}).get("screenshot")
        if isinstance(result.data, dict)
        else None,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def word_leaves() -> list[str]:
    group = typer.main.get_command(cli.app).commands["word"]
    leaves = []
    for name, command in group.commands.items():
        subs = getattr(command, "commands", None)
        leaves += [f"{name} {sub}" for sub in subs] if subs else [name]
    return sorted(leaves)


GROUPS = {
    name
    for name, command in typer.main.get_command(cli.app).commands["word"].commands.items()
    if getattr(command, "commands", None)
}


# ----------------------------------------------------------------- fixtures


def winword_pids() -> list[str]:
    out = subprocess.run(
        ["tasklist", "/fi", "imagename eq WINWORD.EXE", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
    ).stdout
    return [line.split(",")[1].strip('"') for line in out.splitlines() if "WINWORD" in line]


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    root = os.environ.get("PILOT_SCRATCH")
    path = Path(root) / "every-command" if root else tmp_path_factory.mktemp("every-command")
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


@pytest.fixture(scope="module")
def scratch_doc(scratch) -> Path:
    return build_docx(scratch / "scratch.docx")


@pytest.fixture(scope="module")
def word(scratch):
    """Launch #1 (the lifecycle test does #2). Killed at the end whatever happens."""
    assert not winword_pids(), "WINWORD is already running; close it first"
    launched = ok("app", "launch")
    yield launched
    if winword_pids():
        call("app", "kill", "--force")
    assert not winword_pids(), "WINWORD left running"


@pytest.fixture
def opened(word, scratch_doc):
    """The scratch document, open for one test and discarded after it."""
    ok("doc", "open", str(scratch_doc))
    yield scratch_doc
    close_all_docs()


def close_all_docs() -> None:
    for _ in range(10):
        if ok("doc", "close")["closed"] == "no-document":
            return
    raise AssertionError("documents would not close")


@pytest.fixture
def clipboard():
    """Whatever text the user had on the clipboard comes back afterwards."""
    saved = clip_text()
    yield
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        if saved is not None:
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, saved)
    finally:
        win32clipboard.CloseClipboard()
    assert clip_text() == saved


def clip_text() -> str | None:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


# -------------------------------------------------------------- the .docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
PKG = "http://schemas.openxmlformats.org/package/2006"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WML = "application/vnd.openxmlformats-officedocument.wordprocessingml"


def build_docx(path: Path) -> Path:
    parts = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{PKG}/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="{WML}.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="{WML}.styles+xml"/>
<Override PartName="/word/comments.xml" ContentType="{WML}.comments+xml"/>
</Types>""",
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG}/relationships">
<Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/_rels/document.xml.rels": f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG}/relationships">
<Relationship Id="rId1" Type="{OFFICE}/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="{OFFICE}/comments" Target="comments.xml"/>
</Relationships>""",
        "word/styles.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles {W}>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
</w:styles>""",
        "word/comments.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:comments {W}>
<w:comment w:id="0" w:author="Pilot Tester" w:initials="PT" w:date="2026-09-25T00:00:00Z">
<w:p><w:r><w:t>Check this clause</w:t></w:r></w:p></w:comment>
</w:comments>""",
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {W}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Pilot Scratch Heading</w:t></w:r></w:p>
<w:p><w:r><w:t>Alpha bravo charlie delta echo.</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">The </w:t></w:r><w:commentRangeStart w:id="0"/>
<w:r><w:t>commented clause</w:t></w:r><w:commentRangeEnd w:id="0"/>
<w:r><w:commentReference w:id="0"/></w:r><w:r><w:t xml:space="preserve"> ends here.</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Tracked: </w:t></w:r>
<w:ins w:id="1" w:author="Pilot Reviser" w:date="2026-09-25T00:00:00Z"><w:r><w:t>inserted words</w:t></w:r></w:ins>
<w:r><w:t>.</w:t></w:r></w:p>
<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
</w:body></w:document>""",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


def texts(read: dict) -> list[str]:
    return [p["text"] for p in read["paragraphs"]]


def assert_pane_gone(title: str) -> None:
    """On failure, say what the root still resolves to."""
    gone = call("ui", "wait", f"pane:{title}", "--gone", "--timeout", "30", expect_ok=None)
    if gone.code == 0:
        return
    still = call("pane", "read", "--name", title, "--limit", "15", expect_ok=None)
    windows = call("addin", "windows")
    raise AssertionError(f"{gone!r}\nstill resolves to: {still!r}\nwindows: {windows.data}")


def selected_tab_is(tab: str) -> dict:
    return ok("ui", "wait", LOWER_RIBBON_GROUP.format(tab), "--timeout", "15")


# -------------------------------------------------------------------- tests


def test_app_launch_status_attach_resize(word):
    assert word["running"] and word["pids"] and word["hwnd"]
    assert word["ready"] == ["addins_loaded"]
    startup_down = [
        a for a in word["addins_not_connected"] if session.expected_at_startup(a["load_behavior"])
    ]
    assert not startup_down, "an add-in Word loads at startup did not connect"
    assert word["layout"]["dips"] == [1600, 1000] or word["layout"]["clamped"]

    for _ in range(2):
        status = ok("app", "status")
        assert status["running"] and status["pids"] == word["pids"]
        assert status["working_set_mb"] and status["window"]["hwnd"] == word["hwnd"]

    for _ in range(2):
        attached = ok("app", "attach")
        assert attached["pids"] == word["pids"] and attached["hwnd"] == word["hwnd"]

    resized = ok("app", "resize", "1400", "900")
    assert resized["hwnd"] == word["hwnd"] and resized["requested_dips"] == [1400, 900]
    assert ok("app", "status")["window"]["dips"] == resized["dips"]
    back = ok("app", "resize")
    assert back["requested_dips"] == [1600, 1000]
    assert ok("app", "status")["window"]["matches_profile"] is True


def test_state_dialogs_and_windows(word):
    first = ok("state")
    second = ok("state", "--controls", "5")
    for snap in (first, second):
        assert snap["running"] and snap["pids"] == word["pids"]
        assert snap["dialogs"] == [] and "foreground" in snap and "fallback_used" in snap
    assert 0 < len(second["controls"]) <= 5

    for _ in range(2):
        assert ok("dialogs") == {"dialogs": []}
        dismissed = ok("dialogs", "--dismiss")
        assert dismissed["dismissed"] == [] and dismissed["remaining"] == []

    for _ in range(2):
        windows = ok("ui", "windows")["windows"]
        assert [w for w in windows if w["is_main"] and w["class"] == "OpusApp"]
        top = ok("addin", "windows")["windows"]
        assert any(w["hwnd"] == word["hwnd"] for w in top)


def test_doc_open_read_close_twice(word, scratch_doc):
    for _ in range(2):
        opened = ok("doc", "open", str(scratch_doc))
        assert opened["opened"] and opened["name"] == scratch_doc.name
        assert (opened["paragraphs"], opened["comments"], opened["revisions"]) == (4, 1, 1)

        read = ok("doc", "read")
        assert [(p["style"], p["text"]) for p in read["paragraphs"]] == PARAGRAPHS
        assert texts(ok("doc", "read", "--start", "2", "--limit", "1")) == [PARAGRAPHS[1][1]]

        hits = ok("doc", "find", "bravo")["hits"]
        assert len(hits) == 1 and hits[0]["text"] == "bravo" and hits[0]["page"] == 1
        assert ok("doc", "find", "ALPHA", "--match-case")["hits"] == []

        chosen = ok("doc", "select", str(hits[0]["start"]), str(hits[0]["end"]))
        assert chosen["text"] == "bravo"

        comments = ok("doc", "comments")
        assert comments["count"] == 1
        assert comments["comments"][0]["author"] == "Pilot Tester"
        assert comments["comments"][0]["text"] == "Check this clause"
        assert comments["comments"][0]["scope_text"] == "commented clause"

        revisions = ok("doc", "revisions")
        assert revisions["count"] == 1 and revisions["by_author"]["Pilot Reviser"]["count"] == 1
        assert revisions["revisions"][0]["type"] == "Insert"
        assert revisions["revisions"][0]["text"] == "inserted words"

        assert ok("doc", "close")["closed"] == scratch_doc.name

    assert call("doc", "read", expect_ok=False).data["error"] == "NoActiveDocument"
    assert ok("doc", "close")["closed"] == "no-document"


def test_doc_new_insert_save_as_save_twice(word, scratch):
    names = set()
    for i in (1, 2):
        created = ok("doc", "new")
        assert created["created"] and created["path"] is None
        names.add(created["name"])
        assert call("doc", "save", expect_ok=False).data["error"] == "PilotError"

        ok("doc", "insert", f"Body {i}.")
        inserted = ok("doc", "insert", "Start. ", "--at", "start")
        assert inserted["inserted"] == 7 and inserted["at"] == "start"
        assert texts(ok("doc", "read")) == [f"Start. Body {i}."]

        target = scratch / f"saved-{i}.docx"
        saved_to = Path(ok("doc", "save-as", str(target))["saved_to"])
        assert saved_to == target.resolve() and target.exists()

        ok("doc", "insert", " Saved in place.")
        saved = ok("doc", "save")
        assert Path(saved["path"]) == target.resolve() and saved["saved"] == target.name
        assert ok("doc", "close")["closed"] == target.name

        ok("doc", "open", str(target))
        assert texts(ok("doc", "read")) == [f"Start. Body {i}. Saved in place."]
        ok("doc", "close")
    assert len(names) == 2, "the second `doc new` reused the first document's name"


def test_ui_reads_scrolls_and_trees(opened):
    for _ in range(2):
        text = ok("ui", "read", "app//Document")["text"]
        assert "Alpha bravo charlie delta echo." in text and "commented clause" in text

        element = ok("ui", "find", "app//Document")["element"]
        assert element["type"] == "Document" and "Scroll" in element["patterns"]
        tabs = ok("ui", "find", "ribbon//TabItem", "--all")
        assert tabs["count"] >= 10 and "Review" in {e["name"] for e in tabs["elements"]}

    for percent in ("100", "0", "100", "0"):
        scrolled = ok("ui", "scroll", "app//Document", "--vertical", percent)
        assert scrolled["vertical_percent"] == pytest.approx(float(percent), abs=1)

    for _ in range(2):
        tree = ok("ui", "tree", "--root", "ribbon", "--levels", "2")
        assert tree["tree"] and tree["complete"] is True
        flat = ok("ui", "tree", "--flat", "--levels", "4", "--limit", "200")
        assert flat["controls"] and flat["controls"][0]["class"] == "OpusApp"


def test_ui_keys_type_and_insert_at_caret(opened):
    # After a real word, and digits: AutoCorrect rewrites a word when a space follows it.
    caret = ok("doc", "find", "bravo")["hits"][0]["end"]
    ok("doc", "select", str(caret), str(caret))
    for n in (1, 2):
        assert ok("ui", "type", " 4242")["typed"] == " 4242"
        assert len(ok("doc", "find", "4242")["hits"]) == n

    ok("ui", "keys", "{Ctrl}{Home}")
    ok("doc", "insert", "K1 ", "--at", "selection")
    ok("ui", "keys", "{Ctrl}{End}")
    ok("doc", "insert", " K2", "--at", "selection")
    paragraphs = texts(ok("doc", "read"))
    assert paragraphs[0].startswith("K1 ") and paragraphs[-1].endswith(" K2")
    assert paragraphs[1] == "Alpha bravo 4242 4242 charlie delta echo."


def test_ui_select_switches_ribbon_tabs(opened):
    for _ in range(2):
        for tab in ("Review", "Home"):
            assert ok("ui", "select", f"ribbon//TabItem[name='{tab}']")["selected"] == tab
            selected_tab_is(tab)


def test_navigation_pane_set_value_click_and_wait(opened):
    for query in ("bravo", "charlie"):
        ok("ui", "keys", "{Ctrl}f")
        ok("ui", "wait", "pane:Navigation", "--timeout", "20")
        search = "app//Edit[name='Search document']"
        assert ok("ui", "set-value", search, query)["value"] == query
        assert ok("ui", "read", search)["text"] == query
        clicked = ok("ui", "click", "app//Button[name='Close pane']")
        assert clicked["clicked"] and clicked["element"]["name"] == "Close pane"
        assert ok("ui", "wait", "pane:Navigation", "--gone", "--timeout", "20")["gone"] is True


def test_dialog_activate_close_and_dialogs_close(opened):
    for how in ("ui close", "ui close", "dialogs --close", "dialogs --close"):
        ok("ui", "keys", "{Ctrl}h")
        assert "Replace" in ok("ui", "wait", "dialog", "--timeout", "15")["element"]["name"]
        listed = ok("dialogs")["dialogs"]
        assert len(listed) == 1 and "Replace" in listed[0]["title"]
        assert ok("ui", "activate", "--selector", "dialog")["activated"] == "dialog"
        if how == "ui close":
            assert ok("ui", "close", "dialog")["closed"] == "dialog"
        else:
            closed = ok("dialogs", "--close")
            assert closed["closed"] and closed["remaining"] == []
        assert ok("ui", "wait", "dialog", "--gone", "--timeout", "10")["gone"] is True
        assert ok("dialogs") == {"dialogs": []}
    for _ in range(2):
        assert ok("ui", "activate")["activated"] == "app"


def test_ribbon_tabs_controls_and_click(opened):
    first, second = ok("ribbon", "tabs")["tabs"], ok("ribbon", "tabs")["tabs"]
    assert first == second and {"Home", "Review", "View"} <= set(first)

    for _ in range(2):
        review = ok("ribbon", "controls", "--tab", "Review")
        assert review["tab"] == "Review" and "Track Changes" in review["controls"]
        assert ok("ribbon", "controls")["controls"]

    ok("ui", "select", "ribbon//TabItem[name='Home']")
    selected_tab_is("Home")
    tracking = ok("doc", "read", "--limit", "1")["document"]["track_revisions"]
    for _ in range(2):
        clicked = ok("ribbon", "click", "Review>Track Changes")
        assert clicked["tab"] == "Review" and clicked["control"] == "Track Changes"
        selected_tab_is("Review")
        now = ok("doc", "read", "--limit", "1")["document"]["track_revisions"]
        assert now is not tracking, "Track Changes did not toggle"
        tracking = now
    call("ui", "wait", LOWER_RIBBON_GROUP.format("Home"), "--gone", "--timeout", "10")


def test_navigation_pane_opens_reads_and_closes_through_the_ribbon_twice(opened):
    for _ in range(2):
        clicked = ok("ribbon", "click", "View>Navigation Pane")
        assert clicked["element"]["id"] == "NavigationPaneShowHide"
        ok("ui", "wait", "pane:Navigation", "--timeout", "20")
        named = ok("pane", "read", "--name", "Navigation")
        assert "Close pane" in {c["name"] for c in named["controls"]} and named["complete"]
        ok("ribbon", "click", "View>Navigation Pane")
        assert_pane_gone("Navigation")
    assert (
        call("pane", "read", "--name", "Navigation", expect_ok=False).data["error"] == "NotPresent"
    )


def test_layout_and_visible_read_the_view_twice(opened):
    for _ in range(2):
        laid = ok("layout", "--radius", "0")
        assert laid["page_count"] == 1 and laid["centre_page"] == 1 and len(laid["pages"]) == 1
        assert any(r.get("lines") for r in laid["pages"][0]["rectangles"]), laid["pages"][0]

        shown = ok("visible")
        assert shown["found_via"] in ("uia-search", "window-chain")
        assert shown["main"] and shown["main"]["start_page"] == 1
        assert shown["displayed_pages"] == [1]


def test_options_tree_expand_and_count_twice(word):
    ok("ui", "keys", "{Alt}ft")  # File > Options
    ok("ui", "wait", OPTIONS, "--timeout", "30")
    try:
        ok("ui", "click", OPTIONS + "//ListItem[name='Customize Ribbon']", "--mode", "invoke")
        ok("ui", "wait", RIBBON_TREE, "--timeout", "20")
        node = RIBBON_TREE + "/TreeItem#3"
        once, twice = ok("ui", "expand", node), ok("ui", "expand", node)
        assert once["state"] == twice["state"] == "expanded" and twice["expanded"] is False

        walks = [ok("ui", "expand", RIBBON_TREE, "--all") for _ in range(2)]
        assert all(w["complete"] for w in walks) and walks[0]["nodes"] == walks[1]["nodes"]

        counts = [ok("ui", "count", RIBBON_TREE, "--names", "3") for _ in range(2)]
        assert all(c["complete"] and c["via"] == "expand" for c in counts)
        assert counts[0]["count"] == counts[1]["count"] == walks[0]["nodes"] > 100
    finally:
        call("ui", "close", OPTIONS, expect_ok=None)
    assert ok("ui", "wait", OPTIONS, "--gone", "--timeout", "15")["gone"] is True


def test_addin_close_twice(word):
    """Word-only: with no document open no add-in opens a window, so after the
    first close (whatever an earlier test left) the second finds nothing."""
    close_all_docs()
    first, second = ok("addin", "close"), ok("addin", "close")
    for result in (first, second):
        assert "incomplete" not in result and "not_closed" not in result
    assert second["closed"] == [] and second["untitled"] == 0, "something reopened or never closed"


def test_backstage_open_show_close_twice(opened):
    for _ in range(2):
        opened_ = ok("backstage", "open")
        assert opened_["backstage"] == "open" and "Backstage" in opened_["element"]["name"]
        assert "Backstage" in ok("backstage", "show")["element"]["name"]
        assert ok("backstage", "close") == {"backstage": "closed", "ok": True}
        assert ok("ui", "wait", "backstage", "--gone", "--timeout", "10")["gone"] is True
    assert call("backstage", "show", expect_ok=False).data["error"] == "NotPresent"


def test_clip_write_read_twice(word, clipboard):
    for text in ("pilot-clip-1", "pilot-clip-2 \u00fc\u65e5"):
        assert ok("clip", "write", text)["written"] == len(text)
        assert ok("clip", "read")["text"] == text


def test_shot_and_capture_twice(opened, scratch):
    for i, selector in ((1, "app"), (2, "ribbon")):
        target = scratch / f"shot-{i}.png"
        path = Path(ok("shot", str(target), "--selector", selector)["path"])
        assert path == target.resolve() or path == target
        assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(ok("shot", "--out", str(scratch / "shot-3.png"))["path"]).exists()

    plain = ok("shot", str(scratch / "shot-4.png"))
    assert plain["scale"] == 1.0 and plain["image_size"] == plain["size"]
    zoomed = ok("shot", str(scratch / "shot-5.png"), "--region", "10,10,200,100", "--zoom", "3")
    assert zoomed["origin"] == [plain["origin"][0] + 10, plain["origin"][1] + 10]
    assert zoomed["size"] == [200, 100] and zoomed["image_size"] == [600, 300]
    assert zoomed["resample"] == "nearest" and zoomed["clipped"] is False
    auto = ok("shot", str(scratch / "shot-6.png"), "--region", "0,0,100,50", "--grid", "20")
    assert auto["scale"] == 15.0 and auto["image_size"] == [1500, 750]
    assert auto["grid"]["step"] == 20

    for i in (1, 2):
        captured = ok("capture", f"every-{i}", "--directory", str(scratch / "artifacts"))
        assert Path(captured["dir"]).is_dir() and captured["saved"]
        assert not captured["failed"], captured["failed"]


def test_image_shot_annotate_crop_resize_pad_join_twice(opened, scratch):
    box_document = json.dumps({"type": "box", "at": "app//Document"})
    shots = []
    for i in (1, 2):
        shot = ok("image", "shot", str(scratch / f"image-{i}.png"), "--op", box_document)
        assert shot["frame"] and [a["type"] for a in shot["annotations"]] == ["box"]
        shots.append(shot["path"])

    for i, at in ((1, "10,10,50,30"), (2, "app//Document")):
        op = json.dumps({"type": "box", "at": at})
        out = scratch / f"annotated-{i}.png"
        annotated = ok("image", "annotate", shots[0], "--op", op, "-o", str(out))
        assert Path(annotated["path"]) == out.resolve() and annotated["frame"]
        assert len(annotated["annotations"]) == 1

    for i, margin, size in ((1, "0", [200, 100]), (2, "5", [210, 110])):
        out = scratch / f"crop-{i}.png"
        cropped = ok("image", "crop", shots[1], "10,10,200,100", "--margin", margin, "-o", str(out))
        assert cropped["image_size"] == size and cropped["frame"]
    crop = str(scratch / "crop-1.png")

    for i, how, size in ((1, ["--scale", "2"], [400, 200]), (2, ["--width", "100"], [100, 50])):
        resized = ok("image", "resize", crop, *how, "-o", str(scratch / f"resize-{i}.png"))
        assert resized["image_size"] == size

    for i, how, size in ((1, ["--margin", "10"], [220, 120]), (2, ["--border", "2"], [200, 100])):
        padded = ok("image", "pad", crop, *how, "-o", str(scratch / f"pad-{i}.png"))
        assert padded["image_size"] == size

    for i, how, size in ((1, [], [400, 100]), (2, ["--vertical", "--gap", "10"], [200, 210])):
        joined = ok("image", "join", crop, crop, *how, "-o", str(scratch / f"join-{i}.png"))
        assert joined["image_size"] == size


def ffmpeg_missing() -> str | None:
    try:
        recording.find_ffmpeg()
        recording.find_ffprobe()
    except PilotError as exc:
        return str(exc)
    return None


def start_recording(movie: Path) -> dict:
    started = call("record", "start", str(movie), "--fps", "15", expect_ok=None)
    if started.data.get("error") == "Covered":
        pytest.fail(f"a window covers Word; close it and rerun: {started.data.get('details')}")
    assert started.code == 0 and "error" not in started.data, repr(started)
    return started.data


def test_record_start_status_stop_and_gif_twice(word, opened, scratch):
    if missing := ffmpeg_missing():
        pytest.skip(missing)
    artifacts = scratch / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for i in (1, 2):
        movie = artifacts / f"record-{i}.mp4"
        started = start_recording(movie)
        try:
            target = started["target"]
            assert target["kind"] == "window" and target["hwnd"] == word["hwnd"]
            assert started["mic"] is None and Path(started["path"]) == movie.resolve()
            assert ok("record", "status")["recording"] is True
            time.sleep(2)
        finally:
            stopped = ok("record", "stop")
        assert stopped["exit_code"] == 0 and "warning" not in stopped, stopped
        assert stopped["audio"] is None and stopped["video"]["codec"] == "h264"
        assert stopped["duration_s"] >= 1 and stopped["size_bytes"] == movie.stat().st_size
        assert ok("record", "status") == {"recording": False}

        gif = ok("record", "gif", str(movie), "-o", str(artifacts / f"record-{i}.gif"))
        assert Path(gif["path"]).read_bytes()[:6] in (b"GIF87a", b"GIF89a")


def test_events_mark_list_and_dumps_twice(word):
    keys = {"image", "since", "count", "records", "unavailable"}
    for _ in range(2):
        mark = ok("events", "mark")
        assert Path(mark["mark"]).name == "word-events-mark.json" and Path(mark["mark"]).exists()
        assert isinstance(mark["record_id"], int) and mark["reports"] >= 0 and mark["dumps"] >= 0

        since_mark = ok("events", "list", "--since-mark")
        assert keys <= set(since_mark) and since_mark["mark"] == mark["mark"]
        assert since_mark["image"] == "WINWORD.EXE"
        assert since_mark["count"] == len(since_mark["records"])
        # Word is alive and was not crashed: nothing since the mark is fatal.
        assert not winevents.fatal(since_mark["records"]), since_mark["records"]

        recent = ok("events", "list", "--since", "5m")
        assert keys <= set(recent) and recent["image"] == "WINWORD.EXE" and recent["since"] == "5m"
        assert all(r["app"].casefold() == "winword.exe" for r in recent["records"])

        dumps = ok("events", "dumps")
        assert dumps["image"] == "WINWORD.EXE" and dumps["count"] == len(dumps["dumps"])
        assert {"configured", "scope", "folder", "unavailable"} <= set(dumps)
        assert all(d["app"] == "WINWORD.EXE" for d in dumps["dumps"])


def test_lifecycle_kill_launch_kill(word, scratch_doc):
    close_all_docs()
    pids = ok("app", "status")["pids"]
    assert ok("app", "kill")["killed"] == pids
    assert ok("app", "status")["running"] is False and not winword_pids()
    assert ok("app", "kill")["killed"] == []

    relaunched = ok("app", "launch")
    assert relaunched["ready"] == ["addins_loaded"] and relaunched["pids"] != pids
    ok("doc", "open", str(scratch_doc))
    assert texts(ok("doc", "read")) == [text for _, text in PARAGRAPHS]
    ok("doc", "close")
    assert ok("app", "kill", "--force")["killed"] == relaunched["pids"]
    assert ok("app", "status")["running"] is False and not winword_pids()


def test_every_leaf_ran_at_least_twice():
    skip_record = ffmpeg_missing() is not None
    leaves = [leaf for leaf in word_leaves() if not (skip_record and leaf.startswith("record "))]
    missing = {leaf: CALLS[leaf] for leaf in leaves if CALLS[leaf] < 2}
    assert not missing, f"leaf commands called fewer than twice: {missing}"
