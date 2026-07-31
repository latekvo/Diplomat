"""Shared setup for the library's own tests.

Two things, both about running the library *as a library*: import it from this
checkout rather than whatever happens to be installed, and start every test with
no host registered. The second matters because the host is process-global — a test
run that also imports an application (or a test that registers one and doesn't
clean up) would otherwise leave that application's duty catalog and state
directory in force for everything after it.
"""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

# Normalised, not `join(dirname, "..")`: this entry wins over anything the
# application's own conftest put on the path, so an unresolved `tests/..` here is the
# `__file__` every module in the package reports for the rest of the session.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from szpontnet import host  # noqa: E402


def pytest_configure(config):
    """Register the markers this suite uses, so a run is never noisy about them and
    ``-m 'not tor_e2e'`` is a supported way to skip the slow, process-spawning half."""
    config.addinivalue_line(
        "markers",
        "tor_e2e: drives real tor daemons and whole node processes (see tornet.py)")


@contextlib.contextmanager
def nobody_home():
    """No host for the duration — and whoever was behind the node before, put back.

    Restoring, not blanking, because the host is process-global and an application
    registers its own exactly once, at import. Run this suite in the same session as
    that application's (``pytest packages/szpontnet-core/tests
    packages/diplomat-platform/linux/tests``) and a teardown that ends on
    ``reset_host()`` leaves every later test running against the library's
    defaults — passing or failing on collection order, which is how a suite goes
    green in CI (separate jobs) and red on a developer's machine.

    A named helper rather than fixture-body code so the restore itself is testable;
    handed to that test as the ``host_isolation`` fixture below.
    """
    previous = host._host
    host.reset_host()
    try:
        yield
    finally:
        if previous is None:
            host.reset_host()
        else:
            host.set_host(previous)


@pytest.fixture(autouse=True)
def no_host():
    """Every test starts with a node that has nobody behind it."""
    with nobody_home():
        yield


@pytest.fixture
def host_isolation():
    """:func:`nobody_home` itself, for the test that pins its restore.

    Handed over as a fixture rather than imported: run beside another suite and
    ``import conftest`` resolves to whichever ``tests`` directory reached sys.path
    first, which for a test about cross-suite damage is a memorable way to fail.
    """
    return nobody_home


@pytest.fixture(autouse=True)
def no_legacy_env(monkeypatch):
    """Clear the pre-rename ``DIPLOMAT_MESH_*`` names out of the environment.

    :mod:`szpontnet.env` falls back to them when the ``SZPONTNET_*`` spelling is
    unset, which is right for a machine mid-migration and wrong for a test: a
    developer whose shell still exports one would have every ``delenv`` of the new
    name quietly resolve to their value instead of to the default it means to
    exercise. Tests *about* the fallback set it themselves, after this.
    """
    for key in [k for k in os.environ if k.startswith("DIPLOMAT_MESH_")]:
        monkeypatch.delenv(key)
    yield


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Never touch a real node's identity, trust allowlist or snapshot."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path / "state"))
    yield


@pytest.fixture(autouse=True)
def no_tor_by_default(monkeypatch):
    """No test spawns a ``tor`` child unless it asked to.

    The transport is on by default in a shipped node, which means every test that
    reaches ``MeshNode.start()`` on a developer's machine would otherwise fork a real
    tor, bind a SOCKS port and start bootstrapping against the live Tor network —
    slow, non-deterministic, and network-dependent in a suite whose whole claim is
    that it is none of those. The tests that ARE about Tor turn it back on
    themselves (see ``tornet.py``), which is also what keeps them honest about
    running against a tor daemon rather than against an ambient default.
    """
    monkeypatch.setenv("SZPONTNET_TOR", "0")
    yield


@pytest.fixture
def simnet(tmp_path, monkeypatch, no_host):
    """A virtual LAN a test can run a whole mesh on — and break. See simnet.py.

    Depends on ``no_host`` explicitly rather than relying on autouse ordering:
    it registers a host of its own, and that registration has to happen inside
    the isolation that later hands the previous host back.
    """
    import simnet as simnet_module

    return simnet_module.build(tmp_path / "sim", monkeypatch)
