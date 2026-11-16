"""`pilot <app> app`: launch, attach, status, kill and resize the app."""

from __future__ import annotations

import typer

from . import apps
from .commands import app_context, run
from .errors import PilotError
from .native import win32gui

EXTRA_ARGS = typer.Argument(None, help="Extra arguments passed to the app.")


def resize_size(
    width: int | None, height: int | None, width_opt: int | None, height_opt: int | None
) -> list[int] | None:
    """WIDTH HEIGHT from the arguments or --width/--height (not both forms
    for one side); None for the profile's size."""
    if width is not None and width_opt is not None and width != width_opt:
        raise PilotError("WIDTH given twice: as an argument and as --width")
    if height is not None and height_opt is not None and height != height_opt:
        raise PilotError("HEIGHT given twice: as an argument and as --height")
    width = width if width is not None else width_opt
    height = height if height is not None else height_opt
    if (width is None) != (height is None):
        raise PilotError("give both WIDTH and HEIGHT, or neither for the profile's size")
    return [width, height] if width is not None else None


def app_group() -> typer.Typer:
    group = typer.Typer(help="App lifecycle: launch, attach, status, kill, resize.")

    @group.command("launch")
    def app_launch(
        ctx: typer.Context,
        args: list[str] = EXTRA_ARGS,
        fresh: bool = typer.Option(
            None,
            "--fresh/--adopt",
            help="Kill a running instance first, or join it. Default: the profile's.",
        ),
        pin_size: bool = typer.Option(
            True, help="Resize to the profile's window_size (DIPs, scaled by the DPI)."
        ),
    ):
        """Start the app, or adopt a running one. Bound (PILOT_<APP>_PID set), it only adopts that instance. JSON: app, profile, running, pids, working_set_mb, session, session_error, status_errors, bound_pid, bound_by, window, windows (as app status), adopted, killed, hwnd, ready, layout (the size it pinned; null when adopted or not pinned)."""
        run(
            lambda: apps.launch(
                app_context(ctx), extra_args=list(args or []), fresh=fresh, pin_size=pin_size
            )
        )

    @group.command("attach")
    def app_attach(ctx: typer.Context):
        """Record a session for an already-running app. JSON: app, profile, running, pids, working_set_mb, session, session_error, status_errors, bound_pid, bound_by, window, windows (as app status), hwnd."""
        run(lambda: apps.attach(app_context(ctx)))

    @group.command("status")
    def app_status(ctx: typer.Context):
        """Is it running, which pids, how much memory, which windows, and the main window's size in DIPs at the display's current scale. With PILOT_<APP>_PID set (APP: the profile name upper-cased, - as _), every app command sees that pid and its descendants only. JSON: app, profile, running, pids, working_set_mb (per pid), session, session_error, status_errors, bound_pid (the binding, or null), bound_by (the variable that set it), window, windows; a plugin adds its own."""
        run(lambda: apps.status(app_context(ctx)))

    @group.command("kill")
    def app_kill(
        ctx: typer.Context,
        force: bool = typer.Option(
            False,
            help="Kill at once, skipping the graceful quit - even an instance this tool "
            "did not start, documents and all.",
        ),
    ):
        """Quit hooks, then force-kill. Destroys unsaved work. Bound (PILOT_<APP>_PID), only that instance's tree. JSON: killed (pids), quit_errors."""
        run(lambda: apps.kill(app_context(ctx), force=force))

    @group.command("resize")
    def app_resize(
        ctx: typer.Context,
        width: int = typer.Argument(
            None, help="Width in DIPs (96-DPI pixels). Default: the profile's window_size."
        ),
        height: int = typer.Argument(None, help="Height in DIPs. Default: the profile's."),
        width_opt: int = typer.Option(None, "--width", help="WIDTH, as an option."),
        height_opt: int = typer.Option(None, "--height", help="HEIGHT, as an option."),
        pid: int = typer.Option(
            None, "--pid", help="The main window of this pid's process tree, not the largest."
        ),
        hwnd: int = typer.Option(None, "--hwnd", help="This window exactly (an X window id)."),
    ):
        """Pin the main window's size so selectors behave the same everywhere. The size is in DIPs, scaled by the display's DPI and cut to the work area. The window: --hwnd, else the main window of --pid's tree, else the largest main window of the app (of the bound instance with PILOT_<APP>_PID). JSON: hwnd, dpi, scale, requested_dips, physical, dips, clamped, size_differs, work_area."""

        def body():
            actx = app_context(ctx)
            profile = apps.profile_of(actx)
            size = resize_size(width, height, width_opt, height_opt) or profile.window_size
            if not size:
                raise PilotError(f"profile {profile.name!r} has no window_size; pass WIDTH HEIGHT")
            if pid is not None and hwnd is not None:
                raise PilotError("give --pid or --hwnd, not both")
            if hwnd is not None:
                if not win32gui.IsWindow(hwnd):
                    raise PilotError(f"no window {hwnd}")
                target = hwnd
            else:
                target = apps.main_hwnd(profile, pid=pid)
            return {"hwnd": target, **apps.resize(target, *size)}

        run(body)

    return group
