"""Processes over /proc in the shapes the Windows code reads: tasklist rows,
parents, creation times, and a kill that takes the child tree."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from desktoppilot.core import procs as core_procs
from desktoppilot.core.util import wait_until
from desktoppilot.linux import procs

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

ME = os.getpid()


def test_a_row_is_shaped_like_tasklist_csv():
    image = procs.image(ME)
    row = next(row for row in procs.tasklist(image) if core_procs.pid_of(row) == ME)

    assert row[0] == image == Path(os.readlink("/proc/self/exe")).name
    assert row[4].endswith(" K") and core_procs.working_set(row) > 0


def test_another_image_has_no_rows():
    assert procs.tasklist("no-such-image-anywhere") == []


def test_every_pid_has_its_parent_and_an_image():
    assert procs.parents()[ME] == os.getppid()
    assert procs.images_by_pid()[ME] == procs.image(ME)


def test_the_creation_time_is_in_epoch_seconds():
    born = procs.created(ME)
    assert born is not None and time.time() - 24 * 3600 * 365 < born <= time.time()


def test_a_pid_reused_after_the_start_is_not_alive():
    assert procs.alive(ME)
    assert procs.alive(ME, started=time.time())
    assert not procs.alive(ME, started=procs.created(ME) - 60)
    assert not procs.alive(None) and procs.created(2**22 + 1) is None


def test_kill_takes_the_child_tree():
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); child.wait()",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    child = int(parent.stdout.readline())

    procs.kill([parent.pid])

    parent.wait(timeout=10)
    wait_until(lambda: not procs.alive(child), timeout=10, what="the child to die")


def test_kill_sends_sigterm_first_and_sigkill_only_to_what_stays():
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    polite = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stubborn.stdout.readline()

    started = time.monotonic()
    procs.kill([stubborn.pid, polite.pid], grace=1.0)

    assert polite.wait(timeout=5) == -15  # SIGTERM was enough
    assert stubborn.wait(timeout=5) == -9  # SIGKILL after the grace
    assert time.monotonic() - started >= 1.0


def test_kill_never_signals_this_process_or_its_parent():
    procs.kill([os.getpid(), os.getppid()], grace=0)  # still here to assert
    assert procs.alive(os.getpid())


def test_ancestors_follow_the_parent_chain_and_stop_on_a_loop():
    assert procs.ancestors(5, {5: 4, 4: 3, 3: 4}) == {5, 4, 3}
    assert procs.ancestors() >= {ME, os.getppid()}
    assert procs.descendants([1], {2: 1, 3: 2, 4: 9}) == [1, 2, 3]


SNAP_ENV = {
    "HOME": "/home/me",
    "DISPLAY": ":10.0",
    "NO_AT_BRIDGE": "1",
    "GTK_PATH": "/snap/code/268/usr/lib/x86_64-linux-gnu/gtk-3.0",
    "GTK_MODULES": "gail:atk-bridge",
    "GIO_MODULE_DIR": "/home/me/snap/code/common/.cache/gio-modules",
    "LOCPATH": "/snap/code/268/usr/lib/locale",
    "SNAP": "/snap/code/268",
    "SNAP_NAME": "code",
    "XDG_DATA_DIRS": "/snap/code/268/usr/share:/usr/share",
    "XDG_DATA_DIRS_VSCODE_SNAP_ORIG": "/usr/local/share:/usr/share",
    "XDG_CONFIG_DIRS": "/snap/code/268/etc/xdg",
    "XDG_CONFIG_DIRS_VSCODE_SNAP_ORIG": "",
    "GDK_BACKEND_VSCODE_SNAP_ORIG": "x11",
}


def test_an_app_started_from_the_vs_code_snap_gets_the_desktops_environment():
    assert procs.app_env(SNAP_ENV) == {
        "HOME": "/home/me",
        "DISPLAY": ":10.0",
        "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
        "GDK_BACKEND": "x11",
    }


def test_outside_the_snap_the_environment_is_left_alone():
    plain = {k: v for k, v in SNAP_ENV.items() if not k.endswith("_VSCODE_SNAP_ORIG")}
    assert procs.app_env(plain) == plain


def test_electron_run_as_node_is_dropped_in_and_out_of_the_snap():
    plain = {"HOME": "/home/me", "ELECTRON_RUN_AS_NODE": "1"}
    assert procs.app_env(plain) == {"HOME": "/home/me"}
    snap = SNAP_ENV | {"ELECTRON_RUN_AS_NODE": "1"}
    assert "ELECTRON_RUN_AS_NODE" not in procs.app_env(snap)


def test_spawn_starts_a_new_session_and_logs_the_output(tmp_path):
    log = tmp_path / "logs" / "app.log"
    proc = procs.spawn(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        dict(os.environ),
        log,
    )
    assert proc.wait(timeout=10) == 0
    assert log.read_text().strip() == "True"
