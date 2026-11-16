"""Word's pytest fixtures, loaded by core's plugin when --app is word (or an
app that extends it).

    def test_pane_opens(word, app, open_doc, pane):
        open_doc(r"C:\\docs\\report.docx")
        assert pane.contains("Tasks")
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from desktoppilot.core import apps
from desktoppilot.core.errors import PilotError, WalkCut
from desktoppilot.core.pytest_plugin import Scope

from . import com, docs, roots, session


@pytest.fixture(scope="session")
def word(pilot, desktop_app):
    """A running Word, fresh for the session and killed at the end (core's
    desktop_app does the launching; this is the Word-shaped name for it)."""
    return desktop_app


@pytest.fixture(scope="session")
def app(pilot, word):
    """The Word Application COM object."""
    return com.attach(pilot.profile)


@pytest.fixture
def open_doc(request, pilot, app):
    """Open a document; that document - not whichever is active - is
    discarded (never saved) when the test ends. Returns once Word answers UIA
    quickly again (roots.settle)."""
    opened: list[str] = []

    def _open(path: str | Path, *, read_only: bool = False):
        doc = docs.open_doc(app, path, read_only=read_only, profile=pilot.profile)
        opened.append(com.com_retry(lambda: doc.FullName))
        roots.settle(pilot)
        return doc

    yield _open

    close_opened(app, opened, request.node)


def close_opened(app, full_names: list[str], node) -> list[dict]:
    """Discard each document, newest first. One that will not close does not
    stop the others: it is recorded on the test (user_properties
    "docs_not_closed", [{"document", "error"}]) and warned about."""
    failed = []
    for name in reversed(full_names):
        try:
            docs.close_doc(app, save=False, full_name=name)
        except PilotError as exc:
            failed.append({"document": name, "error": str(exc)})
    if failed:
        node.user_properties.append(("docs_not_closed", failed))
        warnings.warn(f"documents left open after {node.nodeid}: {failed}", stacklevel=1)
    return failed


@pytest.fixture
def pane(pilot, word):
    """Handle onto the add-in task pane, with a small find/read API."""
    return Scope(pilot, "pane")


@pytest.fixture
def win(pilot, word):
    """Handle onto a window the add-in opens: win("Inspector")."""
    return lambda title: Scope(pilot, "win:" + title)


@pytest.fixture(autouse=True)
def close_addin_windows_after_test(request, pilot):
    """Each test starts with no add-in window and no modal left over from the
    last one. Word is shared across the session, so isolation is explicit -
    and a forgotten modal does not just fail the next test, it blocks the
    object model ("the Find or Replace dialog box is open")."""
    yield
    if not apps.pids(pilot.profile) or request.config.getoption("--app-keep-open"):
        return
    close_leftovers(pilot, request.node)


def close_leftovers(pilot, node) -> None:
    """Close the add-in's windows. Teardown must not mask a failure, so
    nothing raises - but a toast check Word did not answer, or a window that
    would not close, is recorded on the test (user_properties
    "addin_close_incomplete", "addin_not_closed") and warned about, not
    hidden: the next test may start under it.

    Only in a Word this run launched: an adopted one is never clicked in, so
    its add-in windows are recorded ("addin_left_open") and left open. Stray
    dialogs are core's close_dialogs_after_test, under the same rule."""
    if not apps.launched_here(pilot.profile):
        report_left_open(pilot, node)
        return
    not_closed: list[dict] = []
    try:
        roots.close_addin_windows(pilot, not_closed=not_closed)
    except WalkCut as cut:
        node.user_properties.append(("addin_close_incomplete", str(cut)))
        warnings.warn(f"add-in windows not all checked after {node.nodeid}: {cut}", stacklevel=1)
    except Exception:  # noqa: BLE001 - teardown must not mask a failure
        pass
    if not_closed:
        node.user_properties.append(("addin_not_closed", not_closed))
        warnings.warn(f"add-in windows left open after {node.nodeid}: {not_closed}", stacklevel=1)


def report_left_open(pilot, node) -> None:
    try:
        titles = [
            w["title"]
            for w in roots.top_windows(pilot)
            if w["class"] != session.WORD_WINDOW_CLASS and w["title"]
        ]
    except Exception:  # noqa: BLE001 - teardown must not mask a failure
        return
    if titles:
        node.user_properties.append(("addin_left_open", titles))
        warnings.warn(
            f"add-in windows left open in an adopted Word after {node.nodeid}: {titles}",
            stacklevel=1,
        )
