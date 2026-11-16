"""Backstage, the File menu: a root while it is open, and opening and closing it."""

from __future__ import annotations

from desktoppilot.core import ui
from desktoppilot.core.errors import NotPresent
from desktoppilot.core.registry import AppContext
from desktoppilot.core.util import wait_until

from . import roots
from .frame import DEFAULT_TIMEOUT

BACKSTAGE_NAME = "Backstage view"
BACKSTAGE_CLOSED = "Backstage is not open; open it first (pilot word backstage open)"


def backstage(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT, probe: bool = False):
    """The File menu, once it is open. It only exists in the tree while open,
    so a closed one raises NotPresent rather than resolving to the frame; on a
    busy Word, WalkCut (see _poll_frame_walk). A probe walks once."""
    hit = roots._poll_frame_walk(
        ctx, BACKSTAGE_NAME, depth=4, what="Backstage", timeout=timeout, probe=probe
    )
    if hit is None:
        raise NotPresent(BACKSTAGE_CLOSED)
    return hit


def open_backstage(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT):
    roots.send_keys(ctx, "{Alt}f")
    return backstage(ctx, timeout=timeout)


def close_backstage(ctx: AppContext, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    roots.send_keys(ctx, "{Esc}")
    wait_until(lambda: ui.is_gone("backstage", ctx), timeout=timeout, what="Backstage to close")
    return True
