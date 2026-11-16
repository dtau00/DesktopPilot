"""The generic command groups, built fresh for each app, and how every command
runs: JSON out, errors as JSON and exit 1.

Nothing here is app-specific: `build_app_cli` is called once per profile and
once per plugin, and the app it belongs to travels in the Typer context, so no
command has to look up "which app am I attached to". The groups live in
cmd_app, cmd_ui, cmd_image, cmd_record and cmd_misc.
"""

from __future__ import annotations

import json
import re
from typing import Any

import typer

from . import apps, fallback
from .errors import PilotError
from .profiles import Profile
from .registry import AppContext, Registry

SELECTOR_HELP = (
    # Escaped: Typer's help is Rich markup, which would eat [name='X'] as a tag.
    "root//Type\\[name='X']\\[id='Y']/Type\\[name~='partial']#2. "
    "Core roots: app, win:NAME, focus, dialog, desktop; a plugin adds its own, see its "
    "NOTES.md. Values and a root argument holding '/' take '...' or \"...\": "
    'win:"Find/Replace"//Button.'
)


def emit(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def run(fn, *, ctx: AppContext | None = None, command: str | None = None):
    """Run a command body, turning every error into JSON + exit code 1.

    Our own errors print as they are; anything else - a COMError, a
    pywintypes.error, a bug - is marked `internal`, never a traceback.
    `ctx` and `command` opt the call into the fallback rule: a failure then
    leaves a screenshot behind and says where.
    """
    try:
        emit(fn())
    except (typer.Exit, typer.Abort):
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is JSON, never a traceback
        payload = {"error": type(exc).__name__, "message": str(exc)}
        # An error that carries structured findings (a read-back that did not
        # match) hands them over as `details`, next to the message.
        if isinstance(exc, PilotError) and isinstance(getattr(exc, "details", None), dict):
            payload["details"] = exc.details
        if not isinstance(exc, PilotError):
            payload["internal"] = True
        if ctx is not None and command is not None:
            payload |= fallback.on_failure(ctx, command)
        emit(payload)
        raise typer.Exit(1) from exc


def run_for(ctx: typer.Context, command: str, body):
    """run() with the fallback rule on, for a command that acts on the app:
    a failure leaves a screenshot. `command` names it in the journal."""
    run(body, ctx=app_context(ctx), command=command)


def app_context(ctx: typer.Context) -> AppContext:
    return ctx.obj


def app_profile(ctx: typer.Context) -> Profile:
    """The profile of the app the command acts on; one with none (desktop)
    fails, since it owns no process."""
    return apps.profile_of(app_context(ctx))


def build_app_cli(app_name: str, profile: Profile | None, reg: Registry) -> typer.Typer:
    """One `pilot <app> ...` sub-tree: the core groups plus the plugin's own."""
    # Imported here: the group modules import run and its friends from this one.
    from .cmd_app import app_group
    from .cmd_image import image_group
    from .cmd_misc import add_single_commands, clip_group, events_group
    from .cmd_record import record_group
    from .cmd_ui import ui_group

    root = typer.Typer(add_completion=False, help=app_help(app_name, profile))

    @root.callback()
    def _enter(
        ctx: typer.Context,
        no_shot: bool = typer.Option(
            False, "--no-shot", help="Do not screenshot the app when a call fails."
        ),
    ):
        ctx.obj = AppContext(
            name=app_name, profile=profile, registry=reg, shot_on_failure=not no_shot
        )

    if profile is not None:
        root.add_typer(app_group(), name="app")
    root.add_typer(ui_group(profile), name="ui")
    root.add_typer(clip_group(), name="clip")
    root.add_typer(image_group(), name="image")
    root.add_typer(events_group(profile), name="events")
    root.add_typer(record_group(), name="record")
    add_single_commands(root)

    for name, group in reg.groups_for(app_name).items():
        root.add_typer(group, name=name)
    return root


def app_help(app_name: str, profile: Profile | None) -> str:
    """The first sentence of the profile's notes, which `pilot --help` lists,
    then the rest for the app's own page; "Drive X." when there are none."""
    notes = (profile.notes or "").strip() if profile else ""
    if not notes:
        return f"Drive {app_name}."
    first, *rest = re.split(r"(?<=[.!?])\s+", notes, maxsplit=1)
    return "\n\n".join([first, *rest])


def usage(message: str):
    """A bad combination of arguments: JSON like every other error, not
    Typer's usage text and exit 2."""
    raise PilotError(message)
