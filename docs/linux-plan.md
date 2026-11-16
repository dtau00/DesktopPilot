# Linux support

Status: open (merged into `main` 2026-10-21). Steps 1-5 done, except:
- **Windows run** not done: unit gate, ruff, Word smoke gate. Risk spots:
  `core/native.py` imports, `core/ui.py` input re-exports, tests that
  patch `core.cdp`, the `chrome` profile's Windows paths, `profiles.adhoc_exe`,
  `app launch` now drops `ELECTRON_RUN_AS_NODE` on Windows too, and the
  cross-platform JSON changes (`ui expand`, `ui select`,
  `ui count` through wrappers, `dialogs --close`, `pilot desktop shot`).
- Live check on Linux: done 2026-10-21. Every file in `tests/ui/linux`
  passes (`PILOT_TEST_ACTIVATE=1`), those JSON fixes included.
Delete this plan when these are done.

Goal: `pilot` drives a Linux X11 desktop with the same commands, selectors,
JSON and error kinds as on Windows. Scope: X11 only (no Wayland). Plugins on
Linux: the core groups, `code`/`vscode`, and Chrome / Electron. `word`
and `notepad` stay Windows-only.

## Design: one core, two native seams

The core is written against two seams, and only those change per platform:

1. **Elements**: the `uiautomation.Control` duck type (`Name`,
   `ControlTypeName`, `AutomationId`, `ClassName`, `BoundingRectangle`,
   `IsOffscreen`, `IsEnabled`, `GetChildren`, `GetParentControl`,
   `GetRuntimeId`, `NativeWindowHandle`, `ProcessId`, `Get*Pattern`, ...).
2. **Windows and input**: the subset of `win32gui`, `win32process`,
   `win32api`, `win32con`, `win32clipboard` the core calls, plus `SendInput`.

`core/native.py` is the only place the core imports them. On Windows it
re-exports the real modules, so the Windows behaviour and every test that
patches `apps.win32gui.X` stay the same. On Linux it exports the modules of
`desktoppilot/linux/`, which implement the same names over AT-SPI (elements),
EWMH/X11 through python-xlib (windows), XTest (input) and xclip (clipboard).
A window handle (`hwnd`) on Linux is the X window id of the client window.

What has no Win32 shape (processes, DPI, crash records, recording, the
pytest crash check) branches on `native.WINDOWS` at function level, with the
Linux code in `desktoppilot/linux/`.

## The Linux backend: `src/desktoppilot/linux/`

| module | implements | over |
| --- | --- | --- |
| `x11.py` | the X11/EWMH primitives below | python-xlib |
| `win32gui.py`, `win32process.py`, `win32api.py`, `win32con.py` | the Win32 names the core and plugins call | `x11.py` |
| `win32clipboard.py` | `OpenClipboard` .. `GetClipboardSequenceNumber`, `CF_UNICODETEXT` | xclip (CLIPBOARD selection) |
| `input.py` | pointer and keys: `click_point`, `move_to`, `wheel`, `send_keys` (uiautomation key syntax), `type_text` (keysym per char, Unicode by a spare keycode remap) | XTest |
| `auto.py` | `Control` over `Atspi.Accessible`, `ControlFromHandle`, `GetRootControl`, `GetFocusedControl`, `SendKeys`, `MoveTo`, `WheelDown`, `ExpandCollapseState`, `PatternId`, `Bitmap` | AT-SPI (gi `Atspi` 2.0) |
| `roles.py` | AT-SPI role -> UIA control type name table | - |
| `procs.py` | `tasklist`-shaped rows, kill, parents, creation time | `/proc` |
| `events.py` | crash records: kernel segfault/OOM lines, apport's log (every signal, SIGABRT too), apport `.crash` files, systemd-coredump | `journalctl`, `/var/log/apport.log`, `/var/crash` |
| `dialogs.py` | an app's dialogs: EWMH type `dialog`/state `modal`, transient-for, `dialog_classes` WM_CLASS, or an untyped window with a dialog role | `x11.py`, AT-SPI |
| `recording.py` | ffmpeg `x11grab` input, PulseAudio mic, the terminal emulator to minimize | `x11.py`, `/proc` |

### `x11.py` contract (used by `auto.py`, `win32*.py`, recording)

All functions take and return X window ids (int) of *client* windows (the
window the app created, not the WM frame) unless named `frame`.

- `display()`: one cached `Xlib.display.Display`.
- `lock`: every X request runs under it (python-xlib is not thread safe);
  `input` holds it across a batch. A window that vanishes between calls
  reads as gone (False, None, 0, "" or an empty rect), never raises.
- `client_windows(stacking=True) -> list[int]`: `_NET_CLIENT_LIST_STACKING`,
  topmost first, plus mapped override-redirect windows (menus, popups,
  tooltips) on top.
- `exists(w)`, `pid(w)` (`_NET_WM_PID`, else XRes client pid), `title(w)`
  (`_NET_WM_NAME`, else `WM_NAME`), `wm_class(w) -> (instance, class)`.
- `frame_rect(w) -> (l, t, r, b)`: the visible frame: WM decorations
  included (`_NET_FRAME_EXTENTS`), a CSD window's invisible shadow
  (`_GTK_FRAME_EXTENTS`, GTK's and Chrome's) left out, so it is what a
  screen copy of the window holds; `client_rect(w)` without decorations
  and without the shadow, so always inside `frame_rect`; `x_rect(w)` the X
  window itself, shadow included (GTK4's window-relative extents count from
  its corner). All in root (screen) coordinates.
- `window_type(w) -> list[str]` (`_NET_WM_WINDOW_TYPE_*` short names:
  `normal`, `dialog`, `menu`, `popup_menu`, `dropdown_menu`, `tooltip`, ...),
  `state(w) -> list[str]` (`hidden`, `modal`, `maximized_vert`, `above`, ...),
  `wm_state(w)` (ICCCM `NORMAL`/`ICONIC`/`WITHDRAWN`, None when unmanaged),
  `transient_for(w)`, `is_override_redirect(w)`, `is_mapped(w)` (viewable),
  `is_minimized(w)`, `toplevel(w)` (the client window holding `w`),
  `is_normal(w)` (not override-redirect, first type `normal` or untyped:
  only such a window is an app's main one in `apps.windows`).
- `active_window()` (`_NET_ACTIVE_WINDOW`), `focus_window()` (the client
  holding the X keyboard focus), `server_time()` (a timestamp the WM
  accepts; CurrentTime reads as old to some).
- `activate(w, timeout=3.0) -> bool`: client message, source 2, with the
  server time; waits until the window is active **and** holds the X
  keyboard focus, so keys sent next reach it. Returns whether it took.
- `minimize(w)`, `restore(w)`, `maximize(w)`, `move_resize(w, l, t, width,
  height)` (frame coordinates, static gravity; waits until the frame has it
  or stops changing) each return whether it took. `restore` is meant as
  SW_SHOWNOACTIVATE, but xfwm4 focuses a window it deiconifies.
  `close(w)` (`_NET_CLOSE_WINDOW`).
- `window_at(x, y) -> int | None`: the topmost client window whose frame
  holds the point.
- `screen_rect() -> (l, t, w, h)`, `monitors()`, `work_area(w) -> (l, t, r, b)`
  (`_NET_WORKAREA` cut to the window's monitor), `dpi()` (Xft.dpi, else 96).
- `grab(l, t, w, h) -> PIL.Image` (RGB), from the root window.
- `selection_owner(atom)`, `selection_atom(name)`: for the clipboard's
  sequence number (XFixes owner events).

### `auto.py` contract

- `Control(acc)` wraps one `Atspi.Accessible`. Properties read live (no
  cache), and a dead object raises, as a stale UIA element does.
  An AT-SPI error is `ElementGone` only when the object is gone (the error
  says so, or it no longer answers a state read); on a live object it is
  `AtspiError`, a PilotError: the app refused the call.
- `ControlTypeName`: `roles.py` maps the AT-SPI role to a UIA type name
  (`push button` -> `ButtonControl`, `entry`/`text`/`password text` ->
  `EditControl`, `frame`/`dialog`/`alert`/`window` -> `WindowControl`, ...),
  so selectors are the same on both platforms.
- `Name`: accessible name; `AutomationId`: `get_accessible_id()`, else the
  `id` object attribute (Chromium's DOM id); `ClassName`: the `class` object
  attribute (Chromium's DOM class), else for a top-level window its
  `WM_CLASS` class, else the toolkit role name.
- `BoundingRectangle`: a `Rect` with `left`, `top`, `right`, `bottom`,
  `width()`, `height()` in screen pixels. For a top-level window, the X frame
  rect of its window. GTK4 (and any toolkit that reports window-relative
  extents on X11) is corrected by the window's client origin.
- `IsOffscreen`: not `SHOWING`, or no size; `IsEnabled`: `ENABLED` or
  `SENSITIVE`; `HasKeyboardFocus`: `FOCUSED`.
- `NativeWindowHandle`: the X window id for a top-level window (matched by
  pid, title and rect), else 0. `ControlFromHandle(xid)` goes the other way.
- `GetRuntimeId()`: a stable tuple for one live object (bus name + path).
- Patterns, each `None` when the object cannot do it:
  - Invoke (`Action` named click/press/activate/jump/open/invoke, or action
    0 for a button, menu item, link or icon role; never for a role UIA gives
    Toggle, SelectionItem, Value or ExpandCollapse),
  - Toggle (`checkable`/`CHECKED` + an action; `ToggleState`),
  - Value (`EditableText`/`Text` for text, embedded objects expanded, so a
    contenteditable reads as its paragraphs' text; `Value` for numbers;
    `Value`, `SetValue`, `IsReadOnly`),
  - Text (`DocumentRange.GetText(-1)`; a Document, an Edit, or a paragraph,
    heading or block quote, so `ui read` of a `<p>` reads its children),
  - SelectionItem (parent `Selection.select_child`; `IsSelected`),
  - ExpandCollapse (`EXPANDABLE`/`EXPANDED`, expand/collapse action),
  - Scroll (`Component.scroll_to` / the scroll pane's scroll bars' Value),
  - ScrollItem (`Component.scroll_to`), LegacyIAccessible (`DoDefaultAction`
    = action 0, `Value`).
- `SetFocus()` (activate the window, then `Component.grab_focus`; when the
  app refuses it or answers False, as GTK4 does, `FOCUSED` within 0.5 s
  counts, else a PilotError that says to click first; never the pointer),
  `SetActive()` (activate the top-level window), `GetTopLevelControl()`,
  `CaptureToImage(path)` (grab the rect), `Click()`.

## Profiles

- New key `platforms` (`["windows"]`, `["linux"]`; default both). Discovery
  skips a plugin or profile that is not for this platform and lists it under
  `unsupported` in `pilot apps`.
- Tables `[windows]` and `[linux]` override any key on that platform (for
  example `exe`, `window_class`, `title_strip`, `args`).
- State lives in `$XDG_STATE_HOME/desktoppilot` (default
  `~/.local/state/desktoppilot`) on Linux.

## Steps

1. (done) Foundation: `core/native.py`, imports, pyproject markers, profiles,
   discovery, paths, `linux/` skeleton; the unit gate passes on Linux.
2. (done) Backend, in parallel: `x11.py` + `win32*.py` + `input.py` +
   clipboard; `auto.py` + `roles.py`; core Linux branches (procs, apps, dialogs, state,
   capture, recording, events, pytest plugin).
3. (in progress) Apps: `code`/`vscode` on Linux, a `chrome` profile and
   CDP group for Chrome and Electron.
4. (in review) Docs: README, AGENTS.md, writing-tests, NOTES.md, `--help`,
   `scripts/bootstrap.sh`, `scripts/install.sh`. Left: the `--help` texts
   that say "UIA" for what is AT-SPI on Linux; the plugin NOTES.md with
   step 3.
5. (partly) Live test on the test machine (XFCE, xrdp): every core command on a
   GTK3, a GTK4, a Chromium and an Electron app; Linux smoke tests marked
   `ui`. Done: `tests/ui/linux` (the backend on Mousepad, GTK3, GTK4,
   Chrome).

## What the live runs taught

- An app started from VS Code's snap terminal inherits `NO_AT_BRIDGE`, the
  snap's GTK and GIO paths and `SNAP*`: no AT-SPI tree, or no start.
  `procs.app_env` drops them and puts back the `*_VSCODE_SNAP_ORIG` values.
- Popups (menus, combo lists, popovers) open only in the active window. A
  menu left open in any app holds an X grab that blocks every other popup.
- GTK3 menus cannot be collapsed by a pattern: Escape.
- GTK4 reports window-relative extents on X11 (corrected by the client
  origin), and its pointer coordinates can still be off: prefer patterns.
- Chromium text inputs have no EditableText, so no SetValue: `cdp
  set-value`, or `ui type` with the window in front.
- Mousepad is single instance (`--disable-server`), and a SIGTERM-killed
  Mousepad offers session restore at the next start.
- The kernel logs faults, not signals: a SIGABRT shows only in apport's log.
- libatspi returns some timed-out reads as an empty answer; one that took
  the whole timeout counts as a timeout (`WalkCut`), not as "empty".

## Traps

- The VS Code this session runs in is the user's editor. Never send input
  while it is the foreground window; never kill or restart it. Tests start
  their own VS Code with `--user-data-dir` in a scratch folder.
- Only one agent moves the pointer or types at a time.
- AT-SPI must be on: `gsettings set org.gnome.desktop.interface
  toolkit-accessibility true` (an app reads it at start). Chromium and
  Electron also take `--force-renderer-accessibility`.
