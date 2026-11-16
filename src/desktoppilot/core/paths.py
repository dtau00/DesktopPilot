"""Where the tool keeps its state, and how a name becomes a file name.

Its own module so that fallback, winevents and the plugins can find a path
without importing the process lifecycle. On Windows the state is under
%LOCALAPPDATA%; on Linux under $XDG_STATE_HOME and user profiles under
$XDG_CONFIG_HOME, as the XDG base directory spec places them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .native import WINDOWS


def _xdg(variable: str, default: str) -> Path:
    """An XDG base directory; unset, empty or relative means the default."""
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / default


if WINDOWS:
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "desktoppilot"
    PROFILES_DIR = STATE_DIR / "profiles"
else:
    STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "desktoppilot"
    PROFILES_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "desktoppilot" / "profiles"
SESSION_DIR = STATE_DIR / "sessions"


def slugify(name: str) -> str:
    """A file-name-safe form of a test id or a command name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120] or "unnamed"
