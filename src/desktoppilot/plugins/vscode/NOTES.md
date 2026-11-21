# vscode - agent notes

`vscode` is the user's running VS Code (`extends = "code"`: adopt, depth 25,
`--force-renderer-accessibility`) plus three groups. `codex` drives the
**Codex extension's panel** (`openai.chatgpt`, measured on build
26.917.62051 on Windows, 26.1007.21434 on Linux) through UIA or AT-SPI, the
command palette and the clipboard. `git` checks out a branch through the
palette, and compares HEAD with a base in GitLens' Search & Compare view.
`cdp` drives a VS Code started with `--remote-debugging-port` (see
"cdp"). The Rules, selectors and failure ladder are in the
[README](../../../../README.md). Linux differences are under "Linux".

    pilot vscode codex open
    pilot vscode codex model --name "GPT-6 Sol" --effort high --fast off
    pilot vscode codex send "Reply with the word OK."      # or --file prompt.md
    pilot vscode codex wait --timeout 1800
    pilot vscode codex read
    pilot vscode git checkout user/me/feature --window OtherRepo
    pilot vscode git compare --base main --window MyRepo
    pilot vscode cdp eval "document.title" --port 9335      # a scratch VS Code only

Selector root `codex` is the panel's web document:
`pilot vscode ui find "codex//Button[name='Send']"`.

## Traps

- **This is the user's live editor.** Nothing here kills, restarts, resizes
  or reloads VS Code. Never approve a Codex action; ask the user.
- **Two windows with a Codex panel is `Ambiguous`.** Every `codex` command
  takes `--window` (part of the title).
- **`send` refuses an unsent draft** in the composer unless
  `--replace-draft`.
- **`read` checks the clipboard against the visible reply.** A foreign copy
  is an error, not the reply; run `read` again.
- **Every selector is an English display string.** A localized or updated
  extension breaks the constants, not the logic: stop and ask (README, "When
  the UI text changes").
- **The sidebar can be dead for the whole session.** `open` uses an editor
  panel, never the sidebar.
- **A new panel can take minutes to boot.** `open` waits up to 180 s.
- **"Sol" is ambiguous.** Name the model exactly.
- **The `model` choice is the extension's default** for new chats, not only
  this chat's.
- **Permissions are shown, never changed.** "Full access" lets a review run
  commands without asking.
- **A usage limit cannot be cleared here.** `open` reports it in `blocked`;
  `wait` fails with `CodexFailed`.

Every keystroke is guarded, because another chat (Claude Code) usually sits
in the same window with focus in its composer. The palette is typed into
only after focus is proven to be in `quick-input-widget`, and panel keys
(Esc, arrows, Ctrl+V) only when the focused element is inside the Codex
frame (runtime-id walk). Clicks go through UIA patterns, or the pointer on
the element's own rect.

## Which panel, which window

- The target is a `Document[id='active-frame'][name='Codex']` whose
  `RootWebArea` is not named "Codex could not start". Its RootWebArea is named
  after the chat ("ChatGPT" on a new chat).
- An editor panel beats the sidebar view (a view's frame sits under a
  `pane-body`). Two visible of the same kind (split editor) is `Ambiguous`.
- Two windows with a Codex panel is `Ambiguous` unless `--window` (part of
  the title, one window only) names one. `open` with no panel anywhere
  needs `--window` when more than one window is open. `model` reads back on
  the same panel it set.
- A chat opened from history is its own editor tab, titled after the chat;
  its frame is not named "Codex", so it is never the target.

## open

- **The sidebar can be dead for the whole session.** If VS Code started with
  the Codex view hidden, the extension logs `renderer_ready_timeout` after
  30 s and the view shows "Codex could not start". It keeps that context
  (`retainContextWhenHidden`) until VS Code reloads. So `open` never uses the
  sidebar route (`Codex: Open Codex Sidebar`); it uses an editor panel.
- **A new chat in an editor panel needs a new panel.** The panel's URI is
  fixed (`/extension/panel/new`): `Codex: New Codex Agent` and the panel's own
  New chat button both only re-reveal the tab already holding it. So `open`
  reuses a visible panel that is on a new chat, and otherwise closes the
  editor tab named `Codex` (or `Codex, Editor Group N`) and runs
  `Codex: New Codex Agent`. The closed chat stays in Recent chats. It refuses
  while the visible panel is generating; a `Codex` tab hidden behind another
  tab cannot be read, so it is closed unchecked. The new panel opens in the
  active editor group: if closing the tab emptied its group, that can be
  the Claude group (the Claude tab is covered, not closed).
- **A new panel can take minutes to boot.** Measured 2026-09-27: after the
  extension logged `ipc-connection-reset`, the new panel showed a spinner
  (UIA: an empty document) for 121 s (`client.performance.span
  durationMs=120942 initialSurface=home`). `open` waits up to 180 s, and its
  timeout says whether the document was empty or what it showed instead. A
  panel that renders "Codex could not start" fails at once (`CodexFailed`).
- **Every new panel shows a first-run tour** (Back / Next, no composer: 4
  pages on build 26.917, 5 on 26.1007); `open` pages through it.
  `tour_pages` in the JSON says how many.
- No harness selector exists inside the Codex panel; the only harness choice
  is VS Code's own view switcher (Chat / Claude Code / Codex tabs). "Select
  where to run the chat" is Local / Cloud; `open` reports it as `where`.
- `blocked` in the JSON is a usage-limit or error notice on screen
  ("You've reached your workspace credit limit"), else null.

## model

Deterministic, and verified by reading state, never by the click:

- The picker is the radix drop-down that is not permissions / where / chat
  actions. Closed, its name is `"<model> <effort label>"` ("GPT-6 Sol High");
  open, it is "Select effort". Its menu hangs inside the Codex document.
- Menu (effort view): MenuItem `Select model` (opens the list), CheckBox
  fast mode, MenuItem `Reset to default` (never clicked), MenuItem `Power`
  (the effort slider) and a live-region Text "GPT-6 Sol Standard, 2 of 5.".
- Model list: RadioButtons with SelectionItem.IsSelected (build 26.917, UIA);
  radio menu items (`MenuItem`, checked through SelectionItem or Toggle) on
  build 26.1007 over AT-SPI. A model click closes the menu; on AT-SPI that
  close makes the click's own check fail after it landed, so the closed
  list is the answer. Build 26.917 offers
  Default, GPT-6 Sol, GPT-6 Astra, GPT-6 Luna, GPT-5.6 Sol, GPT-5.6 Terra,
  GPT-5.6 Luna, GPT-5.5. **"Sol" is ambiguous** (GPT-6 Sol, GPT-5.6 Sol) and
  is refused, never guessed: name the model exactly. Select() on the checked
  radio is a no-op, so it is not clicked.
- Fast mode: a CheckBox named for what a click would do - "Enable fast mode"
  (ToggleState 0, off) or "Enable standard mode" (1, on). A name that
  disagrees with its state reads as unknown. The closed label does not show
  fast mode at all; only the checkbox tells.
- Effort: the slider moves only by Left / Right on the focused `Power` item.
  The live region names stops in other words (Standard = medium, Extended =
  high, Extra High = xhigh, Ultra = ultra), so each step is closed and read
  back from the label. Keys: none, minimal, low, medium, high, xhigh, max,
  ultra, persistent (the extension logs `newEffort=<key>`).
- Read-back: model from the checked radio, model and effort from the closed
  label, fast from the checkbox. Any difference, or any field unreadable, is
  `ModelMismatch` with `details.wanted`, `details.got`, `details.mismatches`.
- The choice is the extension's **default** for new chats (log: "Setting
  default model and reasoning effort"), not just this chat's.
- A click that lands while the menu is still animating shut is lost; opening
  retries.

## send / wait / read

- `send` refuses a composer that holds an unsent draft unless
  `--replace-draft`. It pastes through the clipboard (one Ctrl+V for any
  length), checks the composer's value holds the prompt, clicks Send, and
  returns once Stop shows or a new "You said:" turn appears. The
  clipboard's text is put back; `clipboard_restored` is `"text"` or
  `false` (formats other than text are not restored).
- Generating = a Button named `Stop` exists (the Send button turns into it).
  A turn briefly shows Send between tool calls, so `wait` wants calm walks
  in a row (`--settle-polls`, default 2), and a user turn with no reply
  heading after it is still "generating".
- `wait` looks for a question only in the current turn, after the user's
  own message, so an old notice in the transcript cannot answer for it.
- Questions: an approval card (`Allow once`, `Always allow`, `Allow this
  conversation`, `Allow similar commands`, `Allow all edits`, `Deny`,
  `Submit` / `Skip` of the request-input panel, texts like "Allow ChatGPT to
  run this command?") gives `reason: approval`; a reply whose last line ends
  in `?` gives `reason: reply_asks`. The approval strings come from the
  bundle: no approval card was raised live (spikes send no commands).
- A turn that ends in "You've hit your usage limit..." (no reply) fails with
  `CodexFailed` instead of waiting out the timeout.
- `read`: each reply is an sr-only Text "ChatGPT said:", its text, then a
  Button `Copy` (a user message has "Copy message"). Copy is invoked through
  UIA (no hover needed while the reply is in the tree). `read` waits for the
  clipboard sequence number to move, checks that the copied text starts
  like the visible reply (`verified`), and restores the clipboard. The
  transcript is virtualized; if the last reply has no Copy button, `read`
  scrolls the panel to the end (ScrollPattern, else the wheel over the
  panel) and looks again.

## git checkout

- **No foreground default.** With one main window it is used; with several,
  `--window` (part of the title, one window only) is required and its absence
  is `Ambiguous` before any key is sent. The foreground window was once
  another project's, and the palette opened there.
- **The command is not the top row.** After typing "Git: Checkout to...",
  the top row is `Git: Checkout to (Detached)...`. `palette.run` steps
  {Down} (max 8), re-checking focus before each key, until the row reads the
  title exactly. Rows may carry `, recently used` (`_is_row` allows
  `title + ","`).
- **The branch picker opens a moment after Enter** (focus first in its input,
  "Select a branch or tag to checkout"); nothing is typed until it has focus.
- **Picker rows are codicon, ref, comma:** `git-branch <branch>, 2 weeks ago,
  <author> circle-small-filled <sha> ..., branches` (local), `cloud
  origin/<branch>, ..., remote branches` (remote). A row matches on
  `ref + ","`, so `feat` never takes `feat-2`. The top rows (`plus Create new
  branch...`, `plus Create new branch from...`, `debug-disconnect Checkout
  detached...`) always show and are never picked. A walk that wraps to its
  first row stops.
- **Verified by the status bar**: Button (class `statusbar-item-label`)
  named `MyRepo (Git) - <branch>*+!, Checkout Branch/Tag...`;
  `*`, `+`, `!` (modified, staged, merge) follow the branch. A new toast
  (`notifications-toasts`), in-window dialog (`monaco-dialog-box`) or native
  `#32770` modal instead fails with its text and is left on screen.
- `--remote` with only `cloud origin/<branch>` checks out a local tracking
  branch; not run live (it would create a branch in the user's repo).

## git compare

"Show this branch's changes against main" is one call:
`git compare [--base main] [--window T] [--timeout 15]`. It runs
`GitLens: Compare HEAD with...` (`gitlens.compareHeadWith`, one picker),
picks BASE, and reads the node `Comparing HEAD with main <repo>` in GitLens'
Search & Compare view. Measured 2026-10-07 (GitLens `eamodio.gitlens`): about
3 s.

- **`ui keys` is not a palette.** `pilot vscode ui keys "ctrl+shift+p"` with no
  `--into` typed the LITERAL text into the Claude Code composer, which holds
  focus. `ui keys` takes uiautomation syntax (`{Ctrl}{Shift}p`). `compare`
  goes through `palette.run`, with the same guards as `checkout`; nothing is
  typed until focus is proven in `quick-input-widget`.
- **The picker takes focus on its first row, not on its box.** The box (the
  quick input's Edit) is renamed "Choose a reference (branch, tag, etc) to
  compare with (or enter a revision prefixed with #) - Compare HEAD with";
  `palette.prompt()` reads it from the focused widget, and BASE is typed only
  once it asks for a reference.
- **Rows are the ref, a comma, its state:** `main, 11↓ 0↑ ⇄ origin/main
  git-commit f19354e • yesterday`; the current branch reads `<ref> check, ...`.
  A typed `main` also matches `origin/main` and
  `origin/feature/merge-to-main`, so the row up to its first
  comma must be BASE exactly; `palette.pick` steps {Down} with re-checks.
  No exact row is `NotPresent`, after Esc.
- **A UIA click on a picker ListItem does not accept it.** Filter, then
  {Enter} on the exact row.
- **No `GitLens: Compare HEAD with...` in the palette** (GitLens missing or
  disabled) is `NotPresent`; nothing ran.
- **GitLens reuses an identical comparison:** it moves the existing node to
  the top and selects it, and adds none. The node is the new one or the
  selected one; `existed_before` says whether a `Comparing HEAD with BASE`
  node was in the view before the call (null when the view was not rendered).
  A node named `Comparing <branch> with main` (another route) is not this
  command's and is left alone. Nothing is closed or removed.
- **The tree is flat in UIA.** The node's children (`Behind N commits`,
  `Ahead N commits`, `N files changed, A additions (+), D deletions (-)`,
  singular for 1) are its next sibling rows. A collapsed node is expanded
  through ExpandCollapse. A count the view never shows is null.
- **Walk the view, not the window.** A whole-window TreeItem walk at depth 40
  took 8.7 s; `Tree[name='Search & Compare']` with `limit=1` takes ~0.7 s.
- **The deep link does nothing.** `vscode://eamodio.gitlens/link/r/<root
  sha>/compare/<ref>...<ref>?url=<remote>`, with or without encoded refs,
  showed no node and no prompt. Not used.

## Reference

- The display strings are tuples in `panel.py` (`SEND`, `STOP`, `COPY`,
  `REPLY_HEADING`, `SELECT_MODEL`, `POWER`, `FAST`, `STANDARD`,
  `EFFORT_LABELS`, `QUESTION_TEXTS`, `FAILURE_TEXTS`, ...), `codex.py`
  (`NEW_PANEL_COMMAND`, `CLOSE`) and `locate.py` (`FRAME_NAME`). A localized text goes next to the
  English one. The webview gives no stable AutomationId (radix ids like
  `radix-_r_10_` change per render).
- Each walk of the panel takes ~0.5-2 s; the editor tab strip needs depth 32
  (5 s). Elements go stale between walks, so every property read is
  guarded.
- The tree is lazy: a new panel's frame appears in UIA a moment after it
  paints.
- Reading the transcript over CDP is not done: VS Code would need a restart
  with `--remote-debugging-port`, and this plugin never restarts it. The
  `cdp` group serves a VS Code that was started with one (a scratch one).

## cdp

The core `cdp` group (`core/cmd_cdp.py`, the same as `pilot chrome cdp`):
`targets`, `target`, `eval`, `click`, `hover`, `type`, `keys`, `set-value`,
`upload`, `scroll`, `drag`, `navigate`, `read`, `wait`, `shot`, `console`.
Page work: `cdp` first; `ui` and the real pointer only for what is not in a
page (native menus, OS dialogs) or what `cdp` cannot do. The port is `--port`, else the `--remote-debugging-port=N` on the
command line of the pid `PILOT_VSCODE_PID` binds, else one in the
profile's args. The user's VS Code never has one: it runs already, so a
launch adopts it and the flag never reaches it. A scratch profile that
extends `vscode` and adds the flag (and its own `--user-data-dir`) does.
Targets: the workbench is the one `page`; each webview is an `iframe`
target (`--type iframe --url <part of its vscode-webview:// URL>`), the
Codex app inside it one more frame (`#active-frame`) down.

## Linux

Measured 2026-10-18 on VS Code 1.141.0 (snap, Electron Chrome/150) under XFCE
on Xorg, Codex build 26.1007.21434. Every command was run live on a scratch
VS Code; `codex model` set was proven step by step (effort, model click) but
not end to end, since the account offered no fast-mode checkbox.

- **Launch.** A pilot run from VS Code's extension host inherits
  `ELECTRON_RUN_AS_NODE=1`; `app launch` drops it (on both platforms), or
  Electron runs as Node and rejects every flag. The snap's VS Code is
  classic, and its window class is `code_code` (snap) or `code`/`Code`
  (copy, .deb).
- **The frame.** The webview's `<iframe id="active-frame">` is AT-SPI role
  `internal frame`, a `Window[id='active-frame']` to the selectors (UIA: a
  Document); its document has role `document web` and no id (UIA:
  `RootWebArea`). Each document's `<body>` is a level of its own.
- **Editor or sidebar.** This VS Code draws every webview in an overlay
  (`webview-overlay-content`) outside the view, so no `pane-body` is an
  ancestor: a frame whose centre lies in a `pane-body` rectangle is the
  sidebar's.
- **The composer's text** is its Value: on AT-SPI the backend joins its
  paragraphs' text (its own Text is one U+FFFC per paragraph).
- **Headings come twice.** "You said:" and "ChatGPT said:" are each a Text
  inside a Text of the same name, so `user_turns` counts two per turn; it is
  only compared with itself.
- **Keys** go through XTest, paced by the X server (`interval` is dropped).
  Every palette burst first brings the window to the front and checks
  `_NET_ACTIVE_WINDOW` is it, else sends nothing (on Windows too).
- **A native modal** in `git checkout` is what core `dialogs` finds: a window
  of type dialog.
- **`git compare` was not run on Linux.** It uses the same palette guards
  and the backend's ExpandCollapse; GitLens' rows are not measured there.
- **The editor's text.** Monaco is not in the AT-SPI tree unless VS Code's
  screen-reader mode is on: read it with `ui read focus --copy` or `cdp`.
- **Menu item names include the shortcut** (`New Text File Ctrl+N`): match
  with `~=` or the full name.
- **`ui count` on a list** (the command palette's `List`) counts the
  ListItems inside Chromium's unnamed wrapper, not the wrapper.
- **A fresh profile** (a new `--user-data-dir`) opens first-run UI;
  `--skip-welcome --disable-workspace-trust` keep the Welcome page and the
  trust prompt away. Run `dialogs` after `app launch` and answer anything
  left before the first `ui` call.
- **Testing on the test machine.** Never on the user's VS Code. Copy the snap's
  binary under another name so `app launch` cannot adopt the user's and
  `app kill` cannot reach it: `usr/share/code/code-<name>` (the binary
  copied, its siblings linked) plus `usr/lib` linked to the snap's (the
  binary's RPATH is `$ORIGIN/../../lib`), args `--no-sandbox
  --user-data-dir=... --extensions-dir=... --skip-welcome
  --disable-workspace-trust`. `tests/ui/linux/test_vscode_live.py` does
  this. The Codex extension uses `~/.codex` (the user's sign-in and
  `config.toml`): a model change there is the user's default too, and a
  folder it opens gets a `[projects."<folder>"] trust_level` entry there.
  Back the file up first and put it back after.
