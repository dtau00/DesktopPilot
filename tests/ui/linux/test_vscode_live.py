"""The vscode plugin on a real VS Code: `git checkout` through the palette,
and `cdp`, against a scratch VS Code this module starts and kills.

The scratch is the snap's binary copied under its own name (code-pilotlive), so
`app launch` can never adopt the user's VS Code and `app kill` can never
reach it, with its own user data, extensions folder and debugging port. Keys
go only to it: the plugin brings its window to the front, and checks that it
is, before each palette burst.

The Codex tests run only when PILOT_CODEX_EXTENSIONS names an extensions
folder with openai.chatgpt installed and signed in; they read, never send.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SNAP_CODE = Path("/snap/code/current/usr/share/code")
REPO = "pilotlive"

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="VS Code on Linux"),
    pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs an X display"),
    pytest.mark.skipif(not (SNAP_CODE / "code").exists(), reason="needs the VS Code snap"),
]

SETTINGS = {
    "security.workspace.trust.enabled": False,
    "workbench.startupEditor": "none",
    "workbench.welcomePage.experimentalOnboarding": False,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoUpdate": False,
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def copy_vscode(root: Path) -> Path:
    """The snap's VS Code under the image name code-pilotlive: the binary
    copied, the rest linked. Its RPATH looks in $ORIGIN/../../lib."""
    code = root / "usr" / "share" / "code"
    code.mkdir(parents=True)
    for item in SNAP_CODE.iterdir():
        if item.name != "code":
            (code / item.name).symlink_to(item)
    (root / "usr" / "lib").symlink_to(SNAP_CODE.parent.parent / "lib")
    shutil.copy2(SNAP_CODE / "code", code / "code-pilotlive")
    return code / "code-pilotlive"


class Pilot:
    def __init__(self, app: str, env: dict):
        self.app, self.env = app, env
        self.exe = str(Path(sys.executable).parent / "pilot")

    def __call__(self, *args: str, ok: bool | None = True) -> dict:
        """The command's JSON; `ok` is whether it must succeed (None: either)."""
        ran = subprocess.run(
            [self.exe, self.app, "--no-shot", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert ok is None or (ran.returncode == 0) is ok, ran.stdout + ran.stderr
        return json.loads(ran.stdout)


@pytest.fixture(scope="module")
def vscode(tmp_path_factory):
    """(pilot, repo, port) for a running scratch VS Code; killed afterwards."""
    root = tmp_path_factory.mktemp("vscode")
    exe = copy_vscode(root)
    repo = root / REPO
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    for branch in ("feat-one", "feat-one-2"):
        git(repo, "branch", branch)
    user = root / "data" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps(SETTINGS))
    extensions = os.environ.get("PILOT_CODEX_EXTENSIONS") or str(root / "extensions")
    port = free_port()
    args = [
        "--force-renderer-accessibility",
        f"--remote-debugging-port={port}",
        "--no-sandbox",
        "--skip-welcome",
        "--disable-workspace-trust",
        f"--user-data-dir={root / 'data'}",
        f"--extensions-dir={extensions}",
        "--new-window",
        str(repo),
    ]
    profiles = root / "config" / "desktoppilot" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "vscodelive.toml").write_text(
        f'extends = "vscode"\nexe = [{json.dumps(str(exe))}]\nargs = {json.dumps(args)}\n'
        "window_size = [1400, 1000]\n"
    )
    pilot = Pilot("vscodelive", dict(os.environ, XDG_CONFIG_HOME=str(root / "config")))
    try:
        launched = pilot("app", "launch")
        assert launched["adopted"] is False
        pilot(
            "ui", "wait", "app//Button[name~='(Git) - main,']", "--depth", "40", "--timeout", "60"
        )
        yield pilot, repo, port
    finally:
        pilot("app", "kill", "--force", ok=None)


def test_checkout_switches_the_branch_and_reads_it_back(vscode):
    pilot, repo, _ = vscode
    done = pilot("git", "checkout", "feat-one", "--window", REPO)
    assert done["checked_out"] == "feat-one"
    assert done["verified"].startswith(f"{REPO} (Git) - feat-one")
    assert git(repo, "branch", "--show-current") == "feat-one"
    pilot("git", "checkout", "main", "--window", REPO)
    assert git(repo, "branch", "--show-current") == "main"


def test_checkout_of_a_missing_branch_runs_nothing(vscode):
    pilot, repo, _ = vscode
    failed = pilot("git", "checkout", "nope", "--window", REPO, ok=False)
    assert "no branch 'nope'" in failed["message"]
    assert git(repo, "branch", "--show-current") == "main"


def test_cdp_reads_the_workbench(vscode):
    pilot, _, port = vscode
    targets = pilot("cdp", "targets")
    assert targets["port"] == port
    assert pilot("cdp", "eval", "document.title")["value"].startswith(REPO)
    status = pilot("cdp", "read", ".statusbar-item-label[aria-label*='(Git)']")
    assert status["text"].strip() == "main"


codex_only = pytest.mark.skipif(
    not os.environ.get("PILOT_CODEX_EXTENSIONS"), reason="needs PILOT_CODEX_EXTENSIONS"
)


@codex_only
def test_codex_opens_a_panel_and_reads_the_model(vscode):
    pilot, _, _ = vscode
    opened = pilot("codex", "open", "--window", REPO)
    assert opened["new_chat"] is True
    model = pilot("codex", "model", "--window", REPO)
    assert model["model"] in model["offered"]
    assert model["effort"] is not None
