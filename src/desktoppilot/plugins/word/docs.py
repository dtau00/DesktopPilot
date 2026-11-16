"""Document operations over Word's COM object model: open, read, search, close.

Reads go through COM (fast, exact, gives styles and revisions); clicking stays
in the UIA layer so tests still exercise the real UI.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

import uiautomation as auto
import win32con
import win32gui

from desktoppilot.core import apps
from desktoppilot.core import dialogs as dialogs_mod
from desktoppilot.core.errors import PilotError, Timeout
from desktoppilot.core.util import wait_until

from .com import com_retry, com_set
from .view import WD_ACTIVE_END_PAGE_NUMBER


class NoActiveDocument(PilotError):
    pass


WD_ALERTS_NONE = 0
WD_DO_NOT_SAVE = 0
WD_SAVE_CHANGES = -1
WD_STATISTIC_PAGES = 2
WD_STATISTIC_WORDS = 0
WD_ACTIVE_END_ADJUSTED_PAGE_NUMBER = 1
# Given to every Open so a protected document raises instead of prompting; a
# document with no password ignores it.
NO_PASSWORD = "desktoppilot:no-password"
# A dialog that turns up during Documents.Open blocks the call until answered.
OPEN_WATCH_POLL = 0.25
OPEN_DIALOG_GRACE = 1.0
OPEN_ANSWERS = ("Cancel", "No", "Close")
# An answer is one UIA click; a busy Word can block it, and the Open is done.
OPEN_WATCH_JOIN = 5.0
# One read command's budget: each item is several COM calls, and com_retry
# alone would give every one of them 20 s on a busy Word.
READ_TIMEOUT = 120.0


class Budget:
    """One deadline for a loop of COM reads: each call gets what is left."""

    def __init__(self, what: str, timeout: float):
        self.what, self.timeout = what, timeout
        self.deadline = time.monotonic() + timeout

    def check(self, done: int) -> None:
        if time.monotonic() > self.deadline:
            raise Timeout(
                f"{self.what}: {done} read in {self.timeout:g}s, the rest not; Word is busy "
                "or the document is large - narrow it with --start/--limit"
            )

    def read(self, fn):
        return com_retry(fn, timeout=max(0.0, self.deadline - time.monotonic()))


def clean_text(text: str) -> str:
    """Strip Word's paragraph mark and end-of-cell/row bell characters."""
    return text.replace("\r", "").replace("\x07", "")


def active_doc(app):
    if com_retry(lambda: app.Documents.Count) == 0:
        raise NoActiveDocument("no document is open")
    return com_retry(lambda: app.ActiveDocument)


def open_doc(
    app,
    path: str | Path,
    *,
    read_only: bool = False,
    password: str | None = None,
    repair: bool = False,
    profile=None,
    answered: list | None = None,
    timeout: float = 60.0,
):
    """Open a document and return it. Alerts are off for the call and a
    password is always passed, so a protected or damaged file raises rather
    than prompting. With `profile`, a dialog that still blocks the call is
    answered (Cancel/No) and named in the error; `answered` collects those."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise PilotError(f"document not found: {target}")

    alerts = com_retry(lambda: app.DisplayAlerts)
    com_set(app, "DisplayAlerts", WD_ALERTS_NONE)
    watch = OpenWatch(apps.pids(profile), dialogs_mod.dialog_classes(profile)) if profile else None
    try:
        with watch or contextlib.nullcontext():
            com_retry(
                lambda: app.Documents.Open(
                    FileName=str(target),
                    ReadOnly=read_only,
                    AddToRecentFiles=False,
                    ConfirmConversions=False,
                    Visible=True,
                    PasswordDocument=password or NO_PASSWORD,
                    OpenAndRepair=repair,
                )
            )
    except PilotError:
        raise
    except Exception as exc:  # noqa: BLE001 - a COM error from Open is an answer, not a crash
        blocked = f"; Word showed {watch.seen!r}" if watch and watch.seen else ""
        raise PilotError(f"Word could not open {target}: {exc}{blocked}") from exc
    finally:
        if answered is not None and watch:
            answered.extend(watch.seen)
        com_set(app, "DisplayAlerts", alerts)
    leave_protected_view(app)
    wait_until(
        lambda: com_retry(lambda: app.Documents.Count) > 0,
        timeout=timeout,
        what="document to open",
    )
    return find_doc(app, target) or active_doc(app)


class OpenWatch:
    """While a Documents.Open runs, answer the dialogs that turn up and stay
    OPEN_DIALOG_GRACE seconds - a password or repair prompt blocks the call
    until someone does. Dialogs open before the call are left alone. `seen`
    lists what was answered."""

    def __init__(self, pids, classes):
        self.pids, self.classes = pids, classes
        self.seen: list[dict] = []
        self._done = {d["hwnd"] for d in dialogs_mod.dialog_windows(pids, classes=classes)}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="open-watch", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=OPEN_WATCH_JOIN)
        return False

    def _run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            first_seen: dict[int, float] = {}
            while not self._stop.wait(OPEN_WATCH_POLL):
                for dialog in dialogs_mod.dialog_windows(self.pids, classes=self.classes):
                    hwnd = dialog["hwnd"]
                    since = first_seen.setdefault(hwnd, time.monotonic())
                    if hwnd in self._done or time.monotonic() - since < OPEN_DIALOG_GRACE:
                        continue
                    self._done.add(hwnd)
                    self.seen.append(
                        {
                            "title": dialog["title"],
                            "text": dialog["text"][:200],
                            "answered": answer(hwnd),
                        }
                    )


def answer(hwnd: int) -> str:
    try:
        clicked = dialogs_mod.click_button(hwnd, OPEN_ANSWERS)
    except Exception:  # noqa: BLE001 - UIA could not read it; close it instead
        clicked = None
    if clicked is None:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        clicked = "close"
    return clicked


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def find_doc(app, full_name: str | Path):
    """The open document whose FullName is `full_name`, else None."""
    for i in range(com_retry(lambda: app.Documents.Count), 0, -1):
        doc = com_retry(lambda i=i: app.Documents.Item(i))
        if _same_path(str(com_retry(lambda d=doc: d.FullName)), str(full_name)):
            return doc
    return None


def leave_protected_view(app) -> bool:
    """A doc marked as coming from the internet opens read-only in a sandbox
    where the add-in ribbon is dead. Trust it so the test can proceed."""
    count = com_retry(lambda: app.ProtectedViewWindows.Count)
    if not count:
        return False
    for i in range(count, 0, -1):
        com_retry(lambda i=i: app.ProtectedViewWindows.Item(i).Edit())
    return True


def close_doc(app, *, save: bool = False, full_name: str | None = None) -> str:
    """Close the active document, or the one whose FullName is `full_name`
    ("not-open" when it is gone already)."""
    if com_retry(lambda: app.Documents.Count) == 0:
        return "no-document"
    doc = find_doc(app, full_name) if full_name else active_doc(app)
    if doc is None:
        return "not-open"
    name = com_retry(lambda: doc.Name)
    com_set(app, "DisplayAlerts", WD_ALERTS_NONE)
    com_retry(lambda: doc.Close(SaveChanges=WD_SAVE_CHANGES if save else WD_DO_NOT_SAVE))
    return name


def save_as(app, path: str | Path):
    doc = active_doc(app)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    com_retry(lambda: doc.SaveAs2(FileName=str(target)))
    return target


def doc_info(doc) -> dict:
    path = com_retry(lambda: doc.Path)
    return {
        "name": com_retry(lambda: doc.Name),
        "path": str(Path(path) / com_retry(lambda: doc.Name)) if path else None,
        "saved": bool(com_retry(lambda: doc.Saved)),
        "read_only": bool(com_retry(lambda: doc.ReadOnly)),
        "paragraphs": com_retry(lambda: doc.Paragraphs.Count),
        "words": com_retry(lambda: doc.ComputeStatistics(WD_STATISTIC_WORDS)),
        "pages": com_retry(lambda: doc.ComputeStatistics(WD_STATISTIC_PAGES)),
        "revisions": com_retry(lambda: doc.Revisions.Count),
        "comments": com_retry(lambda: doc.Comments.Count),
        "track_revisions": bool(com_retry(lambda: doc.TrackRevisions)),
    }


def read_paragraphs(
    doc,
    *,
    start: int = 1,
    limit: int = 200,
    skip_empty: bool = True,
    timeout: float = READ_TIMEOUT,
) -> list[dict]:
    """Return up to `limit` paragraphs starting at `start` (1-based), or raise
    Timeout past `timeout`. `limit` counts what comes back, so skipping
    empties does not silently shrink the result."""
    budget = Budget("paragraphs", timeout)
    total = budget.read(lambda: doc.Paragraphs.Count)
    out: list[dict] = []
    i = start
    while i <= total and len(out) < limit:
        budget.check(len(out))
        para = budget.read(lambda i=i: doc.Paragraphs.Item(i))
        text = clean_text(budget.read(lambda p=para: p.Range.Text))
        if text.strip() or not skip_empty:
            out.append(
                {
                    "index": i,
                    "style": str(budget.read(lambda p=para: p.Style.NameLocal)),
                    "list": list_string(para, budget),
                    "text": text,
                }
            )
        i += 1
    return out


def list_string(para, budget: Budget | None = None) -> str:
    """The number Word paints in front of a list paragraph ('a.', '1.').

    It is not part of the paragraph text, which is exactly what Convert List
    Nums changes: after conversion the number is literal text and this is "".
    """
    read = budget.read if budget else com_retry
    try:
        return str(read(lambda: para.Range.ListFormat.ListString) or "")
    except Exception:  # noqa: BLE001 - not a list paragraph
        return ""


def numbered_paragraphs(doc, *, limit: int = 400, timeout: float = READ_TIMEOUT) -> list[dict]:
    return [p for p in read_paragraphs(doc, limit=limit, timeout=timeout) if p["list"]]


def find_text(doc, needle: str, *, match_case: bool = False, limit: int = 50) -> list[dict]:
    """Search the main story, reporting where each hit lives: `page` is the
    physical page, `page_label` the number printed on it (a restarted or
    offset section numbering makes them differ)."""
    hits: list[dict] = []
    rng = com_retry(lambda: doc.Content)
    finder = com_retry(lambda: rng.Find)
    com_set(finder, "Text", needle)
    com_set(finder, "MatchCase", match_case)
    com_set(finder, "Forward", True)
    com_set(finder, "Wrap", 0)  # wdFindStop

    while len(hits) < limit and com_retry(lambda: finder.Execute()):
        hits.append(
            {
                "start": com_retry(lambda: rng.Start),
                "end": com_retry(lambda: rng.End),
                "page": com_retry(lambda: rng.Information(WD_ACTIVE_END_PAGE_NUMBER)),
                "page_label": com_retry(
                    lambda: rng.Information(WD_ACTIVE_END_ADJUSTED_PAGE_NUMBER)
                ),
                "text": clean_text(com_retry(lambda: rng.Text)),
            }
        )
    return hits


def select_range(doc, start: int, end: int) -> dict:
    rng = com_retry(lambda: doc.Range(start, end))
    com_retry(lambda: rng.Select())
    return {"start": start, "end": end, "text": clean_text(com_retry(lambda: rng.Text))}


def selection_info(app) -> dict:
    sel = com_retry(lambda: app.Selection)
    return {
        "start": com_retry(lambda: sel.Start),
        "end": com_retry(lambda: sel.End),
        "text": clean_text(com_retry(lambda: sel.Text) or ""),
    }


WD_REVISION_TYPES = {
    1: "Insert",
    2: "Delete",
    3: "Property",
    4: "ParagraphNumber",
    5: "DisplayField",
    6: "Reconcile",
    7: "Conflict",
    8: "Style",
    9: "Replace",
    10: "ParagraphProperty",
    11: "TableProperty",
    12: "SectionProperty",
    13: "StyleDefinition",
    14: "MovedFrom",
    15: "MovedTo",
    16: "CellInsertion",
    17: "CellDeletion",
    18: "CellMerge",
}
REVISION_TEXT_LIMIT = 200
INSERT_AT = ("end", "start", "selection")


def revision_type(code: int) -> str:
    return WD_REVISION_TYPES.get(code, f"Type{code}")


def new_doc(app):
    com_retry(lambda: app.Documents.Add())
    return active_doc(app)


def save(app) -> dict:
    doc = active_doc(app)
    if not com_retry(lambda: doc.Path):
        raise PilotError("the document has never been saved; use `doc save-as PATH`")
    com_retry(lambda: doc.Save())
    info = doc_info(doc)
    return {"saved": info["name"], "path": info["path"]}


def insert_text(app, text: str, *, at: str = "end") -> dict:
    if at not in INSERT_AT:
        raise PilotError(f"--at must be one of {list(INSERT_AT)}, got {at!r}")
    doc = active_doc(app)
    if at == "end":
        com_retry(lambda: doc.Content.InsertAfter(text))
    elif at == "start":
        com_retry(lambda: doc.Content.InsertBefore(text))
    else:
        com_retry(lambda: app.Selection.TypeText(text))
    return {"inserted": len(text), "at": at, "document": doc_info(doc)}


def comments(doc, *, limit: int = 100, timeout: float = READ_TIMEOUT) -> dict:
    budget = Budget("comments", timeout)
    count = budget.read(lambda: doc.Comments.Count)
    out: list[dict] = []
    for i in range(1, min(count, limit) + 1):
        budget.check(len(out))
        comment = budget.read(lambda i=i: doc.Comments.Item(i))
        scope = budget.read(lambda c=comment: c.Scope)
        out.append(
            {
                "index": i,
                "author": budget.read(lambda c=comment: c.Author),
                "initial": budget.read(lambda c=comment: c.Initial),
                "date": str(budget.read(lambda c=comment: c.Date)),
                "text": clean_text(budget.read(lambda c=comment: c.Range.Text) or ""),
                "scope_text": clean_text(budget.read(lambda s=scope: s.Text) or ""),
                "scope_start": budget.read(lambda s=scope: s.Start),
                "scope_end": budget.read(lambda s=scope: s.End),
            }
        )
    return {"count": count, "comments": out}


def revisions(doc, *, limit: int = 100, timeout: float = READ_TIMEOUT) -> dict:
    """The first `limit` tracked changes, and a tally per author of those.
    `count` is every revision; `complete` says whether the tally covers them all."""
    budget = Budget("revisions", timeout)
    count = budget.read(lambda: doc.Revisions.Count)
    by_author: dict[str, dict] = {}
    out: list[dict] = []
    for i in range(1, min(count, limit) + 1):
        budget.check(len(out))
        revision = budget.read(lambda i=i: doc.Revisions.Item(i))
        author = str(budget.read(lambda r=revision: r.Author))
        code = budget.read(lambda r=revision: r.Type)
        name = revision_type(code)
        tally = by_author.setdefault(author, {"count": 0, "types": {}})
        tally["count"] += 1
        tally["types"][name] = tally["types"].get(name, 0) + 1
        rng = budget.read(lambda r=revision: r.Range)
        out.append(
            {
                "index": i,
                "author": author,
                "type": name,
                "type_code": code,
                "text": clean_text(budget.read(lambda r=rng: r.Text) or "")[:REVISION_TEXT_LIMIT],
                "start": budget.read(lambda r=rng: r.Start),
                "end": budget.read(lambda r=rng: r.End),
                "date": str(budget.read(lambda r=revision: r.Date)),
            }
        )
    return {
        "count": count,
        "complete": len(out) == count,
        "by_author": by_author,
        "revisions": out,
    }
