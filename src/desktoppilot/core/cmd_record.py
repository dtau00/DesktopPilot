"""`pilot <app> record`: the app's window, the screen or a region to MP4."""

from __future__ import annotations

import typer

from ..linux import recording as linux_recording
from . import fallback, recording
from .cmd_misc import given
from .commands import app_context, run, run_for


def record_group() -> typer.Typer:
    group = typer.Typer(
        help="Record the app's main window, the screen or a region to MP4 (ffmpeg), "
        "with the pointer drawn in."
    )

    @group.command("start")
    def record_start(
        ctx: typer.Context,
        path_arg: str = typer.Argument(
            None, metavar="[PATH]", help="Destination MP4; a bare name goes in Videos."
        ),
        out: str = typer.Option(
            None,
            "-o",
            "--out",
            help="Destination MP4; a bare name goes in your Videos folder. "
            "Default: <Videos>/<app>-YYYYMMDD-HHMMSS.mp4.",
        ),
        full_screen: bool = typer.Option(
            False,
            "--full-screen",
            help="Every monitor, not the app's window (the default for `pilot desktop`).",
        ),
        region: str = typer.Option(
            None, "--region", help="Screen box 'X,Y,W,H' in pixels, not the app's window."
        ),
        margin: str = typer.Option(
            None,
            "--margin",
            help="Grow the window's rect by 'L,T,R,B' screen pixels (or 'N' on every side), "
            "cut to the screen, for a menu or popup drawn past its edge; the cover check "
            "covers the margin too. Not with --full-screen, --region or --follow.",
        ),
        fps: int = typer.Option(30, "--fps", help="Frames per second."),
        mic: str = typer.Option(
            None,
            "--mic",
            help="Record this microphone too: on Windows a DirectShow name (list them with: "
            "ffmpeg -list_devices true -f dshow -i dummy), on Linux a PulseAudio source "
            f"(list them with: {linux_recording.MIC_HINT}).",
        ),
        overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing file."),
        follow: bool = typer.Option(
            False,
            "--follow",
            help="Follow the window if it moves; on Windows parts drawn through "
            "DirectComposition (Notepad's tab strip, WebView2 panes) record black; on Linux "
            "the window's own pixels, without the title bar.",
        ),
        allow_covered: bool = typer.Option(
            False,
            "--allow-covered",
            help="Record even if another window covers the area; the agent asks the user first.",
        ),
    ):
        """Start recording in the background and return once the file is being written. Confirm with the user what to record (which window, the full screen, or a region) unless the request already says so; mic is off unless --mic. By default the window is brought to the front and the screen under its rect at start is recorded: a moved window is not followed, a window on top is in the video (clipped: the rect was cut to the screen; margin: what --margin added). The terminal this runs in is minimized meanwhile and `stop` restores it. Another app's window over the area (the window's, or a --region of an app that is open; never --full-screen) stops the start: tell the user which, ask whether to close them or continue, and only then rerun with --allow-covered. JSON: path, target, fps, mic, pid, log, minimized_windows, covering - or the Covered error with details.covering."""
        destination = given(path_arg, out)

        def body():
            actx = app_context(ctx)
            result = recording.start(
                actx,
                out=destination,
                full_screen=full_screen,
                region=region,
                fps=fps,
                mic=mic,
                overwrite=overwrite,
                follow=follow,
                allow_covered=allow_covered,
                margin=margin,
            )
            target = recording.describe(result["target"])
            fallback.record(actx, "record", "record start", f"{result['path']} target={target}")
            return result

        run_for(ctx, "record start", body)

    @group.command("stop")
    def record_stop(
        ctx: typer.Context,
        force: bool = typer.Option(
            False, "--force", help="Kill ffmpeg instead; the file may not play."
        ),
    ):
        """Stop recording, wait until the MP4 is finished and playable, bring back the terminal `start` minimized, and report the file. JSON: path, duration_s, size_bytes, size_mb, video (codec, width, height, fps, frames, bit_rate_kbps), audio (null: no sound), target, started, stopped, exit_code, restored_windows, log (+ warning)."""

        def body():
            actx = app_context(ctx)
            result = recording.stop(actx, force=force)
            duration = result["duration_s"] or 0.0
            fallback.record(actx, "record", "record stop", f"{result['path']} {duration:.1f}s")
            return result

        run_for(ctx, "record stop", body)

    @group.command("status")
    def record_status(ctx: typer.Context):
        """Whether a recording is running. One whose ffmpeg ended on its own (the window closed) is reported once as stale, then forgotten. JSON: recording, path, started, elapsed_s, target, pid - or recording: false."""
        run(lambda: recording.status(app_context(ctx)))

    @group.command("gif")
    def record_gif(
        src: str = typer.Argument(..., help="Source video."),
        out: str = typer.Option(
            None, "-o", "--out", help="A bare name goes in Videos. Default: SRC with .gif."
        ),
        fps: int = typer.Option(10, "--fps", help="Frames per second."),
        width: int = typer.Option(800, "--width", help="Width in px, aspect kept."),
    ):
        """Turn a recording into a GIF through a palette made for it. JSON: path, size_bytes."""
        run(lambda: recording.gif(src, out, fps=fps, width=width))

    return group
