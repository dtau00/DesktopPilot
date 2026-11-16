# UIA_E_TIMEOUT (0x80131505): the app did not answer a UIA call in time.
UIA_E_TIMEOUT = -2146233083


def is_uia_timeout(exc: BaseException) -> bool:
    return getattr(exc, "hresult", None) == UIA_E_TIMEOUT or exc.args[:1] == (UIA_E_TIMEOUT,)


class PilotError(Exception):
    """Base error. CLI prints these as clean messages, not tracebacks."""


class NoApp(PilotError):
    """No app to drive: not running, or (a plugin's subclass) not ours. A
    poll gives it a short grace (roots.APP_GRACE) before it stops on it."""


class AppNotRunning(NoApp):
    pass


class NoTarget(PilotError):
    """The app has no window of its own, so 'app' selectors cannot resolve."""


class Timeout(PilotError):
    pass


class Ambiguous(PilotError):
    """More than one thing matches and guessing would act on the wrong one.
    Waiting cannot fix it, so polls give up on it at once."""


class NotPresent(PilotError):
    """The root or element is not there: a window that is not open, a pane
    that is not showing, Backstage while it is closed. `ui wait --gone` counts
    this, and only this, as gone - a parse error, an unknown root or an
    Ambiguous match is a broken selector, never proof of absence. A plugin
    root raises it (or a subclass) when what it resolves is not open."""


class Disabled(PilotError):
    """The target is disabled: a click on it would report success and do
    nothing."""


class WalkCut(Timeout):
    """A UIA walk was cut - by its deadline, or by a UIA call the app timed
    out on - before it covered its tree. What it did not reach is unknown,
    not absent, so a caller must never report "not found", "gone" or a count
    from it. `hits` holds the matches found before the cut; `count`, on a cut
    count, how far it got: a floor, never the count."""

    def __init__(self, message: str, hits: list | None = None, *, count: int | None = None):
        super().__init__(message)
        self.hits = list(hits or [])
        self.count = count


class Covered(PilotError):
    """Another window is over the recording area. Whether that is meant is
    the user's call, so `details.covering` lists them for the agent to ask."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details
