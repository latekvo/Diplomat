"""Shared setup for the library's own tests.

Two things, both about running the library *as a library*: import it from this
checkout rather than whatever happens to be installed, and start every test with
no host registered. The second matters because the host is process-global — a test
run that also imports an application (or a test that registers one and doesn't
clean up) would otherwise leave that application's duty catalog and state
directory in force for everything after it.
"""

from __future__ import annotations

import os
import sys

import pytest

# Normalised, not `join(dirname, "..")`: this entry wins over anything the
# application's own conftest put on the path, so an unresolved `tests/..` here is the
# `__file__` every module in the package reports for the rest of the session.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from szpontnet import host  # noqa: E402


@pytest.fixture(autouse=True)
def no_host():
    """A node with nobody behind it, before and after each test."""
    host.reset_host()
    yield
    host.reset_host()


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Never touch a real node's identity, trust allowlist or snapshot."""
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path / "state"))
    yield
