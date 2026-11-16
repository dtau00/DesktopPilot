"""Word's selector roots: the frame, the ribbon, task panes, Backstage, the
add-in's own windows - and the ribbon commands built on them.

Core's roots are generic; Word's are not. `app` is the OpusApp frame rather
than the largest window the process owns, and `win:NAME` means a window the
ADD-IN opened, so both override the core root of the same name.

This module is the import point; the code lives in frame, panes,
addin_windows, ribbon and backstage. Those call each other through this
module (`roots.word_window`, not `word_window`), so a patch on `roots.X`
reaches every caller of X.
"""

from __future__ import annotations

import uiautomation as auto
import win32gui

from desktoppilot.core import apps
from desktoppilot.core.errors import Timeout

from . import session
from .addin_windows import (
    CLOSE_IDS,
    CLOSE_TIMEOUT,
    UNTITLED,
    WPF_WINDOW_CLASS_PREFIX,
    _close,
    _is_wpf_window,
    addin_window,
    close_addin_windows,
    top_windows,
    untitled_addin_windows,
)
from .backstage import (
    BACKSTAGE_CLOSED,
    BACKSTAGE_NAME,
    backstage,
    close_backstage,
    open_backstage,
)
from .frame import (
    DEFAULT_TIMEOUT,
    FRAME_MAKES,
    FRAME_MAKES_KEPT,
    FRAMES_KEPT,
    SETTLE_FAST,
    SETTLE_POLL,
    SETTLE_TIMEOUT,
    _frames,
    _left,
    _make_frame,
    _poll_frame_walk,
    frame_hwnd,
    settle,
    word_window,
)
from .panes import (
    CONTAINER_TYPES,
    HEADER_DEPTH,
    MIN_PANE_WIDTH,
    PANE_HOSTS_OPTION,
    PANE_WALK_DEPTH,
    WPF_WINDOW_UIA_CLASS,
    _addin_ui,
    _header_texts,
    _is_host,
    _not_a_pane,
    _pane_ancestor,
    _pane_walk,
    _visible,
    _wide,
    host_header,
    host_titled,
    pane_hosts,
    task_pane,
    wpf_hosts,
)
from .ribbon import (
    LOWER_RIBBON_CLASS,
    POPUP_CLASS,
    TAB_MIN_WALKS,
    TAB_POLL,
    TAB_RETRY,
    _find_tab,
    _labels,
    _tab_items,
    _tab_panel,
    open_tab,
    popup,
    popup_roots,
    ribbon,
    ribbon_click,
    ribbon_controls,
    ribbon_tabs,
    select_tab,
    send_keys,
)
from .session import WORD_WINDOW_CLASS
from .view import DOCUMENT_HOST_CLASS
