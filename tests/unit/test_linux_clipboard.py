"""The clipboard over xclip, without a desktop: the command lines it runs
and what it makes of their output."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the X11 backend")

if sys.platform.startswith("linux"):
    from desktoppilot.core.errors import PilotError
    from desktoppilot.linux import win32clipboard as wc
    from desktoppilot.linux import x11
    from desktoppilot.linux.win32con import CF_UNICODETEXT

XCLIP = ["xclip", "-selection", "clipboard"]


@pytest.fixture
def xclip(monkeypatch):
    """xclip answers from `served` (target -> bytes); writes change owner."""
    state = {"owner": 7, "served": {"TARGETS": b"TARGETS\nUTF8_STRING\nSTRING\n"}, "runs": []}
    state["served"]["UTF8_STRING"] = "Wörld".encode()

    def run(argv, **kwargs):
        state["runs"].append((argv, kwargs))
        if "-o" in argv:
            data = state["served"].get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if data is not None else 1, data or b"")
        state["owner"] += 1
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(wc.subprocess, "run", run)
    monkeypatch.setattr(x11, "selection_owner", lambda name="CLIPBOARD": state["owner"])
    return state


def test_reads_utf8_text(xclip):
    assert wc.IsClipboardFormatAvailable(CF_UNICODETEXT)
    assert wc.GetClipboardData(CF_UNICODETEXT) == "Wörld"
    assert xclip["runs"][-1][0] == [*XCLIP, "-o", "-t", "UTF8_STRING"]


def test_no_owner_means_no_text_and_no_formats(xclip):
    xclip["owner"] = 0
    assert not wc.IsClipboardFormatAvailable(CF_UNICODETEXT)
    assert wc.CountClipboardFormats() == 0
    assert xclip["runs"] == []


def test_formats_leave_out_the_meta_targets(xclip):
    assert wc.CountClipboardFormats() == 2


def test_an_image_is_no_text(xclip):
    xclip["served"]["TARGETS"] = b"TARGETS\nimage/png\n"
    assert not wc.IsClipboardFormatAvailable(CF_UNICODETEXT)
    assert wc.CountClipboardFormats() == 1
    with pytest.raises(PilotError):
        wc.GetClipboardData(CF_UNICODETEXT)


def test_a_write_detaches_xclip_and_waits_for_its_ownership(xclip):
    wc.SetClipboardData(CF_UNICODETEXT, "Hé 漢")
    argv, kwargs = xclip["runs"][-1]
    assert argv == [*XCLIP, "-i"]
    assert kwargs["input"] == "Hé 漢".encode()
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] and kwargs["timeout"]


def test_empty_owns_the_clipboard_with_empty_text(xclip):
    wc.EmptyClipboard()
    assert xclip["runs"][-1][1]["input"] == b""
    xclip["served"] = {"TARGETS": b"TARGETS\nUTF8_STRING\n", "UTF8_STRING": b""}
    assert wc.CountClipboardFormats() == 0


def test_open_and_close_are_no_ops(xclip):
    wc.OpenClipboard()
    wc.CloseClipboard()
    assert xclip["runs"] == []


def test_a_missing_xclip_says_so(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("xclip")

    monkeypatch.setattr(wc.subprocess, "run", missing)
    monkeypatch.setattr(x11, "selection_owner", lambda name="CLIPBOARD": 1)
    with pytest.raises(PilotError, match="apt install xclip"):
        wc.GetClipboardData(CF_UNICODETEXT)
