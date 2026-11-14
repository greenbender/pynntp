"""The fault-injection tests are opt-in: see tests/faults/README.md."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SERVER = os.environ.get("PYNNTP_FAULT_SERVER", "")

# Not collected unless PYNNTP_FAULT_SERVER names the nntp-fakeserver binary
collect_ignore: list[str] = [] if SERVER else ["test_faults.py"]


@pytest.fixture(scope="session")
def fault_server() -> Path:
    """The nntp-fakeserver binary of nntp-testkit."""
    return Path(SERVER).resolve()
