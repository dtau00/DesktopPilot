"""Word's own page layout around the view, over COM: Pane.Pages -> Rectangles
-> Lines, each line next to Window.GetPoint and Range.Information, so the three
geometry sources an add-in's overlay window can be checked against are read at
one instant.

Pane.Pages covers only the pages Word has laid out, and every line costs
several cross-process calls, so keep the radius small.
"""

from __future__ import annotations

import time

from .com import com_retry
from .view import (
    WD_COLLAPSE_START,
    WD_HORIZONTAL_POSITION_RELATIVE_TO_PAGE,
    WD_VERTICAL_POSITION_RELATIVE_TO_PAGE,
    Errors,
    centre,
    get_point,
    page_at,
    view_state,
)

TEXT_RECTANGLE = 0
RECTANGLE_TYPES = (
    "text",
    "shape",
    "markup",
    "markup-button",
    "page-border",
    "line-between-columns",
    "selection",
    "system",
    "markup-area",
    "reading-mode-navigation",
    "markup-move-match",
    "reading-mode-panning-area",
    "mail-nav-area",
    "document-control",
)
STORIES = {
    1: "main",
    2: "footnotes",
    3: "endnotes",
    4: "comments",
    5: "text-frame",
    6: "even-pages-header",
    7: "primary-header",
    8: "even-pages-footer",
    9: "primary-footer",
    10: "first-page-header",
    11: "first-page-footer",
    12: "footnote-separator",
    13: "footnote-continuation-separator",
    14: "footnote-continuation-notice",
    15: "endnote-separator",
    16: "endnote-continuation-separator",
    17: "endnote-continuation-notice",
}
# GetPoint and Information on a header or footer line open it for editing.
LINE_STORIES = {1, 2, 3}
LINE_TYPES = {0: "text", 1: "table-row"}
LINE_PREVIEW = 40


def page_window(centre_page: int, radius: int, count: int) -> list[int]:
    """The page numbers centre_page-radius..centre_page+radius that exist."""
    return list(range(max(1, centre_page - radius), min(count, centre_page + radius) + 1))


def rectangle_type(code: int | None) -> str | int | None:
    return RECTANGLE_TYPES[code] if code is not None and 0 <= code < len(RECTANGLE_TYPES) else code


def capture(app, *, page: int | None = None, radius: int = 1) -> dict:
    """The pages around `page`, else around the page at the centre of the
    document window, else around the selection's.
    JSON: view, centre_point, centre_page, centre_from, page_count, pages, errors, seconds."""
    started = time.monotonic()
    errors = Errors()
    window = com_retry(lambda: app.ActiveWindow)
    pane = com_retry(lambda: window.ActivePane)
    view = view_state(window, errors)

    point, centre_page, centre_from = None, page, "page"
    if centre_page is None and view["document_rect"]:
        point = centre(view["document_rect"])
        centre_page = errors.get("centre page", lambda: page_at(window.RangeFromPoint(*point)))
        centre_from = "centre"
    if centre_page is None:
        centre_page = errors.get("selection page", lambda: page_at(window.Selection.Range))
        centre_from = "selection"

    count = errors.get("page count", lambda: int(pane.Pages.Count), 0)
    numbers = page_window(centre_page, radius, count) if centre_page else []
    return {
        "view": view,
        "centre_point": list(point) if point else None,
        "centre_page": centre_page,
        "centre_from": centre_from,
        "page_count": count,
        "pages": [_page(window, pane, n, errors) for n in numbers],
        "errors": errors.items,
        "seconds": round(time.monotonic() - started, 2),
    }


def _box(item) -> list[float]:
    return [float(item.Left), float(item.Top), float(item.Width), float(item.Height)]


def _span(rng) -> list[int]:
    return [int(rng.Start), int(rng.End)]


def _items(collection, what: str, errors: Errors) -> list:
    count = errors.get(f"{what} count", lambda: int(collection.Count), 0)
    found = [errors.get(f"{what} {i}", lambda i=i: collection.Item(i)) for i in range(1, count + 1)]
    return [(i, item) for i, item in enumerate(found, 1) if item is not None]


def _page(window, pane, number: int, errors: Errors) -> dict:
    where = f"page {number}"
    page = errors.get(where, lambda: pane.Pages.Item(number))
    if page is None:
        return {"page": number, "box": None, "rectangles": []}
    rectangles = errors.get(f"{where} rectangles", lambda: page.Rectangles)
    return {
        "page": number,
        "box": errors.get(f"{where} box", lambda: _box(page)),
        "rectangles": [
            _rectangle(window, index, rect, f"{where} rect {index}", errors)
            for index, rect in (
                _items(rectangles, f"{where} rect", errors) if rectangles is not None else []
            )
        ],
    }


def _rectangle(window, index: int, rect, where: str, errors: Errors) -> dict:
    code = errors.get(f"{where} type", lambda: int(rect.RectangleType))
    if code is None:
        # Word answers "not available on that object" for every member of some rectangles.
        return {"index": index, "type": "unavailable"}
    rng = errors.get(f"{where} range", lambda: rect.Range)
    story = errors.get(f"{where} story", lambda: int(rng.StoryType)) if rng is not None else None
    lines = []
    if code == TEXT_RECTANGLE and story in LINE_STORIES:
        collection = errors.get(f"{where} lines", lambda: rect.Lines)
        if collection is not None:
            lines = [
                _line(window, line, f"{where} line {i}", errors)
                for i, line in _items(collection, f"{where} line", errors)
            ]
    return {
        "index": index,
        "type": rectangle_type(code),
        "box": errors.get(f"{where} box", lambda: _box(rect)),
        "range": errors.get(f"{where} range", lambda: _span(rng)) if rng is not None else None,
        "story": STORIES.get(story, story),
        "lines": lines,
    }


def _collapsed(rng):
    point = rng.Duplicate
    point.Collapse(WD_COLLAPSE_START)
    return point


def _position(point) -> list[float]:
    return [
        float(point.Information(WD_HORIZONTAL_POSITION_RELATIVE_TO_PAGE)),
        float(point.Information(WD_VERTICAL_POSITION_RELATIVE_TO_PAGE)),
    ]


def _line(window, line, where: str, errors: Errors) -> dict:
    line_type = errors.get(f"{where} type", lambda: int(line.LineType))
    entry = {
        "box": errors.get(f"{where} box", lambda: _box(line)),
        "type": LINE_TYPES.get(line_type, line_type),
        "range": None,
        "text": None,
        "get_point": None,
        "page": None,
        "page_position_pt": None,
    }
    rng = errors.get(f"{where} range", lambda: line.Range)
    if rng is None:
        return entry
    entry |= {
        "range": errors.get(f"{where} span", lambda: _span(rng)),
        "text": errors.get(f"{where} text", lambda: str(rng.Text)[:LINE_PREVIEW]),
        "get_point": errors.get(f"{where} GetPoint", lambda: get_point(window, rng)),
    }
    start = errors.get(f"{where} start", lambda: _collapsed(rng))
    if start is not None:
        entry["page"] = errors.get(f"{where} page", lambda: page_at(start))
        entry["page_position_pt"] = errors.get(f"{where} Information", lambda: _position(start))
    return entry
