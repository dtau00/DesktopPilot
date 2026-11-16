"""What the `vscode` app adds on top of `code`: the codex, git and cdp groups, the codex root."""

from __future__ import annotations

from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

from . import codex, commands


def register(reg: Registry, profile: Profile) -> None:
    app = profile.name
    ctx = AppContext(name=app, profile=profile, registry=reg)

    reg.groups(commands.groups(), for_app=app)

    reg.root(
        "codex",
        lambda arg, timeout, probe=False: codex.resolve_root(ctx, timeout, probe=probe),
        for_app=app,
    )
