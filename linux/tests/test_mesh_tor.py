"""Tor WAN-transport tests: advertise, learn, backoff, and — the capstone — a
manual-paste link over an injected dialer that a dispatch then rides, proving a
Tor link behaves exactly like a LAN one.

Deterministic and offline: the node's Tor dialer is dependency-injected (a fake
that connects to the peer's real loopback TCP port, standing in for the onion
forward), so no real ``tor`` runs here. ``_own_addresses`` is patched to a fixed
set so constructing a node never blocks on ``getaddrinfo``. The real-``tor``
end-to-end proof is a separate, opt-in test below.

Run with ``python -m pytest linux/tests/test_mesh_tor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import replace as _dc_replace  # noqa: E402

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from diplomat_app.mesh import (  # noqa: E402
    crypto, node as nodemod, onioncache, protocol, tor,
)
from diplomat_app.mesh.protocol import NodeInfo  # noqa: E402

_ONION_A = "a" * 56 + ".onion"
_ONION_B = "b" * 56 + ".onion"


@pytest.fixture(autouse=True)
def _no_getaddrinfo_hang(monkeypatch):
    """Constructing a MeshNode calls _own_addresses() → getaddrinfo(hostname),
    which can block. Pin it so every test here builds a node instantly."""
    monkeypatch.setattr(nodemod, "_own_addresses", lambda: {"127.0.0.1", "::1"})


def _fresh_node(tmp_path, monkeypatch, subdir="n", **env):
    d = tmp_path / subdir
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(d))
    monkeypatch.setenv("DIPLOMAT_MESH_OAUTH_PROBE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return nodemod.MeshNode()


class _FakeWriter:
    """Minimal StreamWriter stand-in for driving _learn_node directly."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data):
        self.chunks.append(bytes(data))

    def close(self):
        pass

    def get_extra_info(self, _key, default=None):
        return default


class _FakeTor:
    """An injected Tor dialer: ``dial`` connects to a real loopback TCP port,
    standing in for the onion service's local forward. ``onion_address`` returns a
    fixed onion so the ctl/redial gates that check readiness pass."""

    def __init__(self, onion: str, connect_port: int):
        self._onion = onion
        self._port = connect_port

    def onion_address(self):
        return self._onion

    async def dial(self, _onion):
        return await asyncio.open_connection(
            "127.0.0.1", self._port, limit=protocol.MAX_LINE_BYTES)

    async def stop(self):
        pass


def _signed_advert(key, node_id, onion="", **kw):
    """A NodeInfo dict signed by ``key``, exactly as a peer would put on the wire."""
    info = NodeInfo(id=node_id, name=kw.get("name", node_id), platform="linux",
                    tier=3, tokens="ok", tcp_port=kw.get("tcp_port", 40900),
                    pubkey=key.public_b64, onion=onion)
    return _dc_replace(
        info, sig=key.sign(protocol.advert_signing_bytes(info.to_dict()))).to_dict()


# MARK: - advertise


def test_node_advertises_its_onion_inside_the_signed_advert(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch)
    node.tor = _FakeTor(_ONION_A, connect_port=0)
    raw = node.info.to_dict()
    assert raw["onion"] == _ONION_A
    # It is signed: the node's own advert verifies, and tampering the onion breaks it.
    assert node._advert_authentic(raw)
    assert not node._advert_authentic(dict(raw, onion=_ONION_B))
    # With Tor off, no onion is advertised (LAN-only nodes stay wire-identical).
    node.tor = None
    assert "onion" not in node.info.to_dict()


# MARK: - learn + persist


def test_learns_and_persists_a_peer_onion_from_its_hello(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    info = NodeInfo.from_dict(braw)
    node._learn_node(info, "192.168.1.9", _FakeWriter(), raw=braw)
    # The onion is cached in memory and persisted, keyed to the proven fingerprint.
    assert node._onion_cache["peer-b"].onion == _ONION_B
    assert node._onion_cache["peer-b"].fingerprint == bkey.fingerprint
    assert onioncache.load()["peer-b"].onion == _ONION_B
    # The LAN address cache is unaffected — both transports coexist.
    assert node._peer_cache["peer-b"] == ("192.168.1.9", 40901)


def test_a_tor_link_host_never_enters_the_lan_redial_cache(tmp_path, monkeypatch):
    """An outbound Tor dial runs the hello with host = the .onion. That must be
    remembered as an ONION (redialable over Tor), never written into peers.json,
    whose entries are dialed as host:port directly and can't resolve an onion."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    node._learn_node(NodeInfo.from_dict(braw), _ONION_B, _FakeWriter(), raw=braw)
    assert node._onion_cache["peer-b"].onion == _ONION_B  # onion remembered…
    assert "peer-b" not in node._peer_cache                # …LAN cache untouched


# MARK: - backoff


def test_backoff_grows_geometrically_and_resets(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch)
    node._tor_grow_backoff("p")
    first = node._tor_backoff["p"]
    assert first.interval == nodemod._TOR_BACKOFF_MIN_SECS * nodemod._TOR_BACKOFF_FACTOR
    assert first.next_attempt > 0
    node._tor_grow_backoff("p")
    assert node._tor_backoff["p"].interval == (
        nodemod._TOR_BACKOFF_MIN_SECS * nodemod._TOR_BACKOFF_FACTOR ** 2)
    # …never past the ceiling.
    for _ in range(20):
        node._tor_grow_backoff("p")
    assert node._tor_backoff["p"].interval == nodemod._TOR_BACKOFF_MAX_SECS
    # The onion answering clears the schedule so a reachable peer reconnects fast.
    node._tor_reset_backoff("p")
    assert "p" not in node._tor_backoff


def test_redial_targets_respects_dial_rule_liveness_and_backoff(tmp_path, monkeypatch):
    import time as _time

    node = _fresh_node(tmp_path, monkeypatch)
    lo, hi = ("0" * 32, "z" * 32)  # one below our id, one above
    node.local = _dc_replace(node.local, id="m" * 32)
    node._onion_cache = {
        hi: onioncache.OnionEntry(onion=_ONION_B),   # we dial (our id sorts below)
        lo: onioncache.OnionEntry(onion=_ONION_A),   # they dial us — never a target
    }
    now = _time.monotonic()
    assert [pid for pid, _ in node._tor_redial_targets(now)] == [hi]
    # A live link to the dialable peer removes it — no aggressive switching.
    node.peers[hi] = nodemod.Peer(NodeInfo.from_dict(
        {"id": hi, "name": hi, "platform": "linux", "tier": 3, "tokens": "ok"}), "x")
    node.peers[hi].writer = _FakeWriter()
    assert node._tor_redial_targets(now) == []
    # And an un-due backoff also holds it back.
    del node.peers[hi]
    node._tor_backoff[hi] = nodemod._TorBackoff(next_attempt=now + 100, interval=20)
    assert node._tor_redial_targets(now) == []


# MARK: - the capstone: a real link over an injected dialer, with a dispatch on it


def test_manual_paste_links_over_tor_and_a_dispatch_runs_on_the_peer(
        tmp_path, monkeypatch):
    """The whole user story on the node level: A pastes B's onion (bypassing the
    LAN entirely), the link comes up with the identical hello/auth/trust handshake,
    and a dispatch A→B rides that link and executes on B — proving the Tor link
    behaves exactly like a LAN link. The only stand-in is the dialer."""
    monkeypatch.setenv("DIPLOMAT_MESH_LOOPBACK", "1")
    out_file = tmp_path / "landed.txt"
    monkeypatch.setenv("DIPLOMAT_MESH_SPAWN", f"cp {{prompt_file}} {out_file}")

    async def scenario():
        # B (executor): personal default trust so it runs A's request directly.
        b = _fresh_node(tmp_path, monkeypatch, "b",
                        DIPLOMAT_MESH_DEFAULT_TRUST="personal")
        await b._start_tcp()
        # A (dispatcher): reaches B only over its injected Tor dialer.
        a = _fresh_node(tmp_path, monkeypatch, "a",
                        DIPLOMAT_MESH_DEFAULT_TRUST="personal")
        await a._start_tcp()
        a.tor = _FakeTor(_ONION_B, connect_port=b.tcp_port)

        dial = asyncio.get_running_loop().create_task(a._tor_dial(_ONION_B))
        try:
            await _await_until(
                lambda: (b.local.id in a.peers and a.peers[b.local.id].linked
                         and a.local.id in b.peers and b.peers[a.local.id].linked),
                5.0, "link never came up over Tor")
            # Both proved their device keys — trust is established exactly as on a LAN.
            await _await_until(
                lambda: (a.peers[b.local.id].verified_fp is not None
                         and b.peers[a.local.id].verified_fp is not None),
                5.0, "device keys were not mutually verified")
            assert a.peers[b.local.id].addr == _ONION_B  # snapshot reads this as Tor
            # A dispatch rides the Tor link and runs on B, just like over the LAN.
            results = await a.dispatch("audit", "hello over tor",
                                       target=b.local.id)
            assert results and results[0]["status"] == "spawned", results
            await _await_until(out_file.exists, 5.0, "the job never landed on B")
            assert "hello over tor" in out_file.read_text(encoding="utf-8")
        finally:
            dial.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dial
            await a.stop()
            await b.stop()

    asyncio.run(scenario())


async def _await_until(pred, timeout: float, msg: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if pred():
            return
        if loop.time() > deadline:
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
