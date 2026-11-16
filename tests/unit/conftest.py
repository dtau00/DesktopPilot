"""Unit tests see no plugin from another installed package: discovery reads no
entry point unless a test fakes one. The smoke and ui suites keep the real
ones.

`desktoppilot.cli` builds its tree at import, before any fixture runs, so the
tree the unit tests use is built here, with no entry points, when pytest
loads this file (before it imports a unit test module). It stands in for
`cli.app` while the unit modules are collected and in every unit test; the
real tree is back for anything else."""

from __future__ import annotations

import pytest

from desktoppilot import cli
from desktoppilot.core import profiles


def _no_entry_points() -> list:
    return []


def _hermetic_tree():
    real = profiles._entry_points
    profiles._entry_points = _no_entry_points
    profiles._external_plugins.cache_clear()
    try:
        return cli.build([])
    finally:
        profiles._entry_points = real
        profiles._external_plugins.cache_clear()


HERMETIC_APP = _hermetic_tree()
_REAL_APP = cli.app
# A unit module that does `from desktoppilot.cli import app` at import binds this.
cli.app = HERMETIC_APP


def pytest_collection_finish(session):
    cli.app = _REAL_APP


@pytest.fixture(autouse=True)
def no_external_plugins(monkeypatch):
    monkeypatch.setattr(profiles, "_entry_points", _no_entry_points)
    monkeypatch.setattr(cli, "app", HERMETIC_APP)
    profiles._external_plugins.cache_clear()
    yield
    profiles._external_plugins.cache_clear()
