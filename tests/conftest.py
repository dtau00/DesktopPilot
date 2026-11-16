"""Off Windows, what needs Windows skips with the reason, in two ways:

- a module in WINDOWS_ONLY skips whole, instead of failing to import: it
  imports the word plugin (COM, winreg, pythoncom) or tests only SendInput;
- a test marked `@pytest.mark.windows("why")` (or a module with that
  `pytestmark`) tests Win32-only behaviour: tasklist and taskkill argv, DPI
  calls, the event log, consoles.

On Windows neither is read and nothing skips."""

from __future__ import annotations

import sys
from fnmatch import fnmatch

import pytest

WINDOWS_ONLY = {
    "test_word_*.py": "the word plugin (Word over COM)",
    "test_keystrokes.py": "SendInput keystrokes on the Windows keyboard layout",
    "test_pointer.py": "SendInput absolute coordinates",
}


class _WindowsOnly(pytest.Module):
    reason = ""

    def collect(self):
        pytest.skip(f"Windows only: {self.reason}")


def pytest_configure(config):
    config.addinivalue_line("markers", "windows(reason): tests Win32-only behaviour")


def pytest_collection_modifyitems(config, items):
    if sys.platform == "win32":
        return
    for item in items:
        marker = item.get_closest_marker("windows")
        if marker is not None:
            why = marker.args[0] if marker.args else "Win32 behaviour"
            item.add_marker(pytest.mark.skip(reason=f"Windows only: {why}"))


def pytest_pycollect_makemodule(module_path, parent):
    if sys.platform == "win32":
        return None
    for pattern, reason in WINDOWS_ONLY.items():
        if fnmatch(module_path.name, pattern):
            module = _WindowsOnly.from_parent(parent, path=module_path)
            module.reason = reason
            return module
    return None
