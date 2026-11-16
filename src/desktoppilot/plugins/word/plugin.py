"""What the `word` app adds to the core: groups, roots, lifecycle hooks."""

from __future__ import annotations

from desktoppilot.core.profiles import Profile
from desktoppilot.core.registry import AppContext, Registry

from . import com, commands, roots, session

FIXTURES = "desktoppilot.plugins.word.fixtures"


def register_roots(reg: Registry, profile: Profile) -> None:
    """Word's selector roots, bound to one app.

    A resolver closes over the AppContext it was built with, so an app that
    extends `word` must call this with its own profile - inherited resolvers
    would still point at `word`. Groups, hooks and fixtures need no rebinding:
    they take the caller's context at call time.
    """
    app = profile.name
    ctx = AppContext(name=app, profile=profile, registry=reg)

    # `app` and `win` override the core roots of the same name.
    reg.root("app", lambda arg, timeout: roots.word_window(ctx, timeout=timeout), for_app=app)
    reg.root(
        "win",
        lambda arg, timeout, probe=False: roots.addin_window(
            ctx, arg, timeout=timeout, probe=probe
        ),
        for_app=app,
    )
    reg.root("ribbon", lambda arg, timeout: roots.ribbon(ctx, timeout=timeout), for_app=app)
    # The "not open" roots take `probe`: ui.is_gone asks them to look once, not wait.
    reg.root(
        "pane",
        lambda arg, timeout, probe=False: roots.task_pane(ctx, arg, timeout=timeout, probe=probe),
        for_app=app,
    )
    reg.root(
        "backstage",
        lambda arg, timeout, probe=False: roots.backstage(ctx, timeout=timeout, probe=probe),
        for_app=app,
    )
    reg.root(
        "popup",
        lambda arg, timeout, probe=False: roots.popup(ctx, timeout=timeout, probe=probe),
        for_app=app,
    )


def register(reg: Registry, profile: Profile) -> None:
    app = profile.name

    reg.groups(commands.groups(), for_app=app)

    register_roots(reg, profile)

    reg.hook("pre_launch", clear_crash_state, for_app=app)
    reg.hook("post_launch", answer_startup_prompts, for_app=app)
    reg.hook("ready", addins_loaded, for_app=app)
    reg.hook("quit", quit_word, for_app=app)
    reg.hook("pre_kill", session.refuse_foreign_word, for_app=app)
    reg.hook("dialog_policy", startup_prompts, for_app=app)
    reg.hook("status", session.status_extras, for_app=app)

    reg.fixtures(FIXTURES, for_app=app)


def clear_crash_state(ctx: AppContext) -> list[str]:
    """Word greets the launch after a kill with a recovery pane and disabled
    add-ins unless its Resiliency keys are gone."""
    return session.clear_resiliency()


def answer_startup_prompts(ctx: AppContext) -> list[dict]:
    return session.reap_prompts(ctx.profile)


def startup_prompts(ctx: AppContext):
    return session.KNOWN_PROMPTS


def addins_loaded(ctx: AppContext) -> bool:
    """Usable means the object model answers and the VSTO add-ins have
    connected - a ribbon click a beat earlier does nothing. An add-in that
    stays down does not fail a plain Word launch: its JSON (the status hook)
    names it in `addins_not_connected`."""
    session.reap_prompts(ctx.profile)
    app = com.attach(ctx.profile, timeout=5.0)
    session.wait_for_addins(app)
    return True


def quit_word(ctx: AppContext) -> str:
    return session.quit_word(ctx.profile)
