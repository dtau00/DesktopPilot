"""The Word Application COM object, and the retry every call to it needs.

Everything that imports pythoncom or win32com lives here or in docs.py, so
the rest of the plugin is ordinary Python.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import pythoncom
import win32com.client

from desktoppilot.core import apps
from desktoppilot.core.errors import AppNotRunning
from desktoppilot.core.profiles import Profile
from desktoppilot.core.util import wait_until

# Word says "I'm busy, call back later" via these HRESULTs while it is
# painting, loading add-ins, or showing a modal. They are normal, not failures.
_BUSY_HRESULTS = {
    -2147418111,  # RPC_E_CALL_REJECTED
    -2147417846,  # RPC_E_SERVERCALL_RETRYLATER
    -2146959355,  # CO_E_SERVER_EXEC_FAILURE
}
# Not busy: RPC_E_SERVERFAULT (0x80010105) means the call threw inside Word,
# and retrying a non-idempotent call (Open, InsertAfter) would repeat it.

# "Word.Application.COMAddIns", "Word.Document.Paragraphs" - the dotted name
# pywin32 raises when late-bound dispatch cannot resolve a member. A sub-object
# it never managed to name reports as "<unknown>.Count", which is the same
# thing: a property get on a Word that is not answering yet.
_BUSY_ATTR_GET = re.compile(r"^(Word\.[A-Za-z0-9_]+|<unknown>)(\.[A-Za-z0-9_]+)+$")

# Quit tears the RPC channel down before the call returns, so a disconnect
# during shutdown means success, not failure.
_GONE_HRESULTS = {
    -2147023174,  # RPC_S_SERVER_UNAVAILABLE (0x800706BA)
    -2147417848,  # RPC_E_DISCONNECTED
    -2147023170,  # RPC_S_CALL_FAILED (0x800706BE)
}


def is_disconnected(exc: BaseException) -> bool:
    return isinstance(exc, pythoncom.com_error) and exc.hresult in _GONE_HRESULTS


def _is_busy(exc: BaseException) -> bool:
    if isinstance(exc, pythoncom.com_error):
        return exc.hresult in _BUSY_HRESULTS
    if not isinstance(exc, AttributeError):
        return False
    # pywin32's late-bound dispatch swallows the real com_error and re-raises
    # AttributeError instead, in two different shapes: "... can not be set" for
    # a property put, and the bare dotted name ("Word.Application.COMAddIns")
    # for a property get on a Word that has not finished starting.
    text = str(exc)
    if "can not be set" in text:
        return True
    # A misspelled attribute on a live Word object is indistinguishable from a
    # busy one here, so it now costs the retry timeout before it raises. That is
    # the cheaper mistake: the alternative is a fixture error on every slow start.
    return bool(_BUSY_ATTR_GET.match(text))


def com_retry[T](fn: Callable[[], T], *, timeout: float = 20.0, interval: float = 0.2) -> T:
    """Run a COM call, retrying only while Word reports itself busy."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised unless Word is busy
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(interval)


def com_set(obj, attr: str, value, *, timeout: float = 30.0, interval: float = 0.25) -> None:
    """Set a COM property, retrying while Word is still busy starting up."""
    com_retry(lambda: setattr(obj, attr, value), timeout=timeout, interval=interval)


def attach(profile: Profile | None = None, *, timeout: float = 60.0, reap: Callable | None = None):
    """The running Word's Application object - ours only: with `profile`, a
    Word this tool did not start or join raises NotOurWord, and no Word at all
    raises AppNotRunning at once instead of after `timeout`.

    With `reap`, startup prompts are answered while waiting: Word does not
    register for COM until they are gone, so otherwise this just times out
    with "Operation unavailable" while a modal sits on screen.
    """
    from . import session

    pythoncom.CoInitialize()
    owned = session.require_owned(profile) if profile is not None else None

    def try_attach():
        if reap is not None:
            reap()
        return win32com.client.GetActiveObject("Word.Application")

    try:
        app = wait_until(try_attach, timeout=timeout, what="Word COM object")
    except Exception as exc:  # noqa: BLE001 - reported with the blocking dialog
        raise AppNotRunning(f"cannot attach to Word: {exc}{_blocking(profile)}") from exc
    if owned is not None:
        session.check_com_owner(app, owned, apps.pids(profile))
    return app


def _blocking(profile: Profile | None) -> str:
    """Name the modal that is holding the object model, if there is one."""
    if profile is None:
        return ""
    from desktoppilot.core import dialogs

    pending = dialogs.dialog_windows(apps.pids(profile), classes=dialogs.dialog_classes(profile))
    if not pending:
        return ""
    return "; Word is showing a dialog: " + repr(
        [d["title"] + " - " + d["text"][:120] for d in pending]
    )
