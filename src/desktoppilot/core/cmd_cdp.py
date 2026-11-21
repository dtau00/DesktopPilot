"""`cdp`: a Chromium-based app's pages over the DevTools Protocol.

Not a core group of every app: a plugin for a Chromium or Electron app adds
it (`chrome`, `vscode`). The port is `--port`, else the
`--remote-debugging-port=N` on the bound pid's command line (PILOT_<APP>_PID),
else the one in the profile's args.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from . import apps, cdp, procs
from .commands import app_profile, run_for
from .errors import PilotError
from .profiles import Profile

# A list default is one object for every call, so it lives here (ruff B008).
UPLOAD_FILES = typer.Argument(..., help="Files to attach; each must exist.")


def port_of(profile: Profile, port: int | None) -> int:
    """--port, else the bound pid's own --remote-debugging-port (the binding
    names one instance, so its port wins over the profile's), else the
    profile's args."""
    if port is not None:
        return port
    bound = apps.bound_pid(profile)
    if bound is not None:
        found = cdp.port_in([procs.command_line(bound) or ""])
        if found is not None:
            return found
    found = cdp.port_in(profile.args)
    if found is not None:
        return found
    binding = apps.binding_env(profile)
    why = (
        f"{binding}={bound} has no --remote-debugging-port=N on its command line, or is gone"
        if bound is not None
        else f"{binding} is not set"
    )
    raise PilotError(
        f"{profile.name} opens no debugging port: pass --port, bind a pid started with "
        f"--remote-debugging-port=N ({why}), or put the flag in its profile's args"
    )


def not_listening(profile: Profile) -> str:
    return (
        f"Start {profile.name} with --remote-debugging-port=N; an instance that was already "
        "running ignores it (`app launch` adopts it), and Chrome ignores it on its default "
        "--user-data-dir"
    )


def _port():
    return typer.Option(
        None,
        "--port",
        help="CDP port (default: the --remote-debugging-port of the bound pid, "
        "PILOT_<APP>_PID, else of the profile's args).",
    )


def _url():
    return typer.Option(None, "--url", help="Part of the target's URL.")


def _title():
    return typer.Option(None, "--title", help="Part of the target's title.")


def _kind():
    return typer.Option("page", "--type", help="Target type: page, iframe, worker, ...")


def _timeout():
    return typer.Option(cdp.TIMEOUT, help="Seconds for the target to answer.")


def _scheme():
    return typer.Option(
        None,
        "--color-scheme",
        help="Emulate prefers-color-scheme (dark | light) for this command only: set on "
        "its connection, cleared at its end (Chrome drops it when the connection closes).",
    )


def cdp_group() -> typer.Typer:
    group = typer.Typer(
        help="The app's pages over the Chrome DevTools Protocol (needs a debugging port)."
    )

    def on_page(
        ctx, url, title, kind, port, timeout, act: Callable[[cdp.Page], dict], scheme=None
    ) -> dict:
        if scheme is not None and scheme not in cdp.COLOR_SCHEMES:
            raise PilotError(f"--color-scheme is dark or light, not {scheme!r}")
        profile = app_profile(ctx)
        number = port_of(profile, port)
        targets = cdp.list_targets(
            number, min(timeout, cdp.LIST_TIMEOUT), hint=not_listening(profile)
        )
        target = cdp.pick(targets, url=url, title=title, kind=kind)
        with cdp.Page(target, timeout=timeout) as page, page.emulating(scheme):
            return act(page)

    @group.command("targets")
    def cdp_targets(ctx: typer.Context, port: int = _port(), timeout: float = _timeout()):
        """Every target on the debugging port. JSON: port, browser, targets."""

        def body():
            profile = app_profile(ctx)
            number = port_of(profile, port)
            wait = min(timeout, cdp.LIST_TIMEOUT)
            hint = not_listening(profile)
            browser = cdp.version(number, wait, hint=hint).get("Browser")
            return {
                "port": number,
                "browser": browser,
                "targets": cdp.list_targets(number, wait, hint=hint),
            }

        run_for(ctx, "cdp targets", body)

    @group.command("target")
    def cdp_target(
        ctx: typer.Context,
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """The one target --url, --title and --type name; none or two fail. JSON: id, type, title, url, webSocketDebuggerUrl."""
        run_for(
            ctx,
            "cdp target",
            lambda: on_page(ctx, url, title, kind, port, timeout, lambda page: page.target),
        )

    @group.command("eval")
    def cdp_eval(
        ctx: typer.Context,
        expression: str = typer.Argument(
            ..., help="JavaScript; wrap awaits in (async () => {...})(). Return values, not nodes."
        ),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
        scheme: str = _scheme(),
    ):
        """Evaluate in the page; with --color-scheme, under that scheme. JSON: value."""
        run_for(
            ctx,
            "cdp eval",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: {"value": p.evaluate(expression)},
                scheme,
            ),
        )

    @group.command("click")
    def cdp_click(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Click an element's centre with real mouse events, so hit-testing applies. JSON: x, y (page pixels)."""
        run_for(
            ctx,
            "cdp click",
            lambda: on_page(ctx, url, title, kind, port, timeout, lambda p: p.click(selector)),
        )

    @group.command("hover")
    def cdp_hover(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Scroll the element into view and move the page's pointer to its centre (mouseMoved only): :hover, mouseover, hover-delay UI. No real pointer moves, and the window need not be in front. The page keeps the pointer there after the command, until a later move: a real pointer moving over the window overrides it. Wait for what opens with `cdp wait`. JSON: x, y (page pixels)."""
        run_for(
            ctx,
            "cdp hover",
            lambda: on_page(ctx, url, title, kind, port, timeout, lambda p: p.hover(selector)),
        )

    @group.command("console")
    def cdp_console(
        ctx: typer.Context,
        level: str = typer.Option(
            "all", "--level", help="Lowest level shown: error | warning | info | all."
        ),
        reload: bool = typer.Option(
            False, "--reload", help="Reload the page and read what the new document logs."
        ),
        listen: float = typer.Option(
            None,
            "--for",
            help="Seconds to keep reading live entries after (default 0; 1 with --reload).",
        ),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """What the page logged: console.* calls, uncaught exceptions, and browser entries (failed loads, CORS, violations), oldest first. Every call is a new connection, and Chrome replays what the current document logged before it (V8 keeps the last 1000 console messages); a navigation or reload starts the buffer over. --reload reloads and reads until the load event, then --for S more seconds. JSON: entries (level, source, text, url, line, timestamp in ms), count, errors (error entries, whatever --level shows)."""
        wait = (1.0 if reload else 0.0) if listen is None else listen
        run_for(
            ctx,
            "cdp console",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: p.console(level=level, reload=reload, listen=wait),
            ),
        )

    @group.command("type")
    def cdp_type(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector of an input or editable."),
        text: str = typer.Argument(..., help="Literal text, any characters."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Focus the element and insert TEXT at its end, as an IME commit: no keys, no layout, no window focus needed. JSON: typed, into, value (what it holds after)."""
        run_for(
            ctx,
            "cdp type",
            lambda: on_page(ctx, url, title, kind, port, timeout, lambda p: p.type(selector, text)),
        )

    @group.command("set-value")
    def cdp_set_value(
        ctx: typer.Context,
        selector: str = typer.Argument(
            ..., help="CSS selector of an input, textarea, select or editable."
        ),
        value: str = typer.Argument(..., help="The new value."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Replace the element's value, with input and change events so a page's framework sees it; read back. For a Chromium input AT-SPI cannot write. JSON: set, value, verified."""
        run_for(
            ctx,
            "cdp set-value",
            lambda: on_page(
                ctx, url, title, kind, port, timeout, lambda p: p.set_value(selector, value)
            ),
        )

    @group.command("upload")
    def cdp_upload(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector of an input[type=file]."),
        files: list[str] = UPLOAD_FILES,
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Put FILES into a file input as the OS file dialog would (DOM.setFileInputFiles); Chrome fires input and change itself. No dialog, no pointer, no window focus, and a hidden input works (GitHub's attach box: `file-attachment input[type=file]`). Every file must exist. The names are read in the input's change event, before the page's handlers can clear it. JSON: selector, files (absolute paths), count, changed (the change event fired), names (what the input held in that event, else after), names_after (what it holds after), verified (names are the files')."""
        run_for(
            ctx,
            "cdp upload",
            lambda: on_page(
                ctx, url, title, kind, port, timeout, lambda p: p.upload(selector, files)
            ),
        )

    @group.command("read")
    def cdp_read(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
        scheme: str = _scheme(),
    ):
        """An input's value, else the element's rendered text; with --color-scheme, under that scheme. JSON: selector, text, via (value | innerText)."""
        run_for(
            ctx,
            "cdp read",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: {"selector": selector, **p.read(selector)},
                scheme,
            ),
        )

    @group.command("wait")
    def cdp_wait(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="CSS selector."),
        gone: bool = typer.Option(False, "--gone", help="Wait for it to go instead."),
        wait: float = typer.Option(10.0, "--wait", help="Seconds before giving up."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
        scheme: str = _scheme(),
    ):
        """Until a visible element matches, or with --gone none does; with --color-scheme, under that scheme. JSON: selector, found or gone."""
        run_for(
            ctx,
            "cdp wait",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: p.wait(selector, gone=gone, timeout=wait),
                scheme,
            ),
        )

    @group.command("shot")
    def cdp_shot(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="PNG to write."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
        scheme: str = _scheme(),
    ):
        """PNG of the page as its renderer draws it; the window may be covered or minimized. --color-scheme dark shoots the dark theme and leaves the page as it was. JSON: path."""
        run_for(
            ctx,
            "cdp shot",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: {"path": str(p.screenshot(path))},
                scheme,
            ),
        )

    @group.command("keys")
    def cdp_keys(
        ctx: typer.Context,
        keys: str = typer.Argument(
            ...,
            help="`ui keys` syntax: '{Ctrl}a', '{Shift}{Tab}', 'abc{Enter}', '{Down 3}'. "
            "^, % and + are typed; use {Ctrl}, {Alt}, {Shift}, {Meta}.",
        ),
        into: str = typer.Option(None, "--into", help="CSS selector to focus first."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Press KEYS as key events in the page (Input.dispatchKeyEvent): keydown, keypress, input and keyup fire as for a keyboard; no real key, no window focus. Named keys: Enter Tab Escape Backspace Delete Insert Space Left Up Right Down Home End PageUp PageDown F1-F12; modifiers Ctrl Alt Shift Meta (Win). {Ctrl}a c x v z y and {Ctrl}{Shift}z run their editing command (a synthetic key alone does not in Chrome); other browser shortcuts (Ctrl+L, Ctrl+T, F5) are Chrome's, not the page's, and do nothing. --into focuses the element, caret at its end. JSON: keys, into, events (key events sent), value (what --into holds after, else null)."""
        run_for(
            ctx,
            "cdp keys",
            lambda: on_page(
                ctx, url, title, kind, port, timeout, lambda p: p.keys(keys, into=into)
            ),
        )

    @group.command("scroll")
    def cdp_scroll(
        ctx: typer.Context,
        selector: str = typer.Argument(
            None, help="CSS selector to wheel over (default: the viewport's centre)."
        ),
        by: str = typer.Option("0,100", "--by", help="DX,DY in pixels; one wheel notch is 100."),
        to: str = typer.Option(None, "--to", help="top | bottom: wheel until there."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Turn the page's mouse wheel (mouseWheel events) over the element's centre, or the viewport's, so wheel handlers and virtual lists react as to a person's wheel; no real pointer, no window focus. The element is scrolled into view first. --to top|bottom wheels until the scroller is at that end or stops moving (an endless list stops after 20 turns). Waits for the position to settle. JSON: selector, x, y (the wheel's point, page pixels), scroller (window | element: what moved), top, left, max_top, max_left (its position after, and its end)."""
        run_for(
            ctx,
            "cdp scroll",
            lambda: on_page(
                ctx,
                url,
                title,
                kind,
                port,
                timeout,
                lambda p: p.scroll(selector, by=cdp.parse_delta(by), to=to),
            ),
        )

    @group.command("drag")
    def cdp_drag(
        ctx: typer.Context,
        source: str = typer.Argument(..., help="CSS selector to press on."),
        target: str = typer.Argument(..., help="CSS selector to release on."),
        steps: int = typer.Option(10, "--steps", help="mouseMoved events on the way."),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Drag from SOURCE's centre to TARGET's with the page's pointer: press, --steps moves, release, so pointer-event drag libraries follow. A native HTML5 drag (draggable=true) is intercepted and dropped on TARGET with dragenter, dragover and drop carrying its data. No real pointer, no window focus. Both are scrolled into view and must then both be in the viewport. JSON: from, to (x, y in page pixels), mode (mouse | html5: which drag the page made)."""
        run_for(
            ctx,
            "cdp drag",
            lambda: on_page(
                ctx, url, title, kind, port, timeout, lambda p: p.drag(source, target, steps=steps)
            ),
        )

    @group.command("navigate")
    def cdp_navigate(
        ctx: typer.Context,
        address: str = typer.Argument(..., metavar="URL", help="Where to go."),
        wait: str = typer.Option(
            "load", "--wait", help="load | domcontentloaded | none: the event to wait for."
        ),
        url: str = _url(),
        title: str = _title(),
        kind: str = _kind(),
        port: int = _port(),
        timeout: float = _timeout(),
    ):
        """Load URL in the page (Page.navigate) and wait for --wait; --url and --title pick the page by where it is now. A load that fails (DNS, refused, aborted) is an error with Chrome's reason; an HTTP error page loads. JSON: url (where it is after, redirects followed), title."""
        run_for(
            ctx,
            "cdp navigate",
            lambda: on_page(
                ctx, url, title, kind, port, timeout, lambda p: p.navigate(address, wait=wait)
            ),
        )

    return group
