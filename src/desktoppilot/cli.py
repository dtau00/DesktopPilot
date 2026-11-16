"""pilot CLI: `pilot <app> <group> <command>`.

The tree is built at start from the shipped profiles and the plugins, so every
call names the app it acts on and no command depends on hidden attach state.
Every command prints JSON on stdout so an agent can parse it.
"""

from __future__ import annotations

import sys

import typer

from .core import profiles
from .core.commands import build_app_cli, run
from .core.errors import PilotError
from .core.registry import Registry, discover

NULL_APP = "desktop"
EPILOG = (
    "Every command prints JSON on stdout, in UTF-8; set PYTHONIOENCODING=utf-8 for pytest "
    'and for scripts that import the package. A failure prints {"error": KIND, "message": '
    "...} and exits 1; one that acted on the app also gives `screenshot`, the path of a shot "
    "of its window (pilot APP --no-shot ... turns that off). Each plugin's notes for an "
    f"agent: {profiles.PLUGINS_DIR / 'NAME' / 'NOTES.md'}; the rules are in the README."
)


def epilog(reg: Registry) -> str:
    """The help epilog; an external plugin that loaded has its NOTES.md named,
    since it lives in another package."""
    external = [
        f"{name}: {plugin.folder / 'NOTES.md'}"
        for name, plugin in profiles.external_plugins()[0].items()
        if name in reg.plugins
    ]
    if not external:
        return EPILOG
    return f"{EPILOG} External plugins' notes: {'; '.join(external)}."


def app_sources(reg: Registry) -> dict[str, str]:
    """Every app name that loads, and where it came from: profile, plugin or
    builtin. One that does not load is in `reg.broken` instead, one for
    another platform in `reg.unsupported`."""
    found = {NULL_APP: "builtin"}
    for name in reg.profiles:
        found[name] = "plugin" if name in reg.plugins else "profile"
    return dict(sorted(found.items()))


def _add_broken(root: typer.Typer, name: str, message: str, *, kind: str = "Broken") -> None:
    """Any command under a profile that does not load, or is not for this
    platform, prints its error as JSON."""

    @root.command(
        name,
        help=f"{kind} profile: {message}",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        add_help_option=False,
    )
    def _broken(ctx: typer.Context):
        def body():
            raise PilotError(message)

        run(body)


def _adhoc_exe(argv: list[str], taken: set[str]) -> str | None:
    """The app token, if it names an existing executable (.exe on Windows)
    rather than a known app."""
    token = next((arg for arg in argv if not arg.startswith("-")), None)
    if token is None or token in taken:
        return None
    return token if profiles.adhoc_exe(token) is not None else None


def build(argv: list[str] | None = None) -> typer.Typer:
    """The command tree, one sub-app per app name.

    Typer needs every sub-app registered before it parses, and an ad-hoc app
    (an existing executable path, which profiles.load accepts) has no name to
    list in advance - so argv is peeked for it and only that one path is
    registered, under the exact token typed. Its app name is the exe's stem
    (on Linux its file name), because the name ends up in session and mark
    file names and a path cannot.
    """
    argv = sys.argv[1:] if argv is None else argv
    reg = Registry()
    discover(reg)

    root = typer.Typer(
        add_completion=False,
        help="Drive a Windows or Linux (X11) desktop app for automated tests. "
        "pilot <app> <group> <command>.",
        epilog=epilog(reg),
    )

    @root.callback()
    def _main():
        # A cp1252 console kills the process on the first non-ASCII control
        # name, and half the UIA tree of a localized app is non-ASCII.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    @root.command("apps")
    def apps_cmd():
        """List the apps this build can drive. JSON: apps (name -> profile|plugin|builtin), broken (name -> why its profile does not load), unsupported (name -> why it is not for this platform)."""
        run(
            lambda: {
                "apps": app_sources(reg),
                "broken": dict(sorted(reg.broken.items())),
                "unsupported": dict(sorted(reg.unsupported.items())),
            }
        )

    for name, profile in sorted(reg.profiles.items()):
        root.add_typer(build_app_cli(name, profile, reg), name=name)
    for name, message in sorted(reg.broken.items()):
        _add_broken(root, name, message)
    for name, message in sorted(reg.unsupported.items()):
        _add_broken(root, name, f"{name} is not supported here: {message}", kind="Unsupported")
    root.add_typer(build_app_cli(NULL_APP, None, reg), name=NULL_APP)
    taken = set(reg.profiles) | set(reg.broken) | set(reg.unsupported) | {NULL_APP, "apps"}
    exe = _adhoc_exe(argv, taken)
    if exe is not None:
        profile = profiles.load(exe)
        root.add_typer(build_app_cli(profile.name, profile, reg), name=exe)
    return root


app = build()
