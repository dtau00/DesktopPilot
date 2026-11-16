"""A selector wait on an app that is not running: it stops after a short
grace, not at its timeout. A fake clock, so nothing here waits."""

from __future__ import annotations

import importlib
import time

import pytest

from desktoppilot.core import apps, profiles, ui, util
from desktoppilot.core.errors import AppNotRunning, NoApp, NotPresent, Timeout
from desktoppilot.core.registry import AppContext, Registry
from desktoppilot.core.roots import APP_GRACE
from desktoppilot.core.util import wait_until

find_mod = importlib.import_module("desktoppilot.core.find")
roots_mod = importlib.import_module("desktoppilot.core.roots")


class Clock:
    """time.monotonic and time.sleep: a sleep moves the clock, a call costs
    nothing. It starts at the real clock, which the walks' deadlines read."""

    def __init__(self):
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    for module in (util, find_mod):
        monkeypatch.setattr(module, "time", fake)
    return fake


class Node:
    def __init__(self, kind: str, name: str = "", children=()):
        self.ControlTypeName = kind + "Control"
        self.Name = name
        self.IsOffscreen = False
        self.children = list(children)

    def GetChildren(self):
        return self.children


def tree():
    return Node("Pane", "", [Node("Button", "ok")])


@pytest.fixture
def root(monkeypatch):
    """resolve_root answers from `answers`: an exception class raises, a
    callable builds the root; the last answer repeats."""
    answers: list = []
    calls: list[float] = []

    def resolve(sel, ctx, timeout, probe=False):
        calls.append(timeout)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer("not running")
        return answer()

    monkeypatch.setattr(ui, "resolve_root", resolve)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    return answers, calls


class NotOurs(NoApp):
    """A plugin's own "no app", as Word's NotOurWord."""


# ------------------------------------------------------------ wait_until


def test_a_lasting_error_ends_the_wait_after_the_grace(clock):
    start = clock.now

    def gone():
        raise AppNotRunning("not running")

    with pytest.raises(AppNotRunning):
        wait_until(gone, timeout=30, lasting=(AppNotRunning,), grace=2)
    assert 2 <= clock.now - start < 2.5


def test_any_other_answer_restarts_the_grace(clock):
    answers = iter([AppNotRunning] * 6 + [None] + [AppNotRunning] * 100)
    start = clock.now

    def flaky():
        answer = next(answers)
        if answer is not None:
            raise answer("not running")
        return None

    with pytest.raises(AppNotRunning):
        wait_until(flaky, timeout=30, interval=0.25, lasting=(AppNotRunning,), grace=2)
    # 1.5 s down, a poll that resolved, then a full grace again
    assert clock.now - start >= 1.75 + 2


def test_a_timeout_on_a_lasting_error_raises_it(clock):
    def gone():
        raise AppNotRunning("not running")

    with pytest.raises(AppNotRunning):
        wait_until(gone, timeout=0.5, lasting=(AppNotRunning,), grace=2)


def test_with_no_lasting_types_the_wait_still_times_out(clock):
    def gone():
        raise AppNotRunning("not running")

    with pytest.raises(Timeout, match="last error: not running"):
        wait_until(gone, timeout=5)


# ------------------------------------------------------------------- find


def test_find_on_an_app_never_running_stops_after_the_grace(clock, root):
    answers, _ = root
    answers.append(AppNotRunning)
    start = clock.now
    with pytest.raises(AppNotRunning):
        ui.find("app//Button", None, timeout=30)
    assert APP_GRACE <= clock.now - start < APP_GRACE + 0.5


def test_find_on_an_app_that_starts_within_the_grace_finds(clock, root):
    answers, _ = root
    answers.extend([AppNotRunning] * 6 + [tree])
    assert ui.find("app//Button", None, timeout=30).Name == "ok"


def test_find_with_a_timeout_inside_the_grace_raises_app_not_running(clock, root):
    answers, _ = root
    answers.append(AppNotRunning)
    start = clock.now
    with pytest.raises(AppNotRunning):
        ui.find("app//Button", None, timeout=1)
    assert 1 <= clock.now - start < APP_GRACE


def test_find_on_a_plugins_no_app_stops_after_the_grace(clock, root):
    answers, _ = root
    answers.append(NotOurs)
    with pytest.raises(NotOurs):
        ui.find("app//Button", None, timeout=30)


def test_find_with_no_match_still_times_out(clock, root):
    answers, _ = root
    answers.append(lambda: Node("Pane"))
    start = clock.now
    with pytest.raises(Timeout, match="complete walk"):
        ui.find("app//Button", None, timeout=5)
    assert clock.now - start >= 5


def test_find_on_a_root_not_there_still_times_out(clock, root):
    answers, _ = root
    answers.append(NotPresent)
    with pytest.raises(Timeout):
        ui.find("app//Button", None, timeout=5)


# ------------------------------------------------------------- wait --gone


def test_wait_gone_with_no_app_is_gone_at_once(clock, monkeypatch):
    looks = []

    def not_running(*args, **kwargs):
        looks.append(1)
        raise AppNotRunning("not running")

    monkeypatch.setattr(ui, "find_all", not_running)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    start = clock.now
    assert ui.wait_for("app//Button", None, gone=True, timeout=30) == {
        "selector": "app//Button",
        "gone": True,
    }
    assert looks == [1] and clock.now == start


def test_wait_gone_on_a_plugins_no_app_stops_after_the_grace(clock, monkeypatch):
    def not_ours(*args, **kwargs):
        raise NotOurs("not ours")

    monkeypatch.setattr(ui, "find_all", not_ours)
    monkeypatch.setattr(ui, "_check_root", lambda sel, ctx: None)
    start = clock.now
    with pytest.raises(NotOurs):
        ui.wait_for("app//Button", None, gone=True, timeout=30)
    assert clock.now - start < APP_GRACE + 0.5


# ------------------------------------------------------------------ roots


@pytest.fixture
def notepad(monkeypatch):
    monkeypatch.setattr(apps, "pids", lambda profile: [])
    return AppContext(name="notepad", profile=profiles.load("notepad"), registry=Registry())


def test_a_root_wait_stops_at_once_on_no_app(clock):
    def gone():
        raise AppNotRunning("not running")

    start = clock.now
    with pytest.raises(AppNotRunning):
        roots_mod.present(gone, timeout=15, what="it")
    assert clock.now == start


@pytest.mark.parametrize("probe", [False, True])
def test_win_and_dialog_roots_say_the_app_is_not_running(clock, notepad, probe):
    with pytest.raises(AppNotRunning):
        roots_mod.named_window(notepad, "Save", timeout=15, probe=probe)
    with pytest.raises(AppNotRunning):
        roots_mod.dialog_window(notepad, timeout=15, probe=probe)
