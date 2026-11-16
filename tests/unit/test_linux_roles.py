"""The AT-SPI role table: every role maps to a UIA type the selectors know,
and the roles whose type depends on their parent."""

from __future__ import annotations

import pytest

from desktoppilot.core import selectors
from desktoppilot.linux import roles


def test_every_type_is_one_the_selectors_know():
    known = set(selectors.CONTROL_TYPES)
    assert {selectors.normalize_type(name) for name in roles.ROLES.values()} <= known


def test_every_atspi_role_is_in_the_table():
    pytest.importorskip("gi", reason="needs gi's Atspi")
    from desktoppilot.linux import auto

    atspi = auto.atspi()
    names = set()
    for value in range(256):
        try:
            names.add(atspi.role_get_name(atspi.Role(value)))
        except ValueError:
            break
    assert names - set(roles.ROLES) == set()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("button", "ButtonControl"),
        ("push button", "ButtonControl"),
        ("toggle button", "ButtonControl"),
        ("entry", "EditControl"),
        ("spin button", "SpinnerControl"),
        ("page tab", "TabItemControl"),
        ("document web", "DocumentControl"),
        ("paragraph", "GroupControl"),
        ("heading", "TextControl"),
        ("application", "PaneControl"),
        ("table cell", "DataItemControl"),
        ("no such role", "CustomControl"),
    ],
)
def test_plain_roles(role, expected):
    assert roles.control_type(role) == expected


def test_a_submenu_in_a_menu_bar_or_menu_is_a_menu_item():
    assert roles.control_type("menu", "menu bar") == "MenuItemControl"
    assert roles.control_type("menu", "menu") == "MenuItemControl"
    assert roles.control_type("menu", "combo box") == "MenuControl"


def test_a_chooser_is_a_window_only_at_the_top():
    assert roles.control_type("file chooser", "application") == "WindowControl"
    assert roles.control_type("file chooser", "panel") == "PaneControl"


def test_a_tree_tables_first_column_cells_are_its_tree_items():
    assert roles.control_type("table cell", "tree table") == "TreeItemControl"
    assert roles.control_type("table cell", "tree table", first_column=False) == "DataItemControl"
    assert roles.control_type("table cell", "table") == "DataItemControl"
