"""util: the one polling loop and the label comparison."""

from __future__ import annotations

import time

import pytest

from desktoppilot.core.errors import Ambiguous, Timeout
from desktoppilot.core.util import collapse_whitespace, wait_until


def test_wait_until_returns_the_first_truthy_answer():
    answers = iter([None, 0, "", "done"])
    assert wait_until(lambda: next(answers), timeout=5, interval=0.01) == "done"


def test_wait_until_with_no_time_still_asks_once():
    calls = []
    assert wait_until(lambda: calls.append(1) or "now", timeout=0) == "now"
    assert calls == [1]
    with pytest.raises(Timeout):
        wait_until(lambda: calls.append(1), timeout=-1)
    assert calls == [1, 1]


def test_a_fatal_error_ends_the_wait_at_once():
    calls = []

    def ambiguous():
        calls.append(1)
        raise Ambiguous("two panes")

    start = time.monotonic()
    with pytest.raises(Ambiguous):
        wait_until(ambiguous, timeout=5, interval=0.01, fatal=(Ambiguous,))
    assert calls == [1] and time.monotonic() - start < 1


def test_the_timeout_names_the_last_error():
    def broken():
        raise OSError("RPC server unavailable")

    with pytest.raises(
        Timeout, match=r"waiting for the pane \(last error: RPC server unavailable\)"
    ):
        wait_until(broken, timeout=0.05, interval=0.01, what="the pane")


def test_a_falsy_answer_after_an_error_clears_the_error():
    answers = iter([OSError("flaky")])

    def flaky():
        for answer in answers:
            raise answer
        return None

    with pytest.raises(Timeout) as err:
        wait_until(flaky, timeout=0.05, interval=0.01, what="it")
    assert "last error" not in str(err.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Track\nChanges\n", "Track Changes"), ("  a \t b  ", "a b"), ("", ""), (None, "")],
)
def test_collapse_whitespace(text, expected):
    assert collapse_whitespace(text) == expected
