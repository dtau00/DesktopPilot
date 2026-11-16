"""The fallback rule: deterministic calls first, vision last, always marked.

The tool never calls a model. When a deterministic call fails it leaves a
screenshot behind and says where, so the agent driving the tests - which IS
the vision model - can look at it once the deterministic options are spent.
Every such step is journalled, so `capture` can say which parts of a run only
worked because somebody looked at a picture.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from . import paths, ui
from .registry import AppContext

SHOT_DIR = paths.STATE_DIR / "shots"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]


def shot_path(app: str, command: str) -> Path:
    """Under the per-app state dir, never the cwd: a failure screenshot is
    tooling state, and a test run that litters its working directory is
    unusable in CI."""
    return SHOT_DIR / app / f"{_stamp()}-{paths.slugify(command)}.png"


# ------------------------------------------------------------------ journal


def journal_path(app: str) -> Path:
    """Next to the session file, so the two are wiped by the same run."""
    return paths.SESSION_DIR / f"{app}.fallbacks.jsonl"


def record(ctx: AppContext, kind: str, command: str, detail: str | None = None) -> dict:
    """Append one event. Never fatal: losing the mark must not lose the run.
    `kind` is failure_screenshot, coordinates, shot, or record (`record
    start` / `stop`)."""
    event = {"ts": time.time(), "kind": kind, "command": command, "detail": detail}
    try:
        path = journal_path(ctx.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a command
        pass
    return event


def events(app: str) -> list[dict]:
    path = journal_path(app)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def used(app: str) -> bool:
    return bool(events(app))


def truncate(app: str) -> None:
    """A fresh run starts with an empty journal, so `fallback_used` answers
    for THIS run and not for everything the machine ever did."""
    journal_path(app).unlink(missing_ok=True)


# ------------------------------------------------------- screenshot on failure


def _target(ctx: AppContext):
    """The app's main window, the desktop when it has none - a failure whose
    cause is "the app is gone" still has to produce a picture."""
    if ctx is not None and ctx.profile is not None:
        return ui.main_window(ctx, timeout=2.0)
    return ui.desktop_window()


def on_failure(ctx: AppContext, command: str) -> dict:
    """The keys a failed command adds to its error JSON. The original error is
    never masked: a screenshot that fails reports why and nothing more."""
    if ctx is None or not getattr(ctx, "shot_on_failure", True):
        return {}
    try:
        path = ui.screenshot(_target(ctx), shot_path(ctx.name, command))
    except Exception as exc:  # noqa: BLE001 - evidence is best effort
        return {"screenshot": None, "screenshot_error": str(exc)}
    record(ctx, "failure_screenshot", command, str(path))
    return {"screenshot": str(path)}
