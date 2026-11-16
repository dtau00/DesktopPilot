"""`pilot <app> image`: annotate, crop, resize, pad and join images."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from . import annotate, imaging, ui
from .annotate import Frame
from .cmd_misc import default_target, save_shot, shot_destination, take_shot
from .commands import SELECTOR_HELP, app_context, run, run_for
from .errors import PilotError
from .registry import AppContext

IN_PLACE = typer.Option(False, "--in-place", help="Allow writing over SRC.")
ORIGIN = typer.Option(
    None, "--origin", help="Screen 'X,Y' of image pixel 0,0; default: the PNG's shot chunk."
)
SCALE = typer.Option(None, "--scale", help="Image px per screen px (with --origin).")
SPEC = typer.Option(None, "--spec", help="JSON file holding a list of ops, or - for stdin.")
OPS = typer.Option(None, "--op", help="One op as JSON; repeatable, runs after --spec.")
OP_TIMEOUT = typer.Option(15.0, help="Seconds to wait for each selector.")
JOIN_SOURCES = typer.Argument(..., help="Two or more images, in order.")


def _load_ops(spec: str | None, ops: list[str] | None) -> list[dict]:
    loaded: list = []
    if spec:
        try:
            raw = (
                sys.stdin.buffer.read().decode("utf-8-sig")
                if spec == "-"
                else Path(spec).expanduser().read_text(encoding="utf-8-sig")
            )
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise PilotError(f"--spec {spec}: {exc}") from exc
        loaded += data if isinstance(data, list) else [data]
    for text in ops or []:
        try:
            loaded.append(json.loads(text))
        except ValueError as exc:
            raise PilotError(f"--op is not JSON: {text!r} ({exc})") from exc
    if not loaded:
        raise PilotError("no ops: give --spec FILE or --op JSON")
    return loaded


def _output(src: str, out: str | None, verb: str, in_place: bool) -> Path:
    source = Path(src).expanduser().resolve()
    if out is None:
        target = source if in_place else source.with_name(f"{source.stem}-{verb}{source.suffix}")
    else:
        target = Path(out).expanduser().resolve()
    if target == source and not in_place:
        raise PilotError(f"would overwrite SRC {str(source)!r}; pass --in-place")
    return target


def _frame(img, origin: str | None, scale: float | None) -> Frame | None:
    if origin is None and scale is None:
        return annotate.read_frame(img)
    if origin is None or scale is None:
        raise PilotError("give --origin X,Y and --scale S together")
    try:
        x, y = (float(part) for part in origin.split(","))
    except ValueError as exc:
        raise PilotError(f"--origin wants 'X,Y'; got {origin!r}") from exc
    if scale <= 0:
        raise PilotError(f"--scale wants a positive number; got {scale}")
    return Frame((x, y), scale)


def _resolver(actx: AppContext, timeout: float):
    return lambda selector: ui.capture_origin(ui.find(selector, actx, timeout=timeout))


def _edit(ctx, src, out, verb, in_place, ops, origin=None, scale=None, timeout=15.0) -> dict:
    """Load SRC, apply `ops`, save to the output path with the moved frame."""
    target = _output(src, out, verb, in_place)
    img = annotate.load(src)
    image, frame, results = annotate.apply(
        img, ops, _frame(img, origin, scale), _resolver(app_context(ctx), timeout)
    )
    frame_dict = frame.as_dict() if frame else None
    saved = imaging.save_image(image, target, frame=frame_dict)
    return {
        "path": str(saved),
        "source": str(Path(src).expanduser().resolve()),
        "image_size": list(image.size),
        "frame": frame_dict,
        "annotations": results,
    }


def image_group() -> typer.Typer:
    group = typer.Typer(
        help="Edit and annotate images: boxes, arrows, labels, badges, highlight, blur, "
        "crop, resize, pad, join. Selectors land on a shot through its origin/scale."
    )
    ops_help = (
        f" Op types: {', '.join(annotate.TYPES)}. Targets: 'X,Y,W,H' or 'X,Y' image px, "
        "or a selector (needs the shot's frame: its PNG chunk, or --origin/--scale)."
    )

    @group.command(
        "annotate",
        help="Apply ops to SRC, in order; never writes SRC without --in-place. JSON: path, "
        "source, image_size, frame, annotations (each op's final box)." + ops_help,
    )
    def image_annotate(
        ctx: typer.Context,
        src: str = typer.Argument(..., help="Source image."),
        out: str = typer.Option(None, "-o", "--out", help="Default: <SRC stem>-annotated."),
        spec: str = SPEC,
        op: list[str] = OPS,
        origin: str = ORIGIN,
        scale: float = SCALE,
        in_place: bool = IN_PLACE,
        timeout: float = OP_TIMEOUT,
    ):
        run(
            lambda: _edit(
                ctx, src, out, "annotated", in_place, _load_ops(spec, op), origin, scale, timeout
            )
        )

    @group.command(
        "shot",
        help="`shot`, then apply ops with selectors resolved in the live UI; saved once. "
        "JSON: the shot's, with image_size after the ops, frame, annotations." + ops_help,
    )
    def image_shot(
        ctx: typer.Context,
        path_arg: str = typer.Argument(None, metavar="[PATH]", help="Destination image."),
        out: str = typer.Option(None, "-o", "--out", help="Destination (default shot.png)."),
        selector: str = typer.Option(
            None, help="What to capture; default as `shot`'s. " + SELECTOR_HELP
        ),
        region: str = typer.Option(None, "--region", help="As `shot --region`."),
        zoom: str = typer.Option(None, "--zoom", help="As `shot --zoom`."),
        grid: str = typer.Option(None, "--grid", help="As `shot --grid`."),
        spec: str = SPEC,
        op: list[str] = OPS,
        timeout: float = OP_TIMEOUT,
    ):
        destination = shot_destination(path_arg, out)

        def body():
            actx = app_context(ctx)
            ops = _load_ops(spec, op)
            target = selector or default_target(actx)
            image, plan, payload = take_shot(actx, target, region, zoom, grid)
            image, frame, results = annotate.apply(
                image, ops, Frame(plan.origin, plan.scale), _resolver(actx, timeout)
            )
            path = save_shot(actx, "image shot", image, plan, destination, frame)
            return {
                "path": path,
                **payload,
                "image_size": list(image.size),
                "frame": frame.as_dict(),
                "annotations": results,
            }

        run_for(ctx, "image shot", body)

    @group.command("crop")
    def image_crop(
        ctx: typer.Context,
        src: str = typer.Argument(..., help="Source image."),
        at: str = typer.Argument(..., help="'X,Y,W,H' image px, or a selector."),
        margin: int = typer.Option(0, help="Grow the box by N px each side."),
        out: str = typer.Option(None, "-o", "--out", help="Default: <SRC stem>-crop."),
        origin: str = ORIGIN,
        scale: float = SCALE,
        in_place: bool = IN_PLACE,
        timeout: float = OP_TIMEOUT,
    ):
        """Crop to a box, clamped to the image; the frame moves with it."""
        ops = [{"type": "crop", "at": at, "margin": margin}]
        run(lambda: _edit(ctx, src, out, "crop", in_place, ops, origin, scale, timeout))

    @group.command("resize")
    def image_resize(
        ctx: typer.Context,
        src: str = typer.Argument(..., help="Source image."),
        factor: float = typer.Option(None, "--scale", help="Factor; NEAREST when whole."),
        width: int = typer.Option(None, help="Target width, aspect kept."),
        height: int = typer.Option(None, help="Target height, aspect kept."),
        out: str = typer.Option(None, "-o", "--out", help="Default: <SRC stem>-resize."),
        in_place: bool = IN_PLACE,
    ):
        """Scale by a factor, or to a width or a height."""

        def body():
            given = {"scale": factor, "width": width, "height": height}
            given = {key: value for key, value in given.items() if value is not None}
            if len(given) != 1:
                raise PilotError("give exactly one of --scale, --width, --height")
            return _edit(ctx, src, out, "resize", in_place, [{"type": "resize", **given}])

        run(body)

    @group.command("pad")
    def image_pad(
        ctx: typer.Context,
        src: str = typer.Argument(..., help="Source image."),
        margin: int = typer.Option(0, help="Margin px on every side."),
        border: int = typer.Option(0, help="Border ring px, inside the new edge."),
        color: str = typer.Option(annotate.PAD_COLOR, help="Margin colour."),
        border_color: str = typer.Option(annotate.COLOR, help="Border colour."),
        out: str = typer.Option(None, "-o", "--out", help="Default: <SRC stem>-pad."),
        in_place: bool = IN_PLACE,
    ):
        """Add a margin, then a border ring."""
        op = {
            "type": "pad",
            "margin": margin,
            "border": border,
            "color": color,
            "border_color": border_color,
        }
        run(lambda: _edit(ctx, src, out, "pad", in_place, [op]))

    @group.command("join")
    def image_join(
        sources: list[str] = JOIN_SOURCES,
        out: str = typer.Option(..., "-o", "--out", help="Destination image."),
        vertical: bool = typer.Option(False, help="Stack top to bottom, aligned left."),
        gap: int = typer.Option(0, help="Px between images."),
        color: str = typer.Option(annotate.PAD_COLOR, help="Gap and background colour."),
    ):
        """Side by side (aligned top), or stacked with --vertical. No frame chunk. JSON: path, image_size, sources."""

        def body():
            if len(sources) < 2:
                raise PilotError("join wants two or more images")
            images = [annotate.load(source) for source in sources]
            image = annotate.join(images, vertical=vertical, gap=gap, color=color)
            path = imaging.save_image(image, out)
            return {"path": str(path), "image_size": list(image.size), "sources": sources}

        run(body)

    return group
