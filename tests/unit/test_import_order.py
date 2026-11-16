"""A split module imported before its facade still loads: the package
imports the facade first."""

from __future__ import annotations

import subprocess
import sys

# The word plugin needs Windows to import at all.
PARTS = [
    "desktoppilot.core.walk",
    *(["desktoppilot.plugins.word.frame"] if sys.platform == "win32" else []),
    "desktoppilot.plugins.vscode.locate",
]


def test_parts_import_before_their_facades():
    code = "; ".join(f"import {part}" for part in PARTS)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
