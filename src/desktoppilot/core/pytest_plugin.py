"""pytest fixtures for desktop UI tests.

Registered as a pytest11 entry point, so any suite that installs desktoppilot
gets these without a conftest:

    from desktoppilot.core import ui
    from desktoppilot.core.pytest_plugin import Scope

    def test_palette(pilot, desktop_app):
        ui.send_keys("{Ctrl}{Shift}p")
        assert Scope(pilot, "app").contains("Preferences")

The app under test comes from --app; its plugin's own fixture modules are
loaded with it.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field

import pytest

from ..linux import procs as linux_procs
from . import apps, artifacts, dialogs, profiles, ui, winevents
from .errors import Timeout, WalkCut
from .native import WINDOWS, win32api, win32con, win32process
from .registry import AppContext, Registry, discover
from .util import wait_until

if WINDOWS:
    import pywintypes

REGISTRY = Registry()
STRAY_DIALOGS = pytest.StashKey[list]()
WINDOWS_EVENTS = pytest.StashKey["EventsWatch"]()
PHASE_FAILED = pytest.StashKey[set]()
# WER writes the crash record a few seconds after the process is gone.
CRASH_RECORD_WAIT = 10.0
STILL_ACTIVE = 259


def pytest_addoption(parser):
    group = parser.getgroup("desktoppilot")
    group.addoption("--app", default=None, help="Name of the app under test.")
    group.addoption(
        "--app-fresh", action="store_true", help="Kill any running instance before launching."
    )
    group.addoption(
        "--app-keep-open",
        action="store_true",
        help="Leave the app running after the session (for inspecting a failure).",
    )
    group.addoption(
        "--pilot-artifacts",
        default="artifacts",
        help="Directory for failure screenshots, trees and state.",
    )
    group.addoption(
        "--pilot-allow-crash",
        action="store_true",
        help="Report a crash, hang or .NET exception of the app in the test report "
        "instead of failing the test (for a test that crashes the app on purpose).",
    )


def pytest_configure(config):
    """A plugin's fixtures are only useful once its app is named, so they load
    here rather than through another entry point."""
    discover(REGISTRY)
    name = config.getoption("--app")
    if not name:
        return
    for module in REGISTRY.fixture_modules_for(name):
        config.pluginmanager.import_plugin(module)


def app_context(name: str) -> AppContext:
    return AppContext(name=name, profile=profiles.load(name), registry=REGISTRY)


@pytest.fixture(scope="session")
def pilot(request) -> AppContext:
    """The AppContext every ui call needs."""
    name = request.config.getoption("--app")
    if not name:
        pytest.skip("no --app given")
    return app_context(name)


@pytest.fixture(scope="session")
def desktop_app(request, pilot):
    """The app under test, launched or adopted for the whole session."""
    fresh = True if request.config.getoption("--app-fresh") else None
    info = apps.launch(pilot, fresh=fresh)

    yield info

    # Never kill an app this run adopted: it belongs to whoever started it.
    if info.get("adopted") or request.config.getoption("--app-keep-open"):
        return
    with contextlib.suppress(Exception):
        apps.kill(pilot)


@pytest.fixture(autouse=True)
def close_dialogs_after_test(request):
    """A modal left over from one test is why the next one fails for the wrong
    reason, so isolation is explicit. What it found is reported with the
    test (a report section and user_properties): a stray product dialog is
    news, not something to tidy away quietly. Only an app this run launched
    is clicked in; in an adopted one the dialogs are reported and left open."""
    yield
    name = request.config.getoption("--app")
    if not name or request.config.getoption("--app-keep-open"):
        return
    with contextlib.suppress(Exception):  # teardown must not mask a failure
        profile = profiles.load(name)
        pids = apps.pids(profile)
        classes = dialogs.dialog_classes(profile)
        if apps.launched_here(profile):
            stray = dialogs.close_all(pids, classes=classes)
        else:
            stray = [
                {"title": d["title"], "text": d["text"], "answered": None}
                for d in dialogs.dialog_windows(pids, classes=classes)
            ]
        if stray:
            request.node.stash[STRAY_DIALOGS] = stray
            request.node.user_properties.append(("desktoppilot_stray_dialogs", stray))


@dataclass
class EventsWatch:
    """What the windows_events fixture knows about one test."""

    image: str
    mark: winevents.Mark
    pids: list[int]
    found: list[dict] = field(default_factory=list)


@pytest.fixture(autouse=True)
def windows_events(request):
    """A crash, hang or unhandled .NET exception of the app during a test is a
    bug even when the test body passed - the test just did not notice. So a
    mark is taken before each test and Windows' own records are checked after
    it: a fatal record for the app fails the test in teardown, or with
    --pilot-allow-crash only lands in the report. Nothing here may raise
    except that verdict: a broken event log must not fail a run."""
    watch = _watch(request.config.getoption("--app"))
    if watch is None:
        yield
        return
    request.node.stash[WINDOWS_EVENTS] = watch
    yield
    watch.found = _fatal_since(watch)
    already_failed = bool(request.node.stash.get(PHASE_FAILED, set()))
    if watch.found and not already_failed and not request.config.getoption("--pilot-allow-crash"):
        pytest.fail(
            "the app crashed or hung during the test:\n" + _describe(watch.found), pytrace=False
        )


def _watch(name: str | None) -> EventsWatch | None:
    if not name:
        return None
    try:
        profile = profiles.load(name)
        return EventsWatch(
            image=profile.image_name,
            mark=winevents.Mark.now(profile.image_name),
            pids=apps.pids(profile),
        )
    except Exception:  # noqa: BLE001 - no profile or no log: the test runs unwatched
        return None


def _fatal_since(watch: EventsWatch) -> list[dict]:
    """Fatal records since the mark. When an instance seen at setup is gone,
    wait up to CRASH_RECORD_WAIT for the record WER writes late; while the app
    is alive one look is enough, and keeps the check cheap."""

    def look() -> list[dict]:
        result = winevents.query(watch.image, kinds=winevents.FATAL_KINDS, mark=watch.mark)
        # A crashed process is gone: a `crash` whose pid still runs is a report
        # the app survived. A hang or .NET record is written as it dies, so stays.
        return [
            r
            for r in winevents.fatal(result["records"], watch.image)
            if not (r["kind"] == "crash" and r["pid"] and _alive(r["pid"]))
        ]

    try:
        if not any(not _alive(pid) for pid in watch.pids):
            return look()
        try:
            return wait_until(look, timeout=CRASH_RECORD_WAIT, interval=0.5, what="crash record")
        except Timeout:
            return []
    except Exception:  # noqa: BLE001 - teardown must not mask the test's own outcome
        return []


def _alive(pid: int) -> bool:
    """One OpenProcess, not a tasklist: this runs after every test."""
    if not WINDOWS:
        return linux_procs.alive(pid)
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == STILL_ACTIVE
    finally:
        win32api.CloseHandle(handle)


def _describe(records: list[dict]) -> str:
    lines = []
    for r in records:
        detail = ", ".join(
            f"{key} {r[key]}" for key in ("pid", "module", "exception_code", "path") if r.get(key)
        )
        lines.append(f"{r['time']} {r['kind']} {r['app']}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


def _events_section(watch: EventsWatch) -> str:
    """Everything Windows recorded for the app since the mark, fatal or not."""
    result = winevents.query(watch.image, mark=watch.mark)
    lines = [_describe(result["records"]) or "no records since the test started"]
    lines += [f"unavailable: {source}: {why}" for source, why in result["unavailable"].items()]
    return "\n".join(lines)


class Scope:
    """A selector root with a small API, so tests read as UI steps rather than
    as selector plumbing."""

    def __init__(self, ctx: AppContext, root: str = "app"):
        self.ctx = ctx
        self.root = root

    @property
    def control(self):
        return ui.find(self.root, self.ctx)

    def _selector(self, step: str) -> str:
        return self.root + "//" + step

    def find(self, step: str, *, timeout: float = ui.DEFAULT_TIMEOUT):
        return ui.find(self._selector(step), self.ctx, timeout=timeout)

    def find_all(self, step: str) -> list:
        return ui.find_all(self._selector(step), self.ctx)

    def count(self, step: str, *, expand: bool = True, timeout: float = ui.DEFAULT_TIMEOUT) -> dict:
        """The real item count of a list or tree - find_all only sees the
        realized rows of a virtualized list."""
        return ui.count_items(self.find(step, timeout=timeout), expand=expand)

    def click(self, step: str, *, mode: str = "auto", timeout: float = ui.DEFAULT_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout

        def find():
            return self.find(step, timeout=max(deadline - time.monotonic(), 0.0))

        return ui.click(
            find(), ctx=self.ctx, mode=mode, timeout=deadline - time.monotonic(), refind=find
        )

    def type(self, step: str, text: str, *, clear: bool = True) -> None:
        ui.type_text(text, control=self.find(step), clear=clear)

    def wait(self, step: str, *, gone: bool = False, timeout: float = ui.DEFAULT_TIMEOUT) -> dict:
        return ui.wait_for(self._selector(step), self.ctx, gone=gone, timeout=timeout)

    def texts(self, *, limit: int = 300) -> list[str]:
        """Every visible name. A walk the app cut raises WalkCut, whose `hits`
        are the names read before the cut."""
        walk: dict = {}
        names = [c["name"] for c in ui.flatten(self.control, limit=limit, report=walk) if c["name"]]
        if not walk["complete"]:
            raise WalkCut(f"the text walk under {self.root!r} was cut; the app is busy", names)
        return names

    def contains(self, needle: str) -> bool:
        """True on a hit, even from a cut walk; False only from a complete
        one - a cut walk raises WalkCut, never answers "absent"."""
        needle = needle.casefold()
        try:
            texts = self.texts()
        except WalkCut as cut:
            if any(needle in text.casefold() for text in cut.hits):
                return True
            raise
        return any(needle in text.casefold() for text in texts)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "teardown" and STRAY_DIALOGS in item.stash:
        # pytest -rA prints a passed test's teardown sections only when the
        # name says "teardown".
        report.sections.append(
            ("desktoppilot stray dialogs (teardown)", _stray(item.stash[STRAY_DIALOGS]))
        )
    watch = item.stash.get(WINDOWS_EVENTS, None)
    if report.failed and report.when in ("setup", "call"):
        item.stash.setdefault(PHASE_FAILED, set()).add(report.when)
        if watch is not None:
            with contextlib.suppress(Exception):  # evidence never masks the failure
                report.sections.append(("desktoppilot windows events", _events_section(watch)))
    elif report.when == "teardown" and watch is not None and watch.found and not report.failed:
        # Allowed, or the test had already failed: the crash is reported, not re-failed.
        report.sections.append(("desktoppilot windows events (teardown)", _describe(watch.found)))
    # A fixture that fails in setup (the app never became ready, a document
    # did not open) needs the evidence as much as a failing test body does.
    if report.when not in ("setup", "call") or not report.failed:
        return
    name = item.config.getoption("--app")
    if not name:
        return
    # An exception in a hookwrapper is an INTERNALERROR that hides the test's own.
    try:
        result = artifacts.capture(
            item.nodeid, app_context(name), directory=item.config.getoption("--pilot-artifacts")
        )
    except Exception as exc:  # noqa: BLE001 - a bad --app, a broken profile
        report.sections.append(("desktoppilot artifacts", f"not captured: {exc}"))
        return
    report.sections.append(("desktoppilot artifacts", _summarize(result)))


def _stray(found: list[dict]) -> str:
    return "\n".join(
        f"left open in an adopted app: {d['title']!r}"
        if d["answered"] is None
        else f"closed after the test: {d['title']!r} (answered {d['answered']!r})"
        for d in found
    )


def _summarize(result: dict) -> str:
    lines = ["dir: " + result["dir"]]
    lines += ["  " + key + ": " + value for key, value in result["saved"].items()]
    lines += ["  " + key + ": FAILED - " + value for key, value in result["failed"].items()]
    return "\n".join(lines)
