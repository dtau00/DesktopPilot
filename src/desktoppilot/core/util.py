"""Waiting helpers. Elsewhere a sleep only paces input or a counting loop, never a wait."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import timedelta

from .errors import PilotError, Timeout

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd])$")


def wait_until[T](
    fn: Callable[[], T],
    *,
    timeout: float = 30.0,
    interval: float = 0.25,
    what: str = "condition",
    fatal: tuple[type[BaseException], ...] = (),
    lasting: tuple[type[BaseException], ...] = (),
    grace: float = 0.0,
) -> T:
    """Poll fn until it returns something truthy without raising. fn is
    always called at least once, so a timeout of 0 means "look now". An
    exception of a `fatal` type ends the wait at once instead of being retried.
    One of a `lasting` type ends it once every poll for `grace` seconds raised
    one; any other answer restarts that clock. A wait that times out on one
    raises it, not Timeout."""
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    since: float | None = None
    while True:
        try:
            result = fn()
            if result:
                return result
            last = None
            since = None
        except fatal:
            raise
        except lasting as exc:
            last = exc
            now = time.monotonic()
            if since is None:
                since = now
            if now - since >= grace:
                raise
        except Exception as exc:  # noqa: BLE001 - polling swallows by design
            last = exc
            since = None
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
        if time.monotonic() >= deadline:
            break
    if lasting and isinstance(last, lasting):
        raise last
    msg = f"timed out after {timeout:g}s waiting for {what}"
    if last is not None:
        msg += f" (last error: {last})"
    raise Timeout(msg)


def settle(seconds: float = 0.3) -> None:
    """Guard after an action that spawns UI. Prefer wait_until where possible."""
    time.sleep(seconds)


def collapse_whitespace(text: str | None) -> str:
    """Labels carry layout whitespace - a ribbon button reports as 'Review
    Error\\n' - so compare on collapsed whitespace."""
    return " ".join((text or "").split())


def parse_duration(text: str) -> timedelta:
    """'30s', '5m', '2h', '1d'. A PilotError, so a typo prints as a usage
    error, never as `internal`."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise PilotError("duration must look like 30s, 5m, 2h or 1d, got " + repr(text))
    value = float(match.group("value"))
    return timedelta(seconds=value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match["unit"]])
