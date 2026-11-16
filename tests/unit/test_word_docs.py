"""Document operations against a fake Word object model: what Documents.Open
is asked, a dialog that blocks it, which document a close closes, and which
page a hit reports. No Word is started here."""

from __future__ import annotations

import contextlib
import threading
import time

import pytest

from desktoppilot.core.errors import PilotError, Timeout
from desktoppilot.plugins.word import docs

PROFILE = type("Profile", (), {"dialog_classes": ()})()


class FakeDoc:
    def __init__(self, full_name: str):
        self.FullName = full_name
        self.Name = full_name.rsplit("\\", 1)[-1]
        self.closed_with = None

    def Close(self, SaveChanges):
        self.closed_with = SaveChanges


class FakeDocuments:
    def __init__(self, word, docs=()):
        self.word, self.docs, self.opened_with = word, list(docs), None

    @property
    def Count(self):
        return len([d for d in self.docs if d.closed_with is None])

    def Item(self, i):
        return [d for d in self.docs if d.closed_with is None][i - 1]

    def Open(self, **kwargs):
        self.opened_with = kwargs | {"alerts": self.word.DisplayAlerts}
        doc = FakeDoc(kwargs["FileName"])
        self.docs.insert(0, doc)  # Word does not make the new one the last Item
        return doc


class FakeWord:
    def __init__(self, *open_docs):
        self.DisplayAlerts = -1  # wdAlertsAll
        self.Documents = FakeDocuments(self, open_docs)
        self.ProtectedViewWindows = type("PV", (), {"Count": 0})()

    @property
    def ActiveDocument(self):
        return self.Documents.Item(self.Documents.Count)


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "contract.docx"
    path.write_bytes(b"")
    return path


def test_open_turns_alerts_off_and_passes_a_password_then_restores(document):
    word = FakeWord()
    docs.open_doc(word, document, password="s3cret", repair=True)

    opened = word.Documents.opened_with
    assert opened["alerts"] == docs.WD_ALERTS_NONE
    assert opened["PasswordDocument"] == "s3cret" and opened["OpenAndRepair"] is True
    assert word.DisplayAlerts == -1


def test_open_without_a_password_still_passes_one_so_word_cannot_prompt(document):
    word = FakeWord()
    docs.open_doc(word, document)
    assert word.Documents.opened_with["PasswordDocument"] == docs.NO_PASSWORD


def test_open_returns_the_document_it_opened_not_the_active_one(document):
    word = FakeWord(FakeDoc(r"C:\other.docx"))
    doc = docs.open_doc(word, document)
    assert doc.FullName == str(document)


def test_a_com_error_from_open_is_an_error_with_json_not_a_crash(document):
    word = FakeWord()

    def refuse(**kwargs):
        raise Exception("The password is incorrect. Word cannot open the document.")  # noqa: TRY002

    word.Documents.Open = refuse
    with pytest.raises(PilotError, match="password is incorrect"):
        docs.open_doc(word, document)
    assert word.DisplayAlerts == -1


def test_a_dialog_that_blocks_open_is_answered_and_named(document, monkeypatch):
    answered = threading.Event()
    prompt = {"hwnd": 5, "title": "Password", "text": "Enter password to open file"}
    started = threading.Event()

    def dialogs(pids, **_):
        return [prompt] if started.is_set() and not answered.is_set() else []

    def click(hwnd, labels):
        answered.set()
        return labels[0]

    monkeypatch.setattr(docs.dialogs_mod, "dialog_windows", dialogs)
    monkeypatch.setattr(docs.dialogs_mod, "click_button", click)
    monkeypatch.setattr(docs.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(docs.auto, "UIAutomationInitializerInThread", contextlib.nullcontext)
    monkeypatch.setattr(docs, "OPEN_WATCH_POLL", 0.01)
    monkeypatch.setattr(docs, "OPEN_DIALOG_GRACE", 0.0)

    word = FakeWord()

    def blocked(**kwargs):
        started.set()
        assert answered.wait(5), "nobody answered the dialog"
        raise Exception("Command failed")  # noqa: TRY002 - what Word raises on Cancel

    word.Documents.Open = blocked
    seen: list = []
    with pytest.raises(PilotError) as err:
        docs.open_doc(word, document, profile=PROFILE, answered=seen)
    assert "'Password'" in str(err.value) and "Cancel" in str(err.value)
    assert seen == [{"title": "Password", "text": prompt["text"], "answered": "Cancel"}]


def test_a_dialog_open_before_the_call_is_left_alone(document, monkeypatch):
    monkeypatch.setattr(
        docs.dialogs_mod,
        "dialog_windows",
        lambda pids, **_: [{"hwnd": 9, "title": "Font", "text": ""}],
    )
    monkeypatch.setattr(docs.dialogs_mod, "click_button", pytest.fail)
    monkeypatch.setattr(docs.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(docs.auto, "UIAutomationInitializerInThread", contextlib.nullcontext)
    monkeypatch.setattr(docs, "OPEN_WATCH_POLL", 0.01)
    monkeypatch.setattr(docs, "OPEN_DIALOG_GRACE", 0.0)

    seen: list = []
    docs.open_doc(FakeWord(), document, profile=PROFILE, answered=seen)
    assert seen == []


# ----------------------------------------------------------------- close


def test_close_by_full_name_closes_that_document_unsaved():
    mine, other = FakeDoc(r"C:\docs\mine.docx"), FakeDoc(r"C:\docs\other.docx")
    word = FakeWord(mine, other)  # other is the active one

    assert docs.close_doc(word, full_name=r"c:\DOCS\mine.docx") == "mine.docx"
    assert mine.closed_with == docs.WD_DO_NOT_SAVE
    assert other.closed_with is None


def test_close_by_full_name_of_a_document_already_gone_closes_nothing():
    other = FakeDoc(r"C:\docs\other.docx")
    assert docs.close_doc(FakeWord(other), full_name=r"C:\docs\mine.docx") == "not-open"
    assert other.closed_with is None


# ------------------------------------------------------------------ find


class FakeRange:
    Start, End, Text = 10, 14, "Term"

    def __init__(self):
        self.hits = 1
        self.Find = self

    def Execute(self):
        self.hits -= 1
        return self.hits >= 0

    def Information(self, kind):
        return {docs.WD_ACTIVE_END_ADJUSTED_PAGE_NUMBER: 1, docs.WD_ACTIVE_END_PAGE_NUMBER: 4}[kind]


def test_find_reports_the_physical_page_and_the_printed_number():
    doc = type("Doc", (), {"Content": FakeRange()})()
    [hit] = docs.find_text(doc, "Term")
    assert hit["page"] == 4, "wdActiveEndPageNumber: the page counted from the start"
    assert hit["page_label"] == 1, "wdActiveEndAdjustedPageNumber: numbering restarted"


# ------------------------------------------------------------- read loops


class Revision:
    def __init__(self, i: int):
        self.Author, self.Type, self.Date = f"author{i % 2}", 1, "2026-09-25"
        self.Range = type("Range", (), {"Text": f"r{i}", "Start": i, "End": i + 1})()


class Revisions:
    def __init__(self, count: int):
        self.Count, self.read = count, []

    def Item(self, i):
        self.read.append(i)
        return Revision(i)


def test_revisions_reads_only_up_to_the_limit_and_says_it_is_incomplete():
    doc = type("Doc", (), {"Revisions": Revisions(500)})()
    result = docs.revisions(doc, limit=3)

    assert doc.Revisions.read == [1, 2, 3], "reading all 500 to tally them is the slow part"
    assert result["count"] == 500 and result["complete"] is False
    assert [r["text"] for r in result["revisions"]] == ["r1", "r2", "r3"]
    assert sum(t["count"] for t in result["by_author"].values()) == 3


def test_revisions_under_the_limit_are_complete():
    result = docs.revisions(type("Doc", (), {"Revisions": Revisions(2)})(), limit=100)
    assert result["complete"] is True and len(result["revisions"]) == 2


class SlowParagraphs:
    """Paragraphs whose every read takes `delay` seconds."""

    def __init__(self, count: int, delay: float):
        self.count, self.delay = count, delay

    @property
    def Count(self):
        return self.count

    def Item(self, i):
        time.sleep(self.delay)
        para = type("Para", (), {})()
        para.Range = type("Range", (), {"Text": f"p{i}\r", "ListFormat": None})()
        para.Style = type("Style", (), {"NameLocal": "Normal"})()
        return para


def test_a_paragraph_read_stops_at_its_budget_and_says_how_far_it_got():
    doc = type("Doc", (), {"Paragraphs": SlowParagraphs(1000, 0.05)})()
    start = time.monotonic()
    with pytest.raises(Timeout, match=r"paragraphs: \d+ read in 0.3s"):
        docs.read_paragraphs(doc, limit=1000, timeout=0.3)
    assert time.monotonic() - start < 0.3 + 0.2


def test_a_comment_read_stops_at_its_budget():
    class Comments:
        Count = 1000

        def Item(self, i):
            time.sleep(0.05)
            scope = type("Scope", (), {"Text": "x", "Start": 0, "End": 1})()
            rng = type("Range", (), {"Text": "c"})()
            return type(
                "C", (), {"Scope": scope, "Author": "a", "Initial": "A", "Date": 0, "Range": rng}
            )()

    with pytest.raises(Timeout, match="comments"):
        docs.comments(type("Doc", (), {"Comments": Comments()})(), limit=1000, timeout=0.3)
