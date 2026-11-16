"""The platform seam: native.py hands the core the real Win32 and
uiautomation modules on Windows, the desktoppilot.linux ones on Linux, and
every core module imports on both."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys

import pytest

import desktoppilot.core
from desktoppilot.core import apps, native, ui

NAMES = ("auto", "win32api", "win32clipboard", "win32con", "win32gui", "win32process")


def test_exactly_one_platform_is_this_one():
    windows, linux = sys.platform == "win32", sys.platform.startswith("linux")
    assert (windows, linux) == (native.WINDOWS, native.LINUX)
    assert ("windows" if windows else "linux") == native.PLATFORM


@pytest.mark.parametrize("name", NAMES)
def test_each_name_comes_from_this_platforms_backend(name):
    module = getattr(native, name)
    if native.WINDOWS:
        expected = "uiautomation" if name == "auto" else name
        assert module.__name__.split(".")[0] == expected
    else:
        assert module.__name__ == f"desktoppilot.linux.{name}"


def test_the_core_modules_hold_the_native_modules_under_the_old_names():
    """So a test that patches `apps.win32gui.X` reaches the module the core calls."""
    assert apps.win32gui is native.win32gui
    assert ui.auto is native.auto


def test_ui_exports_the_input_entry_points_of_this_platform():
    source = "desktoppilot.core.input" if native.WINDOWS else "desktoppilot.linux.input"
    for name in ("click_point", "normalise_point", "virtual_screen_rect", "_keystrokes"):
        assert getattr(ui, name).__module__ == source
    assert ui._send_input.__module__ == source


def test_every_core_module_imports():
    for info in pkgutil.iter_modules(desktoppilot.core.__path__):
        importlib.import_module(f"desktoppilot.core.{info.name}")


def test_another_platform_is_refused_at_import():
    code = "import sys; sys.platform = 'darwin'; import desktoppilot.core"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode != 0
    assert "drives Windows and Linux" in result.stderr
