# DesktopPilot

DesktopPilot lets an AI agent drive a desktop app on Windows or on Linux
(X11). It clicks through UI Automation (UIA) on Windows and AT-SPI on Linux,
reads exact data from the app's own model (Word COM, the Chrome DevTools
Protocol), waits on conditions, and prints JSON. On a failure it keeps the
evidence: a screenshot, the UI tree, the app state, and what the system
recorded (Windows: crash, hang, .NET exception, WER; Linux: crash, OOM,
apport report, core dump).

```
pilot <app> <group> <command>
```

`<app>` is a profile (`notepad`, `code`, `desktop`) or a plugin (`word`,
`vscode`, `chrome`, or one from another package). Every call names the app;
there is no hidden current app. `pilot apps` lists the apps, a profile that does not
load under `broken`, and one for another platform under `unsupported`. An
`.exe` path (on Linux, any executable file) in place of `<app>` is an ad-hoc
app.

The first use is regression tests and bug replays for Word add-ins. The
tool does not live in, or depend on, a product repo.

## A general tool, in a public repo

This repo is public and holds only general code: the core, and plugins for
apps that anyone can install (Word, VS Code, Chrome). Code for one product
or one company goes in a separate package that adds its own plugin (see
[Plugins](#plugins)), in that product's own repo, private if it needs to be.

Do not add any of these here, not in code, docs, tests, samples or commit
messages:

- Names of a private product, company, customer, team or person, or of
  internal repos, servers and builds.
- Secrets: passwords, tokens, API keys, certificates, product keys, account
  ids.
- Internal hosts, IP addresses, URLs, ports of internal services, or local
  paths of one machine or user.
- Product-specific commands, selectors, ribbon and pane labels, log paths,
  registry keys, install steps, or tests that assert what a product does.
- Customer documents or text from them, and screenshots or recordings that
  show them.

Use neutral names in examples (`MyAddin`, `myapp`, `C:\docs\report.docx`).
When a change needs product knowledge, put that part in the product's
plugin and give the core only a general hook or option.

## Rules

- **Never run two Word agents at once** (Windows). A second `app launch`
  kills the first agent's Word.
- **`app launch` kills the running Word** (Windows: `word` and every plugin
  that extends it). Warn the user about unsaved documents first, or pass `--adopt`.
- **Never answer Yes to Word's safe-mode prompt** (Windows). Safe mode
  disables every add-in. The plugin answers No for you.
- **Never sleep.** Wait on a condition: `ui wait SEL [--gone]`.
- **Never fight the pointer.** A run moves the mouse and takes the foreground,
  on Linux too (XTest moves the real pointer). One agent drives a desktop at
  a time.
- **Never log, check in or report customer document text.**
- **Never edit `src` during a run you want to compare.** The install is
  editable, so the edit changes the tool under the run.
- **VS Code is the user's live editor**, on Windows and on Linux. Never
  restart or reload it. Never approve a Codex action; ask the user.
- **English UI only.** When a label stops matching, stop and ask the user,
  then add the new text next to the English text (see below).
- **An app's Claude plugin first, for what it covers.** When a Claude
  plugin or MCP server for the app is connected, read `docs/mcp/<app>.md`:
  a task its table gives to the server goes there, the rest to `pilot`, and
  everything to `pilot` when the server is not connected. In a product
  test the server does only setup and checks (`server, setup only`), never
  the step under test. A server marked `customer text: not allowed` never
  gets a customer document. A call that fails in a way the table does not
  predict: say so, and update that app's `docs/mcp/<app>.md`.
- **A web page in Chrome goes to Claude in Chrome first** (`docs/mcp/chrome.md`).
  When the `claude-in-chrome` extension is connected, drive the page with
  it: it reads the DOM and the accessibility tree, fills forms, and reads
  the console and network, none of which survives Chromium's UIA tree. Use
  `pilot` for what the page does not own: Chrome's own UI (menus, extension
  popups, permission bars), native dialogs (file pickers), other apps, and
  any page when the extension is not connected. Neither tool types a
  password; the user signs in.
- **Page work: `cdp` first.** A page `pilot` drives (a Chromium or Electron
  app with a debugging port) goes through `cdp`: read, type, keys, click,
  hover, scroll, drag, upload, navigate. `ui` and the real pointer only for
  the browser's own UI, OS dialogs, or what `cdp` cannot do.

The tool drives only the app it launched or adopted; another Word fails with
`NotOurWord`. It never clicks in an adopted app unless asked: the pytest
teardown and `dialogs --dismiss` without a policy only report.

**Limits:** Windows, and Linux on X11 only (no Wayland; see [Linux](#linux)).
Word and COM are Windows only. English UI only; the one
exception is the ja-JP Word safe-mode prompt.

## Install

```powershell
powershell -File scripts\install.ps1
pilot apps
```

This puts `pilot` on PATH (`%USERPROFILE%\.local\bin`, an editable
`uv tool install`). If a shell cannot find it, run
`uv tool update-shell`. Set `$env:PYTHONIOENCODING='utf-8'`: the CLI writes
UTF-8, but pytest and scripts that import the package do not. To work on the
tool, read [AGENTS.md](AGENTS.md).

On Linux:

```bash
scripts/install.sh
pilot apps
```

It runs the same editable `uv tool install` into `~/.local/bin`, and says
when that folder is not on PATH (`uv tool update-shell`, then a new shell).
The build needs the system packages in AGENTS.md (`scripts/bootstrap.sh`
installs them), and the desktop needs AT-SPI on (see [Linux](#linux)).

A failure prints `{"error": KIND, "message": ...}` (sometimes `details`;
`"internal": true` for an unexpected exception) and exits 1.

## Commands

One line per group. The flags are in `--help` (`pilot word doc open --help`).

```
pilot <app> app       launch | attach | status | kill | resize [W H] [--pid N | --hwnd H]
pilot <app> ui        tree | find | read | click | hover | type | set-value | select |
                      scroll | count | expand | keys | wait | windows | close | activate
pilot <app> dialogs   list modal dialogs; --dismiss answers known prompts; --close closes all
pilot <app> state     process, windows, dialogs, focus
pilot <app> shot      screenshot a window or control; crop, zoom, grid
pilot <app> capture   NAME: screenshot, tree, state, events for a report
pilot <app> clip      read | write
pilot <app> image     annotate | shot | crop | resize | pad | join    (see Images)
pilot <app> events    list | mark | dumps    (crash records; see Events)
pilot <app> record    start | stop | status | gif    (MP4 through ffmpeg; see Recording)

pilot word doc        open | new | read | find | select | insert | save | save-as | close | comments | revisions
pilot word ribbon     tabs | controls | click "Tab>Control"
pilot word pane       read
pilot word backstage  open | close | show
pilot word addin      windows | close
pilot word layout     page layout around the view, over COM
pilot word visible    what UIA says is on screen, mapped to Word positions

pilot vscode codex    open | model | send | wait | read
pilot vscode git      checkout | compare

pilot chrome scratch  start [--port N] [--url U] [--dir D] | stop PID

pilot <app> cdp targets|target|eval|click|hover|type|keys|set-value|upload|scroll|drag|navigate|read|wait|shot|console
                      [--port N] [--url PART] [--title PART] [--type page]
                      (chrome, vscode: the DevTools Protocol, on an app started with --remote-debugging-port)
```

The `ui` options that are easy to guess wrong:

```
ui tree      [--root SEL] [--levels N] [--root-depth N]   (--levels: how deep it prints; no --depth)
ui find|read|click|hover|wait|...  SEL [--depth N] [--timeout S]   (--depth: search depth per step)
ui click     SEL | --at X,Y [--relative] [--mode auto|invoke|mouse] [--button left|right] [--double]
ui hover     SEL | --at X,Y [--relative] [--dwell S]      (real pointer; stays there)
ui keys      KEYS [--into SEL]       ({Ctrl}l, {Alt}{F4}, {Shift}{Tab}: ^ % + are typed, not modifiers)
ui activate  [--selector SEL | --root SEL]                (the same option; default the main window)
```

`cdp` adds, per command:

```
cdp hover    CSS                     (the page's pointer, mouseMoved only; no real pointer moves)
cdp keys     KEYS [--into CSS]       (`ui keys` syntax as page key events; {Ctrl}a c x v z y edit)
cdp scroll   [CSS] [--by DX,DY | --to top|bottom]   (the page's wheel; default --by 0,100)
cdp drag     FROM_CSS TO_CSS [--steps N]   (page pointer; a draggable=true drag is dropped as HTML5)
cdp navigate URL [--wait load|domcontentloaded|none]   (fails with Chrome's reason, e.g. ERR_NAME_NOT_RESOLVED)
cdp upload   CSS FILE [FILE...]      (an input[type=file], hidden too; no dialog, no pointer, no focus;
                                      names read in its change event, before the page clears it)
cdp console  [--level error|warning|info|all] [--reload] [--for S]
cdp eval|read|wait|shot ... [--color-scheme dark|light]   (for that command only)
```

`cdp` input is made in the renderer: no real pointer or key, and the
window may be behind others, but not minimized. A minimized window stalls
the mouse events (a wheel never answers, a click takes seconds); keys still
work. Restore it first.

Each `cdp` call is its own connection. `cdp console` reads what Chrome
replays to a new connection: the current document's console calls,
uncaught exceptions and browser entries (failed loads, CORS). V8 keeps the
last 1000 console messages; a navigation or reload starts over. `--reload`
reloads and reads the new document up to its load event, plus `--for S`.
`--color-scheme` emulates `prefers-color-scheme` on that call's connection
and clears it at the end (Chrome drops an emulated media when its
connection closes, so a separate "set" command could not last).

The core groups work for every app. A plugin that extends `word` also takes
every `word` group; `vscode` takes every `code` group. `pilot desktop` is the null
app: roots `desktop`, `win:NAME`, `focus`, `dialog`, and no `app` group.
`pilot <app> --no-shot ...` turns off the failure screenshot.
`PILOT_<APP>_PID=N` (the profile name upper-cased, `-` as `_`:
`PILOT_CHROME_PID`) binds the app to one instance: every command sees pid N
and its descendants only, so `app status`, `app resize`, `app kill`, the
`ui` roots and `dialogs` never touch another instance of the same image.
`app status` says so in `bound_pid` and `bound_by`; a bound `app launch`
only adopts. Bound, `cdp` takes its port from the `--remote-debugging-port=N`
on that pid's command line: `--port`, else the bound pid's flag, else the
profile's args.
On both platforms, `app launch` starts the app without
`ELECTRON_RUN_AS_NODE`: VS Code's terminal and extension host set it, and an
Electron app started with it runs as plain Node.

On Linux, `app launch` starts the app in a session of its own, its output in
`~/.local/state/desktoppilot/sessions/<image>.log`, and without what the VS
Code snap leaks into its terminal (`NO_AT_BRIDGE`, its GTK and GIO paths,
`SNAP*`; the `*_VSCODE_SNAP_ORIG` values come back). `app kill` sends
SIGTERM, then SIGKILL after 5 s, and spares the processes of an instance
`pilot` runs inside (VS Code, when run from its terminal).

## Selectors

```
root//Type[name='X'][id='Y']/Type[name~='partial']#2
```

- `//` is descendant, `/` direct child, `*` any type. Any other type must be
  a UIA control type; a typo is an error.
- Keys: `name`, `id` (AutomationId), `class`, `value`. `=` is exact and
  case-insensitive; `~=` is contains. `#N` takes the Nth match (1-based).
- Values take `'...'` or `"..."`, with `\'`, `\"`, `\\` escapes. Quote a
  root argument that holds `/`: `win:"Find/Replace"//Button`.
- Unbalanced brackets or quotes are a parse error, never the whole window.
- Core roots: `app`, `win:NAME`, `dialog`, `focus`, `desktop`. A plugin adds
  and overrides roots; its NOTES.md lists them.
- Offscreen elements do not match unless `--hidden`.

## Images

`image annotate` and `image shot` apply a JSON list of ops in order (`--spec
FILE|-`, `--op JSON` repeatable): `box`, `rounded`, `ellipse`, `line`,
`arrow`, `text`, `badge`, `callout`, `highlight` (dim or fill), `blur`,
`pixelate`, `crop`, `resize`, `pad`. `crop`, `resize`, `pad` and `join` are
also commands of their own.

- A target is `X,Y,W,H` or `X,Y` in image px, or a selector.
- A selector needs the image's frame (screen origin and scale). `shot` writes
  it into the PNG as a `desktoppilot` text chunk, and `crop`, `pad` and
  `resize` move it along, so a box lands right on a cropped, zoomed and
  edited shot. Any other image needs `--origin X,Y --scale S`.
- `image shot` resolves the selectors in the live UI at the moment of the
  shot.
- The output is a new file (`<stem>-annotated.png`) unless `--in-place`. A
  `.jpg` output is JPEG, with no chunk. Each op's final box is in
  `annotations`.

## Recording

```
pilot word record start               # Videos\word-YYYYMMDD-HHMMSS.mp4: the screen under the main window, 30 fps, no audio
pilot word record start demo.mp4      # Videos\demo.mp4; a path with a folder (.\demo.mp4) is used as given
pilot word record status | stop
pilot word record gif C:\Users\me\Videos\demo.mp4   # demo.gif beside it, 10 fps, 800 px wide
```

**Ask first.** When the request does not say what to record (which window,
the full screen, or a region), confirm it with the user before `record
start`. Another app's window over the area refuses the start (`Covered`,
listed in `details.covering`): name them to the user, ask whether to close
them or go on, and only then rerun with `--allow-covered`. Never decide alone
that a cover is meant.

- Needs ffmpeg: `winget install Gyan.FFmpeg`. A shell that was open during the
  install does not see the PATH change; `record` looks in winget's folders too.
- By default the window is brought to the front and the **screen area under
  it at start** is recorded (`target.rect`, `clipped` when cut to the screen),
  so a moved or resized window is not followed and a window on top is in the
  video. `--follow` finds the window by its exact title and tracks it, frame
  size fixed at start, but what it draws through DirectComposition (Windows 11
  Notepad's tab strip and status bar, WebView2 panes) records black.
  `--full-screen` and `--region X,Y,W,H` (screen pixels) copy the screen.
- Files go in your Videos folder: no PATH gives `<app>-YYYYMMDD-HHMMSS.mp4`, a
  bare name (`demo.mp4`, or `gif -o demo.gif`) lands there too, and a path with
  a folder part is used as given. `gif` without `-o` writes beside its source.
- The terminal `pilot` runs in is minimized at `start` so it is not in the
  video (`minimized_windows`), and `stop` restores it without taking the
  focus. `--follow` leaves it alone. With no terminal on screen, the window
  of the app that launched `pilot` (the Claude desktop app) is minimized
  instead; VS Code never is.
- The mic is off. `--mic DEVICE` adds a DirectShow microphone; list the names
  with `ffmpeg -list_devices true -f dshow -i dummy`.
- `start` returns once ffmpeg is writing the file. ffmpeg runs under a detached
  supervisor, so `stop` works from any shell: it sends ffmpeg `q` and waits for
  a finished, playable MP4. `stop --force` kills ffmpeg; that file may not play.
- `stop` reports the file: path, duration, resolution, fps, and audio or `null`
  when there is no sound.
- A `--follow` recording ends by itself when its window closes; `status`
  reports it once as `stale` with ffmpeg's `exit_code`. One recording per app
  at a time.
- State and ffmpeg's log (`<app>.recording.json|log`) sit next to the session
  file; a failed `start` quotes the log's tail (a wrong title, a missing mic).

On Linux (X11):

- ffmpeg from apt (`sudo apt install ffmpeg`); `x11grab` copies `$DISPLAY`.
  Videos is `xdg-user-dir VIDEOS`, else `~/Videos`.
  `--follow` records the window by its X id (its own pixels, no title bar).
- `--mic` takes a PulseAudio source: `pactl list short sources`.
- Only a terminal emulator window that runs `pilot` (xfce4-terminal,
  gnome-terminal, konsole, xterm, kitty, alacritty, tilix, terminator,
  wezterm, foot) is minimized. VS Code's integrated terminal is part of VS
  Code: nothing is minimized there.
- The supervisor runs in a session of its own; `stop --force` is SIGKILL.

## Events

`events list` reads what the system recorded about the app's image (the
executable's file name), newest last; `events mark` and `list --since-mark`
scope it to what came after; `events dumps` lists core dumps and whether the
machine keeps them. The tool never changes that configuration.

| kind | Windows | Linux |
| --- | --- | --- |
| `crash` | Application Error 1000 | the kernel's `segfault at` / `traps:` lines, apport's log, systemd-coredump; one per pid |
| `hang` | Application Hang 1002 | - |
| `dotnet` | .NET Runtime 1026 | - |
| `wer` | Windows Error Reporting 1001 | - |
| `oom` | - | the kernel's OOM killer |
| `report` | WER report folder | apport `.crash` file in /var/crash |
| `dump` | LocalDumps `.dmp` | core file (apport, systemd-coredump) |

`crash`, `hang`, `dotnet` and `oom` are fatal: the pytest plugin fails a
test whose app had one. On Linux with apport (Ubuntu's default):

- apport writes a `.crash` report only for an executable of an installed
  package: a crash of a binary you built is a `crash` only;
- the kernel logs faults, not signals: a SIGABRT (abort, an assertion) shows
  only in apport's log, so it is a `crash`, and a `report` if packaged;
- apport keeps a core file only when the process's core limit
  (`ulimit -c`) is not 0.

## When a call fails

Deterministic calls first, vision last. The tool never calls a model: you
are the vision model.

1. **Read the JSON.** It names the error kind and the selector.
2. **Try the deterministic options:** a wider `--depth`, `ui tree --flat`,
   more `--levels`, another root, `ui read --copy`. Retry a `WalkCut`.
3. **Look at the failure screenshot.** A failed `ui` call with a selector,
   `shot`, or a `word` or `vscode` plugin command that acts on the
   app saves the app window under
   `%LOCALAPPDATA%\desktoppilot\shots\<app>\` (Linux:
   `~/.local/state/desktoppilot/shots/<app>/`) and returns `screenshot`.
4. **Look closer:** `shot --region X,Y,W,H` (pixels of the plain shot) with
   `--zoom` and `--grid STEP` (labels are screen coordinates). Screen =
   `origin + floor(image / scale)`. `occluded: true` means another window is
   on top: `ui activate` first.
5. **`ui click --at X,Y` is the last step.** It clicks screen pixels and
   checks nothing; its JSON says `"fallback": "coordinates"`.

Steps 3 to 5 are journalled per app: `capture` and `state` report
`fallback_used`, and `app launch` or `app attach` starts a new journal. Say
in your report when a run needed vision.

**A language change is a stop-and-ask, never a `--at` click.**

## Error kinds

- `NotPresent`: the root or element is not there (pane not open, Backstage
  closed). The only failure `ui wait --gone` counts as gone.
- `WalkCut`: a UIA walk was cut by its deadline or by a read the app timed
  out on, so the app is busy. Unknown, not absent: never report "not
  found", "gone" or a count from it. Retry.
- `Disabled`: the target stayed disabled for the click timeout.
- `Ambiguous`: more than one target matches. Waiting cannot fix it.
- `NotOurWord`: a Word this tool did not launch or adopt.
- `ModelMismatch`: `codex model` read back other values (`details`).
- `Covered`: another window is over the `record start` area
  (`details.covering`). Ask the user before `--allow-covered`.
- `Timeout`: a wait ran out.
- `AppNotRunning`: launch first. A selector wait (`ui find`, `ui wait`, a
  click or read) gives a missing app 2 s, then fails with it, not at its
  timeout; Word's `NotOurWord` likewise. `ui wait --gone` with no app is
  gone at once.
- `NoTarget`: no window to act on, or no CDP endpoint answers.

## When the UI text changes

Signs: a selector that worked gives `NotPresent` or times out, `ribbon
controls` shows other names, or a Codex read-back shows a field as unknown.
The cause is an app update or another UI language.

1. Stop. Do not try other labels by hand, and do not click with `--at`.
2. Tell the user which control was not found and the probable cause.
3. Ask if they want the tool updated.
4. If yes: read the new text from the running app (`ribbon controls --tab
   X`, `ui tree`, or the app's resource files). Add it next to the English
   text, never in its place: `plugins/word/session.py`
   (`KNOWN_PROMPTS`), `plugins/vscode/panel.py`, `codex.py` and `locate.py`
   (tuples).
   `grep` the old string and update every hit: code, tests, NOTES.md, any
   agent skill that uses it. Run the gates
   and a live check.
5. Go back to the task.

## Linux

X11 only; Wayland is not supported. Tested on Ubuntu with XFCE (xfwm4) over
xrdp. `pilot apps` lists `word` and `notepad` under
`unsupported` there.

**Before the first run:**

- An X session: `DISPLAY` set, `XDG_SESSION_TYPE=x11`.
- AT-SPI on: `gsettings set org.gnome.desktop.interface toolkit-accessibility
  true`. An app reads it at start: restart the apps that were already open.
- Chromium, Chrome and Electron apps (VS Code) need
  `--force-renderer-accessibility`, or AT-SPI sees one opaque frame. The
  `code` profile passes it.
- `scripts/bootstrap.sh` installs the packages, sets the flag and checks all
  of this.

**The same as on Windows:** the commands, the selectors and their control
types, the roots, the JSON, the error kinds, the profiles and the failure
evidence. A selector written for Chrome or an Electron app on Windows finds
the same element on Linux.

**What differs, and why:**

| | Windows | Linux |
| --- | --- | --- |
| element tree | UIA | AT-SPI. The type is mapped from the AT-SPI role (`push button` -> `Button`, `entry` -> `Edit`, `frame`/`dialog` -> `Window`; no match is `Custom`) |
| `id` | AutomationId | the accessible id, else the DOM `id` (Chromium) |
| `class` | ClassName | the DOM `class` (Chromium), else a top-level window's `WM_CLASS` class, else the role name |
| window handle (`hwnd`) | HWND | the X window id of the client window |
| dialog | window class `#32770` or one in `dialog_classes` | EWMH type `dialog` or state `modal`; or transient for an app window; or a WM_CLASS in `dialog_classes`; or an untyped window whose AT-SPI role is dialog, alert or file chooser |
| `ui click --mode invoke` and the other patterns | UIA patterns | AT-SPI actions and interfaces: Invoke is the action `click`, `press`, `activate`, `jump`, `open` or `invoke` (a button, menu item or link: its first action); Toggle, SelectionItem and ExpandCollapse are actions too; Value is EditableText or Value. An element with an action but none of these patterns (a GTK3 menu-bar item: role `menu`) is clicked through its first action, which is what UIA's LegacyIAccessible DoDefaultAction is: `via: LegacyIAccessible` |
| pointer and keys | SendInput; GetCursorPos | XTest, in the active keyboard layout; QueryPointer |
| clipboard | Win32 clipboard | `xclip`, the CLIPBOARD selection, text only |
| recording | gdigrab, DirectShow mic | `x11grab`, PulseAudio mic (see Recording) |
| `events` | event log, WER | journal, apport, systemd-coredump (see Events) |
| state, sessions, shots | `%LOCALAPPDATA%\desktoppilot` | `$XDG_STATE_HOME/desktoppilot` (`~/.local/state/desktoppilot`) |
| user profiles | `%LOCALAPPDATA%\desktoppilot\profiles` | `$XDG_CONFIG_HOME/desktoppilot/profiles` (`~/.config/desktoppilot/profiles`) |
| DPI | per monitor | `Xft.dpi`, else 96 |

A profile key `platforms` (`["windows"]`, `["linux"]`; default both) says
where it runs; a `[linux]` or `[windows]` table replaces any key on that
platform (`exe`, `window_class`, `args`, ...). See Plugins.

**Known limits:**

- **Prefer patterns to the pointer.** GTK4 reports pointer coordinates that
  can be off; a pattern click (`--mode invoke`), `set-value` and `select` do
  not use them.
- **AT-SPI cannot write a Chromium input.** `set-value` fails on a Chrome or
  Electron text box: use `cdp set-value`, or `ui type` with the window in
  front.
- **Popups open only in the active window.** A menu, combo list or popover
  asked of a window in the background does not open: `ui activate` first.
- **A menu left open in any app blocks every other popup.** It holds an X
  grab. Close it (`ui keys "{Esc}"` in that app) before you go on.
- **A GTK3 menu cannot be collapsed by a pattern.** Close it with `{Esc}`.
- **Snap environment.** An app started from VS Code's snap terminal inherits
  the snap's variables (`NO_AT_BRIDGE`, its GTK and GIO paths) and then has
  no AT-SPI tree or fails to start. `app launch` strips them. An app you
  start by hand needs the same: `env -u NO_AT_BRIDGE -u GTK_PATH -u
  GIO_MODULE_DIR APP`.
- **Mousepad** is single instance: pass `--disable-server` to get a window of
  your own. Killed with SIGTERM, it offers to restore its session at the
  next start.
- **Depth.** The default `--depth` 12 is too shallow for GTK4 and Electron:
  give the profile `depth = 25` or more, or pass `--depth 30`.
- **No `ui scroll`.** GTK3, GTK4 and Chromium offer no Scroll pattern.
- **`ui read` of a static Text is null** (a GTK label, a Chromium text
  node): read its parent, or `cdp read`.
- **No ExpandCollapse on a GTK4 MenuButton or a Chromium Document.** Open
  it with `ui click`.
- **GTK4 refuses AT-SPI focus.** `ui type --into`, `ui keys --into` and
  `ui read --copy` fail with a PilotError there: `ui click SEL`, then the
  command without `--into` (`ui read focus --copy`).
- **GTK4 popover radio items are unnamed.** Select by position:
  `ui select "app//MenuItem[class~='radio']#N" --depth 30`. GMenuModel radios
  (Mousepad, gedit) show as plain menu items, with no checked state.
- **A submenu item needs two `select` calls:** one opens the submenu, one
  picks the item.
- **Electron (VS Code).** Monaco's text is not in the tree unless VS Code's
  screen-reader mode is on: use `ui read focus --copy` or `cdp`. A menu
  item's name includes its shortcut (`New Text File Ctrl+N`).
- **The image name is `/proc/<pid>/exe`.** A symlinked binary or a script
  is not matched by its own name: copy the binary instead (see Testing).
- **GTK4 rects can be about 5 px off.** Prefer a pattern to a pointer click.
- No `hang`, `dotnet` or `wer` events: Linux records none.

**Testing:** test on a scratch instance, bound with `PILOT_<APP>_PID`.
For Chrome, `pilot chrome scratch start` starts one and prints the binding.
For another app, start it on its own `--user-data-dir` and bind its pid; a
copied binary under another name (`code-scratch`) works too. Unbound, `app
launch` adopts the user's running instance and `app kill` kills it.

## Plugins

A plugin is one app's code and notes in `src/desktoppilot/plugins/<name>/`,
or in another package (below).

| file | holds |
| --- | --- |
| `profile.toml` | `exe`, `args`, `env`, `window_class`, `title_strip`, `dialog_classes`, `window_size` (DIPs), `ready_timeout`, `launch` (`fresh`/`adopt`), `dialogs` (`auto`/`explicit`), `depth`, `root_depth`, `extends`, `notes`, `platforms` (`windows`, `linux`; default both); a `[windows]` or `[linux]` table replaces any of these keys on that platform; an `[options]` table holds the plugin's own settings (the core does not read them; a child merges its options over its parent's, key by key), e.g. `word`'s `pane_hosts` |
| `plugin.py` | `register(reg, profile)`: `reg.group`, `reg.root`, `reg.hook`, `reg.fixtures` |
| `fixtures.py` | pytest fixtures, loaded with `--app` |
| `NOTES.md` | agent notes: roots, lifecycle, traps |

Hooks are `pre_launch`, `post_launch`, `ready`, `quit`, `pre_kill`,
`dialog_policy` and `status`, each `fn(ctx, **kwargs)`; `pre_kill` gets
`pids=` and `force=` and refuses by raising. `extends` inherits groups,
roots, hooks, fixtures and profile values; a user profile in
`%LOCALAPPDATA%\desktoppilot\profiles` (on Linux
`$XDG_CONFIG_HOME/desktoppilot/profiles`) can extend a plugin too. The session
file is `sessions/<IMAGE>.json`: the launched pid, or after an adopt the
`pids` it joined, and the start time. `word` and a plugin that extends it
share it, so they share one Word.

| plugin | extends | drives |
| --- | --- | --- |
| [`word`](src/desktoppilot/plugins/word/NOTES.md) | - | Word, over UIA and COM |
| [`vscode`](src/desktoppilot/plugins/vscode/NOTES.md) | `code` | the Codex panel in the running VS Code |

`word` and the `notepad` profile are Windows only. `code` and `vscode`
are being brought to Linux; the state is in
[docs/linux-plan.md](docs/linux-plan.md).

A plugin can also live in another installed package. Name its folder in
that package's `pyproject.toml`; the entry name is the plugin name:

```toml
[project.entry-points."desktoppilot.plugins"]
myapp = "mypkg.myapp"
```

The folder `mypkg/myapp/` holds the same files. The parent package's
`__init__` (`mypkg`) must import on every platform; the plugin itself is
imported only after the `platforms` check. A user profile still wins over
it. A name the tool already ships (a plugin or a profile), or a plugin that
does not import, is listed under `broken` by `pilot apps` as
`myapp=mypkg.myapp`; the shipped app stays. `pilot --help`
names each external plugin's NOTES.md.

A plugin teaches the tool to reach an app's controls. It does not assert
what the app should do: product tests live outside this repo.

A plugin for a private or company product is always an external plugin in
its own package, never a folder here. See
[A general tool, in a public repo](#a-general-tool-in-a-public-repo).

## MCP coverage

An app with a Claude plugin or MCP server has `docs/mcp/<app>.md`: which
of our tasks the server does, which stay with `pilot`, and the server's
fingerprint when it was tested. The format is in
[docs/mcp/README.md](docs/mcp/README.md). A server that can do a task is not
a plugin: a plugin is code that drives the GUI.

```
python scripts/mcp_check.py check                        # every doc against its server
python scripts/mcp_check.py check --tools-json chrome=tools.json
python scripts/mcp_check.py fingerprint --server NAME    # or --tools-json FILE
```

`check` exits 1 when a server `changed`; then rerun that app's coverage
test. A server in a Claude Code config is asked over stdio or HTTP; a
claude.ai connector or Claude in Chrome reports `needs_dump` until an agent
passes its tool list. The script uses only the standard library and calls
no model.

## Not done

- AutomationId selectors in the plugins (labels still match by text).
- `pilot vscode cmd <id>`, a general command-palette command.
- Office alerts (OAlerts) in `events`.
- A CI VM: `word` and `ui` tests need a desktop session of their own.
- A user profile that `extends` word gets word's root resolvers, still
  bound to word's profile.
- Linux: Wayland; `code`/`vscode` and a Chrome CDP group; the live test of
  every command (see [docs/linux-plan.md](docs/linux-plan.md)).

## More

- [docs/writing-tests.md](docs/writing-tests.md): pytest plugin, fixtures,
  env vars, where product suites go.
- Each plugin's `NOTES.md` (table above).
- [AGENTS.md](AGENTS.md): for an agent that works on the tool.
