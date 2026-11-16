"""`pilot <app> ui`: inspect the control tree, find, read, click, type, wait."""

from __future__ import annotations

import time

import typer

from . import apps, fallback, imaging, ui
from .commands import SELECTOR_HELP, app_context, run, run_for, usage
from .profiles import DEFAULT_ROOT_DEPTH, Profile


def _find(
    ctx: typer.Context,
    selector: str,
    *,
    timeout: float = ui.DEFAULT_TIMEOUT,
    hidden: bool = False,
    depth: int | None = None,
):
    return ui.find(
        selector, app_context(ctx), timeout=timeout, visible_only=not hidden, max_depth=depth
    )


def ui_group(profile: Profile | None) -> typer.Typer:
    group = typer.Typer(help="Real UI: inspect the control tree, click, type, wait.")
    default_root = "app" if profile else "desktop"
    root_depth_default = profile.root_depth if profile else DEFAULT_ROOT_DEPTH

    # Shared by every command that finds an element.
    selector_arg = typer.Argument(..., help=SELECTOR_HELP)
    into_opt = typer.Option(None, "--into", help="Focus this element first. " + SELECTOR_HELP)
    timeout_opt = typer.Option(ui.DEFAULT_TIMEOUT, help="Seconds to wait for the element.")
    hidden_opt = typer.Option(False, help="Also match offscreen elements.")
    depth_opt = typer.Option(
        profile.depth if profile else ui.DEFAULT_DEPTH, help="Max descendant depth per step."
    )

    @group.command("tree")
    def ui_tree(
        ctx: typer.Context,
        root: str = typer.Option(default_root, help="Subtree root. " + SELECTOR_HELP),
        levels: int = typer.Option(4, "--levels", help="How many levels deep to serialize."),
        limit: int = typer.Option(400, help="Maximum nodes."),
        verbose: bool = typer.Option(False, help="Include patterns and values."),
        flat: bool = typer.Option(False, help="Flat list of named controls instead."),
        root_depth: int = typer.Option(
            root_depth_default,
            help="Search depth used to locate --root (the search depth other ui commands "
            "take as their depth option).",
        ),
    ):
        """Dump the accessibility tree (UIA on Windows, AT-SPI on Linux). JSON: root, tree (or controls with --flat), complete (false when the app timed out a children read), cut."""

        def body():
            node = ui.find(root, app_context(ctx), max_depth=root_depth)
            walk: dict = {}
            if flat:
                controls = ui.flatten(node, depth=levels, limit=limit, report=walk)
                return {"root": root, "controls": controls, **walk}
            dump = ui.tree(node, depth=levels, limit=limit, verbose=verbose, report=walk)
            return {"root": root, "tree": dump, **walk}

        run(body)

    @group.command("find")
    def ui_find(
        ctx: typer.Context,
        selector: str = selector_arg,
        timeout: float = timeout_opt,
        all_matches: bool = typer.Option(False, "--all", help="Every match, not the first."),
        hidden: bool = hidden_opt,
        depth: int = depth_opt,
    ):
        """Locate elements without clicking. JSON: selector, element (or count + elements)."""

        def body():
            if all_matches:
                hits = ui.find_all(
                    selector,
                    app_context(ctx),
                    timeout=timeout,
                    visible_only=not hidden,
                    max_depth=depth,
                )
                return {
                    "selector": selector,
                    "count": len(hits),
                    "elements": [ui.describe(h, verbose=True) for h in hits],
                }
            control = _find(ctx, selector, timeout=timeout, hidden=hidden, depth=depth)
            return {"selector": selector, "element": ui.describe(control, verbose=True)}

        run_for(ctx, "ui find", body)

    @group.command("read")
    def ui_read(
        ctx: typer.Context,
        selector: str = selector_arg,
        timeout: float = timeout_opt,
        copy: bool = typer.Option(
            False, help="Select-all + copy instead of the accessibility value."
        ),
        depth: int = depth_opt,
    ):
        """Read an element's text: value, else TextPattern, else --copy. JSON: selector, via, text."""

        def body():
            control = _find(ctx, selector, timeout=timeout, depth=depth)
            text = ui.copy_text(control) if copy else ui.text_of(control)
            return {"selector": selector, "via": "clipboard" if copy else "uia", "text": text}

        run_for(ctx, "ui read", body)

    @group.command("click")
    def ui_click(
        ctx: typer.Context,
        selector: str = typer.Argument(None, help=SELECTOR_HELP),
        at: str = typer.Option(
            None, "--at", help="Screen point 'X,Y' instead of a selector. The last resort."
        ),
        relative: bool = typer.Option(
            False,
            help="--at is relative to the top-left of a plain `shot` of the main window "
            "(its visible frame), so a point read off that shot lands.",
        ),
        mode: str = typer.Option("auto", help="auto (pattern, else pointer) | invoke | mouse."),
        button: str = typer.Option("left", help="left | right."),
        double: bool = typer.Option(False, help="Double-click."),
        timeout: float = timeout_opt,
        hidden: bool = hidden_opt,
        depth: int = depth_opt,
        allow_disabled: bool = typer.Option(
            False, "--allow-disabled", help="Click the element even when it is disabled."
        ),
    ):
        """Click through an accessibility pattern (UIA, or its AT-SPI action), or the pointer if it has none. --at X,Y clicks a screen point after the agent has read a screenshot. A disabled element is refused (Disabled) unless --allow-disabled. JSON: clicked (the selector), via, element - or with --at: clicked (null), point \\[x, y], relative, via, fallback (coordinates)."""

        def coordinates():
            actx = app_context(ctx)
            x, y = imaging.parse_point(at)
            if relative:
                left, top = ui.window_origin(actx)
                x, y = x + left, y + top
            ui.activate_app(actx)
            how = ui.click_point(x, y, button=button, double=double)
            fallback.record(actx, "coordinates", "ui click", f"{x},{y}")
            return {
                "clicked": None,
                "point": [x, y],
                "relative": relative,
                "via": how,
                "fallback": "coordinates",
            }

        def body():
            deadline = time.monotonic() + timeout

            def find():
                left = max(deadline - time.monotonic(), 0.0)
                return _find(ctx, selector, timeout=left, hidden=hidden, depth=depth)

            control = find()
            how = ui.click(
                control,
                ctx=app_context(ctx),
                mode=mode,
                button=button,
                double=double,
                allow_disabled=allow_disabled,
                timeout=deadline - time.monotonic(),
                refind=find,
            )
            return {"clicked": selector, "via": how, "element": ui.describe(control)}

        if at is not None and selector is not None:
            run(lambda: usage("--at and a SELECTOR are mutually exclusive"))
        elif at is None and selector is None:
            run(lambda: usage("give a SELECTOR or --at X,Y"))
        else:
            run_for(ctx, "ui click", coordinates if at is not None else body)

    @group.command("hover")
    def ui_hover(
        ctx: typer.Context,
        selector: str = typer.Argument(None, help=SELECTOR_HELP),
        at: str = typer.Option(
            None, "--at", help="Screen point 'X,Y' instead of a selector, as `ui click --at`."
        ),
        relative: bool = typer.Option(
            False, help="--at is relative to a plain `shot` of the main window, as `ui click`."
        ),
        dwell: float = typer.Option(
            0.0, "--dwell", help="Seconds to hold there before returning (a hover-delay UI)."
        ),
        timeout: float = timeout_opt,
        hidden: bool = hidden_opt,
        depth: int = depth_opt,
    ):
        """Move the real pointer to an element's centre, or to --at X,Y, and leave it there: tooltips, hover menus, hover previews. The app's main window comes to the front first. --dwell S holds S seconds and stops early if something else moves the pointer. JSON: hovered (the selector, or null with --at), x, y (screen), via, held (the pointer was still there at the end), element - or with --at: relative, fallback (coordinates)."""

        def coordinates():
            actx = app_context(ctx)
            x, y = imaging.parse_point(at)
            if relative:
                left, top = ui.window_origin(actx)
                x, y = x + left, y + top
            ui.activate_app(actx)
            done = ui.hover_point(x, y, dwell=dwell)
            fallback.record(actx, "coordinates", "ui hover", f"{x},{y}")
            return {"hovered": None, **done, "relative": relative, "fallback": "coordinates"}

        def body():
            control = _find(ctx, selector, timeout=timeout, hidden=hidden, depth=depth)
            done = ui.hover(control, ctx=app_context(ctx), dwell=dwell)
            return {"hovered": selector, **done, "element": ui.describe(control)}

        if at is not None and selector is not None:
            run(lambda: usage("--at and a SELECTOR are mutually exclusive"))
        elif at is None and selector is None:
            run(lambda: usage("give a SELECTOR or --at X,Y"))
        else:
            run_for(ctx, "ui hover", coordinates if at is not None else body)

    @group.command("type")
    def ui_type(
        ctx: typer.Context,
        text: str = typer.Argument(..., help="Literal text to type; {Ctrl} types as braces."),
        selector: str = into_opt,
        clear: bool = typer.Option(False, help="Select-all + Delete before typing."),
        timeout: float = timeout_opt,
        depth: int = depth_opt,
    ):
        """Type literal text into the focused element, or into --into SELECTOR, as a keyboard would (virtual key + Shift). Keys and shortcuts go through `ui keys`. JSON: typed, into, via."""

        def body():
            actx = app_context(ctx)
            control = _find(ctx, selector, timeout=timeout, depth=depth) if selector else None
            if control is None and actx.profile is not None:
                ui.activate_app(actx)
            ui.type_text(text, control=control, clear=clear)
            return {"typed": text, "into": selector, "via": "keys"}

        run(body, ctx=app_context(ctx) if selector else None, command="ui type")

    @group.command("set-value")
    def ui_set_value(
        ctx: typer.Context,
        selector: str = selector_arg,
        text: str = typer.Argument(..., help="The new value; '' clears the control."),
        timeout: float = timeout_opt,
        hidden: bool = hidden_opt,
        depth: int = depth_opt,
    ):
        """Set a text or combo box through the Value pattern, not keystrokes. JSON: set, value, verified (the value read back is the one set), element."""

        def body():
            control = _find(ctx, selector, timeout=timeout, hidden=hidden, depth=depth)
            return ui.set_value(control, text, ctx=app_context(ctx))

        run_for(ctx, "ui set-value", body)

    @group.command("select")
    def ui_select(
        ctx: typer.Context,
        selector: str = selector_arg,
        item: str = typer.Option(None, "--item", help="Item to pick inside it."),
        timeout: float = timeout_opt,
        hidden: bool = hidden_opt,
        depth: int = depth_opt,
    ):
        """Pick a list, combo or menu item; expands the control first. Without --item, selects the element itself. JSON: selected (the label; "" for an unnamed item), expanded, via, and element without --item."""

        def body():
            control = _find(ctx, selector, timeout=timeout, hidden=hidden, depth=depth)
            return ui.select_item(control, item, ctx=app_context(ctx), timeout=timeout)

        run_for(ctx, "ui select", body)

    @group.command("scroll")
    def ui_scroll(
        ctx: typer.Context,
        selector: str = selector_arg,
        vertical: float = typer.Option(None, "--vertical", help="Vertical position, 0-100."),
        horizontal: float = typer.Option(None, "--horizontal", help="Horizontal, 0-100."),
        timeout: float = timeout_opt,
        depth: int = depth_opt,
    ):
        """Scroll to a percentage, with no wheel and no pointer. JSON: vertical_percent, horizontal_percent, vertical_view_size."""

        def body():
            control = _find(ctx, selector, timeout=timeout, depth=depth)
            return ui.scroll(control, vertical=vertical, horizontal=horizontal)

        run_for(ctx, "ui scroll", body)

    @group.command("count")
    def ui_count(
        ctx: typer.Context,
        selector: str = selector_arg,
        names: int = typer.Option(ui.DEFAULT_NAMES, "--names", help="Report the first N labels."),
        no_expand: bool = typer.Option(
            False, "--no-expand", help="Do not expand a tree; count its top level only."
        ),
        timeout: float = timeout_opt,
        depth: int = depth_opt,
    ):
        """The real item count of a list or tree, virtualized and collapsed items included. SELECTOR is the list or tree, not one of its items. JSON: selector, count, via, names (+ item_hint when SELECTOR is an item)."""

        def body():
            control = _find(ctx, selector, timeout=timeout, depth=depth)
            return {"selector": selector} | ui.count_items(
                control, names=names, expand=not no_expand
            )

        run_for(ctx, "ui count", body)

    @group.command("expand")
    def ui_expand(
        ctx: typer.Context,
        selector: str = selector_arg,
        all_nodes: bool = typer.Option(
            False,
            "--all",
            help="Expand every node below it, breadth first (depth first when the app "
            "offers no cached tree; `via` says which).",
        ),
        timeout: float = timeout_opt,
        depth: int = depth_opt,
    ):
        """Open a tree node, or with --all every collapsed node under it. JSON: selector, expanded (true when this call opened it; with --all, how many nodes it opened), state (expanded, collapsed, partial or leaf; not with --all), and with --all nodes, depths, complete."""

        def body():
            control = _find(ctx, selector, timeout=timeout, depth=depth)
            result = ui.expand_all(control) if all_nodes else ui.expand(control)
            return {"selector": selector} | result

        run_for(ctx, "ui expand", body)

    @group.command("keys")
    def ui_keys(
        ctx: typer.Context,
        keys: str = typer.Argument(
            ...,
            help="uiautomation syntax, e.g. '{Ctrl}s', '{Ctrl}{Shift}p', '{Enter}'. "
            "Not SendKeys: ^, % and + are not modifiers but typed as they are; "
            "use {Ctrl}, {Alt}, {Shift}.",
        ),
        selector: str = into_opt,
        activate: bool = typer.Option(True, help="Focus the app's main window first."),
        timeout: float = timeout_opt,
        depth: int = depth_opt,
    ):
        """Send keystrokes in uiautomation syntax ({Ctrl}l, not ^l). --into targets a control. JSON: keys, into, via."""

        def body():
            actx = app_context(ctx)
            control = _find(ctx, selector, timeout=timeout, depth=depth) if selector else None
            if control is None and activate:
                ui.activate_app(actx)
            ui.send_keys(keys, control=control)
            return {"keys": keys, "into": selector, "via": "keys"}

        run(body)

    @group.command("wait")
    def ui_wait(
        ctx: typer.Context,
        selector: str = selector_arg,
        gone: bool = typer.Option(False, help="Wait for it to disappear instead."),
        timeout: float = typer.Option(ui.DEFAULT_TIMEOUT, help="Seconds."),
        hidden: bool = typer.Option(False, help="Count offscreen elements as present."),
        depth: int = typer.Option(
            profile.depth if profile else ui.DEFAULT_DEPTH,
            help="Max descendant depth per step while waiting for it to appear.",
        ),
    ):
        """Wait for an element to appear or disappear. JSON: selector, found or gone."""

        def body():
            if gone:
                return ui.wait_for(
                    selector, app_context(ctx), gone=True, timeout=timeout, visible_only=not hidden
                )
            control = _find(ctx, selector, timeout=timeout, hidden=hidden, depth=depth)
            return {
                "selector": selector,
                "found": True,
                "element": ui.describe(control, verbose=True),
            }

        run_for(ctx, "ui wait", body)

    @group.command("windows")
    def ui_windows(ctx: typer.Context):
        """Every visible top-level window the app owns. JSON: windows."""
        run(lambda: {"windows": apps.windows(app_context(ctx).profile)})

    @group.command("close")
    def ui_close(
        ctx: typer.Context,
        selector: str = typer.Argument(..., help="Window selector, e.g. win:Settings."),
        timeout: float = timeout_opt,
    ):
        """Close a window by posting WM_CLOSE. JSON: closed."""

        def body():
            ui.close_window(_find(ctx, selector, timeout=timeout))
            return {"closed": selector}

        run_for(ctx, "ui close", body)

    @group.command("activate")
    def ui_activate(
        ctx: typer.Context,
        selector: str = typer.Option(
            None, "--selector", "--root", help="Window selector; default the main one."
        ),
        timeout: float = timeout_opt,
    ):
        """Bring a window to the foreground. The window is --selector (or --root, the same). JSON: activated."""

        def body():
            actx = app_context(ctx)
            window = (
                _find(ctx, selector, timeout=timeout)
                if selector
                else ui.main_window(actx, timeout=timeout)
            )
            ui.activate(window)
            return {"activated": selector or "app"}

        run_for(ctx, "ui activate", body)

    return group
