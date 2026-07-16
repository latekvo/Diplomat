"""A control-channel edit (set-attr / set-overrides / trust) must be visible to
the UI *immediately*, not on the next 2s snapshot-loop write.

The panel only ever sees the node through the on-disk snapshot (``state.json``),
which it re-reads the instant the ctl reply returns. If the node applied the edit
to memory but left the statefile until its next ``_snapshot_loop`` tick (up to
``stateWriteIntervalSecs`` away), that immediate re-read would show stale values —
the ~2s "lag after changing a setting" the console used to have.

Each test drives ``_ctl_command`` directly and never calls ``start()``. With no
event loop running, the snapshot loop provably cannot write; and the snapshot
interval is pinned to 10000s besides. So if the edit is on disk right after the
ctl reply, only the command-path flush could have put it there.
"""

import asyncio

import pytest

from argent_utils.mesh import statefile
from argent_utils.mesh.node import MeshNode


@pytest.fixture
def isolated_node(tmp_path, monkeypatch):
    """A never-started node whose identity/stats/state all live under tmp_path,
    with the snapshot loop's write interval pinned absurdly high."""
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("ARGENT_MESH_LOOPBACK", "1")
    # The only writer we permit during the test is the ctl flush.
    monkeypatch.setenv("ARGENT_MESH_STATE_SECS", "10000")
    node = MeshNode()
    # No start(): sockets and the snapshot loop never run.
    assert statefile.read_state() is None  # nothing on disk yet
    return node


def test_set_attr_flushes_immediately(isolated_node):
    async def go():
        reply = await isolated_node._ctl_command(
            {"t": "set-attr", "target": "self",
             "attrs": {"tier": 1, "strengthAuto": False}}
        )
        assert reply == {"t": "ok"}
        snap = statefile.read_state()
        assert snap is not None, "set-attr did not flush state.json"
        assert snap["self"]["tier"] == 1
        assert snap["self"]["strengthAuto"] is False

    asyncio.run(go())


def test_set_overrides_flushes_immediately(isolated_node):
    async def go():
        from argent_utils.mesh import config

        duty = next(iter(config.duty_ids()))
        reply = await isolated_node._ctl_command(
            {"t": "set-overrides", "duty": duty,
             "placement": {"strategy": "pin", "nodes": []}}
        )
        assert reply == {"t": "ok"}
        snap = statefile.read_state()
        assert snap is not None, "set-overrides did not flush state.json"
        assert duty in snap["overrides"]["duties"]

    asyncio.run(go())


def test_trust_and_untrust_flush_immediately(isolated_node):
    async def go():
        fp = "ab" * 32  # a plausible 64-hex fingerprint
        reply = await isolated_node._ctl_command(
            {"t": "trust", "fingerprint": fp, "label": "my-laptop"}
        )
        assert reply == {"t": "ok"}
        snap = statefile.read_state()
        assert snap is not None, "trust did not flush state.json"
        assert any(e["fingerprint"] == fp for e in snap["trusted"])

        reply = await isolated_node._ctl_command(
            {"t": "untrust", "fingerprint": fp}
        )
        assert reply == {"t": "ok"}
        snap = statefile.read_state()
        assert not any(e["fingerprint"] == fp for e in snap["trusted"])

    asyncio.run(go())
