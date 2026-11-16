"""The parts of the driver every Word test leans on: keystrokes, the invoke
click mode, Word's own dialogs and panes, and Backstage.

Retargeted from tests/test_ui_primitives.py onto the plugin: the roots come
from the registry and every call names the app through `pilot`.
"""

from __future__ import annotations

import pytest

from desktoppilot.core import apps, ui
from desktoppilot.core import dialogs as dialogs_mod
from desktoppilot.core.errors import PilotError
from desktoppilot.core.util import wait_until
from desktoppilot.plugins.word import docs, roots

pytestmark = pytest.mark.word


def test_keys_reach_word(pilot, word, app, open_doc, gate_doc):
    doc = open_doc(gate_doc)
    docs.select_range(doc, 0, 0)

    roots.send_keys(pilot, "{Ctrl}{End}")

    assert wait_until(
        lambda: docs.selection_info(app)["start"] > 0,
        timeout=10,
        what="Ctrl+End to move the caret",
    )


def test_invoke_mode_clicks_without_the_pointer(pilot, word, open_doc, gate_doc):
    open_doc(gate_doc)
    result = roots.ribbon_click(pilot, "View>Navigation Pane", mode="invoke")

    assert result["clicked_via"] != "mouse"
    ui.wait_for("pane:Navigation", pilot, timeout=30)
    roots.ribbon_click(pilot, "View>Navigation Pane", mode="invoke")
    ui.wait_for("pane:Navigation", pilot, gone=True, timeout=30)


def test_word_dialog_is_reachable_through_the_dialog_root(pilot, word, open_doc, gate_doc):
    open_doc(gate_doc)
    roots.send_keys(pilot, "{Ctrl}h")  # Find and Replace - a plain Win32 #32770 dialog

    dialog = ui.find("dialog", pilot, timeout=15)
    assert "replace" in (dialog.Name or "").casefold()

    # Esc is unreliable here (Word keeps focus in the search box), and a modal
    # left open blocks the object model for every later test.
    assert dialogs_mod.close_all(
        apps.pids(pilot.profile), classes=dialogs_mod.dialog_classes(pilot.profile)
    )
    assert wait_until(lambda: ui.is_gone("dialog", pilot), timeout=10, what="the dialog to close")


def test_named_pane_root_finds_words_own_pane(pilot, word, open_doc, gate_doc):
    open_doc(gate_doc)
    roots.send_keys(pilot, "{Ctrl}f")  # Word's Navigation pane

    pane = ui.find("pane:Navigation", pilot, timeout=15)
    assert "navigation" in (pane.Name or "").casefold()

    roots.send_keys(pilot, "{Esc}")


def test_backstage_opens_and_closes(pilot, word, open_doc, gate_doc):
    open_doc(gate_doc)

    with pytest.raises(PilotError):
        ui.find("backstage", pilot, timeout=2)  # closed: it must not silently resolve

    roots.open_backstage(pilot)
    # Backstage opens on Home, whose left rail is the stable part; the command
    # list below it depends on the document and the account.
    labels = {c["name"] for c in ui.flatten(roots.backstage(pilot), limit=400) if c["name"]}
    assert {"Home", "New", "Open"} <= labels, "unexpected Backstage: " + repr(sorted(labels)[:15])

    roots.close_backstage(pilot)
    assert ui.is_gone("backstage", pilot)
