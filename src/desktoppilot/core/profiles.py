r"""App profiles: everything app-specific lives in TOML data, not in code.

Adding an app is a file, not a patch. A plugin carries its profile next to its
code, so `plugins/<name>/profile.toml` is searched too. So is the folder of a
plugin another installed package names in the `desktoppilot.plugins` entry-point
group (name = plugin name, value = the plugin's package).

Search order: the user's profiles (%LOCALAPPDATA%\desktoppilot\profiles;
on Linux $XDG_CONFIG_HOME/desktoppilot/profiles), the built-in plugins, the
external plugins, then the ones shipped here. A name that is an existing .exe path (on Linux, any executable
file) becomes an ad-hoc profile instead.

`platforms` names where a profile runs; a `[windows]` or `[linux]` table
replaces any of its keys on that platform (`exe`, `window_class`, ...).

`[options]` holds a plugin's own settings: the core checks only that it is a
table, and the plugin reads it from `ctx.profile.options`. A child merges its
options over its parent's, key by key.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from importlib import metadata
from pathlib import Path

from . import paths
from .errors import PilotError
from .native import PLATFORM, WINDOWS

BUILTIN_DIR = Path(__file__).parent.parent / "profiles"
PLUGINS_DIR = Path(__file__).parent.parent / "plugins"
USER_DIR = paths.PROFILES_DIR
ENTRY_POINT_GROUP = "desktoppilot.plugins"
PLATFORMS = ("windows", "linux")

MAX_EXTENDS_DEPTH = 8
DEFAULT_ROOT_DEPTH = 20
LAUNCH_POLICIES = ("fresh", "adopt")
DIALOG_POLICIES = ("auto", "explicit")


@dataclass
class Profile:
    name: str
    exe: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    window_class: list[str] = field(default_factory=list)
    title_strip: list[str] = field(default_factory=list)
    dialog_classes: list[str] = field(default_factory=list)
    window_size: list[int] | None = None
    ready_timeout: float = 60.0
    launch: str = "adopt"  # "fresh" kills a running instance, "adopt" joins it
    dialogs: str = "explicit"  # "auto" lets a plugin policy answer prompts
    depth: int = 12
    root_depth: int = DEFAULT_ROOT_DEPTH
    extends: str | None = None
    notes: str = ""
    platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    # A plugin's own settings, read by the plugin, never by the core.
    options: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.options, dict):
            raise PilotError(f"profile {self.name!r}: [options] must be a table")
        # A typo here would silently fall back to the safe-looking branch.
        if self.launch not in LAUNCH_POLICIES:
            raise PilotError(
                f"profile {self.name!r}: launch must be one of {LAUNCH_POLICIES}, "
                f"got {self.launch!r}"
            )
        if self.dialogs not in DIALOG_POLICIES:
            raise PilotError(
                f"profile {self.name!r}: dialogs must be one of {DIALOG_POLICIES}, "
                f"got {self.dialogs!r}"
            )
        bad = [p for p in self.platforms if p not in PLATFORMS]
        if bad or not self.platforms:
            raise PilotError(
                f"profile {self.name!r}: platforms must be a non-empty list of "
                f"{PLATFORMS}, got {self.platforms!r}"
            )

    @property
    def supported(self) -> bool:
        """Whether the profile runs on this platform."""
        return PLATFORM in self.platforms

    def resolve_exe(self) -> Path:
        """The first candidate that exists. On Linux a bare name (no
        directory part) is looked up on PATH."""
        for candidate in self.exe:
            expanded = os.path.expandvars(candidate)
            if not WINDOWS and os.sep not in expanded:
                found = shutil.which(expanded)
                if found:
                    return Path(found)
                continue
            path = Path(expanded).expanduser()
            if path.exists():
                return path
        raise PilotError(f"profile {self.name!r}: none of its exe candidates exist ({self.exe})")

    @property
    def image_name(self) -> str:
        """Process image name, from the first candidate - the exe need not
        exist to ask "is it running", and a missing install should surface as
        "not running", not as a resolve failure."""
        return Path(os.path.expandvars(self.exe[0])).name if self.exe else self.name


@dataclass(frozen=True)
class ExternalPlugin:
    """A plugin another installed package ships: `package` is importable,
    `folder` holds its profile.toml, plugin.py and NOTES.md."""

    name: str
    package: str
    folder: Path


def _shipped_names(plugins_dir: Path, builtin_dir: Path) -> set[str]:
    """Every app name desktoppilot ships: its plugins and its profiles."""
    names: set[str] = set()
    if plugins_dir.is_dir():
        names |= {
            child.name
            for child in plugins_dir.iterdir()
            if (child / "__init__.py").is_file() or (child / "profile.toml").is_file()
        }
    if builtin_dir.is_dir():
        names |= {path.stem for path in builtin_dir.glob("*.toml")}
    return names


def _entry_points() -> list[metadata.EntryPoint]:
    return list(metadata.entry_points(group=ENTRY_POINT_GROUP))


def external_plugins() -> tuple[dict[str, ExternalPlugin], dict[str, str]]:
    """The plugins other packages declare, and the ones that cannot load
    (key -> why). Read once per process: a profile load asks for them on
    every lookup. A name desktoppilot ships (plugin or profile) wins a clash."""
    return _external_plugins(PLUGINS_DIR, BUILTIN_DIR)


def broken_key(name: str, package: str) -> str:
    """The `reg.broken` key of an external plugin that does not load. It is
    never a profile name, so it hides no shipped or user profile."""
    return f"{name}={package}"


@functools.cache
def _external_plugins(
    plugins_dir: Path, builtin_dir: Path
) -> tuple[dict[str, ExternalPlugin], dict[str, str]]:
    shipped = _shipped_names(plugins_dir, builtin_dir)
    found: dict[str, ExternalPlugin] = {}
    broken: dict[str, str] = {}
    for entry in sorted(_entry_points(), key=lambda e: (e.name, e.value)):
        name, package = entry.name, entry.value
        key = broken_key(name, package)
        where = f"entry point {name!r} = {package!r}"
        if name in shipped:
            broken[key] = f"{where}: name {name!r} already shipped by desktoppilot; not loaded"
            continue
        if name in found:
            broken[key] = (
                f"{where}: name {name!r} already taken by {found[name].package!r}; not loaded"
            )
            continue
        try:
            # Imports the parent packages, never the plugin package itself.
            spec = importlib.util.find_spec(package)
        except Exception as exc:  # a broken package must not stop discovery
            broken[key] = f"{where}: {type(exc).__name__}: {exc}"
            continue
        locations = list(spec.submodule_search_locations or []) if spec else []
        if not locations:
            broken[key] = f"{where}: not an importable package"
            continue
        folder = Path(locations[0])
        if not (folder / "profile.toml").is_file():
            broken[key] = f"{where}: no profile.toml in {folder}"
            continue
        found[name] = ExternalPlugin(name, package, folder)
    return found, broken


def plugin_folders() -> dict[str, Path]:
    """Plugin name -> the folder holding its profile.toml, plugin.py and
    NOTES.md: the built-ins, then the external plugins."""
    folders: dict[str, Path] = {}
    if PLUGINS_DIR.is_dir():
        folders |= {
            child.name: child
            for child in sorted(PLUGINS_DIR.iterdir())
            if (child / "profile.toml").is_file()
        }
    folders |= dict(sorted((n, p.folder) for n, p in external_plugins()[0].items()))
    return folders


def _search_paths() -> list[tuple[str, Path]]:
    """(name, path) for every profile file, nearest source first."""
    found: list[tuple[str, Path]] = []
    if USER_DIR.is_dir():
        found += [(path.stem, path) for path in sorted(USER_DIR.glob("*.toml"))]
    found += [(name, folder / "profile.toml") for name, folder in plugin_folders().items()]
    if BUILTIN_DIR.is_dir():
        found += [(path.stem, path) for path in sorted(BUILTIN_DIR.glob("*.toml"))]
    return found


def _path_of(name: str) -> Path | None:
    for candidate, path in _search_paths():
        if candidate == name:
            return path
    return None


def _read_data(path: Path, name: str) -> dict:
    """The file's keys, with this platform's override table applied. Both
    tables are checked on every platform, so a typo under [linux] fails on
    Windows too."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PilotError(f"profile {path}: {exc}") from exc
    known = {f.name for f in fields(Profile)}
    overrides = {platform: data.pop(platform) for platform in PLATFORMS if platform in data}
    for platform, table in overrides.items():
        if not isinstance(table, dict):
            raise PilotError(f"profile {path}: [{platform}] must be a table")
        unknown = set(table) - (known - {"name"})
        if unknown:
            raise PilotError(f"profile {path}: unknown keys in [{platform}] {sorted(unknown)}")
    data |= overrides.get(PLATFORM, {})
    data.setdefault("name", name)
    unknown = set(data) - known
    if unknown:
        raise PilotError(f"profile {path}: unknown keys {sorted(unknown)}")
    return data


def _read(path: Path, name: str | None = None) -> Profile:
    return Profile(**_read_data(path, name or path.stem))


def available() -> list[str]:
    return sorted({name for name, _ in _search_paths()})


def adhoc_exe(token: str) -> Path | None:
    """The executable an ad-hoc app token names, or None: an existing .exe
    path on Windows, any existing executable file on Linux."""
    path = Path(os.path.expandvars(token)).expanduser()
    if WINDOWS:
        return path if path.suffix.lower() == ".exe" and path.is_file() else None
    return path if path.is_file() and os.access(path, os.X_OK) else None


def load(name: str) -> Profile:
    direct = adhoc_exe(name)
    if direct is not None:
        # The stem on Windows (WINWORD.EXE -> WINWORD); the whole name on
        # Linux, where python3.12 is not "python3".
        return Profile(name=direct.stem if WINDOWS else direct.name, exe=[str(direct)])

    data = _inherited_data(name, depth=0)
    return Profile(**data)


def _inherited_data(name: str, *, depth: int) -> dict:
    """A child's own keys win; everything it does not set comes from its parent."""
    if depth > MAX_EXTENDS_DEPTH:
        raise PilotError(f"profile {name!r}: extends chain is too deep or circular")
    path = _path_of(name)
    if path is None:
        raise PilotError(f"unknown profile {name!r}; available: {available()}")
    data = _read_data(path, name)
    parent = data.get("extends")
    if not parent:
        return data
    inherited = _inherited_data(parent, depth=depth + 1)
    merged = inherited | data | {"name": name, "extends": parent}
    if isinstance(inherited.get("options"), dict) and isinstance(data.get("options"), dict):
        merged["options"] = inherited["options"] | data["options"]
    return merged
