"""What the `chrome` app adds: the cdp and scratch groups."""

from __future__ import annotations

import typer

from desktoppilot.core import cmd_cdp
from desktoppilot.core.commands import app_profile, run
from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import Registry

from . import scratch


def register(reg: Registry, profile: Profile) -> None:
    reg.group("cdp", cmd_cdp.cdp_group(), for_app=profile.name)
    reg.group("scratch", scratch_group(), for_app=profile.name)


def scratch_group() -> typer.Typer:
    group = typer.Typer(
        help="A scratch Chrome beside the user's: its own user data folder and CDP port."
    )

    @group.command("start")
    def scratch_start(
        ctx: typer.Context,
        port: int = typer.Option(None, help="The debugging port. Default: a free one."),
        url: str = typer.Option(None, help="The page to open. Default: about:blank."),
        directory: str = typer.Option(
            None, "--dir", help="The user data folder, kept at stop. Default: a temporary one."
        ),
        timeout: float = typer.Option(
            scratch.START_TIMEOUT, help="Seconds for the port to answer with a page."
        ),
    ):
        """Start Chrome on its own user data folder with --remote-debugging-port, --force-renderer-accessibility, no first-run UI, and the environment app launch gives; wait until the port answers with a page. Bind the app commands to it with `bind` (export PILOT_CHROME_PID=<pid>). JSON: pid, created, port, user_data_dir, temporary (start made the folder; stop removes it), url, browser, log, bind."""
        run(
            lambda: scratch.start(
                app_profile(ctx), port=port, url=url, directory=directory, timeout=timeout
            )
        )

    @group.command("stop")
    def scratch_stop(pid: int = typer.Argument(..., help="The pid scratch start printed.")):
        """Kill a scratch Chrome that scratch start started: that pid's process tree only, never by image name; remove its user data folder if start made it. JSON: pid, killed (pids), user_data_dir, removed."""
        run(lambda: scratch.stop(pid))

    return group
