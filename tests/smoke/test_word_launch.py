"""The word plugin against a real Word: launch with add-ins, open the gate
document, read it, close it. The session fixture kills Word at the end.

    pytest tests/smoke/test_word_launch.py -m word --app word
"""

from __future__ import annotations

import pytest

from desktoppilot.core import apps
from desktoppilot.plugins.word import docs, roots, session

pytestmark = pytest.mark.word


def test_word_launches_with_its_addins_connected(word):
    assert word["running"] and word["pids"]
    assert word["ready"] == ["addins_loaded"]
    startup = [a for a in word["addins"] if session.expected_at_startup(a["load_behavior"])]
    assert all(a["connected"] for a in startup), word["addins"]


def test_the_main_window_is_the_word_frame(pilot, word):
    frame = roots.word_window(pilot)
    assert frame.ClassName == roots.WORD_WINDOW_CLASS
    assert [w for w in apps.windows(pilot.profile) if w["is_main"]]


def test_a_document_opens_reads_and_closes(pilot, app, open_doc, gate_doc):
    doc = open_doc(gate_doc)
    info = docs.doc_info(doc)
    assert info["name"] == gate_doc.name
    assert info["paragraphs"] > 0

    paragraphs = docs.read_paragraphs(doc, limit=3)
    assert paragraphs and paragraphs[0]["text"].strip()

    assert docs.close_doc(app) == gate_doc.name
    assert not docs.com_retry(lambda: app.Documents.Count)
