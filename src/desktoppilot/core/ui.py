"""UI Automation layer: find, dump, and actually click a running app's UI.

Every entry point takes the AppContext, because which window "app" means is a
property of the command line, not of a saved session.

This module is the import point; the code lives in its parts: uia_read
(element reads), roots (core root resolvers), walk (the tree walk and dumps),
find (selectors to controls, waits), input (SendInput; on Linux, XTest from
linux/input.py), actions (clicks, typing, selection), count (list and tree
counts), capture (screen copies).
The parts call each other through this module (`ui.find`, not `find`), so a
test or a caller that patches `ui.X` reaches every caller of X. A name a part
needs at definition time (a default argument) it imports from the part that
defines it."""

from __future__ import annotations

import ctypes
import time

from . import uiacache
from .actions import (
    _ITEM_TYPES,
    _STRONG_PATTERNS,
    _WEAK_PATTERNS,
    ENABLED_POLL,
    POPUP_CLASSES,
    _centre,
    _click_by_pattern,
    _click_with_pointer,
    _enabled,
    _find_item,
    _is_enabled,
    _refusal,
    _target_window,
    _wait_enabled,
    activate,
    activate_app,
    click,
    close_window,
    copy_text,
    hover,
    hover_point,
    popup_roots,
    restore_app,
    scroll,
    select_item,
    send_keys,
    set_value,
    type_text,
)
from .capture import (
    _top_level_handle,
    capture_origin,
    capture_rect,
    is_minimized,
    is_occluded,
    screenshot,
    window_origin,
)
from .count import (
    _COLLAPSED_STATES,
    COUNT_CAP,
    COUNT_TIMEOUT,
    DEFAULT_NAMES,
    EXPAND_PASSES,
    EXPAND_TIMEOUT,
    _count_tree,
    _enumerate_items,
    _expand_all_walk,
    _expand_stats,
    _item_container,
    _label,
    _raw_name,
    _tally,
    _tree_nodes,
    _type_of,
    _with_names,
    count_items,
    expand,
    expand_all,
    is_tree_like,
)
from .errors import UIA_E_TIMEOUT
from .find import (
    find,
    find_all,
    is_gone,
    wait_for,
)
from .input import (
    _AT_POINT,
    _BUTTONS,
    _SHIFT_STATE,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MAPVK_VK_TO_VSC,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    VK_CONTROL,
    VK_MENU,
    VK_RETURN,
    VK_SHIFT,
    VK_TAB,
    _Input,
    _InputUnion,
    _key,
    _KeyboardInput,
    _mouse,
    _MouseInput,
    _press,
    _unicode_packets,
    _user32,
)
from .native import WINDOWS, auto, win32gui
from .roots import (
    CORE_ROOTS,
    DEFAULT_TIMEOUT,
    LEGACY_PROBE_TIMEOUT,
    PROBE_TIMEOUT,
    _check_root,
    _unknown_root,
    app_window,
    desktop_window,
    dialog_window,
    focused_window,
    main_window,
    named_window,
    present,
    resolve_root,
    valid_roots,
)
from .uia_read import (
    PATTERN_NAMES,
    _describe,
    _pattern,
    available_patterns,
    describe,
    text_of,
    value_of,
)
from .walk import (
    DEFAULT_DEPTH,
    _children,
    _children_or_cut,
    _matches,
    _search,
    _unique,
    _visible_rect,
    _walk_report,
    by_label,
    default_depth,
    first,
    flatten,
    identity,
    is_visible,
    matches,
    search,
    tree,
)

# The entry points the parts call through `ui`: SendInput on Windows, XTest
# on Linux. Same names on both, so a test patches `ui._send_input` either way.
if WINDOWS:
    from .input import (
        _keystroke,
        _keystrokes,
        _send_input,
        click_point,
        move_point,
        normalise_point,
        virtual_screen_rect,
    )
else:
    from ..linux.input import (
        _keystroke,
        _keystrokes,
        _send_input,
        click_point,
        move_point,
        normalise_point,
        virtual_screen_rect,
    )
