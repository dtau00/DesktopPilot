# Working on DesktopPilot

For an agent that changes the tool. To use the tool, read
[README.md](README.md) first: its Rules apply here too.

## Setup

```powershell
uv sync --extra dev          # or scripts\bootstrap.ps1
$env:PYTHONIOENCODING = 'utf-8'
```

On Linux (X11), `uv sync` builds PyGObject and pycairo from source, so it
needs these system packages first:

```bash
sudo apt install build-essential pkg-config python3-dev \
    libgirepository-2.0-dev libcairo2-dev \
    gir1.2-atspi-2.0 at-spi2-core xclip ffmpeg \
    xdotool wmctrl x11-utils      # diagnostics only: the tool does not call them
uv sync --extra dev               # or scripts/bootstrap.sh, which does all of it
```

`scripts/bootstrap.sh` also turns AT-SPI on and checks the session
(`DISPLAY`, `XDG_SESSION_TYPE=x11`, the AT-SPI bus). The live tests also
use `mousepad`, `gir1.2-gtk-3.0`, `gir1.2-gtk-4.0` with the system
`python3-gi`, and Chrome; a test skips when its app is missing.

## Gates

Run them before you hand back a change:

```powershell
uv run pytest -m "not word and not ui" -q    # unit tests, no desktop
uvx ruff check src tests
uvx ruff format .
uv run pytest -m word --app word -q          # smoke, on a real Word
```

- The smoke run launches and kills Word. It needs a free Word (no other Word
  agent on the machine) and a document: `C:\pilot-gate\sample.docx`, or
  the path in `PILOT_TEST_DOC`.
- On Linux the Word smoke run does not apply. Run the unit gate, ruff, and
  the live gate:

  ```bash
  uv run pytest tests/ui/linux -m ui -q
  ```

  It starts and closes its own Mousepad, GTK3 and GTK4 windows and a Chrome
  on a scratch profile. It needs an X session with AT-SPI on, and no other
  agent driving that desktop. The popup tests activate their window, so
  they skip unless `PILOT_TEST_ACTIVATE=1`: set it only when nobody else
  uses the desktop.
- A test of Win32-only behaviour (tasklist argv, DPI calls, the event log,
  consoles) is marked `@pytest.mark.windows("why")`; a module that imports
  the `word` plugin, or tests only SendInput, goes in
  `WINDOWS_ONLY` in `tests/conftest.py`. Both skip off Windows with the
  reason.
- No tool test may depend on a third-party add-in. Mark a test that needs
  one so it skips when the add-in is absent.
- Product tests do not go here. See
  [docs/writing-tests.md](docs/writing-tests.md).

## Rules

- **One Word agent at a time** (Windows). Two agents kill each other's Word.
  On Linux, one agent drives a desktop at a time.
- **One change, every place.** A command change updates, in the same commit:
  the `--help` text (its `JSON:` list names only keys the command emits;
  `test_json_keys.py` checks), the README command grammar, the plugin's
  NOTES.md, any agent skill that uses it, the tests, and the Linux emulation (below). A change to a
  task an MCP server covers updates its row in `docs/mcp/<app>.md` too.
- **The core imports the platform only through `core/native.py`.**
  `uiautomation`, `win32gui`, `win32process`, `win32api`, `win32con` and
  `win32clipboard` come from there: the real modules on Windows, the
  `desktoppilot/linux/` ones on Linux. What has no Win32 shape (`winreg`,
  the event log, consoles, the shell) is imported under `if WINDOWS:` and
  has a Linux branch at function level.
- **A Win32 or uiautomation name the core calls is emulated on Linux in
  the same commit.** A new name, a new argument or a new return value goes
  into `src/desktoppilot/linux/` with a unit test (`tests/unit/test_linux_*`),
  and a live test in `tests/ui/linux` when it needs a desktop.
- **Never edit `src` during a comparative run.** The install is editable,
  so an edit mid-run makes the two halves of the comparison run different
  tools. Finish or stop the run first.
- **Plans live in `docs/`** with a `Status:` line under the title. Delete a
  plan when it is done; git keeps the history.
- **LF line endings** everywhere except `*.ps1` (CRLF). See
  `.gitattributes`.
- **A localized UI text goes next to the English one**, never in its place
  (vscode display strings are tuples for this). The procedure is in the
  README.
- **Keep the tool standalone.** No DesktopPilot code in a product repo.
- **This repo is public: general code only.** Nothing private or
  product-specific goes here, in code, docs, tests, samples or commit
  messages: names of a private product, company, customer or person;
  internal repos, hosts, IPs, URLs or local paths; secrets, keys or account
  ids; a product's commands, labels, log paths, registry keys or tests;
  customer text. That code goes in an external plugin (below) in the
  product's own repo. Use neutral names in examples (`MyAddin`, `myapp`).
  The full list is in the README, "A general tool, in a public repo".
  Before you commit, check the diff against that list.

## The plugin contract

A plugin is a folder `src/desktoppilot/plugins/<name>/` (or in another
package, below) with
`profile.toml` (the app's data: exe, window class, launch and dialog
policy, depth, `extends`, `platforms`, `[windows]`/`[linux]` overrides),
`plugin.py` (`register(reg, profile)` adds
groups, roots, hooks and a fixture module), optional `fixtures.py`, and
`NOTES.md` for the agent at run time. Core discovers every profile, takes
the lineage from `extends`, and builds the CLI from the registry; core
imports no plugin. A hook is `fn(ctx, **kwargs)`. A root that can mean "not
open" raises `NotPresent`, and a UIA read the app timed out on raises
`WalkCut`, never "not found" (an AT-SPI timeout counts as a UIA one). The
README "Plugins" section has the full key and hook list.

A plugin in another installed package declares its folder in the
`desktoppilot.plugins` entry-point group, name = plugin name:

```toml
[project.entry-points."desktoppilot.plugins"]
myapp = "mypkg.myapp"
```

`profiles.plugin_folders()` is the one place that maps a plugin name to its
folder. The parent package's `__init__` must import on every platform; the
plugin itself is imported only after the `platforms` check. A clash with a
shipped name (plugin or profile) or any import failure goes to `reg.broken`
under `name=package`, never a profile name; discovery never raises for it.

In the code: every command prints JSON through `commands.run`, and one that
acts on the app uses `run_for`, so that a failure leaves a screenshot. Poll
with `util.wait_until`; never sleep.
