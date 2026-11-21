"""Processes over /proc, in the shapes the core's Windows code uses: rows
like `tasklist /FO CSV` ([image, pid, session name, session number, "N K"]),
a kill that takes the child tree like `taskkill /T` (SIGTERM, then SIGKILL),
the parent of every pid, and creation times in epoch seconds."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterable
from pathlib import Path

PROC = Path("/proc")
_DELETED = " (deleted)"


def _pids() -> list[int]:
    return [int(entry.name) for entry in PROC.iterdir() if entry.name.isdecimal()]


def _stat(pid: int) -> list[str] | None:
    """/proc/PID/stat from field 3 (state) on: the comm before it is in
    parentheses and may hold spaces and parentheses itself."""
    try:
        text = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    return text[text.rindex(")") + 2 :].split()


def image(pid: int) -> str | None:
    """The basename of the process's executable, as tasklist's image name.
    /proc/PID/exe is unreadable for another user's process and empty for a
    kernel thread; comm (cut to 15 characters) stands in then."""
    try:
        return Path(os.readlink(PROC / str(pid) / "exe").removesuffix(_DELETED)).name
    except OSError:
        pass
    try:
        return (PROC / str(pid) / "comm").read_text().strip() or None
    except OSError:
        return None


def _rss_kb(pid: int) -> int:
    try:
        for line in (PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _row(pid: int, name: str) -> list[str] | None:
    stat = _stat(pid)
    if stat is None:
        return None
    # Field 6 is the session id; there is no session name to match tasklist's.
    return [name, str(pid), "", stat[3], f"{_rss_kb(pid)} K"]


def tasklist(image_name: str) -> list[list[str]]:
    """The rows of one image. Case-sensitive: Linux file names are."""
    rows = []
    for pid in _pids():
        if image(pid) == image_name:
            row = _row(pid, image_name)
            if row is not None:
                rows.append(row)
    return rows


def images_by_pid() -> dict[int, str]:
    """The image name of every running process that has one."""
    found = {}
    for pid in _pids():
        name = image(pid)
        if name:
            found[pid] = name
    return found


def parents() -> dict[int, int]:
    """Parent pid of every process."""
    found = {}
    for pid in _pids():
        stat = _stat(pid)
        if stat is not None:
            found[pid] = int(stat[1])
    return found


def command_line(pid: int) -> str | None:
    """The process's arguments joined by spaces (/proc/PID/cmdline is
    NUL-separated); None when it is gone or unreadable, "" for a kernel
    thread."""
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return " ".join(arg.decode(errors="replace") for arg in raw.split(b"\0") if arg)


def _boot_time() -> float:
    for line in (PROC / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise OSError("no btime in /proc/stat")


def created(pid: int) -> float | None:
    """When the process started, in epoch seconds; None if it is gone.
    Field 22 counts clock ticks since boot."""
    stat = _stat(pid)
    if stat is None:
        return None
    try:
        return _boot_time() + int(stat[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def alive(pid: int | None, started: float | None = None) -> bool:
    """Running and not a zombie, and - when `started` is known - created no
    later than it, so a reused pid does not count."""
    if not pid:
        return False
    stat = _stat(pid)
    if stat is None or stat[0] in ("Z", "X"):
        return False
    if started is None:
        return True
    born = created(pid)
    return born is None or born <= started + 1.0


SNAP_ORIG = "_VSCODE_SNAP_ORIG"
ELECTRON_AS_NODE = "ELECTRON_RUN_AS_NODE"
# What the VS Code snap sets for its own GTK and Electron, and every process
# started from its terminal inherits: with NO_AT_BRIDGE a GTK app never
# joins AT-SPI, and with the snap's GTK and GIO paths some apps crash.
SNAP_LEAKS = frozenset(
    {
        "NO_AT_BRIDGE",
        "ELECTRON_NO_ATTACH_CONSOLE",
        "GTK_PATH",
        "GTK_EXE_PREFIX",
        "GTK_MODULES",
        "GTK_IM_MODULE_FILE",
        "GDK_PIXBUF_MODULE_FILE",
        "GDK_PIXBUF_MODULEDIR",
        "GIO_MODULE_DIR",
        "GSETTINGS_SCHEMA_DIR",
        "LOCPATH",
    }
)


def app_env(environ: dict[str, str]) -> dict[str, str]:
    """The environment for an app this tool starts. Under the VS Code snap
    (it leaves `X_VSCODE_SNAP_ORIG` copies behind) its leaks and the SNAP_*
    variables are dropped, and every X with an ORIG copy gets that copy back:
    an empty one means X was unset. Anywhere else, `environ` as it is.
    `ELECTRON_RUN_AS_NODE` goes either way: VS Code's terminal and extension
    host set it, and an Electron app started with it runs as plain Node."""
    env = _snap_free(environ)
    env.pop(ELECTRON_AS_NODE, None)
    return env


def _snap_free(environ: dict[str, str]) -> dict[str, str]:
    if not any(key.endswith(SNAP_ORIG) for key in environ):
        return dict(environ)
    env = {
        key: value
        for key, value in environ.items()
        if key not in SNAP_LEAKS and key != "SNAP" and not key.startswith("SNAP_")
    }
    for key, value in environ.items():
        if key.endswith(SNAP_ORIG):
            name = key.removesuffix(SNAP_ORIG)
            del env[key]
            if value:
                env[name] = value
            else:
                env.pop(name, None)
    return env


def spawn(args: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    """Start an app in a session of its own, so the pilot's exit or a Ctrl+C
    in its terminal does not take the app along. Its stdout and stderr go to
    `log`, rewritten on each start; DISPLAY comes with `env`."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:
        return subprocess.Popen(
            args,
            env=env,
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def descendants(pids: Iterable[int], tree: dict[int, int] | None = None) -> list[int]:
    """The pids and every process below them, parents first."""
    tree = parents() if tree is None else tree
    found = list(dict.fromkeys(pids))
    for pid in found:
        found += [child for child, parent in tree.items() if parent == pid and child not in found]
    return found


def ancestors(pid: int | None = None, tree: dict[int, int] | None = None) -> set[int]:
    """The process (default: this one) and its parent chain."""
    tree = parents() if tree is None else tree
    pid = os.getpid() if pid is None else pid
    chain = {pid}
    current = tree.get(pid)
    while current and current not in chain:
        chain.add(current)
        current = tree.get(current)
    return chain


# How long a SIGTERM has before SIGKILL follows: what the app gets to save
# its state and remove its lock files, as Windows gives WM_ENDSESSION.
KILL_GRACE = 5.0


def _signal_all(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


def _stop(pids: list[int], grace: float) -> None:
    """SIGTERM, then SIGKILL what is still there after `grace` seconds; 0
    sends SIGKILL at once. This process and its ancestors are never
    signalled. A pid is followed by its creation time, so one reused
    meanwhile is not killed."""
    from ..core.errors import Timeout
    from ..core.util import wait_until

    spared = ancestors()
    born = {pid: created(pid) for pid in pids if pid not in spared}
    if grace <= 0:
        _signal_all(born, signal.SIGKILL)
        return

    def left() -> list[int]:
        return [pid for pid, started in born.items() if alive(pid, started)]

    _signal_all(born, signal.SIGTERM)
    with contextlib.suppress(Timeout):
        wait_until(lambda: not left(), timeout=grace, interval=0.1, what="SIGTERM")
    _signal_all(left(), signal.SIGKILL)


def kill(pids: Iterable[int], grace: float = KILL_GRACE) -> None:
    """Each pid and its child tree, as `taskkill /T`: SIGTERM first, SIGKILL
    after `grace`. Never raises, as taskkill."""
    _stop(descendants(pids), grace)


def kill_images(images: Iterable[str], grace: float = KILL_GRACE) -> None:
    """Every process of each image, SIGTERM then SIGKILL. Never raises."""
    wanted = set(images)
    _stop([pid for pid, name in images_by_pid().items() if name in wanted], grace)
