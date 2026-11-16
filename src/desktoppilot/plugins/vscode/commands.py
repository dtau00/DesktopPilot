"""vscode's command groups: codex, git, and cdp for a VS Code started with a debugging port."""

from __future__ import annotations

from pathlib import Path

import typer

from desktoppilot.core import cmd_cdp
from desktoppilot.core.commands import app_context, run_for
from desktoppilot.core.errors import PilotError

from . import codex, git


def groups() -> dict[str, typer.Typer]:
    return {"codex": _codex_group(), "git": _git_group(), "cdp": cmd_cdp.cdp_group()}


def _on_off(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("on", "true", "yes", "1"):
        return True
    if lowered in ("off", "false", "no", "0"):
        return False
    raise PilotError(f"--fast takes on or off, not {value!r}")


WINDOW_HELP = (
    "Part of the VS Code window title; must match one window only. Required when more "
    "than one window has a Codex panel."
)


def _window_option():
    return typer.Option(None, "--window", help=WINDOW_HELP)


def _codex_group() -> typer.Typer:
    group = typer.Typer(
        help="The Codex extension's panel (openai.chatgpt), through the accessibility tree."
    )

    @group.command("open")
    def codex_open(
        ctx: typer.Context,
        timeout: float = typer.Option(
            codex.OPEN_TIMEOUT, help="Seconds for a new panel's composer to appear."
        ),
        window: str = _window_option(),
    ):
        """Show a Codex panel on a new chat: reuse a visible one that is on a new chat, else open one with "Codex: New Codex Agent" from the palette. JSON: opened, reused, closed_previous, tour_pages (first-run tour pages clicked; a new panel only), title, window, new_chat, model_label, permissions, where, generating, blocked (a usage-limit or error notice, or null)."""
        run_for(
            ctx,
            "codex open",
            lambda: codex.open_panel(app_context(ctx), timeout=timeout, window=window),
        )

    @group.command("model")
    def codex_model(
        ctx: typer.Context,
        name: str = typer.Option(
            None, "--name", help="Model, as the picker names it; 'Sol' must match one model only."
        ),
        effort: str = typer.Option(
            None, "--effort", help="Reasoning effort: low, medium, high, xhigh, ... or its label."
        ),
        fast: str = typer.Option(None, "--fast", help="Fast mode: on or off."),
        window: str = _window_option(),
    ):
        """Set model, effort and fast mode, then read all three back from the picker; any difference fails with ModelMismatch. With no option, only reads. JSON: verified, model, effort, fast, label."""

        def body():
            actx = app_context(ctx)
            given = [name, effort, fast]
            if not any(v is not None for v in given):
                return codex.read_model(actx, window)
            if any(v is None for v in given):
                raise PilotError("give --name, --effort and --fast together, or none to read")
            return codex.set_model(
                actx, name=name, effort=effort, fast=_on_off(fast), window=window
            )

        run_for(ctx, "codex model", body)

    @group.command("send")
    def codex_send(
        ctx: typer.Context,
        text: str = typer.Argument(None, help="The prompt."),
        file: str = typer.Option(None, "--file", help="Read the prompt from this UTF-8 file."),
        timeout: float = typer.Option(20.0, help="Seconds for Codex to take the message."),
        replace_draft: bool = typer.Option(
            False, "--replace-draft", help="Discard an unsent draft in the composer."
        ),
        window: str = _window_option(),
    ):
        """Paste a prompt into the composer and Send; returns once Codex has taken it. An unsent draft is refused without --replace-draft. JSON: sent, chars, generating, clipboard_restored ("text" or false)."""

        def body():
            if (text is None) == (file is None):
                raise PilotError("give the prompt as TEXT or --file, not both")
            prompt = Path(file).read_text(encoding="utf-8") if file is not None else text
            return codex.send(
                app_context(ctx),
                prompt,
                timeout=timeout,
                window=window,
                replace_draft=replace_draft,
            )

        run_for(ctx, "codex send", body)

    @group.command("wait")
    def codex_wait(
        ctx: typer.Context,
        timeout: float = typer.Option(1800.0, help="Seconds before giving up."),
        settle_polls: int = typer.Option(
            codex.SETTLE_POLLS, "--settle-polls", help="Calm walks in a row that count as done."
        ),
        window: str = _window_option(),
    ):
        """Block until Codex stops. JSON: state (done | question), and for a question its reason (approval | reply_asks) and prompt. A turn that ends in a usage-limit or error notice fails with CodexFailed."""
        run_for(
            ctx,
            "codex wait",
            lambda: codex.wait(
                app_context(ctx), timeout=timeout, settle_polls=settle_polls, window=window
            ),
        )

    @group.command("read")
    def codex_read(ctx: typer.Context, window: str = _window_option()):
        """The last reply's full text, through its Copy button, checked against the visible reply; the clipboard's text is put back. JSON: text, chars, title, verified, clipboard_restored ("text" or false)."""
        run_for(ctx, "codex read", lambda: codex.read(app_context(ctx), window))

    return group


def _git_group() -> typer.Typer:
    group = typer.Typer(
        help="Git in the running VS Code, through the palette and the accessibility tree."
    )

    @group.command("checkout")
    def git_checkout(
        ctx: typer.Context,
        branch: str = typer.Argument(..., help="Local branch name, as `git branch` shows it."),
        remote: bool = typer.Option(
            False, "--remote", help="Allow a branch that exists only on a remote (origin/BRANCH)."
        ),
        window: str = typer.Option(
            None,
            "--window",
            help="Part of the window title; must match one window only. Required when "
            "more than one VS Code window is open.",
        ),
        timeout: float = typer.Option(10.0, help="Seconds per step, and for the status bar."),
    ):
        """Check out BRANCH with "Git: Checkout to...", then wait for the status bar's branch item to show it. A dialog or notification instead fails with its text. JSON: checked_out, row, window, verified."""
        run_for(
            ctx,
            "git checkout",
            lambda: git.checkout(
                app_context(ctx), branch, remote=remote, window=window, timeout=timeout
            ),
        )

    @group.command("compare")
    def git_compare(
        ctx: typer.Context,
        base: str = typer.Option("main", "--base", help="The ref to compare HEAD with."),
        window: str = typer.Option(
            None,
            "--window",
            help="Part of the window title; must match one window only. Required when "
            "more than one VS Code window is open.",
        ),
        timeout: float = typer.Option(15.0, help="Seconds per step, and for the node's counts."),
    ):
        """Compare HEAD with BASE in GitLens' Search & Compare view ("GitLens: Compare HEAD with..."; the picker row must be exactly BASE), then read the node's counts. A count the view does not show is null. JSON: compared (head, base), node, existed_before, ahead, behind, files_changed, additions, deletions, row, window."""
        run_for(
            ctx,
            "git compare",
            lambda: git.compare(app_context(ctx), base, window=window, timeout=timeout),
        )

    return group
