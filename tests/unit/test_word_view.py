"""`word layout` and `word visible`: the pure helpers, and the JSON they shape
from fake COM objects. No Word is started here."""

from __future__ import annotations

import json

import pytest
import pythoncom
from typer.testing import CliRunner

from desktoppilot.cli import app as cli
from desktoppilot.plugins.word import layout, view, visible

# ----------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("centre", "radius", "count", "pages"),
    [(5, 1, 10, [4, 5, 6]), (1, 1, 10, [1, 2]), (10, 2, 10, [8, 9, 10]), (3, 0, 10, [3])],
)
def test_page_window_clamps_to_the_pages_that_exist(centre, radius, count, pages):
    assert layout.page_window(centre, radius, count) == pages


def test_page_window_of_an_unlaid_document_is_empty():
    assert layout.page_window(1, 1, 0) == []


def test_rectangle_type_names_known_codes_and_passes_others_through():
    assert layout.rectangle_type(0) == "text"
    assert layout.rectangle_type(4) == "page-border"
    assert layout.rectangle_type(99) == 99
    assert layout.rectangle_type(None) is None


def test_centre_and_empty_rect():
    assert view.centre([100, 200, 300, 600]) == (200, 400)
    assert view.is_empty_rect([0, 0, 0, 0])
    assert view.is_empty_rect([10, 10, 20, 10])
    assert not view.is_empty_rect([10, 10, 20, 11])


def test_same_text_ignores_whitespace_list_numbers_and_footnote_marks():
    assert view.same_text("(ii) the review of each other ", "the review of each other Secti")
    assert view.same_text(" 40 Compare this with the Exam", "Compare this with the Example ")
    assert view.same_text("\x02 Readers may\r", "Readers  may")
    assert (
        view.same_text("entirely different words here", "the review of each other Secti") is False
    )
    assert view.same_text("", "x") is None
    assert view.same_text(None, "x") is None


def test_head_and_tail():
    assert view.head("abcdef", 3) == "abc"
    assert view.tail("abcdef", 3) == "def"
    assert view.head(None) is None


def test_errors_collects_instead_of_raising():
    errors = view.Errors()

    def boom():
        raise ValueError("nope")

    assert errors.get("thing", boom, default=7) == 7
    assert errors.get("fine", lambda: 3) == 3
    assert errors.items == ["thing: ValueError nope"]


def test_y_span_takes_the_extremes_not_the_first_and_last_rect():
    # A table's rects come column by column: the second is the topmost.
    rects = (10.0, 300.0, 50.0, 14.0, 80.0, 100.0, 50.0, 14.0, 10.0, 200.0, 50.0, 20.0)
    assert visible.y_span(rects) == (100, 314, 3)
    assert visible.y_span(()) == (None, None, 0)
    assert visible.y_span(None) == (None, None, 0)


def test_number_in_reads_the_trailing_number_of_a_localized_name():
    assert visible.number_in("Footnote 41") == 41
    assert visible.number_in("脚注 7 ") == 7
    assert visible.number_in("Footnote area") is None
    assert visible.number_in(None) is None


def test_main_span_skips_empty_ranges():
    ranges = [
        {"start": 10, "end": 10},
        {"start": 10, "end": 50},
        {"start": 50, "end": 90},
        {"start": 90, "end": 90},
    ]
    assert visible.main_span(ranges) == (10, 90)
    assert visible.main_span([{"start": 5, "end": 5}]) is None


def test_last_char():
    assert visible.last_char(10, 20) == 19
    assert visible.last_char(10, 10) == 10


# --------------------------------------------------------------- fake Word


class FakeRange:
    """A Range over a fake document: text and page by position."""

    def __init__(self, doc, start: int, end: int, story: int = 1):
        self.doc, self.Start, self.End, self.StoryType = doc, start, end, story

    @property
    def Text(self):
        return self.doc.text[self.Start : self.End]

    @property
    def Duplicate(self):
        return FakeRange(self.doc, self.Start, self.End, self.StoryType)

    def Collapse(self, direction):
        assert direction == view.WD_COLLAPSE_START
        self.End = self.Start

    def Information(self, kind):
        if kind == view.WD_ACTIVE_END_PAGE_NUMBER:
            return self.doc.page_of(self.End)
        return {
            view.WD_HORIZONTAL_POSITION_RELATIVE_TO_PAGE: 72.0,
            view.WD_VERTICAL_POSITION_RELATIVE_TO_PAGE: 100.0 + self.Start,
        }[kind]


class Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index - 1]


class Unavailable:
    """Word throws for every member of some rectangles."""

    @property
    def RectangleType(self):
        raise pythoncom.com_error(-2147352567, "That property is not available", None, None)


class Box:
    def __init__(self, left, top, width, height):
        self.Left, self.Top, self.Width, self.Height = left, top, width, height


class FakeLine(Box):
    def __init__(self, rng, top):
        super().__init__(72, top, 400, 14)
        self.Range, self.LineType = rng, 0


class FakeRectangle(Box):
    def __init__(self, kind, rng, lines=()):
        super().__init__(72, 72, 451, 600)
        self.RectangleType, self.Range, self.Lines = kind, rng, Collection(lines)


class FakeDoc:
    def __init__(self, text: str, page_size: int):
        self.text, self.page_size = text, page_size
        self.Content = FakeRange(self, 0, len(text))
        self.notes: dict[int, tuple[int, int, str, int]] = {}

    def page_of(self, position: int) -> int:
        return position // self.page_size + 1

    def Range(self, start, end):
        return FakeRange(self, start, end)

    @property
    def Footnotes(self):
        doc = self

        class Notes:
            def Item(self, n):
                start, end, text, reference = doc.notes[n]
                note_doc = FakeDoc(" " * start + text, doc.page_size)

                class Note:
                    Range = FakeRange(note_doc, start, end, story=2)
                    Reference = FakeRange(doc, reference, reference + 1)

                return Note()

        return Notes()


def fake_word(doc: FakeDoc):
    """A Word with two pages laid out: page 1 has a header rectangle, an
    unavailable one and a main-text rectangle of two lines; page 2 is empty."""
    main = [FakeLine(FakeRange(doc, 0, 20), 72), FakeLine(FakeRange(doc, 20, 40), 86)]
    page1 = Box(0, 0, 595, 842)
    page1.Rectangles = Collection(
        [
            FakeRectangle(0, FakeRange(doc, 0, 1, story=7)),
            Unavailable(),
            FakeRectangle(0, FakeRange(doc, 0, 40), main),
        ]
    )
    page2 = Box(0, 0, 595, 842)
    page2.Rectangles = Collection([])

    class Pane:
        Pages = Collection([page1, page2])

    class Selection:
        Range = FakeRange(doc, 0, 0)

    class Window:
        ActivePane = Pane()
        Document = doc

    Window.Selection = Selection()

    class App:
        ActiveWindow = Window()

    return App()


@pytest.fixture
def no_screen(monkeypatch):
    """No real window: a fixed view and a GetPoint that echoes the range."""
    monkeypatch.setattr(
        layout,
        "view_state",
        lambda window, errors: {"hwnd": 1, "document_rect": None, "zoom": 100},
    )
    monkeypatch.setattr(layout, "get_point", lambda window, rng: [500, 300 + rng.Start, 10, 14])


def test_layout_centres_on_the_selection_without_a_document_window(no_screen):
    doc = FakeDoc("x" * 100, page_size=50)
    result = layout.capture(fake_word(doc), radius=1)
    assert (result["centre_page"], result["centre_from"]) == (1, "selection")
    assert result["page_count"] == 2
    assert [p["page"] for p in result["pages"]] == [1, 2]


def test_layout_reads_lines_of_body_text_only_and_reports_bad_rectangles(no_screen):
    doc = FakeDoc("abcdefghij" * 10, page_size=50)
    result = layout.capture(fake_word(doc), page=1, radius=0)
    header, unavailable, body = result["pages"][0]["rectangles"]

    assert (header["story"], header["lines"]) == ("primary-header", [])
    assert unavailable == {"index": 2, "type": "unavailable"}
    assert any(e.startswith("page 1 rect 2 type:") for e in result["errors"])

    assert (body["type"], body["story"], body["range"]) == ("text", "main", [0, 40])
    first, second = body["lines"]
    assert first == {
        "box": [72.0, 72.0, 400.0, 14.0],
        "type": "text",
        "range": [0, 20],
        "text": "abcdefghijabcdefghij",
        "get_point": [500, 300, 10, 14],
        "page": 1,
        "page_position_pt": [72.0, 100.0],
    }
    assert second["get_point"] == [500, 320, 10, 14]
    assert second["page_position_pt"] == [72.0, 120.0]


def test_layout_an_explicit_page_wins_and_is_clamped(no_screen):
    result = layout.capture(fake_word(FakeDoc("x" * 100, 50)), page=2, radius=3)
    assert (result["centre_page"], result["centre_from"]) == (2, "page")
    assert [p["page"] for p in result["pages"]] == [1, 2]


# ------------------------------------------------------------ visible: map


def snapshot(ranges, pages=()):
    return {"ranges": ranges, "page_elements": 3, "pages": list(pages)}


def uia_range(start, end, text):
    return {"start": start, "end": end, "uia_head": text[:30], "uia_tail": text[-30:]}


def test_map_to_word_maps_uia_offsets_by_identity():
    text = "Intro paragraph.\r" + "(a) The second paragraph runs on.\r" + "z" * 200
    doc = FakeDoc(text, page_size=100)
    second = text.index("The second")
    shot = snapshot(
        [
            uia_range(0, 17, text[0:17]),
            uia_range(17, 160, "(a) " + text[second:160]),
            uia_range(160, 160, ""),
        ]
    )
    errors = view.Errors()
    visible.map_to_word(doc, shot, errors)

    first, numbered, empty = shot["ranges"]
    assert first["word"] == {
        "start": 0,
        "end": 17,
        "start_text": "Intro paragraph.\r",
        "end_text": "Intro paragraph.\r",
        "start_page": 1,
        "end_page": 1,
    }
    assert first["text_match"] is True
    assert numbered["text_match"] is True  # UIA's "(a) " is not in Range.Text
    assert (numbered["word"]["start_page"], numbered["word"]["end_page"]) == (1, 2)
    assert empty["word"]["start"] == empty["word"]["end"] == 160
    assert shot["main"] == {"start": 0, "end": 160, "start_page": 1, "end_page": 2}
    assert errors.items == []


def test_map_to_word_clamps_to_the_story_end_and_skips_uncomparable_ranges():
    doc = FakeDoc("a" * 50, page_size=100)
    shot = snapshot([uia_range(40, 80, "a" * 40), {"start": None, "end": None, "uia_head": "h"}])
    visible.map_to_word(doc, shot, view.Errors())
    assert (shot["ranges"][0]["word"]["start"], shot["ranges"][0]["word"]["end"]) == (40, 50)
    assert "word" not in shot["ranges"][1]


def test_map_to_word_gives_pages_their_word_page_and_footnotes_their_text():
    doc = FakeDoc("m" * 400, page_size=100)
    doc.notes[3] = (12, 40, "Compare with the sample texts.", 250)
    page = {
        "ordinal": 3,
        "span": [200, 300],
        "parts": [
            {
                "footnotes": [
                    {"name": "Footnote 3", "number": 3, "uia_text": " 3 Compare with the sample "},
                    {"name": "Footnote", "number": None, "uia_text": "x"},
                ]
            }
        ],
    }
    shot = snapshot([uia_range(200, 300, "m" * 100)], [page])
    visible.map_to_word(doc, shot, view.Errors())

    assert page["word_page"] == 3
    numbered, unnumbered = page["parts"][0]["footnotes"]
    assert numbered["word"]["start"] == 12
    assert numbered["word"]["reference_page"] == 3
    assert numbered["text_match"] is True
    assert "word" not in unnumbered
    assert shot["displayed_pages"] == [3]


def test_map_to_word_with_nothing_visible_has_no_main():
    shot = snapshot([])
    visible.map_to_word(FakeDoc("", 10), shot, view.Errors())
    assert shot["main"] is None
    assert shot["displayed_pages"] == []


# --------------------------------------------------------------------- CLI


def test_layout_and_visible_are_word_commands():
    result = CliRunner().invoke(cli, ["word", "--help"])
    assert result.exit_code == 0
    assert "layout" in result.output
    assert "visible" in result.output


def test_word_layout_prints_json(monkeypatch):
    monkeypatch.setattr("desktoppilot.plugins.word.commands._app", lambda ctx: "app")
    monkeypatch.setattr(layout, "capture", lambda app, page, radius: {"page": page, "r": radius})
    result = CliRunner().invoke(cli, ["word", "layout", "--page", "7", "--radius", "2"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"page": 7, "r": 2}


def test_word_visible_prints_json(monkeypatch):
    monkeypatch.setattr("desktoppilot.plugins.word.commands._app", lambda ctx: "app")
    monkeypatch.setattr(visible, "capture", lambda app, timeout: {"timeout": timeout})
    result = CliRunner().invoke(cli, ["word", "visible", "--timeout", "3"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"timeout": 3.0}
