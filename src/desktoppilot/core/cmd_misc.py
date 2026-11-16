"""The single commands (`dialogs`, `state`, `shot`, `capture`), the shot
helpers `image shot` shares, and the `clip` and `events` groups."""

from __future__ import annotations

import typer

from . import apps, artifacts, clip, fallback, imaging, ui, winevents
from . import dialogs as dialogs_mod
from . import state as state_mod
from .annotate import Frame
from .commands import SELECTOR_HELP, app_context, run, run_for, usage
from .errors import PilotError
from .profiles import Profile
from .registry import AppContext

KIND_OPTION = typer.Option(
    None, "--kind", help=f"Only this kind; repeatable. One of {', '.join(winevents.KINDS)}."
)


def clip_group() -> typer.Typer:
    group = typer.Typer(help="The system clipboard (on X11 the CLIPBOARD selection).")

    @group.command("read")
    def clip_read():
        """Read the clipboard as text. JSON: text."""
        run(lambda: {"text": clip.read()})

    @group.command("write")
    def clip_write(
        text: str = typer.Argument(..., help="Text to put on the clipboard."),
    ):
        """Put text on the clipboard. JSON: written (character count)."""
        run(lambda: {"written": clip.write(text)})

    return group


def events_group(profile: Profile | None) -> typer.Typer:
    """Named `events`, not `logs`: a plugin's own `logs` group
    would win the name clash and hide it."""
    group = typer.Typer(
        help="What the system recorded about the app. Windows: crashes, hangs, .NET "
        "exceptions, WER reports, dumps. Linux: crashes, OOM kills, apport reports, core dumps."
    )

    def image_of(any_app: bool) -> str | None:
        # The null app owns no process, so it can only ask about every app.
        return None if any_app or profile is None else profile.image_name

    @group.command("list")
    def events_list(
        ctx: typer.Context,
        since: str = typer.Option(
            None,
            help=f"Only records newer than this, e.g. 5m, 2h (default {winevents.DEFAULT_SINCE}).",
        ),
        since_mark: bool = typer.Option(
            False, "--since-mark", help="Only what came after the last 'events mark'."
        ),
        kind: list[str] = KIND_OPTION,
        any_app: bool = typer.Option(
            False, "--any-app", help="Every app's records, not only this app's image."
        ),
        limit: int = typer.Option(50, help="Maximum records (newest kept)."),
    ):
        """Crash records, newest last. Windows: crash, hang, .NET and WER records from the Application log, WER report folders and LocalDumps. Linux: kernel fault and OOM lines from the journal, apport's log and .crash files, systemd-coredump, core files; apport writes a report only for a packaged executable. A source that cannot be read is listed in `unavailable`, never an error. JSON: image, since, count, records, unavailable (+ mark with --since-mark)."""

        def body():
            image = image_of(any_app)
            if not since_mark:
                return winevents.query(
                    image, since=since or winevents.DEFAULT_SINCE, kinds=kind, limit=limit
                )
            if since:
                raise PilotError("--since and --since-mark are mutually exclusive")
            path = winevents.mark_file(app_context(ctx).name)
            mark = winevents.Mark.load(path)
            return winevents.query(image, kinds=kind, limit=limit, mark=mark) | {"mark": str(path)}

        run(body)

    @group.command("mark")
    def events_mark(ctx: typer.Context):
        """Mark where every source stands now, for `list --since-mark`. JSON: mark (its file), record_id, reports, dumps (counts)."""

        def body():
            mark = winevents.Mark.now(image_of(False))
            path = mark.save(winevents.mark_file(app_context(ctx).name))
            return {
                "mark": str(path),
                "record_id": mark.record_id,
                "reports": len(mark.reports),
                "dumps": len(mark.dumps),
            }

        run(body)

    @group.command("dumps")
    def events_dumps():
        """Core dumps for the app's image, and whether the machine keeps them: on Windows LocalDumps, on Linux the kernel's core_pattern (apport, systemd-coredump). The tool never turns either on: that is a machine-wide, elevated change. JSON: image, configured, scope, folder, count, dumps, unavailable (on Linux also core_pattern, handler, apport_active)."""
        run(lambda: winevents.local_dumps(image_of(False)))

    return group


def given(path_arg: str | None, out: str | None) -> str | None:
    """The destination named by PATH or --out; both naming different files is
    a usage error."""
    if path_arg and out and path_arg != out:
        run(lambda: usage("give the destination once: PATH or --out"))
    return path_arg or out


def shot_destination(path_arg: str | None, out: str | None) -> str:
    return given(path_arg, out) or "shot.png"


def default_target(actx: AppContext) -> str:
    """What `shot` takes with no --selector: the app's window, or the whole
    desktop for an app with no window of its own (`pilot desktop`)."""
    return "app" if actx.profile is not None else "desktop"


def take_shot(actx: AppContext, selector: str, region, zoom, grid):
    """Capture, crop, zoom and grid, unsaved. Returns the image, its plan and
    the shot JSON without `path`."""
    box = imaging.parse_region(region)
    factor = imaging.parse_zoom(zoom)
    step = imaging.parse_grid(grid)
    control = ui.find(selector, actx)
    if ui.is_minimized(control):
        raise PilotError(f"{selector!r} is minimized; `ui activate` it first")
    plan = imaging.plan(ui.capture_origin(control), box, factor, ui.virtual_screen_rect())
    image, grid_info = imaging.render(ui.capture_rect(*plan.origin, *plan.size), plan, step)
    screen_rect = (*plan.origin, *plan.size)
    return (
        image,
        plan,
        {
            "origin": list(plan.origin),
            "size": list(plan.size),
            "scale": plan.scale,
            "image_size": list(image.size),
            "resample": plan.resample,
            "clipped": plan.clipped,
            "occluded": ui.is_occluded(control, screen_rect),
            "grid": grid_info,
        },
    )


def save_shot(actx: AppContext, command: str, image, plan, destination: str, frame: Frame) -> str:
    """Save with the frame chunk and journal the shot."""
    path = str(imaging.save_image(image, destination, frame=frame.as_dict()))
    screen_rect = (*plan.origin, *plan.size)
    fallback.record(
        actx,
        "shot",
        command,
        f"{path} region={','.join(map(str, screen_rect))} scale={plan.scale:g}",
    )
    return path


NO_POLICY_NOTE = "no prompt policy for this app: nothing answered; --close closes every dialog"


def add_single_commands(root: typer.Typer) -> None:
    @root.command("dialogs")
    def dialogs_cmd(
        ctx: typer.Context,
        dismiss: bool = typer.Option(
            False, help="Answer the prompts this app's policy knows; report the rest."
        ),
        close: bool = typer.Option(False, help="Close every open dialog, known or not."),
        prefer: str = typer.Option(
            ",".join(dialogs_mod.CLOSE_ORDER), help="Comma-separated button order for --close."
        ),
    ):
        """List the app's modal dialogs. Every answered or closed dialog is {title, text, answered}. JSON: dialogs; or dismissed + remaining (+ note without a policy); or closed + remaining."""

        def body():
            actx = app_context(ctx)
            pids = apps.pids(actx.profile) if actx.profile else None
            classes = dialogs_mod.dialog_classes(actx.profile)
            order = tuple(p.strip() for p in prefer.split(",") if p.strip())

            handles: list[int] = []

            def remaining():
                # After the answered windows are gone: a closed dialog the
                # app has not destroyed yet is not one that remains.
                return dialogs_mod.remaining(pids, handles, classes=classes)

            if close:
                if pids is None:
                    raise PilotError(
                        f"{actx.name!r} owns no process, so --close would click in every "
                        "dialog of every app; name the app: pilot <app> dialogs --close"
                    )
                closed = dialogs_mod.close_all(pids, prefer=order, classes=classes, handles=handles)
                return {"closed": closed, "remaining": remaining()}
            if dismiss:
                known = dialogs_mod.policy(actx)
                if not known:
                    return {"dismissed": [], "remaining": remaining(), "note": NO_POLICY_NOTE}
                answered = dialogs_mod.reap(pids, known, classes=classes, handles=handles)
                return {"dismissed": answered, "remaining": remaining()}
            return {"dialogs": remaining()}

        run(body)

    @root.command("state")
    def state_cmd(
        ctx: typer.Context,
        controls: int = typer.Option(0, help="Also list up to N visible controls."),
    ):
        """Process, windows, dialogs and focus at once. JSON: the whole picture."""
        run(lambda: state_mod.snapshot(app_context(ctx), controls=controls))

    @root.command("shot", short_help="Screenshot a window or a control: crop, zoom, grid.")
    def shot(
        ctx: typer.Context,
        path_arg: str = typer.Argument(None, metavar="[PATH]", help="Destination PNG."),
        out: str = typer.Option(None, "-o", "--out", help="Destination PNG (default shot.png)."),
        selector: str = typer.Option(
            None,
            help="What to capture; default the app's window (`app`), the whole desktop "
            "for `pilot desktop` (`desktop`). " + SELECTOR_HELP,
        ),
        region: str = typer.Option(
            None,
            "--region",
            help="Crop 'X,Y,W,H', in pixels of the plain shot of the same selector.",
        ),
        zoom: str = typer.Option(
            None,
            "--zoom",
            help=f"Integer 1-{imaging.MAX_ZOOM} (nearest) or 'auto' (the largest factor within "
            f"{imaging.AUTO_LONG_EDGE} px and {imaging.AUTO_MAX_PIXELS:,} px). "
            "Default: auto with --region, else 1.",
        ),
        grid: str = typer.Option(
            None,
            "--grid",
            help="Lines every STEP screen px, each crossing labelled 'X,Y' for ui click --at.",
        ),
    ):
        """Screenshot a window or a single control, optionally cropped, zoomed and gridded. A shot is a copy of the screen: a window on top of the target is in it (occluded says so), and a minimized target is refused. JSON: path, origin \\[x, y], size \\[w, h] (source px), scale, image_size, resample (none|nearest|lanczos), clipped, occluded, grid (null or step, label_every). Image to screen: X = origin.x + floor(ix / scale), same for y; grid labels are already screen coordinates."""
        destination = shot_destination(path_arg, out)

        def body():
            actx = app_context(ctx)
            target = selector or default_target(actx)
            image, plan, payload = take_shot(actx, target, region, zoom, grid)
            path = save_shot(actx, "shot", image, plan, destination, Frame(plan.origin, plan.scale))
            return {"path": path, **payload}

        run_for(ctx, "shot", body)

    @root.command(
        "capture", short_help="Dump screenshot, tree, state and events for a failure report."
    )
    def capture_cmd(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="Folder name under the artifacts directory."),
        directory: str = typer.Option(str(artifacts.DEFAULT_DIR), help="Artifacts directory."),
    ):
        """Dump the evidence for a failure report into DIRECTORY/NAME: window.png (the main window; the whole desktop for `pilot desktop`), state.json, tree.json, controls.json (a flat list), events.json (the system's crash records of the last 15 min) and manifest.json (this JSON). A piece that cannot be taken is listed in `failed`, never an error. JSON: dir, saved (piece -> file), failed (piece -> why), complete (whether the tree and controls walks covered the window), fallback_used, fallbacks."""
        run(lambda: artifacts.capture(name, app_context(ctx), directory=directory))
