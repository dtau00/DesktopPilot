"""AT-SPI role -> UIA control type name, so a selector such as
`ButtonControl[name=OK]` means the same element on both platforms.

Keys are `Atspi.Role` value names as `Atspi.role_get_name` spells them
(lower case, spaces); every role of AT-SPI 2.60 is listed. A role with no
UIA counterpart maps to `CustomControl`, as UIA itself calls such an element.
Where Chromium on Windows already maps a web role to a UIA type (paragraph,
section, form, cell, row, ...), the same type is used here, so a selector
written against Chrome or an Electron app on Windows finds the same element.

Four roles depend on where the object sits; `control_type` takes the parent's
role for them:

- `menu`: a submenu that is an item of a menu bar or a menu is a MenuItem, as
  UIA shows "File" in a menu bar; any other menu (a combo box's list) a Menu.
- `file chooser`, `color chooser`, `font chooser`: a Window when top-level
  (the child of an application), else the Pane they are embedded in.
- `table cell` in a `tree table` (a GTK3 tree view): its first-column cells
  are the tree's rows, so they are TreeItems - `ui count` and `ui expand`
  look for TreeItem children. Its other cells are DataItems.
"""

from __future__ import annotations

FALLBACK = "CustomControl"

ROLES: dict[str, str] = {
    "invalid": "CustomControl",
    "accelerator label": "TextControl",
    "alert": "WindowControl",
    "animation": "ImageControl",
    "arrow": "ButtonControl",
    "calendar": "CalendarControl",
    "canvas": "ImageControl",
    "check box": "CheckBoxControl",
    "check menu item": "MenuItemControl",
    "color chooser": "PaneControl",
    "column header": "HeaderItemControl",
    "combo box": "ComboBoxControl",
    "date editor": "EditControl",
    "desktop icon": "ListItemControl",
    "desktop frame": "PaneControl",
    "dial": "SliderControl",
    "dialog": "WindowControl",
    "directory pane": "PaneControl",
    "drawing area": "PaneControl",
    "file chooser": "PaneControl",
    "filler": "PaneControl",
    "focus traversable": "CustomControl",
    "font chooser": "PaneControl",
    "frame": "WindowControl",
    "glass pane": "PaneControl",
    "html container": "PaneControl",
    "icon": "ImageControl",
    "image": "ImageControl",
    "internal frame": "WindowControl",
    "label": "TextControl",
    "layered pane": "PaneControl",
    "list": "ListControl",
    "list item": "ListItemControl",
    "menu": "MenuControl",
    "menu bar": "MenuBarControl",
    "menu item": "MenuItemControl",
    "option pane": "PaneControl",
    "page tab": "TabItemControl",
    "page tab list": "TabControl",
    "panel": "PaneControl",
    "password text": "EditControl",
    "popup menu": "MenuControl",
    "progress bar": "ProgressBarControl",
    # Role 43: AT-SPI 2.60 names it "button"; older ones and some toolkits
    # (role names read over D-Bus) still say "push button".
    "button": "ButtonControl",
    "push button": "ButtonControl",
    "radio button": "RadioButtonControl",
    "radio menu item": "MenuItemControl",
    "root pane": "PaneControl",
    "row header": "HeaderItemControl",
    "scroll bar": "ScrollBarControl",
    "scroll pane": "PaneControl",
    "separator": "SeparatorControl",
    "slider": "SliderControl",
    "spin button": "SpinnerControl",
    "split pane": "PaneControl",
    "status bar": "StatusBarControl",
    "table": "TableControl",
    "table cell": "DataItemControl",
    "table column header": "HeaderItemControl",
    "table row header": "HeaderItemControl",
    "tearoff menu item": "MenuItemControl",
    "terminal": "DocumentControl",
    "text": "EditControl",
    "toggle button": "ButtonControl",
    "tool bar": "ToolBarControl",
    "tool tip": "ToolTipControl",
    "tree": "TreeControl",
    "tree table": "TreeControl",
    "unknown": "CustomControl",
    "viewport": "PaneControl",
    "window": "WindowControl",
    "extended": "CustomControl",
    "header": "GroupControl",
    "footer": "GroupControl",
    "paragraph": "GroupControl",
    "ruler": "PaneControl",
    # The desktop's children skip the application level (top-level windows,
    # as on Windows), so an application object surfaces only when asked for
    # by hand; a Pane never matches a `Window` step.
    "application": "PaneControl",
    "autocomplete": "ComboBoxControl",
    "editbar": "EditControl",
    "embedded": "PaneControl",
    "entry": "EditControl",
    "chart": "ImageControl",
    "caption": "TextControl",
    "document frame": "DocumentControl",
    "heading": "TextControl",
    "page": "GroupControl",
    "section": "GroupControl",
    "redundant object": "CustomControl",
    "form": "GroupControl",
    "link": "HyperlinkControl",
    "input method window": "WindowControl",
    "table row": "DataItemControl",
    "tree item": "TreeItemControl",
    "document spreadsheet": "DocumentControl",
    "document presentation": "DocumentControl",
    "document text": "DocumentControl",
    "document web": "DocumentControl",
    "document email": "DocumentControl",
    "comment": "GroupControl",
    "list box": "ListControl",
    "grouping": "GroupControl",
    "image map": "ImageControl",
    "notification": "GroupControl",
    "info bar": "GroupControl",
    "level bar": "ProgressBarControl",
    "title bar": "TitleBarControl",
    "block quote": "GroupControl",
    "audio": "GroupControl",
    "video": "GroupControl",
    "definition": "GroupControl",
    "article": "GroupControl",
    "landmark": "GroupControl",
    "log": "GroupControl",
    "marquee": "GroupControl",
    "math": "GroupControl",
    "rating": "SliderControl",
    "timer": "GroupControl",
    "static": "TextControl",
    "math fraction": "GroupControl",
    "math root": "GroupControl",
    "subscript": "TextControl",
    "superscript": "TextControl",
    "description list": "ListControl",
    "description term": "ListItemControl",
    "description value": "GroupControl",
    "footnote": "GroupControl",
    "content deletion": "GroupControl",
    "content insertion": "GroupControl",
    "mark": "GroupControl",
    "suggestion": "GroupControl",
    "push button menu": "ButtonControl",
    "switch": "ButtonControl",
    "last defined": "CustomControl",
}

# The roles whose type depends on the parent's role (see the module doc).
MENU_PARENTS = ("menu bar", "menu", "popup menu")
CHOOSERS = ("file chooser", "color chooser", "font chooser")
CONTEXT_ROLES = ("menu", "table cell", *CHOOSERS)


def control_type(
    role_name: str, parent_role: str | None = None, *, first_column: bool = True
) -> str:
    """The UIA control type name for an AT-SPI role name. `parent_role` is
    read only for CONTEXT_ROLES; `first_column` only for a tree table's cell."""
    if role_name == "menu" and parent_role in MENU_PARENTS:
        return "MenuItemControl"
    if role_name in CHOOSERS:
        return "WindowControl" if parent_role == "application" else "PaneControl"
    if role_name == "table cell" and parent_role == "tree table":
        return "TreeItemControl" if first_column else "DataItemControl"
    return ROLES.get(role_name, FALLBACK)
