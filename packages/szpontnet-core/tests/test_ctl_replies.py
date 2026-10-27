"""What a control edit is answered: ``ok`` once the files it changed are on disk.

An edit the node could not write down is still applied — it is live in memory and
retried until it saves — but the reply is ``error`` naming the file, because a
restart before then loses it. Each command names exactly the files its edit
changes, so each of those is failed here on its own, with every other file still
writable.
"""

from __future__ import annotations

import asyncio
import errno

import pytest

from szpontnet import banned, identity, stats, trust
from szpontnet.node import MeshNode

FP = "ab" * 32


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A never-started node on its own state dir."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    monkeypatch.setenv("SZPONTNET_LOOPBACK", "1")
    monkeypatch.setenv("SZPONTNET_STATE_SECS", "10000")
    return MeshNode()


def _ctl(node: MeshNode, msg: dict) -> dict:
    return asyncio.run(node._ctl_command(msg))


def _refuse(monkeypatch, module) -> None:
    """Every later write of ``module``'s state file fails as on a full disk."""
    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(module, "save", full)


def _trusted(node):
    node.add_trusted(FP, "mine")


def _banned(node):
    node.ban_device(FP)


@pytest.mark.parametrize("setup, module, msg, name", [
    (None, trust, {"t": "trust", "fingerprint": FP}, "trusted.json"),
    # A promotion lifts a ban, so it writes the ban list too.
    (_banned, banned, {"t": "trust", "fingerprint": FP}, "banned.json"),
    (_trusted, trust, {"t": "untrust", "fingerprint": FP}, "trusted.json"),
    (None, banned, {"t": "ban", "fingerprint": FP}, "banned.json"),
    # A ban takes the device off the allowlist, so it writes that too.
    (_trusted, trust, {"t": "ban", "fingerprint": FP}, "trusted.json"),
    (_banned, banned, {"t": "unban", "fingerprint": FP}, "banned.json"),
    (None, trust, {"t": "set-default-trust", "level": "personal"}, "trusted.json"),
    (None, identity, {"t": "set-attr", "target": "self", "attrs": {"name": "renamed"}},
     "node.json"),
    (None, stats, {"t": "set-attr", "target": "self", "attrs": {"usage": 1.0}},
     "stats.json"),
], ids=["trust", "trust-unbans", "untrust", "ban", "ban-untrusts", "unban",
        "set-default-trust", "set-attr-node", "set-attr-stats"])
def test_an_edit_that_could_not_be_saved_is_answered_error(
        node, monkeypatch, setup, module, msg, name):
    if setup:
        setup(node)
    with monkeypatch.context() as m:
        _refuse(m, module)
        reply = _ctl(node, msg)
    assert reply["t"] == "error" and name in reply["reason"], reply
    assert node._unsaved.keys() == {name}

    node._save_owed()
    assert node._unsaved == {}


def test_untrust_needs_a_fingerprint(node):
    _trusted(node)
    for msg in ({"t": "untrust"}, {"t": "untrust", "fingerprint": "  "}):
        assert _ctl(node, msg) == {"t": "error", "reason": "untrust needs a fingerprint"}
    assert FP in node._trusted
