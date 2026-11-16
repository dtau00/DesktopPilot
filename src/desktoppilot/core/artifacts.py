"""Failure evidence: screenshot, control tree, app state, the system's crash records.

Collected automatically when a test fails, because by the time you read the
report the app is gone and the UI that misbehaved is unreproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import fallback, paths, ui, winevents
from . import state as state_mod
from .registry import AppContext

DEFAULT_DIR = Path("artifacts")
# Long enough to cover a test that led up to the failure, short enough to
# leave out yesterday's crash.
EVENTS_SINCE = "15m"
TREE_DEPTH = 6


def _window(ctx: AppContext):
    """The app's main window; the desktop for an app with no window of its
    own (`pilot desktop`)."""
    return ui.main_window(ctx) if ctx.profile is not None else ui.desktop_window()


def capture(name: str, ctx: AppContext, *, directory: Path | str = DEFAULT_DIR) -> dict:
    """Best-effort dump. Every piece is optional: a crashed app must still
    produce whatever evidence is left rather than raising inside teardown."""
    target = Path(directory) / paths.slugify(name)
    target.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    problems: dict[str, str] = {}
    walks: dict[str, dict] = {"tree": {}, "controls": {}}

    def attempt(key: str, fn) -> None:
        try:
            result = fn()
            if result is not None:
                saved[key] = str(result)
        except Exception as exc:  # noqa: BLE001 - evidence gathering never fails a run
            problems[key] = str(exc)

    attempt("screenshot", lambda: ui.screenshot(_window(ctx), target / "window.png"))
    attempt("state", lambda: _write_json(target / "state.json", state_mod.snapshot(ctx)))
    attempt(
        "tree",
        lambda: _write_json(
            target / "tree.json",
            ui.tree(_window(ctx), depth=TREE_DEPTH, limit=1500, report=walks["tree"]),
        ),
    )
    attempt(
        "controls",
        lambda: _write_json(
            target / "controls.json",
            ui.flatten(_window(ctx), depth=12, limit=600, report=walks["controls"]),
        ),
    )
    image = ctx.profile.image_name if ctx.profile else None
    attempt(
        "events",
        lambda: _write_json(target / "events.json", winevents.query(image, since=EVENTS_SINCE)),
    )
    marks = fallback.events(ctx.name)
    manifest = {
        "dir": str(target),
        "saved": saved,
        "failed": problems,
        # false: the app timed out a children read, so that dump is partial
        "complete": {key: walk["complete"] for key, walk in walks.items() if walk},
        "fallback_used": bool(marks),
        "fallbacks": marks,
    }
    attempt("manifest", lambda: _write_json(target / "manifest.json", manifest))
    return manifest


def _write_json(path: Path, payload) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return path
