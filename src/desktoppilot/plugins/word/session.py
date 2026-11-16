"""The Word half of the lifecycle: which Word is ours, crash leftovers, startup
prompts, add-in registration and readiness, the COM quit. Processes, windows
and the session file are core's.
"""

from __future__ import annotations

import contextlib
import ctypes
import winreg
from datetime import UTC, datetime

import win32gui
import win32process

from desktoppilot.core import apps
from desktoppilot.core import dialogs as dialogs_mod
from desktoppilot.core.errors import NoApp, PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext
from desktoppilot.core.util import wait_until

from . import com

WORD_WINDOW_CLASS = "OpusApp"
ADDIN_WAIT = 20.0

# Word parks crash leftovers here: disabled add-ins, the Document Recovery
# pane, "Word didn't shut down properly". All of it breaks a clean run.
RESILIENCY_KEY = r"Software\Microsoft\Office\16.0\Word\Resiliency"
RESILIENCY_SUBKEYS = ("DisabledItems", "DocumentRecovery", "StartupItems")

ADDIN_KEYS = (
    r"Software\Microsoft\Office\Word\Addins",
    r"Software\WOW6432Node\Microsoft\Office\Word\Addins",
)
# Word's own order: HKCU before HKLM, the 64-bit view before WOW6432Node.
ADDIN_HIVES = (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE))
LOAD_DISABLED = 2
LOAD_ENABLED = 3
# LoadBehavior bits: connected (1) and load at startup (2) - so 3 is "load it
# at startup", and 2 alone is what Word writes when that start failed. 16 is
# "connect the first time", which also loads at the next start.
LOAD_AT_STARTUP = 0x3
LOAD_FIRST_TIME = 0x10

# (text to look for, buttons to try in order[, Win32 button IDs to try when no
# label matches]). Anything not listed here is reported but left alone.
# Answering matters: "Yes" to the safe-mode prompt starts Word with add-ins
# disabled, which fails every add-in test for the wrong reason. The IDs are
# the same in every UI language, so an unverified label still gets its No.
SAFE_MODE_IDS = (dialogs_mod.IDNO, dialogs_mod.IDCANCEL)  # never IDYES
KNOWN_PROMPTS: tuple[tuple, ...] = (
    ("safe mode", ("No",), SAFE_MODE_IDS),
    # ja-JP: "セーフ モードで Word を起動しますか?"; its No button reads "いいえ(N)".
    ("セーフ", ("No", "いいえ(N)", "いいえ"), SAFE_MODE_IDS),
    ("couldn't start last time", ("No",), SAFE_MODE_IDS),
    ("didn't shut down properly", ("No", "Cancel")),
    ("recover", ("No", "Cancel")),
    ("last time you opened", ("No", "Cancel")),
)


# -------------------------------------------------------------- whose Word


class NotOurWord(NoApp):
    """Word is running, but not one this tool launched or joined."""


def _profile_and_name(ctx: AppContext | Profile) -> tuple[Profile, str]:
    # com.attach's callers hold only the profile; its name is the app's name.
    if isinstance(ctx, Profile):
        return ctx, ctx.name
    return ctx.profile, ctx.name


def owned_pids(ctx: AppContext | Profile, *, running: list[int] | None = None) -> list[int]:
    """The running Words this tool drives (apps.owned_pids)."""
    return apps.owned_pids(_profile_and_name(ctx)[0], running=running)


def require_owned(ctx: AppContext | Profile) -> list[int]:
    """owned_pids, or AppNotRunning / NotOurWord at once instead of a wait."""
    profile, name = _profile_and_name(ctx)
    running = apps.require_running(profile)
    owned = owned_pids(ctx, running=running)
    if not owned:
        raise NotOurWord(not_ours_message(name, running))
    return owned


def not_ours_message(name: str, running: list[int]) -> str:
    return (
        f"Word is running (pids {running}), but this tool did not start or join it as "
        f"{name!r}; run `pilot {name} app launch`, or `pilot {name} app attach` to join it"
    )


def word_frames(pids) -> list[dict]:
    """The visible OpusApp windows of `pids`, in z-order: {"hwnd", "pid", "title"}."""
    wanted = set(pids)
    found: list[dict] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != WORD_WINDOW_CLASS:
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if pid in wanted:
            found.append({"hwnd": hwnd, "pid": pid, "title": win32gui.GetWindowText(hwnd)})
        return True

    win32gui.EnumWindows(collect, None)
    return found


def documents_open_in(pid: int) -> list[str]:
    """Names of the documents a Word instance is showing, from its window titles.

    COM cannot answer this: only one Word registers in the running-object
    table, so the others cannot be asked what they hold. The frame title is the
    one thing an outside process can always read - "Contract - Word", or plain
    "Word" for an instance with nothing open.
    """
    names: list[str] = []
    for frame in word_frames([pid]):
        stem = frame["title"].removesuffix(" - Word").removesuffix("  -  Word").strip()
        if stem and stem != "Word":
            names.append(stem)
    return names


def check_com_owner(app, owned: list[int], running: list[int]) -> None:
    """GetActiveObject answers with whichever Word holds the running-object
    table entry. Raise NotOurWord unless that can only be one of `owned`."""
    foreign = [pid for pid in running if pid not in owned]
    if not foreign:
        return
    pid = com_owner_pid(app)
    if pid is not None:
        if pid in owned:
            return
        raise NotOurWord(
            f"Word's COM object is pid {pid}, a Word this tool did not start (ours: {owned}); "
            "close that Word"
        )
    # No window to tell by: only a Word that shows no document could be it.
    blank = [p for p in foreign if not documents_open_in(p)]
    if blank:
        raise NotOurWord(
            f"cannot tell which Word answers COM: ours ({owned}) has no document window, and "
            f"neither has pid(s) {blank}, which this tool did not start; close it"
        )


def com_owner_pid(app) -> int | None:
    """The pid behind a Word Application, from its first window; None without one."""
    try:
        if not com.com_retry(lambda: app.Windows.Count, timeout=5.0):
            return None
        hwnd = com.com_retry(lambda: app.Windows.Item(1).Hwnd, timeout=5.0)
        return win32process.GetWindowThreadProcessId(int(hwnd))[1]
    except Exception:  # noqa: BLE001 - unknown; the caller decides what that means
        return None


# --------------------------------------------------------- crash leftovers


def clear_resiliency() -> list[str]:
    """Delete Word's crash-leftover keys so the next start has no recovery UI."""
    cleared: list[str] = []
    for name in RESILIENCY_SUBKEYS:
        try:
            _delete_tree(winreg.HKEY_CURRENT_USER, RESILIENCY_KEY + "\\" + name)
            cleared.append(name)
        except FileNotFoundError:
            pass
    return cleared


def _delete_tree(hive: int, path: str) -> None:
    with winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS) as key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_tree(hive, path + "\\" + child)
    winreg.DeleteKey(hive, path)


# ------------------------------------------------------ add-in registration


def registrations(name: str, *, hives=ADDIN_HIVES, values=("LoadBehavior",)):
    """Each key that registers the add-in `name` for Word, in Word's order:
    {"key", "hive", "path", and each of `values`: its data, None when absent}."""
    for hive_name, hive in hives:
        for base in ADDIN_KEYS:
            path = base + "\\" + name
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as handle:
                    found = {value: _query(handle, value) for value in values}
            except FileNotFoundError:
                continue
            yield {"key": hive_name + "\\" + path, "hive": hive, "path": path, **found}


def _query(handle, name: str):
    try:
        return winreg.QueryValueEx(handle, name)[0]
    except FileNotFoundError:
        return None


def load_behavior(name: str) -> list[dict]:
    """Every Word registration of the add-in `name`: [{"key", "load_behavior"}]."""
    return [{"key": r["key"], "load_behavior": r["LoadBehavior"]} for r in registrations(name)]


def effective_load_behavior(name: str) -> int | None:
    """The LoadBehavior Word acts on: the first registration's."""
    return next((r["LoadBehavior"] for r in registrations(name)), None)


def expected_at_startup(load_behavior: int | None) -> bool:
    """Whether Word connects an add-in with this LoadBehavior when it starts:
    the one meaning of "enabled". 0 (do not load), 2 (disabled after a failed
    start), 8/9 (on demand) and no registration at all are not."""
    if load_behavior is None:
        return False
    return load_behavior & LOAD_AT_STARTUP == LOAD_AT_STARTUP or bool(
        load_behavior & LOAD_FIRST_TIME
    )


def reset_load_behavior(name: str, *, key: str | None = None) -> list[dict]:
    """Put a LoadBehavior Word set to 2 back to 3. Word writes 2 when an
    add-in throws at startup and then never loads it again; clearing
    Resiliency does not undo that. HKLM is written only by an elevated
    process - otherwise it is reported, not reset. With `key` (as load_behavior
    names it) only that registration is touched.
    Returns one {"key", "before", "after", "reset"[, "reason"]} per registration."""
    report: list[dict] = []
    for found in registrations(name):
        if key is not None and found["key"].casefold() != key.casefold():
            continue
        before = found["LoadBehavior"]
        entry = {"key": found["key"], "before": before, "after": before, "reset": False}
        if before == LOAD_DISABLED:
            if found["hive"] == winreg.HKEY_LOCAL_MACHINE and not _is_admin():
                entry["reason"] = "HKLM needs an elevated process; run it as admin"
            else:
                with winreg.OpenKey(found["hive"], found["path"], 0, winreg.KEY_SET_VALUE) as h:
                    winreg.SetValueEx(h, "LoadBehavior", 0, winreg.REG_DWORD, LOAD_ENABLED)
                entry |= {"after": LOAD_ENABLED, "reset": True}
        report.append(entry)
    return report


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - no answer is no elevation
        return False


# ----------------------------------------------------------------- prompts


def reap_prompts(profile: Profile) -> list[dict]:
    """Answer the startup prompts. Word does not register for COM until they
    are gone, so this runs before every attach during a launch."""
    return dialogs_mod.reap(
        apps.pids(profile), KNOWN_PROMPTS, classes=dialogs_mod.dialog_classes(profile)
    )


class ForeignWordOpen(PilotError):
    """A Word this tool did not launch has a document open, so killing every
    Word would destroy someone else's unsaved work."""


def refuse_foreign_word(ctx: AppContext, *, pids: list[int], force: bool) -> None:
    """taskkill takes the whole image name, so it cannot spare one instance -
    which is why killing Word is refused outright while a Word this tool did
    not LAUNCH is holding a document: a foreign one, and an adopted one too,
    since the graceful quit marks every document saved and so discards the
    user's work. An orphan from an interrupted run is still killed: its pid
    is the one in the session file."""
    if force:
        return
    profile = apps.profile_of(ctx)
    saved = apps.Session.load(profile)
    launched = saved.pid if saved is not None and saved.launched else None
    adopted = set() if launched is not None else set(apps.owned_pids(profile, running=pids))
    held = {pid: documents_open_in(pid) for pid in pids if pid != launched}
    held = {pid: docs for pid, docs in held.items() if docs}
    if not held:
        return
    listing = "; ".join(
        f"pid {pid}{' (adopted)' if pid in adopted else ''}: " + ", ".join(names)
        for pid, names in sorted(held.items())
    )
    raise ForeignWordOpen(
        "refusing to kill Word: an instance this tool did not launch has documents "
        f"open ({listing}); quitting it would discard their unsaved changes. Close "
        "them, or pass --force to kill every Word anyway."
    )


# --------------------------------------------------------- connected add-ins


def addins(app) -> list[dict]:
    """Word's COM add-ins: {"progid", "description", "connected", "load_behavior"}."""
    items: list[dict] = []
    for addin in com.com_retry(lambda: app.COMAddIns):
        progid = com.com_retry(lambda a=addin: a.ProgId)
        items.append(
            {
                "progid": progid,
                "description": com.com_retry(lambda a=addin: a.Description),
                "connected": bool(com.com_retry(lambda a=addin: a.Connect)),
                "load_behavior": effective_load_behavior(progid) if progid else None,
            }
        )
    return items


def find_addin(loaded: list[dict], name: str) -> dict | None:
    """The add-in whose ProgId is `name` - exactly: a stale `Name.ThisAddIn`
    registration is another add-in, not this one."""
    wanted = name.casefold()
    return next((a for a in loaded if (a["progid"] or "").casefold() == wanted), None)


def not_connected(loaded: list[dict]) -> list[dict]:
    """[{"progid", "load_behavior"}] for each add-in that is not connected."""
    return [
        {"progid": a["progid"], "load_behavior": a.get("load_behavior")}
        for a in loaded
        if not a["connected"]
    ]


def wait_for_addins(app, *, timeout: float = ADDIN_WAIT, require: str | None = None) -> list[dict]:
    """Wait until the COM add-ins Word loads at startup report connected. VSTO
    add-ins connect a beat after Word's window appears; clicking their ribbon
    before that fails. One whose LoadBehavior does not ask for a startup load
    (0, a disabled 2, on demand) never connects by itself, so it is not waited
    for - not_connected still names it, with its LoadBehavior.

    It does not raise when they stay down: the list it returns then holds
    connected False, and a caller that needs one add-in checks it. `require`
    waits for that add-in (by exact ProgId) alone, whatever its LoadBehavior.
    Only when no read of the add-ins succeeded at all does it raise."""
    if timeout <= 0:
        return addins(app)
    last: list[list[dict]] = []

    def ready() -> tuple | None:
        loaded = addins(app)
        last[:] = [loaded]
        if require is not None:
            hit = find_addin(loaded, require)
            done = bool(hit and hit["connected"])
        else:
            done = all(a["connected"] for a in loaded if expected_at_startup(a["load_behavior"]))
        # A tuple: a Word with no add-ins at all ([]) is ready, not falsy.
        return (loaded,) if done else None

    try:
        return wait_until(ready, timeout=timeout, what="COM add-ins to connect")[0]
    except Exception:  # noqa: BLE001 - a stubborn add-in should not fail launch
        if last:
            return last[0]
        raise


# -------------------------------------------------------------------- quit


def quit_word(profile: Profile, *, save: bool = False, timeout: float = 15.0) -> str:
    """Graceful Quit, so the next start has no recovery pane. Core force-kills
    whatever is still running afterwards."""
    app = com.attach(profile, timeout=5.0)
    com.com_set(app, "DisplayAlerts", 0)  # wdAlertsNone
    if not save:
        # Telling Word each document is already saved is what actually
        # suppresses the "do you want to save?" block on Quit.
        for i in range(com.com_retry(lambda: app.Documents.Count), 0, -1):
            with contextlib.suppress(Exception):  # a doc that vanished is fine
                com.com_set(com.com_retry(lambda i=i: app.Documents.Item(i)), "Saved", True)
    try:
        com.com_retry(lambda: app.Quit(SaveChanges=-1 if save else 0))
    except Exception as exc:  # noqa: BLE001
        if not com.is_disconnected(exc):
            raise
    wait_until(lambda: not apps.pids(profile), timeout=timeout, what="Word to quit")
    return "quit"


# ------------------------------------------------------------------ status


def started_at(ctx: AppContext | Profile, *, owned: list[int] | None = None) -> datetime | None:
    """When our Word started, in local time as the add-in logs it, so a caller
    can scope an add-in's log to this session - the add-in appends to one
    file across restarts. None when no Word of ours is running."""
    pids = owned_pids(ctx) if owned is None else owned
    starts = [born for pid in pids if (born := apps.created(pid)) is not None]
    if not starts:
        return None
    return datetime.fromtimestamp(min(starts), tz=UTC).astimezone().replace(tzinfo=None)


def status_extras(ctx: AppContext) -> dict:
    """Our Word's add-ins and open documents, for `app status` and `state`.
    A Word this tool does not own is listed in `not_owned`, never asked."""
    profile = apps.profile_of(ctx)
    running = apps.pids(profile)
    if not running:
        return {}
    owned = owned_pids(ctx, running=running)
    extras: dict = {
        "documents_open": {pid: documents_open_in(pid) for pid in owned},
        "started_at": started_at(ctx, owned=owned),
    }
    if foreign := [pid for pid in running if pid not in owned]:
        extras["not_owned"] = foreign
    if not owned:
        extras["com_error"] = not_ours_message(ctx.name, running)
        return extras
    try:
        app = com.attach(profile, timeout=5.0)
        extras["addins"] = addins(app)
        extras["addins_not_connected"] = not_connected(extras["addins"])
        extras["word"] = {
            "version": com.com_retry(lambda: app.Version),
            "visible": bool(com.com_retry(lambda: app.Visible)),
            "documents": com.com_retry(lambda: app.Documents.Count),
            "protected_view_windows": com.com_retry(lambda: app.ProtectedViewWindows.Count),
        }
    except Exception as exc:  # noqa: BLE001 - a busy or dialog-blocked Word still has a status
        extras["com_error"] = str(exc)
    return extras
