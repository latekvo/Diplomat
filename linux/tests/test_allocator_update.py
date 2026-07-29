"""When a launch installs the device allocator, and when it must keep its hands off.

The allocator is not a one-time install: everything ``--install`` writes is a copy
of something in the checkout (the skill, the always-on rule, the CLAUDE.md coercion
block, the MCP registration), so a ``git pull`` moves the originals and leaves the
copies behind. Refreshing them on launch is the whole point of
:func:`deviceallocator.needs_install` — and the reason it needs a test is the case
next door: a user who removed the allocator in Settings must not find it back after
a restart.

The macOS twin (``DeviceAllocator.needsInstall``, run by ``DIPLOMAT_ALLOCATOR_TEST``)
pins the same table. Two applets, one decision.
"""

from __future__ import annotations

import pytest

from diplomat_app import deviceallocator


def _status(installed: bool, outdated: bool = False) -> dict:
    return {"installed": installed, "outdated": outdated, "drift": ["skill"] if outdated else []}


# (case, status, setup_done, expected)
CASES = [
    ("first run: nothing installed, nothing settled",
     _status(False), False, True),
    ("settled uninstall: the user took it off in Settings",
     _status(False), True, False),
    ("steady state: installed and current",
     _status(True), True, False),
    ("installed by something else, and current — adopt it rather than reinstall",
     _status(True), False, False),
    ("stale: the checkout moved on from what is deployed",
     _status(True, outdated=True), True, True),
    ("stale on a machine that has not settled yet",
     _status(True, outdated=True), False, True),
]


@pytest.mark.parametrize("case,status,setup_done,expected",
                         CASES, ids=[c[0] for c in CASES])
def test_the_launch_decision(case, status, setup_done, expected):
    assert deviceallocator.needs_install(status, setup_done) is expected, case


def test_a_check_that_could_not_run_is_a_first_run_until_setup_settles():
    """``check()`` returns None when node is missing or the installer won't parse.
    Retrying is right — that is how a machine that has no node yet gets set up once
    one arrives — but only until the setup is settled, or an uninstalled machine
    with a broken check would be reinstalled on every single launch."""
    assert deviceallocator.needs_install(None, False) is True
    assert deviceallocator.needs_install(None, True) is False


def test_an_installer_too_old_to_report_drift_is_left_alone():
    """The .app can outrun its checkout (it may sit in /Applications while the source
    moves), so a new front-end can meet an ``install.js`` that predates drift
    detection and reports no ``outdated`` at all.

    Reading "current" as a positive flag would make that machine reinstall on every
    launch forever. Deriving it from ``installed and not outdated`` makes the old
    installer's silence mean what it should: installed, and nothing known to be
    wrong with it."""
    assert deviceallocator.needs_install({"installed": True}, True) is False
    assert deviceallocator.needs_install({"installed": True}, False) is False
