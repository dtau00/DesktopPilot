"""Git in the running VS Code: checkout through the palette's branch picker,
and GitLens' "Compare HEAD with..." into its Search & Compare view.

Rows and the status bar are read through UIA, and the checkout is verified by
the status bar's branch item, never by a sleep. See NOTES.md, "git checkout".
"""

from __future__ import annotations

import re

from desktoppilot.core import apps, dialogs, selectors, ui
from desktoppilot.core.errors import NotPresent, PilotError
from desktoppilot.core.native import auto
from desktoppilot.core.registry import AppContext
from desktoppilot.core.util import collapse_whitespace, wait_until

from . import palette
from .common import pick_window, read

# Display strings, each a tuple of accepted texts (see panel.py).
CHECKOUT_COMMAND = ("Git: Checkout to...",)
STATUS_SUFFIX = (", Checkout Branch/Tag...",)
GIT_PROVIDER = ("Git",)  # "<repo> (Git) - <branch>" in the status item
# Codicon ids, not display text: they do not change with the UI language.
LOCAL_ICON = "git-branch"
REMOTE_ICON = "cloud"
DIRTY_MARKS = "*+!"  # modified, staged, merge changes, after the branch name
STATUS_DEPTH = 30
NOTICE_DEPTH = 20
# VS Code's in-window notices; a native modal is a top-level #32770 window.
NOTICE_CLASSES = ("notifications-toasts", "monaco-dialog-box")
_BUTTON = selectors.parse("app//Button").steps[0]


# ------------------------------------------------------------- pure logic


def branch_row_matches(row: str | None, branch: str, remote: bool) -> bool:
    """Is `row` the branch picker's row for `branch`? A row is the codicon
    name, the ref, then a comma: "git-branch feat, 2 weeks ago, ..." (local),
    "cloud origin/feat, ..." (remote; only when `remote`)."""
    if not row or " " not in row:
        return False
    icon, rest = row.split(" ", 1)
    if icon == LOCAL_ICON:
        return rest.startswith(branch + ",")
    if icon == REMOTE_ICON and remote:
        ref = rest.split(",", 1)[0]
        return "/" in ref and ref.split("/", 1)[1] == branch and rest.startswith(ref + ",")
    return False


def is_remote_row(row: str | None, branch: str) -> bool:
    return branch_row_matches(row, branch, True) and not branch_row_matches(row, branch, False)


def status_branch(name: str | None) -> str | None:
    """The branch the status bar item shows, from its UIA name:
    "MyRepo (Git) - feat*+!, Checkout Branch/Tag..." -> "feat"."""
    if not name:
        return None
    name = collapse_whitespace(name)
    suffix = next((s for s in STATUS_SUFFIX if name.endswith(s)), None)
    if suffix is None:
        return None
    label = name.removesuffix(suffix)
    providers = "|".join(re.escape(p) for p in GIT_PROVIDER)
    label = re.sub(rf"^.*? \((?:{providers})\) - ", "", label)
    parts = label.split()  # a leading codicon (e.g. sync~spin) is its own word
    if not parts:
        return None
    return parts[-1].rstrip(DIRTY_MARKS) or None


# ---------------------------------------------- status bar and notices


def status_items(window) -> list[str]:
    names = [read(c, "Name") for c in ui.search(window, _BUTTON, max_depth=STATUS_DEPTH)]
    return [n for n in names if any(collapse_whitespace(n).endswith(s) for s in STATUS_SUFFIX)]


def _texts(control, depth: int = 8) -> list[str]:
    out: list[str] = []

    def walk(c, d):
        name = collapse_whitespace(read(c, "Name"))
        if name and name not in out:
            out.append(name)
        if d >= depth:
            return
        try:
            children = c.GetChildren()
        except Exception:  # noqa: BLE001 - element vanished mid-walk
            return
        for child in children:
            walk(child, d + 1)

    walk(control, 0)
    return out


def notices(ctx: AppContext, window) -> list[str]:
    """Text of every toast, in-window dialog and native modal on screen. A
    native modal is what core `dialogs` finds: a #32770 on Windows, a window
    of type dialog on Linux."""
    found: list[str] = []
    for cls in NOTICE_CLASSES:
        step = selectors.parse(f"app//*[class~='{cls}']").steps[0]
        for control in ui.search(window, step, max_depth=NOTICE_DEPTH):
            found += _texts(control)
    profile = apps.profile_of(ctx)
    modals = dialogs.dialog_windows(apps.all_pids(profile), classes=dialogs.dialog_classes(profile))
    for modal in modals:
        control = auto.ControlFromHandle(modal["hwnd"])
        found += _texts(control) if control is not None else [modal["title"], modal["text"]]
    return [text for text in found if text]


# ------------------------------------------------------------- checkout


def checkout(
    ctx: AppContext,
    branch: str,
    *,
    remote: bool = False,
    window: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Check out `branch` with "Git: Checkout to...", verified by the status
    bar. A branch that exists only on a remote needs `remote`."""
    target = pick_window(ctx, window)
    title = collapse_whitespace(read(target, "Name"))
    before = set(notices(ctx, target))

    palette.run(target, CHECKOUT_COMMAND, timeout=timeout)
    palette.wait_row(
        lambda r: not any(palette._is_row(r, t) for t in CHECKOUT_COMMAND),
        "the branch picker",
        timeout=timeout,
    )
    palette.type_filter(branch)
    seen: list[str] = []
    try:
        row = palette.pick(
            lambda r: branch_row_matches(r, branch, remote),
            f"branch {branch!r}",
            timeout=timeout,
            seen=seen,
        )
    except palette.NoRow as exc:
        if not remote and any(is_remote_row(r, branch) for r in seen):
            raise PilotError(
                f"{branch!r} exists only as a remote branch; pass --remote to check it out"
            ) from exc
        raise PilotError(
            f"no branch {branch!r} in the branch picker (last row {seen[-1] if seen else None!r}); "
            "closed it, checked out nothing"
        ) from exc

    def outcome():
        shown = [status_branch(n) for n in status_items(target)]
        if branch in shown:
            return {"status_bar": branch}
        new = [t for t in notices(ctx, target) if t not in before]
        if new:
            return {"notice": new}
        return None

    try:
        result = wait_until(
            outcome, timeout=timeout, interval=0.5, what=f"the status bar to show {branch!r}"
        )
    except PilotError as exc:
        raise PilotError(
            f"picked {row!r} but the status bar shows {[n for n in status_items(target)]} ({exc})"
        ) from exc
    if "notice" in result:
        raise PilotError(
            f"VS Code did not check out {branch!r}; it says: {' | '.join(result['notice'])} "
            "(left on screen)"
        )
    item = next(n for n in status_items(target) if status_branch(n) == branch)
    return {"checked_out": branch, "row": row, "window": title, "verified": item}


# ------------------------------------------------------------- compare

# GitLens (eamodio.gitlens) display strings, tuples of accepted texts.
COMPARE_COMMAND = ("GitLens: Compare HEAD with...",)
REF_PROMPT = ("Choose a reference (branch, tag, etc) to compare with",)  # the box's name starts so
VIEW_NAME = ("Search & Compare",)  # the Tree's name
NODE_PREFIX = ("Comparing",)  # "Comparing HEAD with main MyRepo, has actions"
ROW_ACTIONS = (", has actions",)
VIEW_DEPTH = 30
ROW_DEPTH = 3  # Tree > Group (monaco-list-rows) > TreeItem
_TREE_ROW = selectors.parse("app//TreeItem").steps[0]
_BEHIND = re.compile(r"^Behind (\d+) commits?\b")
_AHEAD = re.compile(r"^Ahead (\d+) commits?\b")
_FILES = re.compile(r"^(\d+) files? changed\W+(\d+) additions? \(\+\)\W+(\d+) deletions? \(-\)")


def ref_row_matches(row: str | None, ref: str) -> bool:
    """Is `row` the GitLens reference picker's row for exactly `ref`? A row is
    the ref, a comma, then its tracking state: "main, 11↓ 0↑ ⇄ origin/main
    ...". "origin/main" and "x-to-main" also match a typed "main"; they are
    not it."""
    return bool(row) and row.split(",", 1)[0].strip() == ref and "," in row


def _strip_actions(name: str) -> str:
    name = collapse_whitespace(name)
    for suffix in ROW_ACTIONS:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def compare_node_matches(name: str | None, head: str, base: str) -> bool:
    """Is `name` the Search & Compare node "Comparing <head> with <base>
    [<repo>], has actions"?"""
    if not name:
        return False
    label = _strip_actions(name)
    for prefix in NODE_PREFIX:
        stem = f"{prefix} {head} with {base}"
        if label == stem or label.startswith(stem + " "):
            return True
    return False


def is_compare_node(name: str | None) -> bool:
    return bool(name) and any(_strip_actions(name).startswith(p + " ") for p in NODE_PREFIX)


def compare_counts(children: list[str]) -> dict:
    """ahead, behind, files_changed, additions, deletions from a comparison
    node's child rows; a count no row gives is None."""
    out = dict.fromkeys(("behind", "ahead", "files_changed", "additions", "deletions"))
    for raw in children:
        row = _strip_actions(raw)
        if m := _BEHIND.match(row):
            out["behind"] = int(m[1])
        elif m := _AHEAD.match(row):
            out["ahead"] = int(m[1])
        elif m := _FILES.match(row):
            out["files_changed"], out["additions"], out["deletions"] = map(int, m.groups())
    return out


def _view(window):
    """The Search & Compare tree, or None while the view is not rendered."""
    for name in VIEW_NAME:
        step = selectors.parse(f"app//Tree[name={name!r}]").steps[0]
        hits = ui.search(window, step, max_depth=VIEW_DEPTH, limit=1)
        if hits:
            return hits[0]
    return None


def _nodes(view, base: str) -> list:
    """The view's "Comparing HEAD with <base>" rows; offscreen ones too."""
    if view is None:
        return []
    rows = ui.search(view, _TREE_ROW, max_depth=ROW_DEPTH, visible_only=False)
    return [r for r in rows if compare_node_matches(read(r, "Name"), "HEAD", base)]


def _children(node) -> list[str]:
    """The rows under a comparison node. The tree is flat in UIA: its child
    rows are the node's next siblings, up to the next comparison node."""
    try:
        siblings = node.GetParentControl().GetChildren()
    except Exception:  # noqa: BLE001 - row re-rendered under us
        return []
    mine = _runtime(node)
    out: list[str] = []
    after = False
    for sibling in siblings:
        name = read(sibling, "Name")
        if after:
            if is_compare_node(name):
                break
            out.append(name)
        elif _runtime(sibling) == mine:
            after = True
    return out


def _runtime(control):
    try:
        return tuple(control.GetRuntimeId())
    except Exception:  # noqa: BLE001 - stale element
        return ("object", id(control))


def _expand(node) -> None:
    """Show a collapsed node's children, through UIA (no keys)."""
    try:
        pattern = node.GetExpandCollapsePattern()
        if pattern is not None and pattern.ExpandCollapseState != auto.ExpandCollapseState.Expanded:
            pattern.Expand()
    except Exception:  # noqa: BLE001 - no pattern, or the row went stale
        pass


def _wait_picker(timeout: float) -> None:
    """Until the palette's box asks for a reference: the command's picker has
    replaced the palette. Focus lands on its first row, so the row cannot tell."""
    try:
        wait_until(
            lambda: any((palette.prompt() or "").startswith(p) for p in REF_PROMPT),
            timeout=timeout,
            interval=0.2,
            what="the GitLens reference picker",
        )
    except PilotError as exc:
        shown = palette.prompt()
        palette._close()
        raise PilotError(
            f"the GitLens reference picker did not show (palette box {shown!r}); typed nothing "
            f"({exc})"
        ) from exc


def compare(
    ctx: AppContext,
    base: str = "main",
    *,
    window: str | None = None,
    timeout: float = 15.0,
) -> dict:
    """Compare HEAD with `base` in GitLens' Search & Compare view
    ("GitLens: Compare HEAD with..."), then read the node's counts."""
    if not base or "{" in base or "}" in base or " " in base:
        raise PilotError(f"--base {base!r} is not a ref name")
    target = pick_window(ctx, window)
    title = collapse_whitespace(read(target, "Name"))
    view = _view(target)
    before = len(_nodes(view, base)) if view is not None else None

    try:
        palette.run(target, COMPARE_COMMAND, timeout=timeout)
    except palette.NotOffered as exc:
        raise NotPresent(
            f"the palette has no {COMPARE_COMMAND[0]!r}: is GitLens (eamodio.gitlens) installed "
            f"and enabled? ({exc})"
        ) from exc
    _wait_picker(timeout)
    palette.type_filter(base)
    seen: list[str] = []
    try:
        row = palette.pick(
            lambda r: ref_row_matches(r, base), f"ref {base!r}", timeout=timeout, seen=seen
        )
    except palette.NoRow as exc:
        raise NotPresent(
            f"no ref {base!r} in the GitLens picker (rows seen {seen}); closed it, compared nothing"
        ) from exc

    view = wait_until(
        lambda: _view(target), timeout=timeout, interval=0.3, what="the Search & Compare view"
    )

    def appeared():
        # Row elements are re-made on every render, so a new node is told by
        # the count; GitLens selects the comparison it adds or reveals.
        nodes = _nodes(view, base)
        chosen = [n for n in nodes if "selected" in read(n, "ClassName")]
        if chosen or len(nodes) > (before or 0):
            return (chosen or nodes)[0]
        return None

    try:
        node = wait_until(
            appeared, timeout=timeout, interval=0.3, what=f"a 'Comparing HEAD with {base}' node"
        )
    except PilotError:
        nodes = _nodes(view, base)
        if not nodes:
            raise PilotError(
                f"picked {row!r} but no 'Comparing HEAD with {base}' node showed in "
                f"Search & Compare within {timeout:g} s"
            ) from None
        node = nodes[0]  # GitLens kept the comparison it had, unselected

    _expand(node)

    def all_counts():
        counts = compare_counts(_children(node))
        return counts if None not in counts.values() else None

    try:
        counts = wait_until(all_counts, timeout=timeout, interval=0.3, what="the counts")
    except PilotError:
        counts = compare_counts(_children(node))  # what shows; None for the rest
    return {
        "compared": {"head": "HEAD", "base": base},
        "node": _strip_actions(read(node, "Name")),
        "existed_before": None if before is None else before > 0,
        **counts,
        "row": row,
        "window": title,
    }
