"""Documents for the smoke suites. Never checked in: point the suite at one on
disk, or drop a copy at the default path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

GATE_DOC_ENV = "PILOT_TEST_DOC"
DEFAULT_GATE_DOC = Path(r"C:\pilot-gate\sample.docx")


@pytest.fixture(scope="session")
def gate_doc() -> Path:
    path = Path(os.environ.get(GATE_DOC_ENV, DEFAULT_GATE_DOC))
    if not path.exists():
        pytest.skip(f"no test document: set {GATE_DOC_ENV} or put one at {DEFAULT_GATE_DOC}")
    return path
