"""Shared setup for the bindings' own tests.

Two things, both about the fact that what is being wrapped is process-global. The
library resolves its state directory and its host once per process, so a test that
registers a host, or that lets a call fall through to a real ``~/.szpontnet``,
changes the answers every later test gets. Every test here starts with nobody
behind the node and a state directory of its own.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

# This checkout's bindings, not whatever happens to be installed under the name.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from szpontnet import host  # noqa: E402


@pytest.fixture(autouse=True)
def no_host():
    """Every test starts with a node that has nobody behind it, and leaves one.

    Restored rather than blanked at the end too: the host cache is global, so a
    suite run alongside another that registers its own host must not hand this
    one's leftovers to it.
    """
    previous = host._host
    host.reset_host()
    yield
    if previous is None:
        host.reset_host()
    else:
        host.set_host(previous)


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Never read or write a real node's identity, allowlist or snapshot."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path / "state"))
    yield


@pytest.fixture(autouse=True)
def no_real_sockets(monkeypatch):
    """No test in this suite may reach the network.

    The state directory is isolated per test; the port inside a snapshot is not.
    The fixture below names 40878, which is the protocol's default and therefore
    exactly the port a developer's own node is listening on. A test that gets as
    far as the transport would drive that live node - read its topology, edit its
    attributes, or dispatch real work into the mesh - and pass while doing it.

    So the transport is taken away, and reaching it is the failure rather than the
    silent success. Every test here is about what this package sends and what it
    makes of the answer, so any test that needs a socket is a test that forgot to
    stub the :mod:`szpontnet.ctl` call it goes through.
    """
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "this test reached the network - stub the szpontnet.ctl call it makes")

    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def no_legacy_env(monkeypatch):
    """Clear the pre-rename ``DIPLOMAT_MESH_*`` spellings.

    The library falls back to them when the ``SZPONTNET_*`` name is unset, so a
    developer whose shell still exports one would have the state-directory
    isolation above resolve to their machine's real node instead.
    """
    for key in [k for k in os.environ if k.startswith("DIPLOMAT_MESH_")]:
        monkeypatch.delenv(key)
    yield


@pytest.fixture
def snapshot_dict():
    """A snapshot in the shape a node actually publishes (08-state).

    Two peers so the tests can tell ordering, lookup and link filtering apart:
    one linked, verified and personal over the LAN, one down, keyless and foreign.
    """
    return {
        "updatedAt": "2026-07-30T18:00:00+00:00",
        "pid": 4242,
        "tcpPort": 40878,
        "v": 1,
        "linking": 1,
        "beaconBlocked": False,
        "beaconBlockReason": "",
        "self": {
            "id": "aaaaaaaa1111", "name": "mbp", "platform": "macos",
            "tier": 2, "tokens": "ok", "tokensAuto": True, "strengthAuto": False,
            "tokensPct": 0.75, "tokensSessionPct": 0.8, "tokensWeekPct": 0.75,
            "tcpPort": 40878, "dutiesEnabled": {"review": True},
            "pubkey": "AAAA", "fingerprint": "f" * 64,
            "stats": {"plan": "max-20x", "surplus": 2.5, "quotaLeft": 12.0,
                      "usageAvg": 3.0},
        },
        "peers": [
            {
                "id": "bbbbbbbb2222", "name": "tower", "platform": "linux",
                "tier": 1, "tokens": "low", "tokensPct": 0.2,
                "pubkey": "BBBB", "fingerprint": "b" * 64, "verified": True,
                "trust": "personal", "link": "up", "addr": "192.168.1.7",
                "transport": "lan", "surplus": 0.5, "lastSeenSecsAgo": 1.4,
                "uptimeSecs": 903.0,
                "stats": {"plan": "pro", "surplus": 0.5},
            },
            {
                "id": "cccccccc3333", "name": "stranger", "platform": "linux",
                "tier": 4, "tokens": "out", "link": "down", "addr": "10.0.0.9",
                "transport": "tor", "trust": "foreign", "verified": False,
            },
        ],
        "assignments": {
            "review": {"duty": "review", "assigned": ["bbbbbbbb2222"],
                       "shortfall": []},
            "audit": {"duty": "audit", "assigned": ["aaaaaaaa1111"],
                      "shortfall": [{"platform": "linux", "missing": 1}]},
        },
        "trusted": [{"fingerprint": "b" * 64, "label": "tower"}],
        "banned": [{"node": "dddddddd4444", "label": "junk",
                    "reason": "accepted work and went silent"}],
        "defaultTrust": "foreign",
        "tor": {"enabled": True, "ready": True, "onion": "abc.onion"},
        "overrides": {"rev": 0, "duties": {}},
    }
