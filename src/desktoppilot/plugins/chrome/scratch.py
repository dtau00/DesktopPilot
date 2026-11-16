"""A scratch Chrome: its own user data folder and debugging port, beside the
user's Chrome and never mistaken for it. `start` records it under the state
folder; `stop` takes that record, kills the pid's tree only and removes the
folder `start` made."""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import tempfile
from pathlib import Path

from desktoppilot.core import apps, cdp, paths, procs
from desktoppilot.core.errors import PilotError
from desktoppilot.core.profiles import Profile
from desktoppilot.core.util import wait_until

RECORDS = paths.STATE_DIR / "chrome-scratch"
START_TIMEOUT = 30.0
STOP_TIMEOUT = 20.0
FLAGS = [
    "--force-renderer-accessibility",
    "--no-first-run",
    "--no-default-browser-check",
]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _record(pid: int) -> Path:
    return RECORDS / f"{pid}.json"


def _page_up(port: int, proc) -> bool:
    """Whether the browser answers on the port with a page. A Chrome that
    exits first handed its window to another one on the same folder."""
    if proc.poll() is not None:
        raise PilotError(f"chrome exited at start with code {proc.returncode}")
    with contextlib.suppress(PilotError):
        return any(t.get("type") == "page" for t in cdp.list_targets(port, timeout=1))
    return False


def start(
    profile: Profile,
    *,
    port: int | None = None,
    url: str | None = None,
    directory: str | None = None,
    timeout: float = START_TIMEOUT,
) -> dict:
    """Start a scratch Chrome and wait until its port answers with a page."""
    temporary = directory is None
    folder = Path(tempfile.mkdtemp(prefix="pilot-chrome-")) if temporary else Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    url = url or "about:blank"
    flags = [*FLAGS, f"--user-data-dir={folder}", f"--remote-debugging-port={port}"]
    args = [str(profile.resolve_exe()), *dict.fromkeys([*profile.args, *flags]), url]
    log = folder / "pilot-chrome.log"
    proc = apps.start_process(args, apps.launch_env(profile), log)
    try:
        wait_until(
            lambda: _page_up(port, proc),
            timeout=timeout,
            what=f"the scratch Chrome on port {port}",
            fatal=(PilotError,),
        )
        browser = cdp.version(port).get("Browser")
    except BaseException:
        _kill_tree(proc.pid, apps.created(proc.pid))
        if temporary:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    record = {
        "pid": proc.pid,
        "created": apps.created(proc.pid),
        "port": port,
        "user_data_dir": str(folder),
        "temporary": temporary,
    }
    RECORDS.mkdir(parents=True, exist_ok=True)
    _record(proc.pid).write_text(json.dumps(record), encoding="utf-8")
    return {
        **record,
        "url": url,
        "browser": browser,
        "log": str(log),
        "bind": f"{apps.binding_env(profile)}={proc.pid}",
    }


def _kill_tree(pid: int, born: float | None) -> list[int]:
    """The pid's tree, killed and waited for; [] when the pid is not the
    process that was started (gone, or reused)."""
    if not apps.alive(pid, born):
        return []
    tree = sorted(apps.tree_of(pid))
    started = {p: apps.created(p) for p in tree}
    procs.taskkill([pid])
    wait_until(
        lambda: not [p for p in tree if apps.alive(p, started[p])],
        timeout=STOP_TIMEOUT,
        what=f"the scratch Chrome {pid} to exit",
    )
    return tree


def stop(pid: int) -> dict:
    """Kill a scratch Chrome this tool started, by its record: never a pid it
    did not start, never by image name."""
    path = _record(pid)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PilotError(
            f"pid {pid} is no scratch Chrome this tool started (no {path}); it is left alone"
        ) from None
    killed = _kill_tree(pid, record.get("created"))
    folder = Path(record["user_data_dir"])
    removed = False
    if record.get("temporary"):
        # Chrome's crash handler outlives the browser a moment, writing there.
        wait_until(
            lambda: shutil.rmtree(folder, ignore_errors=True) or not folder.exists(),
            timeout=STOP_TIMEOUT,
            what=f"{folder} to be removed",
        )
        removed = True
    path.unlink(missing_ok=True)
    return {
        "pid": pid,
        "killed": killed,
        "user_data_dir": str(folder),
        "removed": removed,
    }
