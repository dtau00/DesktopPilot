"""What Word is displaying, read from outside through UI Automation and mapped
onto Word positions over COM, with no add-in needed.

The document element (class _WwG) has a TextPattern whose visible ranges are
the main text on screen, and one Custom child per page whose bounding rect is
empty unless the page is displayed. A page's children are its parts (header,
content, footnote area, footer). UIA offsets in the main story are Word
positions, so a range maps by identity; element names are localized, so pages
are told apart by position and footnotes by the number ending their name.
"""

from __future__ import annotations

import re
import time

import comtypes

from desktoppilot.core import uiacache
from desktoppilot.core.errors import NotPresent, PilotError, is_uia_timeout
from desktoppilot.core.util import wait_until

from .com import com_retry
from .view import (
    DOCUMENT_CLASS,
    SNIPPET,
    Errors,
    document_hwnds,
    head,
    is_empty_rect,
    largest,
    page_at,
    same_text,
    tail,
    view_state,
)

CLASS_NAME_PROPERTY = 30012
CUSTOM = 50025
TEXT_PATTERN = 10014
SCOPE_DESCENDANTS = 4
START, END = 0, 1
# A page's parts and a part's children are few; the caps guard against a surprising tree.
MAX_PARTS = 12
MAX_CHILDREN = 40
FIND_TIMEOUT = 10.0
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def y_span(rects) -> tuple[int | None, int | None, int]:
    """Top and bottom screen y over a range's line rects (left, top, width,
    height, ...), and how many there are. A table's come column by column, so
    the first and last are not the top and bottom."""
    values = list(rects or ())
    lines = [values[i : i + 4] for i in range(0, len(values) - 3, 4)]
    if not lines:
        return None, None, 0
    top = min(line[1] for line in lines)
    bottom = max(line[1] + line[3] for line in lines)
    return int(top), int(bottom), len(lines)


def number_in(name: str | None) -> int | None:
    match = _TRAILING_NUMBER.search(name or "")
    return int(match.group(1)) if match else None


def main_span(ranges: list[dict]) -> tuple[int, int] | None:
    """From the first non-empty range's start to the last one's end."""
    spans = [(r["start"], r["end"]) for r in ranges if r["end"] > r["start"]]
    return (spans[0][0], spans[-1][1]) if spans else None


def last_char(start: int, end: int) -> int:
    return max(start, end - 1)


# ---------------------------------------------------------------- UIA side


def find_document(frame: int, *, timeout: float = FIND_TIMEOUT) -> tuple:
    """The _WwG element under the frame, and how it was found.

    Searched by class, never by control type: the add-in's WPF panes hold a
    FlowDocumentScrollViewer that is a Document too. With Word's "Resume
    Reading" pane showing, that search finds nothing, so the window chain
    _WwF -> _WwB -> _WwG is walked instead. The first search on a new window
    builds Word's UIA tree and can take seconds."""
    uia = uiacache.automation()
    condition = uia.CreatePropertyCondition(CLASS_NAME_PROPERTY, DOCUMENT_CLASS)

    def search() -> tuple | None:
        try:
            return (uia.ElementFromHandle(frame).FindFirst(SCOPE_DESCENDANTS, condition),)
        except comtypes.COMError as exc:
            if is_uia_timeout(exc):
                return None
            raise

    [found] = wait_until(
        search, timeout=timeout, what=f"the {DOCUMENT_CLASS} search", fatal=(comtypes.COMError,)
    )
    if found:
        return found, "uia-search"
    hwnd = largest(document_hwnds(frame))
    if hwnd is None:
        raise NotPresent(f"no {DOCUMENT_CLASS} document window under the Word frame 0x{frame:X}")
    return uia.ElementFromHandle(hwnd), "window-chain"


def _text_pattern(element):
    pattern = element.GetCurrentPattern(TEXT_PATTERN)
    if not pattern:
        return None
    return pattern.QueryInterface(uiacache.client().UIAutomationCore.IUIAutomationTextPattern)


def _rect(element) -> list[int]:
    r = element.CurrentBoundingRectangle
    return [r.left, r.top, r.right, r.bottom]


def _offsets(rng, document_range) -> tuple[int, int] | None:
    """The range's ends as offsets into the main story; None for a range in
    another story (a header, a footnote), which UIA refuses to compare."""
    try:
        return (
            rng.CompareEndpoints(START, document_range, START),
            rng.CompareEndpoints(END, document_range, START),
        )
    except comtypes.COMError:
        return None


def _main_range(rng, document_range) -> dict:
    start, end = _offsets(rng, document_range) or (None, None)
    text = rng.GetText(-1) or ""
    top, bottom, lines = y_span(rng.GetBoundingRectangles())
    return {
        "start": start,
        "end": end,
        "uia_length": len(text),
        "uia_head": head(text),
        "uia_tail": tail(text),
        "top_y": top,
        "bottom_y": bottom,
        "line_rects": lines,
    }


def _children(walker, element, cap: int) -> list:
    found = []
    child = walker.GetFirstChildElement(element)
    while child and len(found) < cap:
        found.append(child)
        child = walker.GetNextSiblingElement(child)
    return found


def _part(walker, part, document_range) -> dict:
    entry = {
        "name": part.CurrentName,
        "type": part.CurrentLocalizedControlType,
        "rect": _rect(part),
        "visible": [],
        "footnotes": [],
    }
    if is_empty_rect(entry["rect"]):
        return entry
    pattern = _text_pattern(part)
    if pattern is not None:
        visible = pattern.GetVisibleRanges()
        for i in range(visible.Length):
            rng = visible.GetElement(i)
            top, bottom, _ = y_span(rng.GetBoundingRectangles())
            offsets = _offsets(rng, document_range)
            entry["visible"].append(
                {
                    "text": head(rng.GetText(SNIPPET)),
                    "top_y": top,
                    "bottom_y": bottom,
                    "main_offsets": list(offsets) if offsets else None,
                }
            )
    for child in _children(walker, part, MAX_CHILDREN):
        if child.CurrentControlType != CUSTOM:
            continue
        child_pattern = _text_pattern(child)
        child_range = child_pattern.DocumentRange if child_pattern is not None else None
        # A footnote reference mark sits in the main story; a footnote does not.
        if child_range is None or _offsets(child_range, document_range) is not None:
            continue
        name = child.CurrentName
        entry["footnotes"].append(
            {
                "name": name,
                "number": number_in(name),
                "rect": _rect(child),
                "uia_text": head(child_range.GetText(SNIPPET)),
            }
        )
    return entry


def _pages(walker, document, pattern, document_range, errors: Errors) -> tuple[int, list[dict]]:
    """How many page elements there are, and the displayed ones with their
    parts. `ordinal` is the position among the page elements, from 1."""
    count, displayed = 0, []
    for page in _children(walker, document, cap=1 << 20):
        if page.CurrentControlType != CUSTOM:
            continue
        count += 1
        rect = _rect(page)
        if is_empty_rect(rect):
            continue
        entry = {"ordinal": count, "name": page.CurrentName, "rect": rect, "span": None}
        try:
            span = pattern.RangeFromChild(page)
            entry["span"] = list(_offsets(span, document_range) or ()) or None
        except comtypes.COMError as exc:
            errors.add(f"page {count} span", exc)
        parts = []
        for index, part in enumerate(_children(walker, page, MAX_PARTS), 1):
            try:
                parts.append(_part(walker, part, document_range))
            except comtypes.COMError as exc:
                errors.add(f"page {count} part {index}", exc)
        entry["parts"] = parts
        displayed.append(entry)
    return count, displayed


def query(document, errors: Errors) -> dict:
    """Everything UIA says about the view. No Word COM here."""
    pattern = _text_pattern(document)
    if pattern is None:
        raise PilotError(f"the {DOCUMENT_CLASS} element has no TextPattern")
    document_range = pattern.DocumentRange
    visible = pattern.GetVisibleRanges()
    ranges = [_main_range(visible.GetElement(i), document_range) for i in range(visible.Length)]
    walker = uiacache.automation().ControlViewWalker
    count, pages = _pages(walker, document, pattern, document_range, errors)
    return {"ranges": ranges, "page_elements": count, "pages": pages}


# ---------------------------------------------------------------- Word side


def _word_text(doc, start: int, end: int) -> str:
    return str(doc.Range(start, end).Text or "")


def _page_of(doc, position: int) -> int:
    return page_at(doc.Range(position, position))


def map_to_word(doc, snapshot: dict, errors: Errors) -> None:
    """Fill in Word's text and page at the UIA offsets, in place."""
    story_end = errors.get("story end", lambda: int(doc.Content.End), 0)
    for index, rng in enumerate(snapshot["ranges"]):
        if rng["start"] is None:
            continue
        start, end = min(rng["start"], story_end), min(rng["end"], story_end)
        where = f"range {index}"
        start_text = errors.get(
            f"{where} text", lambda s=start, e=end: _word_text(doc, s, min(e, s + SNIPPET))
        )
        rng["word"] = {
            "start": start,
            "end": end,
            "start_text": start_text,
            "end_text": errors.get(
                f"{where} end text", lambda s=start, e=end: _word_text(doc, max(s, e - SNIPPET), e)
            ),
            "start_page": errors.get(f"{where} page", lambda s=start: _page_of(doc, s)),
            "end_page": errors.get(
                f"{where} end page", lambda s=start, e=end: _page_of(doc, last_char(s, e))
            ),
        }
        rng["text_match"] = same_text(rng["uia_head"], start_text)

    for page in snapshot["pages"]:
        if page["span"]:
            page["word_page"] = errors.get(
                f"page {page['ordinal']} word page", lambda s=page["span"][0]: _page_of(doc, s)
            )
        for part in page["parts"]:
            for note in part["footnotes"]:
                if note["number"] is not None:
                    note["word"] = errors.get(
                        f"footnote {note['number']}", lambda n=note["number"]: _footnote(doc, n)
                    )
                    if note["word"]:
                        note["text_match"] = same_text(note["uia_text"], note["word"]["text"])

    span = main_span([r for r in snapshot["ranges"] if r["start"] is not None])
    snapshot["main"] = None
    if span:
        start, end = span
        snapshot["main"] = {
            "start": start,
            "end": end,
            "start_page": errors.get("main start page", lambda: _page_of(doc, start)),
            "end_page": errors.get("main end page", lambda: _page_of(doc, last_char(start, end))),
        }
    snapshot["displayed_pages"] = [p.get("word_page") for p in snapshot["pages"]]


def _footnote(doc, number: int) -> dict:
    """Word's footnote with that index: where its text is, and its page."""
    note = doc.Footnotes.Item(number)
    text_range = note.Range
    return {
        "index": number,
        "start": int(text_range.Start),
        "end": int(text_range.End),
        "text": head(str(text_range.Text or "")),
        "page": page_at(text_range),
        "reference_page": page_at(note.Reference),
    }


def capture(app, *, timeout: float = FIND_TIMEOUT) -> dict:
    """UIA's view of the active Word window, mapped onto Word.
    JSON: view, found_via, find_s, ranges, page_elements, pages, main,
    displayed_pages, errors, seconds."""
    started = time.monotonic()
    errors = Errors()
    window = com_retry(lambda: app.ActiveWindow)
    view = view_state(window, errors)
    if not view["hwnd"]:
        raise PilotError("the active Word window has no hwnd: " + "; ".join(errors.items))
    document, via = find_document(view["hwnd"], timeout=timeout)
    found_s = round(time.monotonic() - started, 2)
    view["document_hwnd"] = document.CurrentNativeWindowHandle or view["document_hwnd"]
    snapshot = query(document, errors)
    map_to_word(com_retry(lambda: window.Document), snapshot, errors)
    return {
        "view": view,
        "found_via": via,
        "find_s": found_s,
        **snapshot,
        "errors": errors.items,
        "seconds": round(time.monotonic() - started, 2),
    }
