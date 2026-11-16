# chrome: Claude in Chrome

- server: claude-in-chrome
- source: builtin
- fingerprint: `sha256:becff8c3590ba43f`
- tested: 2026-10-17
- customer text: not allowed
- acts on: the tabs of the user's own Chrome, with their sign-ins

Tested on the host (extension 1.0.101, Chrome Profile 1, a 640x480
screen) against a probe form injected into example.com, with `pilot
desktop` and the root `win:<page title> - Google Chrome`. The window title
follows the active tab: switch tabs (`ui click "//TabItem[name~='...']"`)
before you use the new title.

| task | path | result | gaps |
| --- | --- | --- | --- |
| Read a page (DOM, accessibility tree, text) | server | full | `get_page_text`, `read_page`, `find` and `javascript_tool` all read it |
| Click, type, fill a form on a page | server | full | `form_input` sets a checkbox and a select; `computer` click and type reach a text box and a button |
| Read the console and network | server | full | network recording starts at the first `read_network_requests` call: earlier requests are missed, so call it once, then reload or act |
| Screenshot a page | server | full | the page's viewport only, at the window's size; not Chrome's own UI |
| Chrome's own UI: menus, extension popups, permission bars | pilot | full | the toolbar, address bar, tabs, side panel and infobars are all in the tree; `Button[name='Chrome']` opens the main menu, its items are `desktop//MenuItem` (`--depth 20`), `{Esc}` closes it. A minimized window is at -32000: `ui activate` first |
| Native dialogs: file pickers | pilot | full | click the file input (`Button[name~='<label>']`), `ui set-value "dialog//Edit[name='File name:']" PATH`, then `dialog//SplitButton[id='1']`; `Button[name='Open']` is the drop-down arrow. On Linux the picker is the portal's "Open File" window, found with `pilot desktop ui windows` (see chrome NOTES.md). `file_upload` skips the picker, and so does `pilot chrome cdp upload CSS FILE...` (a hidden input too) |
| Any page when the extension is not connected | pilot | full | reads and clicks through UIA (`--depth 25`), but an element below the fold does not match (`--hidden` finds it) and the Document has no Scroll pattern: click page text, `ui keys "{End}"`, then act. Or `pilot chrome cdp` (eval, click, hover, type, keys, set-value, upload, scroll, drag, navigate, read, wait, shot, console; `--color-scheme dark` on eval, read, wait, shot) on a Chrome started with `--remote-debugging-port` and its own `--user-data-dir`: never the user's Chrome |
| Sign in, type a password | neither | - | the user signs in |

## Linux

Not tested against the server: the pilot rows only, on Chrome 153 under
XFCE on Xorg (2026-10-18), with the `chrome` app (or `pilot desktop` and the
root `win:<page title> - Google Chrome`).

- **Chrome's own UI**: the toolbar, tabs and pages are in the AT-SPI tree
  (Chrome started with `--force-renderer-accessibility`, which the `chrome`
  profile passes, and `toolkit-accessibility` on). Menus and popups are
  override-redirect windows: activate the Chrome window first.
- **File pickers** are GTK dialogs (a window of type dialog that `dialogs`
  lists), not `#32770`: the Windows steps above do not apply. Not run.
- **Any page when the extension is not connected**: `ui set-value` fails on
  a page's input (Chromium gives AT-SPI no EditableText); use
  `pilot chrome cdp set-value` or `cdp type`, or `ui type` with the window
  in front.
