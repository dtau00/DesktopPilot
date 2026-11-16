"""Word's lifecycle helpers against fakes: which add-ins a launch reports as
down, the LoadBehavior a startup crash leaves behind, the ja-JP safe-mode
prompt, which Word is ours, crash keys and the quit. No Word, and no real
registry, is touched here."""

from __future__ import annotations

import contextlib
import time
import winreg

import pytest
import pythoncom

from desktoppilot.core import dialogs as dialogs_mod
from desktoppilot.core import paths
from desktoppilot.plugins.word import session

HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
ADDINS = r"Software\Microsoft\Office\Word\Addins"


class FakeRegistry:
    """The winreg calls session makes, over {(hive, path): {value: data}}."""

    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = HKCU, HKLM
    KEY_READ, KEY_SET_VALUE, REG_DWORD = winreg.KEY_READ, winreg.KEY_SET_VALUE, winreg.REG_DWORD

    def __init__(self, keys: dict):
        self.keys = keys
        self.writes: list = []

    def OpenKey(self, hive, path, reserved, access):
        if (hive, path) not in self.keys:
            raise FileNotFoundError(path)
        return contextlib.nullcontext((hive, path))

    def QueryValueEx(self, handle, name):
        values = self.keys[handle]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], winreg.REG_DWORD

    def SetValueEx(self, handle, name, reserved, kind, value):
        self.writes.append((handle, name, value))
        self.keys[handle][name] = value


@pytest.fixture
def registry(monkeypatch):
    def with_keys(keys: dict, *, admin: bool = False) -> FakeRegistry:
        fake = FakeRegistry(keys)
        monkeypatch.setattr(session, "winreg", fake)
        monkeypatch.setattr(session, "_is_admin", lambda: admin)
        return fake

    return with_keys


def test_a_load_behavior_word_disabled_is_put_back_in_hkcu(registry):
    fake = registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 2}})

    report = session.reset_load_behavior("Contoso")

    assert report == [
        {"key": "HKCU\\" + ADDINS + r"\Contoso", "before": 2, "after": 3, "reset": True}
    ]
    assert fake.writes == [((HKCU, ADDINS + r"\Contoso"), "LoadBehavior", 3)]


def test_hklm_is_reported_not_written_without_elevation(registry):
    fake = registry({(HKLM, ADDINS + r"\Contoso"): {"LoadBehavior": 2}})

    [entry] = session.reset_load_behavior("Contoso")

    assert entry["reset"] is False and entry["after"] == 2
    assert "admin" in entry["reason"]
    assert fake.writes == []


def test_hklm_is_written_when_elevated(registry):
    fake = registry({(HKLM, ADDINS + r"\Contoso"): {"LoadBehavior": 2}}, admin=True)
    assert session.reset_load_behavior("Contoso")[0]["reset"] is True
    assert fake.writes


def test_an_enabled_add_in_is_read_and_left_alone(registry):
    fake = registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 3}})

    assert session.load_behavior("Contoso") == [
        {"key": "HKCU\\" + ADDINS + r"\Contoso", "load_behavior": 3}
    ]
    assert session.reset_load_behavior("Contoso")[0]["reset"] is False
    assert fake.writes == []


def test_an_unregistered_add_in_has_no_registrations(registry):
    registry({})
    assert session.reset_load_behavior("Contoso") == []


# ------------------------------------------------------- connected add-ins


class FakeAddin:
    def __init__(self, progid: str, connected: bool):
        self.ProgId, self.Description, self.Connect = progid, progid, connected


class FakeWord:
    Version, Visible = "16.0", True

    def __init__(self, *addins):
        self.COMAddIns = list(addins)
        self.Documents = type("Docs", (), {"Count": 0})()
        self.ProtectedViewWindows = type("PV", (), {"Count": 0})()


WORD = session.Profile(name="word", exe=[r"C:\Office\WINWORD.EXE"])
CTX = type("Ctx", (), {"profile": WORD, "name": "word"})()


@pytest.fixture
def ours(monkeypatch):
    """Word pid 7 running and ours; documents and start time faked."""
    monkeypatch.setattr(session.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(session, "owned_pids", lambda ctx, running=None: [7])
    monkeypatch.setattr(session, "documents_open_in", lambda pid: [])
    monkeypatch.setattr(session, "started_at", lambda ctx, owned=None: None)


def test_status_names_the_add_ins_that_did_not_connect(monkeypatch, registry, ours):
    registry({(HKCU, ADDINS + r"\Contoso.Addin"): {"LoadBehavior": 0}})
    word = FakeWord(FakeAddin("Contoso.Addin", False), FakeAddin("Other.Addin", True))
    monkeypatch.setattr(session.com, "attach", lambda profile, timeout=0: word)

    extras = session.status_extras(CTX)
    assert extras["addins_not_connected"] == [{"progid": "Contoso.Addin", "load_behavior": 0}]


def test_status_with_a_com_error_still_answers_and_says_why(monkeypatch, ours):
    def blocked(profile, timeout=0):
        raise session.PilotError("the Find or Replace dialog box is open")

    monkeypatch.setattr(session.com, "attach", blocked)

    extras = session.status_extras(CTX)
    assert extras["com_error"] == "the Find or Replace dialog box is open"
    assert extras["documents_open"] == {7: []} and "addins" not in extras


def test_status_of_a_word_that_is_not_ours_never_asks_it(monkeypatch):
    monkeypatch.setattr(session.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(session, "owned_pids", lambda ctx, running=None: [])
    monkeypatch.setattr(session, "started_at", lambda ctx, owned=None: None)
    monkeypatch.setattr(session.com, "attach", pytest.fail)

    extras = session.status_extras(CTX)
    assert extras["not_owned"] == [7] and extras["documents_open"] == {}
    assert "app launch" in extras["com_error"]


def test_wait_for_addins_returns_the_down_ones_instead_of_raising(registry):
    registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 3}})
    word = FakeWord(FakeAddin("Contoso", False))
    loaded = session.wait_for_addins(word, timeout=0.1, require="Contoso")
    assert session.not_connected(loaded) == [{"progid": "Contoso", "load_behavior": 3}]


@pytest.mark.parametrize(
    ("load_behavior", "expected"),
    [(3, True), (16, True), (2, False), (0, False), (9, False), (8, False), (None, False)],
)
def test_only_a_startup_load_behavior_is_expected_to_connect(load_behavior, expected):
    assert session.expected_at_startup(load_behavior) is expected


def test_the_wait_skips_add_ins_word_does_not_load_at_startup(registry):
    """This machine's case: a stale Contoso.ThisAddIn at 2 and OneNote
    at 0 kept every launch waiting the whole 20 s."""
    registry(
        {
            (HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 3},
            (HKCU, ADDINS + r"\Contoso.ThisAddIn"): {"LoadBehavior": 2},
            (HKCU, ADDINS + r"\OneNote.TakeNotes"): {"LoadBehavior": 0},
        }
    )
    word = FakeWord(
        FakeAddin("Contoso", True),
        FakeAddin("Contoso.ThisAddIn", False),
        FakeAddin("OneNote.TakeNotes", False),
    )
    start = time.monotonic()
    loaded = session.wait_for_addins(word, timeout=5)
    assert time.monotonic() - start < 1
    assert session.not_connected(loaded) == [
        {"progid": "Contoso.ThisAddIn", "load_behavior": 2},
        {"progid": "OneNote.TakeNotes", "load_behavior": 0},
    ]


def test_the_wait_still_waits_for_a_startup_add_in_that_is_down(registry):
    registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 3}})
    start = time.monotonic()
    session.wait_for_addins(FakeWord(FakeAddin("Contoso", False)), timeout=0.5)
    assert time.monotonic() - start >= 0.5


def test_require_waits_for_that_add_in_whatever_its_load_behavior(registry):
    registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 2}})
    word = FakeWord(FakeAddin("Contoso.ThisAddIn", True), FakeAddin("Contoso", False))
    start = time.monotonic()
    loaded = session.wait_for_addins(word, timeout=0.5, require="Contoso")
    assert time.monotonic() - start >= 0.5
    assert session.find_addin(loaded, "Contoso")["connected"] is False


def test_a_reset_with_a_key_touches_that_registration_only(registry):
    real = (HKCU, ADDINS + r"\Contoso")
    copy = (HKLM, ADDINS + r"\Contoso")
    stale = (HKCU, ADDINS + r"\Contoso.ThisAddIn")
    fake = registry(
        {real: {"LoadBehavior": 2}, copy: {"LoadBehavior": 2}, stale: {"LoadBehavior": 2}},
        admin=True,
    )

    report = session.reset_load_behavior("Contoso", key="HKCU\\" + ADDINS + r"\Contoso")

    assert [entry["key"] for entry in report] == ["HKCU\\" + ADDINS + r"\Contoso"]
    assert fake.writes == [(real, "LoadBehavior", 3)]


# --------------------------------------------------------------- prompts


def test_the_ja_jp_safe_mode_prompt_is_answered_no(monkeypatch):
    prompt = {
        "hwnd": 1,
        "title": "Microsoft Word",
        "text": "前回 Word を起動できませんでした。セーフ モードで Word を起動しますか?",
    }
    monkeypatch.setattr(session.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(dialogs_mod, "dialog_windows", lambda pids, **_: [prompt])
    buttons = ("はい(Y)", "いいえ(N)")
    monkeypatch.setattr(
        dialogs_mod,
        "click_button",
        lambda hwnd, labels, **_: next((b for b in labels if b in buttons), None),
    )

    assert session.reap_prompts(None)[0]["answered"] == "いいえ(N)"


def test_a_safe_mode_prompt_whose_labels_match_nothing_is_answered_by_button_id(monkeypatch):
    prompt = {"hwnd": 1, "title": "Microsoft Word", "text": "セーフ モードで起動しますか?"}
    monkeypatch.setattr(session.apps, "pids", lambda profile: [7])
    monkeypatch.setattr(dialogs_mod, "dialog_windows", lambda pids, **_: [prompt])
    given = []
    monkeypatch.setattr(
        dialogs_mod,
        "click_button",
        lambda hwnd, labels, fallback_ids=(): given.append(fallback_ids) or "x (id 7)",
    )

    session.reap_prompts(None)

    assert given == [(dialogs_mod.IDNO, dialogs_mod.IDCANCEL)]


# ---------------------------------------------------------------- whose Word

LAUNCHED = session.apps.Session(app="word", pid=7, started_at=100.0, launched=True)
ADOPTED = session.apps.Session(app="word", pid=None, started_at=100.0, launched=False)


@pytest.fixture
def word_procs(monkeypatch, tmp_path):
    """Running Word pids, their start times, and the saved session - a real
    session file, read back by the real Session.load."""
    monkeypatch.setattr(paths, "SESSION_DIR", tmp_path / "sessions")

    def given(running, *, saved=None, born=None):
        monkeypatch.setattr(session.apps, "pids", lambda profile: list(running))
        session.apps.Session.path(WORD).unlink(missing_ok=True)
        if saved is not None:
            saved.save(WORD)
        monkeypatch.setattr(session.apps, "created", lambda pid: (born or {}).get(pid))

    return given


def test_the_word_this_tool_launched_is_ours_and_no_other(word_procs):
    word_procs([99, 7], saved=LAUNCHED)
    assert session.owned_pids(CTX) == [7]


def test_a_launched_word_that_exited_leaves_nothing_ours(word_procs):
    word_procs([99], saved=LAUNCHED)
    assert session.owned_pids(CTX) == []


def test_after_an_adopt_the_words_running_then_are_ours_and_later_ones_not(word_procs):
    word_procs([7, 99], saved=ADOPTED, born={7: 50.0, 99: 150.0})
    assert session.owned_pids(CTX) == [7]


def test_with_no_session_no_word_is_ours(word_procs):
    word_procs([7], saved=None)
    assert session.owned_pids(CTX) == []


def test_owned_pids_takes_a_bare_profile_as_com_attach_does(word_procs):
    word_procs([7], saved=LAUNCHED)
    assert session.owned_pids(WORD) == [7]


def test_after_an_adopt_only_the_recorded_words_are_ours(word_procs):
    adopted = session.apps.Session(app="word", pid=None, started_at=100.0, launched=False, pids=[7])
    word_procs([7, 99], saved=adopted, born={7: 50.0, 99: 60.0})
    assert session.owned_pids(CTX) == [7]


def test_require_owned_fails_at_once_with_no_word_or_a_foreign_one(word_procs):
    word_procs([], saved=LAUNCHED)
    with pytest.raises(session.apps.AppNotRunning):
        session.require_owned(CTX)
    word_procs([99], saved=LAUNCHED)
    with pytest.raises(session.NotOurWord, match="app launch"):
        session.require_owned(CTX)


@pytest.fixture
def desktop(monkeypatch):
    """Top-level windows, topmost first: (hwnd, class, pid, title)."""

    def given(*windows):
        by_hwnd = {w[0]: w for w in windows}
        monkeypatch.setattr(
            session.win32gui, "EnumWindows", lambda fn, arg: [fn(w[0], arg) for w in windows]
        )
        monkeypatch.setattr(session.win32gui, "IsWindowVisible", lambda hwnd: True)
        monkeypatch.setattr(session.win32gui, "GetClassName", lambda hwnd: by_hwnd[hwnd][1])
        monkeypatch.setattr(session.win32gui, "GetWindowText", lambda hwnd: by_hwnd[hwnd][3])
        monkeypatch.setattr(
            session.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, by_hwnd[hwnd][2])
        )

    return given


def test_word_frames_lists_only_the_given_pids_in_z_order(desktop):
    desktop(
        (10, "OpusApp", 99, "Theirs - Word"),
        (20, "Chrome_WidgetWin_1", 7, "Browser"),
        (30, "OpusApp", 7, "Ours - Word"),
        (40, "OpusApp", 7, "Word"),
    )
    assert [f["hwnd"] for f in session.word_frames([7])] == [30, 40]
    assert session.documents_open_in(7) == ["Ours"]
    assert session.documents_open_in(99) == ["Theirs"]


class WindowedWord:
    """A Word Application whose first window belongs to `hwnd`."""

    def __init__(self, hwnd: int | None):
        window = type("Window", (), {"Hwnd": hwnd})()
        items = [window] if hwnd else []
        self.Windows = type(
            "Windows", (), {"Count": len(items), "Item": lambda s, i: items[i - 1]}
        )()


def test_a_com_object_from_a_foreign_word_on_top_is_refused(desktop):
    desktop((10, "OpusApp", 99, "Theirs - Word"), (30, "OpusApp", 7, "Ours - Word"))
    with pytest.raises(session.NotOurWord, match="pid 99"):
        session.check_com_owner(WindowedWord(10), owned=[7], running=[99, 7])
    session.check_com_owner(WindowedWord(30), owned=[7], running=[99, 7])


def test_with_no_foreign_word_the_com_object_is_not_even_asked():
    session.check_com_owner(object(), owned=[7], running=[7])


def test_a_windowless_com_object_is_ours_unless_a_blank_foreign_word_could_be_it(desktop):
    desktop((10, "OpusApp", 99, "Theirs - Word"), (30, "OpusApp", 7, "Word"))
    session.check_com_owner(WindowedWord(None), owned=[7], running=[99, 7])

    desktop((10, "OpusApp", 99, "Word"), (30, "OpusApp", 7, "Word"))
    with pytest.raises(session.NotOurWord, match="cannot tell"):
        session.check_com_owner(WindowedWord(None), owned=[7], running=[99, 7])


def test_started_at_is_our_words_start_not_the_oldest_word(word_procs):
    word_procs([99, 7], saved=LAUNCHED, born={99: 1_000.0, 7: 2_000.0})
    expected = session.datetime.fromtimestamp(2_000.0, tz=session.UTC).astimezone()
    assert session.started_at(CTX) == expected.replace(tzinfo=None)


def test_started_at_with_no_word_of_ours_is_none(word_procs):
    word_procs([99], saved=LAUNCHED, born={99: 1_000.0})
    assert session.started_at(CTX) is None


# --------------------------------------------------------------- the quit


class QuittingWord:
    def __init__(self, *docs):
        self.docs = [type("Doc", (), {"Saved": False})() for _ in docs]
        self.DisplayAlerts, self.quit_with = -1, None
        word = self

        class Documents:
            Count = len(self.docs)

            def Item(self, i):
                return word.docs[i - 1]

        self.Documents = Documents()

    def Quit(self, SaveChanges):
        self.quit_with = SaveChanges
        raise pythoncom.com_error(-2147023174, "The RPC server is unavailable.", None, None)


def test_quit_discards_every_document_and_takes_a_disconnect_as_success(monkeypatch):
    word = QuittingWord("a", "b")
    monkeypatch.setattr(session.com, "attach", lambda profile, timeout=0: word)
    monkeypatch.setattr(session.apps, "pids", lambda profile: [])

    assert session.quit_word(None) == "quit"
    assert word.DisplayAlerts == 0 and word.quit_with == 0
    assert all(doc.Saved for doc in word.docs)


def test_quit_raises_a_com_error_that_is_not_a_disconnect(monkeypatch):
    word = QuittingWord()
    word.Quit = lambda SaveChanges: (_ for _ in ()).throw(
        pythoncom.com_error(-2147352567, "Exception occurred.", None, None)
    )
    monkeypatch.setattr(session.com, "attach", lambda profile, timeout=0: word)
    with pytest.raises(pythoncom.com_error):
        session.quit_word(None)


# --------------------------------------------------------- crash leftovers


class FakeTree:
    """winreg's delete calls over a set of key paths under HKCU."""

    HKEY_CURRENT_USER, KEY_ALL_ACCESS = HKCU, winreg.KEY_ALL_ACCESS

    def __init__(self, *paths):
        self.paths = set(paths)

    def OpenKey(self, hive, path, reserved, access):
        if path not in self.paths:
            raise FileNotFoundError(path)
        return contextlib.nullcontext(path)

    def EnumKey(self, key, index):
        children = sorted(p for p in self.paths if p.rpartition("\\")[0] == key)
        if index >= len(children):
            raise OSError("no more")
        return children[index].rpartition("\\")[2]

    def DeleteKey(self, hive, path):
        self.paths.remove(path)


def test_clear_resiliency_deletes_the_leftover_trees_it_finds(monkeypatch):
    base = session.RESILIENCY_KEY
    tree = FakeTree(
        base,
        base + r"\DisabledItems",
        base + r"\DisabledItems\x",
        base + r"\StartupItems",
    )
    monkeypatch.setattr(session, "winreg", tree)

    assert session.clear_resiliency() == ["DisabledItems", "StartupItems"]
    assert tree.paths == {base}


# --------------------------------------------------- the add-in wait, edges


def test_a_word_with_no_add_ins_at_all_is_ready_at_once(registry):
    registry({})
    start = time.monotonic()
    assert session.wait_for_addins(FakeWord(), timeout=5) == []
    assert time.monotonic() - start < 1


def test_a_read_that_fails_after_one_succeeded_returns_that_one(registry, monkeypatch):
    registry({(HKCU, ADDINS + r"\Contoso"): {"LoadBehavior": 3}})
    reads = []

    def flaky(app):
        reads.append(1)
        if len(reads) > 1:
            raise pythoncom.com_error(-2147418111, "Call was rejected by callee.", None, None)
        return [{"progid": "Contoso", "connected": False, "load_behavior": 3}]

    monkeypatch.setattr(session, "addins", flaky)
    loaded = session.wait_for_addins(FakeWord(), timeout=0.3)
    assert loaded == [{"progid": "Contoso", "connected": False, "load_behavior": 3}]


def test_a_word_whose_add_ins_never_read_raises_for_the_ready_poll(monkeypatch):
    def broken(app):
        raise pythoncom.com_error(-2147418111, "Call was rejected by callee.", None, None)

    monkeypatch.setattr(session, "addins", broken)
    with pytest.raises(session.PilotError):
        session.wait_for_addins(FakeWord(), timeout=0.3)
