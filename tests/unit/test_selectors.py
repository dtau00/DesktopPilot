"""Selector parsing - the one part of the driver that needs no app."""

from __future__ import annotations

import pytest

from desktoppilot.core import selectors
from desktoppilot.core.errors import PilotError


def test_default_root_is_app():
    sel = selectors.parse("//Button[name='OK']")
    assert (sel.root, sel.root_arg) == ("app", None)
    # A bare step is read as a root name, not as an implicit app-relative step.
    with pytest.raises(PilotError):
        selectors.parse("Button[name='OK']")


def test_root_only():
    sel = selectors.parse("app")
    assert (sel.root, sel.root_arg, sel.steps) == ("app", None, [])


def test_root_argument():
    assert selectors.parse("win:Settings//Button").root_arg == "Settings"
    assert selectors.parse("desktop//Pane").root == "desktop"


def test_an_unknown_root_token_parses():
    """Which roots exist depends on the app, so the parser accepts any name
    and ui.resolve_root decides."""
    sel = selectors.parse("ribbon//Button")
    assert (sel.root, sel.root_arg) == ("ribbon", None)

    sel = selectors.parse("pane:MyAddin//Button[name='x']")
    assert (sel.root, sel.root_arg) == ("pane", "MyAddin")
    assert sel.steps[0].predicates[0].value == "x"


def test_steps_predicates_and_index():
    sel = selectors.parse("app//Edit[name~='Do anything'][id='x']/Button#3")
    first, second = sel.steps
    assert (first.control_type, first.descendant) == ("Edit", True)
    assert [(p.key, p.op, p.value) for p in first.predicates] == [
        ("name", "~=", "Do anything"),
        ("automation_id", "=", "x"),
    ]
    assert (second.control_type, second.descendant, second.index) == ("Button", False, 3)


def test_child_step_index_and_contains():
    sel = selectors.parse("app/Pane[class='NetUIHWND']//Text[name~='No results']#3")
    child, descendant = sel.steps
    assert (child.descendant, child.control_type) == (False, "Pane")
    assert child.predicates[0].key == "class_name"
    assert (descendant.descendant, descendant.index) == (True, 3)
    assert descendant.predicates[0].op == "~="


def test_predicate_matching_is_case_insensitive():
    pred = selectors.Predicate("name", "=", "clear")
    assert pred.matches("Clear")
    assert not pred.matches("Clear all")

    contains = selectors.Predicate("name", "~=", "sults")
    assert contains.matches("No results found")
    assert not selectors.Predicate("name", "=", "Send").matches("send message")


def test_type_normalization():
    assert selectors.normalize_type("ButtonControl") == "Button"
    assert selectors.normalize_type("button") == "Button"
    assert selectors.normalize_type("*") == "*"


def test_predicate_containing_a_slash_does_not_split_the_step():
    sel = selectors.parse("pane//Text[name~='Show details/all']")
    assert len(sel.steps) == 1
    assert sel.steps[0].predicates[0].value == "Show details/all"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "app//Button[bogus='x']",
        "app//Button[name]",
        "app//",
        "not a root//Button",
    ],
)
def test_bad_selectors_raise(bad):
    with pytest.raises(PilotError):
        selectors.parse(bad)


def test_a_quoted_root_argument_may_hold_a_slash():
    for text in ('win:"Find/Replace"//Button', "win:'Find/Replace'//Button"):
        sel = selectors.parse(text)
        assert (sel.root, sel.root_arg) == ("win", "Find/Replace")
        assert [s.control_type for s in sel.steps] == ["Button"]


def test_an_unquoted_root_argument_split_at_its_slash_fails_loudly():
    with pytest.raises(PilotError, match="quote the argument"):
        selectors.parse("win:Find/Replace//Button")


@pytest.mark.parametrize(
    "bad",
    [
        "win:Foo[bar//Button",
        "app//Button[name='x'",
        "app//Button[name='x']]",
        "app//Button[name='x",
        "pane:Terms]//List",
    ],
)
def test_an_unbalanced_bracket_or_quote_is_a_parse_error(bad):
    with pytest.raises(PilotError):
        selectors.parse(bad)


@pytest.mark.parametrize("bad", ["app//Button#0", "app//Button#-1", "app/Pane#0//Button"])
def test_an_index_below_one_is_a_parse_error(bad):
    with pytest.raises(PilotError):
        selectors.parse(bad)


@pytest.mark.parametrize(
    "text",
    [
        """app//Button[name="Don't Save"]""",
        r"""app//Button[name='Don\'t Save']""",
    ],
)
def test_predicate_values_take_either_quote_and_escapes(text):
    assert selectors.parse(text).steps[0].predicates[0].value == "Don't Save"


def test_escapes_inside_a_value():
    sel = selectors.parse(r"""app//Text[name="say \"hi\""][value='C:\dir\x']""")
    assert [p.value for p in sel.steps[0].predicates] == ['say "hi"', r"C:\dir\x"]


def test_a_quoted_value_may_hold_a_bracket():
    assert selectors.parse("app//Text[name='[draft]']").steps[0].predicates[0].value == "[draft]"


def test_an_explicit_index_is_marked():
    first, second = selectors.parse("app/Pane#1//Button").steps
    assert (first.index, first.indexed) == (1, True)
    assert (second.index, second.indexed) == (1, False)


@pytest.mark.windows("uiautomation's own control type list")
def test_control_types_are_uias_own_and_case_is_forgiven():
    import uiautomation as auto

    uia = {selectors.normalize_type(name) for name in auto.ControlTypeNames.values()}
    assert set(selectors.CONTROL_TYPES) == uia
    assert selectors.parse("app//checkbox").steps[0].control_type == "CheckBox"
    with pytest.raises(PilotError, match="unknown control type"):
        selectors.parse("app//Buton")


def test_readme_examples_still_parse():
    for text in (
        "pane:Task List//List[class='ListBox']",
        "ribbon//TabItem[name='MyAddin']",
        "win:Inspector//Text[name~='Errors']",
        "app//Text[name~='No results']",
    ):
        selectors.parse(text)
