# chrome - agent notes

`chrome` is Google Chrome: its own UI (tabs, toolbar, menus, infobars) and
its pages through UIA or AT-SPI, plus `cdp`, the DevTools Protocol, once it
runs with `--remote-debugging-port`. `launch = "adopt"`, `dialogs =
"explicit"`, depth 25, `--force-renderer-accessibility`. The Rules,
selectors and failure ladder are in the [README](../../../../README.md); the
Claude in Chrome server's coverage is in `docs/mcp/chrome.md`.

    pilot chrome ui find "app//Button[name='Reload']"
    pilot chrome cdp targets --port 9333
    pilot chrome cdp set-value "#email" "a@b.c" --port 9333 --url login
    pilot chrome cdp type "textarea" "more text" --port 9333 --title Inbox
    pilot chrome cdp click "button[type=submit]" --port 9333 --url login
    pilot chrome cdp keys "{Ctrl}a{Delete}hello{Enter}" --into "#q" --port 9333

**Page work: `cdp` first.** Read, type, keys, click, hover, scroll, drag,
upload and navigate a page with `cdp`; `ui` and the real pointer only for
Chrome's own UI, OS dialogs, or what `cdp` cannot do.

## Traps

- **This is the user's Chrome.** `app launch` adopts the one that runs (its
  windows, tabs and sign-ins) and, unbound, `app kill` takes every Chrome of
  the image, theirs too. Never kill it; test on a scratch one (below), with
  `PILOT_CHROME_PID` set.
- **The debugging port needs its own Chrome.** A flag reaches only a Chrome
  this launch starts: a running one takes the new window and ignores it, and
  Chrome refuses the port on its default user data folder. So the profile
  opens none: use `scratch start`, or a profile that `extends = "chrome"`
  with `--remote-debugging-port=N` and `--user-data-dir=<its own folder>`.
- **The port has no authentication.** Anything running as the user can drive
  that Chrome while it is up.
- **AT-SPI cannot write a Chromium input** (no EditableText): `ui set-value`
  fails there. Use `cdp set-value`, or `ui type` with the window in front.
- **`cdp` acts on one target.** `--url` and `--title` (parts) and `--type`
  (default `page`) must name exactly one; two is `Ambiguous`, never a guess.
- **A minimized window stalls `cdp` mouse input.** The hidden page answers
  no `mouseWheel` (`cdp scroll` times out) and a click or drag takes
  seconds; keys still work. Behind other windows is fine. Restore it first
  (measured 2026-11-12, Chrome 157).
- **A control with no accessible name cannot be found by `ui`** (a
  `role=button` drop zone with no `aria-label` times out on `[name~=...]`):
  use `cdp click <css>`. The missing name is the page's fault; report it.

## scratch

A Chrome of its own beside the user's, for `cdp` and the `app` and `ui`
commands:

    pilot chrome scratch start [--port N] [--url U] [--dir D]
    export PILOT_CHROME_PID=<pid>      # the `bind` it printed
    pilot chrome cdp targets --port <port>
    pilot chrome app resize 1280 800   # bound: the scratch window only
    pilot chrome scratch stop <pid>

- `start` runs the profile's exe with a fresh `--user-data-dir` (a temporary
  folder unless `--dir`), `--remote-debugging-port` (a free one unless
  `--port`), `--force-renderer-accessibility`, `--no-first-run` and
  `--no-default-browser-check`, in the environment `app launch` gives (no
  snap leaks), and waits until the port answers with a page.
- `PILOT_CHROME_PID` binds every `pilot chrome` command to that pid and its
  descendants: `app status`, `app resize`, `app kill`, the `ui` roots and
  `dialogs` see the scratch Chrome only. `app resize --pid N` or `--hwnd H`
  picks a window without a binding.
- `stop` kills that pid's tree, never Chrome by name, and removes the folder
  `start` made (one given with `--dir` stays). It refuses a pid `start` did
  not record.

## cdp

The core group (`core/cmd_cdp.py`); the client is `core/cdp.py`, which
other plugins use too.

- `targets`: every target, and `browser` from `/json/version`.
- `target`: the one target the filters name.
- `eval EXPR`: the value, a promise awaited. Return JSON, not nodes.
- `click SELECTOR`: hover, press, release at the element's centre after
  scrolling it into view: hit-testing applies, so a covered element takes
  nothing.
- `type SELECTOR TEXT`: focus, caret at the end, `Input.insertText`: one
  input event, any character, no keyboard layout, no window focus.
- `set-value SELECTOR VALUE`: an input, textarea or select through the
  prototype's value setter plus `input` and `change` (React sees it); a
  contenteditable by select-all and insert. Read back: `verified`.
- `upload SELECTOR FILE [FILE...]`: an `input[type=file]` gets the files
  as from the OS file dialog (`DOM.setFileInputFiles`); Chrome fires `input`
  and `change`. No dialog, no pointer, the window may be behind others, and
  a hidden input works: GitHub's attach box is
  `file-attachment input[type=file]`. Every file must exist; the paths go in
  absolute. Read back: `names`, `verified`.
- `keys KEYS [--into SELECTOR]`: `Input.dispatchKeyEvent` in `ui keys`
  syntax (`core/keysyntax.py`, the Linux emulation's parser too): `abc`,
  `{Enter}`, `{Shift}{Tab}`, `{Ctrl}(ac)`, `{Down 3}`. A key that types is a
  `keyDown` with its text (keydown, keypress, input), others a
  `rawKeyDown`; key, code and keyCode as the US layout. Named: Enter Tab
  Escape Backspace Delete Insert Space Left Up Right Down Home End PageUp
  PageDown F1-F12; modifiers Ctrl Alt Shift Meta (Win). A synthetic key runs
  no editing shortcut in a headful Chrome, so `{Ctrl}` a, c, x, v, z, y and
  `{Ctrl}{Shift}z` send their command (`selectAll`, `copy`, `cut`,
  `paste`, `undo`, `redo`) with the key. Chrome's own shortcuts (Ctrl+L,
  Ctrl+T, F5) never reach the browser: use `navigate`, or `ui keys`.
  `--into` focuses the element first, caret at the end; `value` reads it
  back.
- `scroll [SELECTOR] [--by DX,DY | --to top|bottom]`: `mouseWheel` at the
  element's centre (after scrolling it into view), or the viewport's: wheel
  handlers and virtual lists react. Default `--by 0,100` (one notch).
  `--to` wheels until the scroller is at that end or stops moving (20 turns
  at most: an endless list keeps growing). Waits until the position holds,
  then reports the scroller (the element or its nearest scrollable
  ancestor, else `window`): `top`, `left`, `max_top`, `max_left`. Without a
  selector it reports the window, even when the wheel landed on a box.
- `drag FROM TO [--steps N]`: press at FROM's centre, N `mouseMoved` with
  the button down, release at TO's: pointer-event libraries (dnd-kit,
  sortable lists) follow. A native drag (`draggable=true`) would start the
  OS drag loop, which waits for a real pointer, so `Input.setInterceptDrags`
  catches it and `dragEnter`, `dragOver`, `drop` carry its data to TO:
  `mode: html5`. Both elements must be in the viewport at once.
- `navigate URL [--wait load|domcontentloaded|none]`: `Page.navigate`, then
  the event (a `#fragment` has none and returns at once). A failed load
  raises with Chrome's `net::ERR_...` reason, and the tab then shows the
  error page at that URL: `--url` must match it from then on. An HTTP 404
  is a load. With `--wait none` the `url` and `title` may be the old page's.
- `read SELECTOR`: a field's value, else `innerText`.
- `wait SELECTOR [--gone] [--wait S]`: until a visible element matches (or
  none does).
- `shot PATH`: the page as the renderer draws it, window covered or not.
- `hover SELECTOR`: scroll into view and one `mouseMoved` at the centre:
  `:hover`, `mouseover`, a hover-delay preview. No real pointer moves and
  the window need not be in front. The page keeps that pointer after the
  call, until a later move: a real pointer crossing the window overrides it.
  Wait for what opens with `cdp wait`. For the real pointer: `ui hover SEL
  [--dwell S]` (the window comes to the front).
- `console [--level error|warning|info|all] [--reload] [--for S]`: what the
  page logged, oldest first: `console.*` calls (`source: console`), uncaught
  exceptions (`exception`) and browser entries (`network`, `javascript` for
  CORS, `violation`, ...). A new connection gets the current document's
  buffer replayed (V8 keeps the last 1000 console messages); a navigation or
  reload empties it. `--reload` reloads and reads up to the load event, then
  `--for S` more seconds (default 1). `errors` counts the error entries
  whatever `--level` shows. "No JS errors": `cdp console --level error`
  gives `count: 0`.
- `--color-scheme dark|light` on `eval`, `read`, `wait` and `shot`:
  `prefers-color-scheme` emulated for that call only. Chrome drops an
  emulated media when the connection that set it closes (measured
  2026-10-21), so there is no separate set command; the page is as it was
  after the call.

      pilot chrome cdp shot dark.png --color-scheme dark --port 9333
      pilot chrome cdp eval "getComputedStyle(document.body).backgroundColor" --color-scheme dark --port 9333

Selectors are CSS in the target's main document; an iframe's content is its
own target (`--type iframe`) or reached with `eval`.

## Electron apps

An Electron app's profile can `extends = "chrome"` for `cdp`, with its own
`exe`, `window_class` and `title_strip`, and `--remote-debugging-port=N` in
`args`. VS Code has `cdp` through the `vscode` plugin.

## Linux

Measured 2026-10-18 on Chrome 153 under XFCE on Xorg.

- `exe` is the binary `/opt/google/chrome/chrome` first, so the image is
  `chrome`; the `google-chrome` script execs it. `window_class` is the
  WM_CLASS class `Google-chrome` (the instance part carries the
  `--user-data-dir` when one is given).
- The page's tree: DOM ids are `id`, DOM classes `class`, an `<input>` an
  `Edit` with Value but no write, a `<p>` a Text whose words are in its
  children.
- **Testing on the test machine.** A scratch Chrome on its own `--user-data-dir` runs
  as a separate browser beside the user's, with its own process tree (its
  crash handler leaves the tree and exits with it). `scratch start` and
  `PILOT_CHROME_PID` keep the `app` commands on it; no copied binary is
  needed. Measured 2026-10-21. See `tests/ui/linux/test_chrome_cdp.py`.
- **The OS file dialog is not Chrome's.** An `<input type=file>` opens the
  xdg-desktop-portal GTK "Open File" window, which is no child of Chrome:
  `pilot chrome dialogs` returns `[]`. Find it, and give it the path:

      pilot desktop ui windows                      # its title, e.g. "Open File"
      pilot desktop ui activate --selector "win:Open File"
      pilot desktop ui keys "{Ctrl}l"               # the location field
      pilot desktop ui type "/path/to/file.png"
      pilot desktop ui keys "{Enter}"

  `{Ctrl}l`, not `^l`: `ui keys` is not SendKeys.
