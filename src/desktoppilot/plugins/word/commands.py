"""Word's own command groups: doc, ribbon, pane, backstage, addin.

The JSON shapes are a contract: scripts and the product suites parse them.
"""

from __future__ import annotations

import typer

from desktoppilot.core import ui
from desktoppilot.core.commands import app_context, app_profile, run_for
from desktoppilot.core.errors import WalkCut

from . import com, docs, layout, roots, visible


def groups() -> dict[str, typer.Typer]:
    return {
        "doc": _doc_group(),
        "ribbon": _ribbon_group(),
        "pane": _pane_group(),
        "backstage": _backstage_group(),
        "addin": _addin_group(),
        "layout": _layout_command(),
        "visible": _visible_command(),
    }


def _app(ctx: typer.Context):
    """Our Word's Application; no Word, or not ours, fails at once (com.attach)."""
    return com.attach(app_profile(ctx))


# --------------------------------------------------------------------- doc


def _doc_group() -> typer.Typer:
    group = typer.Typer(help="Document open/read/close, over Word's COM object model.")

    @group.command("open")
    def doc_open(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="Path to the .docx to open."),
        read_only: bool = typer.Option(False, help="Open read-only."),
        password: str = typer.Option(None, help="The document's open password."),
        repair: bool = typer.Option(False, help="Open and repair a damaged document."),
    ):
        """Open a document (leaves Protected View automatically), then wait until Word answers UIA quickly again. A protected or damaged file fails with an error instead of prompting; a dialog that still blocks the open is answered (Cancel/No) and named. JSON: opened, name, pages, settled, settle_s, and dialogs_answered when there were any."""

        def body():
            answered: list[dict] = []
            doc = docs.open_doc(
                _app(ctx),
                path,
                read_only=read_only,
                password=password,
                repair=repair,
                profile=app_profile(ctx),
                answered=answered,
            )
            info = docs.doc_info(doc)
            settled = roots.settle(app_context(ctx))
            result = {
                "opened": True,
                **info,
                "settled": settled["settled"],
                "settle_s": settled["seconds"],
            }
            if answered:
                result["dialogs_answered"] = answered
            return result

        run_for(ctx, "doc open", body)

    @group.command("close")
    def doc_close(
        ctx: typer.Context,
        save: bool = typer.Option(False, help="Save before closing."),
    ):
        """Close the active document. JSON: closed (its name)."""
        run_for(ctx, "doc close", lambda: {"closed": docs.close_doc(_app(ctx), save=save)})

    @group.command("save-as")
    def doc_save_as(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="Destination path."),
    ):
        """Save the active document to a new path. JSON: saved_to."""
        run_for(ctx, "doc save-as", lambda: {"saved_to": str(docs.save_as(_app(ctx), path))})

    @group.command("read")
    def doc_read(
        ctx: typer.Context,
        start: int = typer.Option(1, help="First paragraph index (1-based)."),
        limit: int = typer.Option(200, help="Maximum paragraphs to return."),
        keep_empty: bool = typer.Option(False, help="Include empty paragraphs."),
    ):
        """Read the active document as paragraphs with their styles. JSON: document, paragraphs."""

        def body():
            doc = docs.active_doc(_app(ctx))
            return {
                "document": docs.doc_info(doc),
                "paragraphs": docs.read_paragraphs(
                    doc, start=start, limit=limit, skip_empty=not keep_empty
                ),
            }

        run_for(ctx, "doc read", body)

    @group.command("find")
    def doc_find(
        ctx: typer.Context,
        text: str = typer.Argument(..., help="Text to search for."),
        match_case: bool = typer.Option(False, help="Case-sensitive search."),
        limit: int = typer.Option(50, help="Maximum hits to return."),
    ):
        """Find text in the active document. JSON: query, hits (ranges and pages)."""

        def body():
            doc = docs.active_doc(_app(ctx))
            return {
                "query": text,
                "hits": docs.find_text(doc, text, match_case=match_case, limit=limit),
            }

        run_for(ctx, "doc find", body)

    @group.command("select")
    def doc_select(
        ctx: typer.Context,
        start: int = typer.Argument(..., help="Start character offset."),
        end: int = typer.Argument(..., help="End character offset."),
    ):
        """Select a character range (use before clicking a ribbon command). JSON: start, end, text."""
        run_for(
            ctx, "doc select", lambda: docs.select_range(docs.active_doc(_app(ctx)), start, end)
        )

    @group.command("new")
    def doc_new(ctx: typer.Context):
        """Create a blank document. JSON: created, name, pages."""
        run_for(ctx, "doc new", lambda: {"created": True, **docs.doc_info(docs.new_doc(_app(ctx)))})

    @group.command("save")
    def doc_save(ctx: typer.Context):
        """Save the active document in place. JSON: saved, path."""
        run_for(ctx, "doc save", lambda: docs.save(_app(ctx)))

    @group.command("insert")
    def doc_insert(
        ctx: typer.Context,
        text: str = typer.Argument(..., help="Text to insert."),
        at: str = typer.Option("end", help="end | start | selection."),
    ):
        """Insert text into the active document. JSON: inserted, at, document."""
        run_for(ctx, "doc insert", lambda: docs.insert_text(_app(ctx), text, at=at))

    @group.command("comments")
    def doc_comments(
        ctx: typer.Context,
        limit: int = typer.Option(100, help="Maximum comments to return."),
    ):
        """Comments with the text they anchor to. JSON: count, comments."""
        run_for(ctx, "doc comments", lambda: docs.comments(docs.active_doc(_app(ctx)), limit=limit))

    @group.command("revisions")
    def doc_revisions(
        ctx: typer.Context,
        limit: int = typer.Option(100, help="Maximum revisions to list."),
    ):
        """The first --limit tracked changes, and a tally per author of those. JSON: count (all of them), complete, by_author, revisions."""
        run_for(
            ctx, "doc revisions", lambda: docs.revisions(docs.active_doc(_app(ctx)), limit=limit)
        )

    return group


# ------------------------------------------------------------------ ribbon


def _ribbon_group() -> typer.Typer:
    group = typer.Typer(help="Word's ribbon.")

    @group.command("tabs")
    def ribbon_tabs(
        ctx: typer.Context,
        timeout: float = typer.Option(15.0, help="Seconds."),
    ):
        """List ribbon tab names. A Word too busy to list them fails (WalkCut) rather than answering []. JSON: tabs."""
        run_for(
            ctx,
            "ribbon tabs",
            lambda: {"tabs": roots.ribbon_tabs(app_context(ctx), timeout=timeout)},
        )

    @group.command("controls")
    def ribbon_controls(
        ctx: typer.Context,
        tab: str = typer.Option(None, help="Activate this tab first, then list its controls."),
        timeout: float = typer.Option(15.0, help="Seconds."),
    ):
        """Clickable labels on the ribbon - the names `ribbon click` expects. With --tab, that tab's controls only; an unknown tab fails and lists the tabs. JSON: tab, controls."""
        run_for(
            ctx,
            "ribbon controls",
            lambda: {
                "tab": tab,
                "controls": roots.ribbon_controls(app_context(ctx), tab, timeout=timeout),
            },
        )

    @group.command("click")
    def ribbon_click(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="'Tab>Control' or 'Tab>Group>Control'."),
        mode: str = typer.Option("auto", help="auto | invoke | mouse."),
        timeout: float = typer.Option(15.0, help="Seconds."),
        allow_disabled: bool = typer.Option(
            False, "--allow-disabled", help="Click a control that reads as disabled."
        ),
    ):
        """Activate a ribbon tab and click one of its controls. JSON: tab, group, control, clicked_via, element."""
        run_for(
            ctx,
            "ribbon click",
            lambda: roots.ribbon_click(
                app_context(ctx), path, mode=mode, timeout=timeout, allow_disabled=allow_disabled
            ),
        )

    return group


# -------------------------------------------------------------------- pane


def _pane_group() -> typer.Typer:
    group = typer.Typer(help="Task panes, Word's own and the add-in's.")

    @group.command("read")
    def pane_read(
        ctx: typer.Context,
        name: str = typer.Option(None, help="Partial pane title, e.g. Tasks."),
        depth: int = typer.Option(8, help="Levels to walk."),
        limit: int = typer.Option(300, help="Maximum controls."),
        hidden: bool = typer.Option(False, help="Include offscreen/collapsed controls."),
    ):
        """Flat dump of a task pane: every named control, with rects and state. JSON: pane, controls, complete, cut (subtrees Word timed out on)."""

        def body():
            pane = roots.task_pane(app_context(ctx), name)
            walk: dict = {}
            controls = ui.flatten(
                pane, depth=depth, limit=limit, only_visible=not hidden, report=walk
            )
            return {"pane": ui.describe(pane), "controls": controls, **walk}

        run_for(ctx, "pane read", body)

    return group


# --------------------------------------------------------------- backstage


def _backstage_group() -> typer.Typer:
    group = typer.Typer(help="Word's File menu.")

    @group.command("open")
    def backstage_open(ctx: typer.Context):
        """Open Backstage, then use the `backstage` selector root. JSON: backstage, element."""
        run_for(
            ctx,
            "backstage open",
            lambda: {
                "backstage": "open",
                "element": ui.describe(roots.open_backstage(app_context(ctx))),
            },
        )

    @group.command("close")
    def backstage_close(ctx: typer.Context):
        """Close Backstage. JSON: backstage, ok."""
        run_for(
            ctx,
            "backstage close",
            lambda: {"backstage": "closed", "ok": roots.close_backstage(app_context(ctx))},
        )

    @group.command("show")
    def backstage_show(ctx: typer.Context):
        """Describe the open Backstage without touching it. JSON: element."""
        run_for(
            ctx,
            "backstage show",
            lambda: {"element": ui.describe(roots.backstage(app_context(ctx)))},
        )

    return group


# ------------------------------------------------------------------- addin


def _addin_group() -> typer.Typer:
    group = typer.Typer(help="Windows the add-in opens.")

    @group.command("windows")
    def addin_windows(ctx: typer.Context):
        """Word's top-level windows - the titles usable as a win:NAME root. JSON: windows."""
        run_for(ctx, "addin windows", lambda: {"windows": roots.top_windows(app_context(ctx))})

    @group.command("close")
    def addin_close(ctx: typer.Context):
        """Close the add-in's WPF windows and untitled toasts (leaves task panes alone). JSON: closed (titles), untitled (count), not_closed (windows that refused, with why) when there are any, and incomplete (why) when Word did not answer, so the untitled toasts were not checked."""

        def body():
            incomplete = None
            not_closed: list[dict] = []
            try:
                closed = roots.close_addin_windows(app_context(ctx), not_closed=not_closed)
            except WalkCut as cut:  # Word did not answer: the toasts were not checked
                closed, incomplete = cut.hits, str(cut)
            result = {
                "closed": [t for t in closed if t != roots.UNTITLED],
                "untitled": closed.count(roots.UNTITLED),
            }
            if not_closed:
                result["not_closed"] = not_closed
            if incomplete is not None:
                result["incomplete"] = incomplete
            return result

        run_for(ctx, "addin close", body)

    return group


# ------------------------------------------------------- layout and visible
# Single commands, not groups: a Typer whose callback does the work.


def _layout_command() -> typer.Typer:
    command = typer.Typer(short_help="Word's page layout around the view, over COM.")

    @command.callback(invoke_without_command=True)
    def word_layout(
        ctx: typer.Context,
        page: int = typer.Option(
            None, help="Centre page. Default: the page at the centre of the document window."
        ),
        radius: int = typer.Option(1, help="Pages either side of the centre page."),
    ):
        """Word's page layout around the view, over COM: each page's box, its Rectangles and their Lines, each line next to Window.GetPoint (screen px) and Range.Information (page, position on the page in pt). A COM error on one item lands in `errors`. JSON: view, centre_point, centre_page, centre_from, page_count, pages, errors, seconds."""
        run_for(ctx, "layout", lambda: layout.capture(_app(ctx), page=page, radius=radius))

    return command


def _visible_command() -> typer.Typer:
    command = typer.Typer(
        short_help="What Word is displaying, from UIA, mapped to Word positions over COM."
    )

    @command.callback(invoke_without_command=True)
    def word_visible(
        ctx: typer.Context,
        timeout: float = typer.Option(
            visible.FIND_TIMEOUT,
            help="Seconds to find the document element (the first search on a window is slow).",
        ),
    ):
        """What Word is displaying, from UI Automation: the document element's visible ranges and its displayed pages with their parts and footnotes, mapped onto Word positions and pages over COM. JSON: view, found_via, find_s, ranges, page_elements, pages, main, displayed_pages, errors, seconds."""
        run_for(ctx, "visible", lambda: visible.capture(_app(ctx), timeout=timeout))

    return command
