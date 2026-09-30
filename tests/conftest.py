"""Shared pytest fixtures."""

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def evmone():
    evmone_location = os.environ.get("EVMONE")
    if not evmone_location:
        pytest.skip("set EVMONE to an evmone binary to run this test")
    path = Path(evmone_location).resolve()
    if not path.is_file():
        pytest.fail(f"EVMONE does not point to a file: {path}")
    return path
