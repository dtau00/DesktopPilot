# word - agent notes

`pilot word` drives Microsoft Word desktop: UIA for the UI, COM for the
document. Explore with `ribbon tabs`, `ui tree --flat` and `pane read`, then
act. The selector grammar, the Rules, the failure ladder and the error kinds
are in the [README](../../../../README.md); this file is what is particular
to Word.

## Traps

- **Only our Word is driven.** Another Word fails with `NotOurWord`; `app
  status` lists it under `not_owned`.
- **`app launch` kills every running Word** and wipes its crash state. Warn
  the user first. It refuses while a Word it did not start holds documents.
- **Never answer Yes to the safe-mode prompt.** The plugin answers No.
- **An open modal blocks COM.** `doc open` then fails with the dialog's
  text. Run `pilot word dialogs`.
- **A folded ribbon group reads as disabled.** `Disabled` on the ribbon
  usually means the window is too narrow, not a feature gate.
- **`window_size` is in DIPs**, scaled by the DPI at launch.
- **`ui type` and `ui keys` cannot drive a WPF pane.** Use `ui set-value`
  for a text box and `ui select --item` for a combo box.
- **WPF lists are virtualized and trees collapsed.** Count with `ui count`,
  never with `ui find --all`.
- **Every UIA query grows WINWORD**, and only a restart gives it back.
- **`shot` copies the screen.** Run `ui activate` first; check `occluded`.
- **`{PGDN}` and `ui scroll` do not scroll the document.** Scroll over COM.
- **A disconnect during Word's `Quit` is success**, not a crash.
- **Paths past MAX_PATH break Word.** Keep documents near the drive root.
- **Find the document window `_WwG` by class**, never by control type.
- **`WalkCut` means Word is busy.** Retry; never read it as "not found".

## Roots

| Root | Resolves to |
| --- | --- |
| `app` | Word's frame window, class `OpusApp`. Overrides core's `app`, which takes the largest window of the process; Backstage and add-in WPF windows belong to the same process. |
| `ribbon` | The `Ribbon` pane inside the frame. |
| `pane` | An add-in's WPF task-pane host: a `Pane` whose name is in the profile's `[options] pane_hosts` (see below). With two or more hosts open it fails and lists their headers: use `pane:TITLE`. With no host open, or no `pane_hosts` set, it is the first wide pane below the frame. |
| `pane:TITLE` | The listed WPF host whose header equals TITLE (else contains it; whitespace collapsed, case ignored). Failing that, a wide pane (120 px or more) below the frame whose text holds TITLE, e.g. `pane:Navigation`. The ribbon, Backstage, the document and an add-in's toolbar never answer. |
| `backstage` | The File menu, only while it is open. |
| `win:NAME` | A top-level window an add-in opened, matched on title; the Word frame is excluded. Overrides core's `win`. |
| `popup` | An open ribbon drop-down: Word hangs it in a top-level `NetUIToolWindow`. |
| `dialog`, `desktop`, `focus` | Core's. |

A WPF add-in hosts each task pane in a window it names itself; Word gives
that window no title, so lookup by name misses the pane. The add-in's own
profile (one that `extends = "word"`) lists those names, and `pane` then
finds the pane by its header and leaves the host out of the ribbon,
Backstage and frame walks:

```toml
[options]
pane_hosts = ["MyAddin pane host"]
```

`word` itself lists none.

```
ribbon//TabItem[name='Review']
pane//Button[name='Clear']
win:Inspector//Text[name~='Errors']
app//Text[name~='No results']
```

A root that means "not open" (`pane:TITLE`, `backstage`, `win:NAME`,
`popup`) raises `NotPresent`. A walk that a busy Word cut raises `WalkCut`,
so `backstage close` never reports an open Backstage as closed. `ui wait
--gone` asks each root to look once per poll (one pass of walks, up to 5 s;
`pane:TITLE` takes about six walks, ~1.7 s on an idle Word).

Default `--depth` is 12, `--root-depth` 20.

## Lifecycle

### Launch

The profile says `launch = "fresh"`. `pilot word app launch` kills every
running Word, wipes the keys under
`HKCU\Software\Microsoft\Office\16.0\Word\Resiliency`, and starts Word with
`/q /n` at 1600x1000 DIPs. `--adopt` joins a running Word instead, and
`--no-pin-size` keeps Word's own window size.

The Resiliency keys hold a crashed Word's leftovers: disabled add-ins, the
Document Recovery pane, "Word didn't shut down properly". Each one changes
what the next run sees, so a launch wipes them.

The launch ends when the `ready` hook says so: COM answers and the COM
add-ins that load at startup report connected. A ribbon click before that
does nothing. Only an add-in whose LoadBehavior asks for a startup load (3
or 16) is waited for. One still down after 20 s does not fail a plain
`word` launch: the JSON (and `app status`) lists it in
`addins_not_connected` as `{"progid", "load_behavior"}`. Add-ins match on
their exact ProgId.

**LoadBehavior 2.** Word sets an add-in that threw at startup to 2 and
never loads it again; the Resiliency wipe does not undo that.
`session.reset_load_behavior(name)` puts 2 back to 3 in every registration
and reports each one. HKLM is written only from an elevated process.

### Which Word is ours

The tool drives the Word `app launch` started, or, after `app launch
--adopt` or `app attach`, exactly the Words the session recorded in its
`pids` then (`session.owned_pids`); a creation time after the adopt rules
out a pid Windows has reused. Only a session file from before `pids`
existed falls back to creation time alone. The frame, every root under it, the COM object,
`started_at` and `documents_open` look at those pids only. A Word started
later, even one on top, is never clicked or asked: a command fails with
`NotOurWord`, naming the pids. With no Word at all, a `doc` or `ribbon`
command fails at once with `AppNotRunning`; a selector wait gives it
2 s (`APP_GRACE`) first. `com.attach` checks whose COM object
`GetActiveObject` returned, and refuses a foreign one, or one it cannot
tell from a foreign Word with no document open.

`word` and every plugin that extends it share one session file
(`WINWORD.EXE.json`), so they agree on which Word is ours.

### The safe-mode prompt

After a force-kill, Word can ask "Word couldn't start last time. Do you want
to start in safe mode?" before it registers for COM; every command then
fails with "cannot attach to Word ... Operation unavailable". Safe mode
disables every add-in, so the run fails for the wrong reason.

The plugin answers No. Core's launch reaps the known prompts on every poll
while it waits for the main window; the `post_launch` hook and each `ready`
poll reap them again. By hand: `pilot word dialogs --dismiss`. The prompts
match on their text: English, plus the ja-JP safe-mode prompt ("セーフ
モード"). When no label matches, the plugin answers by Win32 button ID
(IDNO, then IDCANCEL; never IDYES), which works in any language.

The Resiliency wipe does not prevent the prompt. Measured 2026-09-24: it
shows 0.8 s after the start, before any `OpusApp` window.

### Kill

`app kill` runs a graceful `Quit`, then force-kills, and returns
`{killed, quit_errors}`. It refuses while a Word this tool did not start
holds documents (`ForeignWordOpen`). On a busy or orphaned Word the `Quit`
only waits out its timeouts (about 50 s); `app kill --force` skips it and
kills every Word at once, documents and all.

## Dialogs

Word's own dialogs (Find and Replace, Options, Font) are class
`bosa_sdm_msword`, not `#32770`, and some are child windows of the frame
that `EnumWindows` never reports. The profile's `dialog_classes` and core's
child-window walk cover both.

An open modal blocks the object model: `doc open` answers "the Find or
Replace dialog box is open".

- `pilot word dialogs` lists them.
- `--dismiss` answers only the known prompts (safe mode, recovery) and
  reports the rest.
- `--close` closes every open dialog, known or not.

## Ribbon and panes

- Ribbon labels carry layout whitespace ("Track Changes\n"). Every comparison
  collapses it: pass the label as a person reads it.
- `ribbon click "Tab>Control"`, or `"Tab>Group>Control"` to choose between
  two. It activates the tab with `SelectionItem.Select()` (Invoke does
  nothing on a `TabItem`), then looks only in the selected tab's group
  inside the Lower Ribbon (`NetUIPanViewer`), so "MyAddin>Compare" cannot
  hit Review's Compare. With a collapsed ribbon it searches the whole
  ribbon.
- A tab's controls are built on first activation. A busy Word can answer
  the ribbon walk with nothing: the ribbon is polled for 15 s and a missing
  tab re-walked for 10 s before "ribbon tab not found" (which lists the tabs
  it saw). `ribbon tabs` raises `WalkCut`, never `[]`; with no Word,
  `AppNotRunning` at once.
- A click waits for a disabled control to enable, within its timeout, then
  raises `Disabled`. Add-in buttons can enable 2-4 s after a document
  opens.
- **A narrow window folds ribbon groups.** Office collapses the rightmost
  groups behind a chevron; their controls stay in the tree but read as
  disabled, which looks like a feature gate. Click the chevron, or make the
  window wider: `app launch --no-pin-size` then `app resize W H`, or change
  the profile's `window_size`. Do not drag the window by hand.
- **`window_size` is in DIPs.** Launch scales it by the window's DPI, so
  1600x1000 is 2400x1500 physical at 150 %, and the ribbon is the same at
  every scale. When the scaled size does not fit the work area, it is cut
  and the window moves to the top-left (`clamped: true`; the ribbon can then
  fold). The launch's `layout` says what it got: `dpi`, `scale`,
  `requested_dips`, `physical`, `dips`, `clamped`, `work_area`. The scale can
  change under a running Word (an RDP reconnect): `app status` reports
  `window` with `matches_profile`, and `app resize` with no arguments pins
  the profile's size again.
- WPF list rows are often named after their view-model class. Assert on
  their `Text` children.
- `pilot word addin windows` lists Word's top-level windows (the titles for
  `win:NAME`). `pilot word addin close` closes add-in WPF windows:
  titled ones, and untitled child windows of the frame that carry buttons
  (toasts). It leaves task panes. A window that would not close is in
  `not_closed`; toasts Word did not answer for, in `incomplete`.
- Between tests the `close_addin_windows_after_test` fixture closes the
  add-in's windows only in a Word this run launched. In an adopted Word it
  clicks nothing: it records the titles it left on the test as
  `addin_left_open` and warns.
- The first `ControlFromHandle` for a new frame costs 3-50 ms warm and
  about 274 ms cold (measured); it is not why a busy Word is slow. A make
  that ends past the caller's budget raises `WalkCut`, and the retry gets
  the cached element. Each make's time is in `roots.FRAME_MAKES`.

## Documents

Read documents over COM (`doc read`, `doc find`), never from the canvas:
COM gives exact text, styles, list numbers, revisions and pages.

- `doc open` leaves Protected View, so add-ins run. Alerts are off and
  a password is always passed (`--password`, or a dummy), so a protected
  file fails with "The password is incorrect" instead of a prompt.
  `--repair` is Open and Repair. A dialog that still blocks the open for 1 s
  is answered Cancel or No and named in the error, or in
  `dialogs_answered`.
- `doc open` returns once Word answers UIA quickly again (`settled`,
  `settle_s`).
- `doc find` hits carry `page` (physical, wdActiveEndPageNumber) and
  `page_label` (the printed number). They differ when a section restarts
  its numbering.
- `doc read --limit N` counts what comes back, so skipped empty paragraphs
  do not shrink the result.
- `doc read`, `doc comments` and `doc revisions` each have one 120 s budget,
  then fail with `Timeout` (how many were read). `doc revisions` reads only
  the first `--limit`: `count` is all of them, `by_author` tallies the
  listed ones, `complete` says whether that is all.
- A COM call Word answers "busy" is retried. One that threw inside Word
  (RPC_E_SERVERFAULT) is not, so Open or InsertAfter never runs twice.

### Scrolling

There is no scroll command for the document. `ui keys "{PGDN}"` does not
scroll Word, and `ui scroll` on `app//Document[class='_WwG']` gets
`WalkCut` on a large document. Scroll over COM; it goes through Word's own
scroll path, so an add-in's scroll hook sees it:

    $w = [Runtime.InteropServices.Marshal]::GetActiveObject('Word.Application')
    $win = $w.ActiveWindow
    $win.VerticalPercentScrolled = 25           # jump to 25 % of the document
    $win.SmallScroll(18)                        # down 18 lines (negative: up)
    $win.LargeScroll(1)                         # down one screen
    $r = $w.ActiveDocument.Footnotes.Item(5).Reference
    $r.Select(); $win.ScrollIntoView($r)        # bring a range on screen

`SmallScroll` on a window that is not active scrolls that window;
`Window.Activate()` alone does not.

## Memory: every UIA query grows WINWORD

An add-in pane built with WPF leaks: WPF keeps a COM wrapper for each
element a UIA client touches (dotnet/wpf #11337, open, .NET 4.8-10). Measured
2026-09-24: a full expand of a 2456-node outline adds 250-570 MB to
WINWORD; Collapse frees nothing; closing the document frees only part.
After several large documents (about 2 GB) Word stops answering UIA for
minutes, while its message pump is fine.

- Count a tree once (`ui count` expands in one cached pass).
- Do not dump big trees without a reason.
- In a long run, log WINWORD's working set per document and restart Word at
  a threshold. A `Quit` helps only when the process exits.

## What is on screen: `layout` and `visible`

Two read-only probes of the view:

- `pilot word layout [--page P] [--radius 1]` reads the page layout over COM
  for pages P-R to P+R: each page's box, its Rectangles and, for body text
  (main, footnotes, endnotes), each Line with its box, range, a text preview,
  `get_point` (Window.GetPoint, screen px), `page` and `page_position_pt`. P
  defaults to the page under the centre of `_WwG`, else the selection's.
  Header and footer lines are skipped: GetPoint on them opens the header for
  editing.
- `pilot word visible [--timeout 10]` asks UIA for the `_WwG` element's
  visible text ranges and the page elements with a non-empty rect (with
  their header, content, footnote and footer parts), then maps them onto
  Word over COM. Main-story UIA offsets are Word positions, so each range
  gets Word's text and page and a `text_match`.

Both put an error on one item into `errors` instead of failing. Take them
back to back: GetPoint tops and UIA range tops agreed within 1 px at 50, 68
and 100 % zoom; a `layout` taken 30 s after a zoom change was 25 px off,
because the view had moved.

## Reference

### `layout` internals

- **`Window.GetPoint` returns through [out] parameters**, which late-bound
  pywin32 cannot pass. `view.get_point` calls `_oleobj_.InvokeTypes` with
  `(VT_BYREF|VT_I4, PARAMFLAG_FOUT)` per out parameter. Do not run makepy:
  a generated cache makes every `GetActiveObject` early-bound and changes
  the error shapes `com_retry` relies on.
- **`layout` is slow.** Each Line member costs 7-20 ms inside Word, about
  0.2 s a line: radius 1 on a 251-page document (3 pages, ~115 lines) takes
  25-30 s, radius 0 about 10 s.
- **Some rectangles answer "That property is not available on that object"**
  for every member (10-17 per page). They come back as
  `{"type": "unavailable"}`, one line in `errors` each: expected noise.
- **`Pane.Pages` covers only laid-out pages.** A line on a laid-out page off
  screen gets real off-screen GetPoint coordinates. Only `visible` says
  what is displayed.

### `visible` internals

- Page elements are the Custom children of the document element; `ordinal`
  is the position, because names are localized. The list starts with a
  non-Custom Pane (a floating control).
- Each page gets `word_page` (from `RangeFromChild`); each footnote gets
  Word's footnote with the number at the end of its name, checked by
  `text_match`. `main` and `displayed_pages` summarize.
- **Find `_WwG` by class.** An add-in's Inspector pane hosts a WPF
  FlowDocumentScrollViewer that is also a Document. With Word's "Welcome
  back! Pick up where you left off" card expanded, `FindFirst(Descendants)`
  from the OpusApp finds nothing at all; `visible` then walks OpusApp ->
  `_WwF` -> `_WwB` -> `_WwG` by Win32 class and says
  `found_via: "window-chain"`.
