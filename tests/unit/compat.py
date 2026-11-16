"""What the core tests need that differs by platform, and nothing else."""

from __future__ import annotations

import sys

# A shipped app with a profile and no plugin, for tests of the core command
# tree: notepad is for Windows only.
PLAIN_APP = "notepad" if sys.platform == "win32" else "code"

try:
    from _ctypes import COMError
except ImportError:

    class COMError(Exception):
        """A stand-in with the real one's shape (args and `hresult`), which
        is all `errors.is_uia_timeout` and the core read."""

        def __init__(self, hresult, text, details):
            super().__init__(hresult, text, details)
            self.hresult, self.text, self.details = hresult, text, details


__all__ = ["PLAIN_APP", "COMError"]
