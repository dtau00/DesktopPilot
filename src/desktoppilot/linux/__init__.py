"""The Linux (X11) backend: the Win32 and uiautomation names the core calls,
implemented over AT-SPI (elements), EWMH/X11 through python-xlib (windows),
XTest (input), xclip (clipboard) and /proc (processes).

`core/native.py` exports these modules under the Windows names, so the core
runs unchanged. A window handle (`hwnd`) here is the X window id of the
client window. Every module imports without a display: what needs one opens
it on first use (`x11.display()`).
"""

# The core first, as the core loads its `ui` facade first: core/ui.py imports
# linux/input.py, so a backend module imported on its own (one that imports
# core.errors) would otherwise be asked for its names half-loaded.
import desktoppilot.core  # noqa: F401
