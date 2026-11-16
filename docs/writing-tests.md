# Writing tests

DesktopPilot ships a pytest plugin (the `pytest11` entry point
`desktoppilot`). A suite that installs the package gets the fixtures with no
conftest. `--app NAME` picks the app; the plugin then loads that app's
fixture modules, and those of every app it `extends`.

```python
from desktoppilot.core.pytest_plugin import Scope
from desktoppilot.plugins.word import roots


def test_task_list(pilot, open_doc):  # --app word
    open_doc(r"C:\docs\report.docx")
    roots.ribbon_click(pilot, "MyAddin>Task List")
    tasks = Scope(pilot, "pane:Task List")
    tasks.wait("Text[name~='Task List']", timeout=30)
    assert tasks.count("List[class='ListBox']")["count"] == 63
```

Count a list with `Scope.count` (or `ui count`), never with `find_all`: a
virtualized list holds only the rows on screen. Count the list, not an item:
an item's count is its own children. Escape a value you put into
a selector (`\'`, `\\`): a heading with an apostrophe is otherwise a parse
error.

## Fixtures

| fixture | from | is |
| --- | --- | --- |
| `pilot` | core | session; the `AppContext` every call needs |
| `desktop_app` | core | session; the app, launched or adopted per its profile. Killed at the end unless it was adopted or `--app-keep-open` |
| `windows_events` | core | autouse; a crash, hang or .NET exception of the app during the test fails it in teardown |
| `close_dialogs_after_test` | core | autouse; closes stray dialogs in an app this run launched, and only reports them in an adopted one |
| `word` | word | session; `desktop_app` under a Word name |
| `app` | word | session; the COM `Application` |
| `open_doc(path, read_only=False)` | word | opens a document and waits for Word to settle; discards that document (never saves) at the end |
| `pane` | word | `Scope(pilot, "pane")`; bare `pane` fails once two panes are open, so prefer `Scope(pilot, "pane:TITLE")` |
| `win(title)` | word | a `Scope` on `win:TITLE`, a window the add-in opened |
| `close_addin_windows_after_test` | word | autouse; closes the add-in's windows and dialogs between tests |

A plugin from another package adds its own fixtures the same way.

`Scope(pilot, root)` has `find`, `find_all`, `count`, `click`, `type`,
`wait`, `texts`, `contains` and `control`. `texts` and `contains` raise
`WalkCut` on a cut walk instead of answering "absent".

## Flags

- `--app NAME`: the app under test. Without it no app fixture works.
- `--app-fresh`: kill a running instance before the launch.
- `--app-keep-open`: leave the app running after the session, and skip the
  teardown clean-up, to inspect a failure.
- `--pilot-artifacts DIR`: where failure evidence goes (default
  `artifacts`).
- `--pilot-allow-crash`: report a crash of the app instead of failing the
  test, for a test that crashes it on purpose.

## On failure

A test that fails in setup or in its body gets a folder
`<artifacts>/<test id>/` with `window.png`, `state.json`, `tree.json`,
`controls.json`, `events.json` and `manifest.json` (`saved`, `failed`,
`complete`, `fallback_used`). The report lists them in a "desktoppilot
artifacts" section. Other report sections:

- "desktoppilot windows events": what Windows recorded since the test began.
- "desktoppilot stray dialogs (teardown)": dialogs found after the test.

Teardown problems go to `user_properties` and a warning, never hidden:
`desktoppilot_stray_dialogs`, `docs_not_closed`, `addin_not_closed`,
`addin_close_incomplete`. `pilot <app> capture NAME` collects the same
evidence by hand.

## Environment variables

| variable | read by | points at |
| --- | --- | --- |
| `PILOT_TEST_DOC` | the tool's smoke tests | their document (default `C:\pilot-gate\sample.docx`; without one the document tests skip) |
| `DESKTOPPILOT_WER_DIRS` | `events` | WER report folders, `os.pathsep` list |
| `PILOT_TEST_ACTIVATE` | `tests/ui/linux` | set: run the tests that activate a window (popups) |

A product suite may read `PILOT_TEST_DOC` too, so one document serves
both.

## The tool's own gates

```powershell
uv run pytest -m "not word and not ui" -q    # unit tests, no desktop
uvx ruff check src tests
uvx ruff format .
uv run pytest -m word --app word -q          # smoke: launches and kills Word
```

- The smoke run needs a free Word (no other Word agent) and a document:
  `PILOT_TEST_DOC`, or a copy at `C:\pilot-gate\sample.docx`.
- `tests/unit` needs no desktop. `tests/smoke` is Word only.
- No tool test depends on a third-party add-in. A test that needs one
  skips when the add-in is absent.
- The `ui` marker is for live desktop tests: today the Linux ones in
  `tests/ui/linux` (see below).
- There is no CI: a `word` or `ui` test drives the real pointer and needs a
  desktop session of its own.

## On Linux

The core fixtures (`pilot`, `desktop_app`, `windows_events`,
`close_dialogs_after_test`) and the flags work on Linux (X11). The `word`
fixtures do not: that plugin is Windows only and does not load there (`pilot apps` lists them under `unsupported`).

- `windows_events` keeps its name. On Linux it reads the journal, apport
  and systemd-coredump, and fails a test on a `crash` or an `oom` (README,
  Events). Its report section is still "desktoppilot windows events".
- Failure artifacts go to the same `<artifacts>/<test id>/` folder;
  `tree.json` is the AT-SPI tree, with UIA type names.
- Markers: `ui` for a test that needs a real desktop app;
  `@pytest.mark.windows("why")` for one that tests Win32-only behaviour, so
  it skips off Windows. A module that cannot import off Windows (it imports
  `word`, or tests only SendInput) goes in
  `WINDOWS_ONLY` in `tests/conftest.py`.
- `tests/unit` runs on Linux with no desktop; a `test_linux_*` module tests
  the backend with X11 and AT-SPI faked.
- `tests/ui/linux` is the live gate:

  ```bash
  uv run pytest tests/ui/linux -m ui -q
  PILOT_TEST_ACTIVATE=1 uv run pytest tests/ui/linux -m ui -q   # also the popup tests
  ```

  It starts its own Mousepad, GTK3 and GTK4 windows (the system
  `/usr/bin/python3` with `python3-gi`) and Chrome on a scratch profile, and
  closes them. It needs `DISPLAY`, AT-SPI on, and no other agent driving the
  desktop. `test_atspi.py` acts only through patterns (no pointer, no
  keys); its popup tests activate their window and run only with
  `PILOT_TEST_ACTIVATE=1`. `test_linux_live.py` always activates its
  Mousepad, then clicks and types into it, after it checks that Mousepad is
  the active window.
- A live test strips the VS Code snap's variables from its app's
  environment (`procs.app_env`), or the app has no AT-SPI tree when the run
  starts from VS Code's terminal (README, Linux).

## Product suites

Tests that assert what a product does do not live here. They live in the
product's own repo or package, with their own `pytest.ini`, and import this
package's fixtures. A failing product test is a product
regression; a failing tool test is a tool bug. Keep them apart, so that
each failure points at one cause. A new product suite goes there too,
not into this repo.
