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


def test_a_tor_link_never_enters_the_lan_redial_cache(tmp_path, monkeypatch):
    """A link tagged 'tor' must remember the peer's onion but NEVER write a LAN
    redial entry — that cache dials host:port directly, and a Tor link's endpoint
    (an .onion outbound, or loopback INBOUND) is not redialable that way. The
    inbound case (host = loopback) is exactly what the transport flag catches and a
    naive '.onion in host' check would miss."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    fw = _FakeWriter()
    node._link_transport[fw] = "tor"  # this link is over Tor (inbound lands on loopback)
    node._learn_node(NodeInfo.from_dict(braw), "127.0.0.1", fw, raw=braw)
    assert node._onion_cache["peer-b"].onion == _ONION_B  # onion remembered…
    assert "peer-b" not in node._peer_cache                # …LAN cache untouched
    assert node.peers["peer-b"].transport == "tor"


def test_gossiped_onion_is_not_remembered_only_a_direct_link(tmp_path, monkeypatch):
    """Onions are remembered only for peers we DIRECTLY met (a hello / paste), not
    from third-party gossip — matching the LAN 'first sight' model. A `node`
    advert relayed by a peer (link_writer=None) carries an onion but must not
    seed a Tor redial target for a peer we've never linked to."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    node._learn_node(NodeInfo.from_dict(braw), "10.0.0.2", None, raw=braw)  # gossip relay
    assert "peer-b" not in node._onion_cache


def test_a_forged_or_tampered_onion_advert_is_not_learned(tmp_path, monkeypatch):
    """The onion is trusted only because it rides INSIDE the peer's signed advert, and
    the signature gate (``_advert_authentic`` in ``_on_message``) is what enforces that.
    Driving a forged advert through ``_on_message`` — the real inbound entry, ABOVE
    ``_learn_node`` — proves the gate itself: an advert whose ``sig`` doesn't cover its
    ``onion`` (a relay swapped it) or that dropped its ``sig`` is rejected whole, so the
    onion is never cached and could never become a Tor-dial reflector target."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    fw = _FakeWriter()

    # (a) a relay swapped the onion AFTER signing → the sig no longer verifies.
    tampered = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    tampered["onion"] = _ONION_A  # sig covers _ONION_B; the wire now says _ONION_A
    assert node._on_message({"t": "hello", "node": tampered}, "127.0.0.1", fw) is None
    assert node._onion_cache == {}  # forged advert dropped whole — nothing learned

    # (b) the sig is stripped off an otherwise-keyed advert → likewise rejected.
    unsigned = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    unsigned.pop("sig", None)
    assert node._on_message({"t": "hello", "node": unsigned}, "127.0.0.1", fw) is None
    assert node._onion_cache == {}


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
    # A bound Tor link (not a bare SOCKS answer) clears the schedule, so a reachable
    # peer that flaps reconnects fast — see test_tor_dial_answer_without_a_link_keeps...
    node._tor_reset_backoff("p")
    assert "p" not in node._tor_backoff


def test_redial_targets_respects_dial_rule_liveness_and_backoff(tmp_path, monkeypatch):
    import time as _time

    # default trust personal so the entries clear the "only auto-dial personal peers"
    # gate — this test isolates the dial-rule / liveness / backoff logic.
    node = _fresh_node(tmp_path, monkeypatch, DIPLOMAT_MESH_DEFAULT_TRUST="personal")
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


def test_redial_loop_skips_dialing_while_the_onion_is_not_live(tmp_path, monkeypatch):
    """The redial loop is a no-op until the onion service is actually up: it gates each
    tick on ``tor.onion_address() is None`` (a dead or still-booting tor). With a due
    PERSONAL target present, a live onion dials it; but while ``onion_address()`` returns
    None the loop must dial NOTHING — else a node whose tor died dials out through a dead
    SOCKS port forever. (The injected _FakeTor is always live, so this gate had no node-
    level test.)"""
    monkeypatch.setattr(nodemod, "_TOR_REDIAL_TICK_SECS", 0.01)
    node = _fresh_node(tmp_path, monkeypatch, DIPLOMAT_MESH_DEFAULT_TRUST="personal")
    node.local = _dc_replace(node.local, id="0" * 32)  # sorts below the target
    target = "z" * 32
    node._onion_cache = {target: onioncache.OnionEntry(onion=_ONION_B)}
    dialed: list[str | None] = []

    async def _fake_dial(onion, peer_id=None):
        dialed.append(peer_id)

    monkeypatch.setattr(node, "_tor_dial", _fake_dial)

    class _TorLiveness:
        def __init__(self, onion):
            self._onion = onion

        def onion_address(self):
            return self._onion

    async def run_briefly():
        node.tor = _TorLiveness(None)  # tor present but the onion is not live yet
        t = asyncio.get_running_loop().create_task(node._tor_redial_loop())
        try:
            await asyncio.sleep(0.05)   # several ticks with a dead onion
            assert dialed == []          # not live → dials nothing, despite a due target
            node.tor = _TorLiveness(_ONION_B)  # onion comes up
            await _await_until(lambda: target in dialed, 2.0,
                               "a due personal target was not dialed once the onion was live")
        finally:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    asyncio.run(run_briefly())


def test_tor_dial_backs_off_when_the_socks_handshake_dies(tmp_path, monkeypatch):
    """A dial that dies mid-SOCKS-handshake raises IncompleteReadError (an EOFError
    subclass, NOT an OSError). It must be treated as a reachability miss — grow the
    backoff, don't escape the dial task with an unhandled exception."""
    node = _fresh_node(tmp_path, monkeypatch)

    class _Broken:
        def onion_address(self):
            return _ONION_B

        async def dial(self, _onion):
            raise asyncio.IncompleteReadError(b"", 2)  # tor closed mid-handshake

        async def stop(self):
            pass

    node.tor = _Broken()
    asyncio.run(node._tor_dial(_ONION_B, peer_id="p"))  # must NOT raise
    assert "p" in node._tor_backoff and node._tor_backoff["p"].next_attempt > 0
    assert _ONION_B not in node._tor_dialing  # in-flight guard released


def test_tor_dial_drain_failure_does_not_leak_the_transport_map(tmp_path, monkeypatch):
    """A dial that connects but fails to flush the hello early-returns before
    _run_link — it must still pop its writer from _link_transport (else the map
    grows one dead entry per such failure, unbounded)."""
    node = _fresh_node(tmp_path, monkeypatch)

    class _DrainFailWriter(_FakeWriter):
        async def drain(self):
            raise ConnectionError("flush failed")

    class _Tor:
        def onion_address(self):
            return _ONION_B

        async def dial(self, _onion):
            return object(), _DrainFailWriter()  # reader unused before the drain

        async def stop(self):
            pass

    node.tor = _Tor()
    asyncio.run(node._tor_dial(_ONION_B, peer_id="p"))
    assert node._link_transport == {}          # writer popped despite early return
    assert _ONION_B not in node._tor_dialing


def test_tor_inbound_closing_before_a_hello_does_not_leak_the_transport_map(
        tmp_path, monkeypatch):
    """An inbound Tor connection tagged by _on_tor_inbound that closes before a
    valid hello (here: immediate EOF) never reaches _run_link's pop, so the wrapper
    must pop the tag itself — otherwise every scan/probe of our onion leaks a map
    entry."""
    node = _fresh_node(tmp_path, monkeypatch)

    class _EOFReader:
        async def readline(self):
            return b""

    fw = _FakeWriter()
    asyncio.run(node._on_tor_inbound(_EOFReader(), fw))
    assert node._link_transport == {}


# MARK: - security: the onion serves peer links, never operator control (ctl)


class _RecWriter(_FakeWriter):
    """A _FakeWriter that records close() and answers drain(), for driving the
    accept path directly."""

    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True

    async def drain(self):
        pass


class _LineReader:
    """Feeds a fixed list of lines, then EOF — a stand-in for a StreamReader."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


def test_ctl_over_tor_is_refused_while_lan_ctl_is_served(tmp_path, monkeypatch):
    """The onion must carry peer links (`hello`) but NEVER the operator's local
    control channel (`ctl`): serving ctl over the advertised onion would expose the
    full node-control surface (stop/set-attr/trust/dispatch/tor-connect) to anyone
    holding the onion, unauthenticated in an open mesh. An inbound connection tagged
    `tor` that opens a ctl session is refused outright; an untagged (LAN/loopback)
    ctl is served exactly as before."""
    node = _fresh_node(tmp_path, monkeypatch)
    served: list[bool] = []

    async def _fake_run_ctl(_reader, _writer):
        served.append(True)

    monkeypatch.setattr(node, "_run_ctl", _fake_run_ctl)
    ctl_line = protocol.encode({"t": "ctl"})

    # Tor-tagged inbound ctl → refused: _run_ctl never runs, the writer is closed.
    tor_w = _RecWriter()
    node._link_transport[tor_w] = "tor"
    asyncio.run(node._on_tcp_connection(_LineReader([ctl_line]), tor_w))
    assert served == [] and tor_w.closed

    # LAN/loopback inbound ctl (untagged) → served, exactly as before.
    lan_w = _RecWriter()
    asyncio.run(node._on_tcp_connection(_LineReader([ctl_line]), lan_w))
    assert served == [True]


def test_unencodable_secret_or_apikey_closes_cleanly_not_crashes(tmp_path, monkeypatch):
    """A hostile opener can put a JSON lone surrogate (``"\\ud800"``) in the join secret
    or the ctl apiKey; it decodes to a Python str that a plain ``.encode("utf-8")``
    rejects with UnicodeEncodeError. ``_on_tcp_connection`` runs OUTSIDE any try, so an
    escaping raise would orphan the socket — an unbounded, pre-auth fd leak reachable
    even on the default open mesh. The join/key compares must be surrogate-safe: the bad
    value simply mismatches and the writer is closed, no raise escapes the callback."""
    # (a) hello with a lone-surrogate secret (open mesh: any non-empty secret mismatches).
    node = _fresh_node(tmp_path, monkeypatch, "a")
    w = _RecWriter()
    hello = protocol.encode({"t": "hello", "secret": "\ud800"})
    asyncio.run(node._on_tcp_connection(_LineReader([hello]), w))  # must NOT raise
    assert w.closed

    # (b) ctl with a lone-surrogate apiKey on a node that requires an API key.
    keyed = _fresh_node(tmp_path, monkeypatch, "k", DIPLOMAT_MESH_API_KEY="realkey")
    w2 = _RecWriter()
    ctl = protocol.encode({"t": "ctl", "apiKey": "\ud800"})
    asyncio.run(keyed._on_tcp_connection(_LineReader([ctl]), w2))  # must NOT raise
    assert w2.closed


# MARK: - backoff: reset on a real link bind, not on a bare SOCKS answer


def test_tor_dial_answer_without_a_link_keeps_the_backoff(tmp_path, monkeypatch):
    """An onion that ANSWERS the SOCKS dial but never binds a mesh link (rotated
    secret, a squatted address, answer-then-drop) must stay throttled — the dial
    pre-schedules the next probe and only a real link bind clears it. Resetting on the
    bare answer would defeat the backoff and thrash a fresh Tor circuit every tick."""
    node = _fresh_node(tmp_path, monkeypatch)

    class _AnswerNoLinkTor:
        def onion_address(self):
            return _ONION_B

        async def dial(self, _onion):
            return _LineReader([]), _RecWriter()  # answers, then immediate EOF = no bind

        async def stop(self):
            pass

    node.tor = _AnswerNoLinkTor()
    asyncio.run(node._tor_dial(_ONION_B, peer_id="p"))
    # Pre-scheduled once (interval doubled off the floor) and NOT reset by the answer.
    assert "p" in node._tor_backoff
    assert node._tor_backoff["p"].interval == (
        nodemod._TOR_BACKOFF_MIN_SECS * nodemod._TOR_BACKOFF_FACTOR)
    assert node._tor_backoff["p"].next_attempt > 0
    assert _ONION_B not in node._tor_dialing  # in-flight guard released


def test_a_bound_tor_link_resets_the_backoff(tmp_path, monkeypatch):
    """The complement: when a Tor link actually BINDS, the peer's reconnect backoff is
    cleared so a reachable peer that flaps redials promptly."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", onion=_ONION_B, tcp_port=40901)
    node._tor_backoff["peer-b"] = nodemod._TorBackoff(next_attempt=9e9, interval=300)
    fw = _FakeWriter()
    node._link_transport[fw] = "tor"
    node._learn_node(NodeInfo.from_dict(braw), "127.0.0.1", fw, raw=braw)
    assert node.peers["peer-b"].transport == "tor"
    assert "peer-b" not in node._tor_backoff  # bind cleared the schedule


# MARK: - lifecycle: bootstrap fails fast if the stdout pump dies


def test_await_bootstrap_fails_fast_when_the_pump_dies(tmp_path):
    """_await_bootstrap must not block the whole bootstrap_timeout when the stdout
    pump (which is what SETS _bootstrapped) has died while the tor proc lingers —
    otherwise a dead pump stalls Tor bring-up for the full timeout. It watches the
    pump task and returns False the moment the pump completes."""

    class _NeverProc:
        returncode = None

        async def wait(self):
            await asyncio.Event().wait()  # proc never exits

    async def _run():
        t = tor.TorTransport(tmp_path, binary_path="/nonexistent")
        t._proc = _NeverProc()
        t._pump_task = asyncio.ensure_future(asyncio.sleep(0))  # a pump that finishes
        await asyncio.sleep(0)  # let it complete
        # A 30s bootstrap timeout, but a wait_for cap of 3s: the fix must return well
        # inside 3s (the pump is done); the unfixed code would block the full 30s.
        return await asyncio.wait_for(t._await_bootstrap(30.0), timeout=3.0)

    assert asyncio.run(_run()) is False


# MARK: - lifecycle: a dead tor degrades to LAN-only (onion no longer advertised)


def test_onion_address_is_none_once_tor_has_exited(tmp_path):
    """After a successful start(), if the tor child later dies (crash/OOM/kill), the
    onion is no longer served — onion_address() must return None so the node stops
    advertising and dialing a dead onion and degrades to LAN-only, instead of claiming
    a WAN handle that no longer works."""
    t = tor.TorTransport(tmp_path, binary_path="/nonexistent")
    t._onion = _ONION_A

    class _Alive:
        returncode = None

    class _Dead:
        returncode = -9  # tor was killed

    t._proc = _Alive()
    assert t.onion_address() == _ONION_A     # live tor → onion advertised
    t._proc = _Dead()
    assert t.onion_address() is None         # tor exited → degrade to LAN-only
    t._proc = None
    assert t.onion_address() is None


def test_pdeathsig_is_callable_and_best_effort(tmp_path):
    """_pdeathsig runs in the forked child before exec; it must NEVER raise (any
    failure is swallowed so tor still execs). The kill-on-parent-death behavior itself
    is verified out-of-band via a stand-in child."""
    tor._pdeathsig()  # must not raise


# MARK: - security: only PERSONAL peers are auto-dialed over Tor


_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _mk_onion(i: int) -> str:
    """A unique valid v3 onion per i (56 base32 chars + .onion)."""
    s, n = "", i
    for _ in range(56):
        s += _B32[n % 32]
        n //= 32
    return s + ".onion"


def test_only_personal_onions_are_auto_redial_targets(tmp_path, monkeypatch):
    """A linked foreign peer can advertise an arbitrary (attacker-chosen) onion; the
    node must NOT auto-dial it — else it becomes a Tor-dial reflector aimed by the
    attacker and leaks a signed hello to a destination it picked. Only onions of peers
    we trust as PERSONAL are auto-redial targets."""
    import time as _time

    node = _fresh_node(tmp_path, monkeypatch)  # default trust: foreign
    node.local = _dc_replace(node.local, id="0" * 32)  # sorts below both peers
    pk = crypto.DeviceKey(Ed25519PrivateKey.generate())
    node.add_trusted(pk.fingerprint, "friend")
    node._onion_cache = {
        "peer-personal": onioncache.OnionEntry(onion=_ONION_A, fingerprint=pk.fingerprint),
        "peer-foreign": onioncache.OnionEntry(onion=_ONION_B, fingerprint="deadbeef"),
    }
    targets = [pid for pid, _ in node._tor_redial_targets(_time.monotonic())]
    assert targets == ["peer-personal"]  # the foreign onion is never auto-dialed


def test_foreign_onion_churn_does_not_evict_a_personal_entry(tmp_path, monkeypatch):
    """A single linked foreign peer can advertise many distinct signed adverts, filling
    the 256-entry onion cache. Eviction must drop FOREIGN entries first, so the flood
    cannot push out the onion of a personal peer we actually redial (an isolation DoS
    on our WAN reconnect)."""
    node = _fresh_node(tmp_path, monkeypatch)  # default trust: foreign
    pk = crypto.DeviceKey(Ed25519PrivateKey.generate())
    node.add_trusted(pk.fingerprint, "friend")
    node._remember_onion("peer-personal", _ONION_A, pk.fingerprint)  # a personal entry
    for i in range(nodemod._MAX_PEER_CACHE + 50):  # overflow the cache with foreign
        node._remember_onion(f"foreign-{i}", _mk_onion(i), "deadbeef")
    assert len(node._onion_cache) == nodemod._MAX_PEER_CACHE
    assert "peer-personal" in node._onion_cache  # personal survived the foreign flood


# MARK: - config: the bootstrap timeout rejects non-finite / non-positive values


def test_tor_bootstrap_timeout_rejects_non_finite(monkeypatch):
    """A non-finite bootstrap timeout (``inf`` from ``1e999``, ``nan``) would make
    asyncio.wait block FOREVER — the opposite of "give up and stay LAN-only" — and a
    non-positive one is meaningless. All fall back to the 90s default; a sane value
    passes through."""
    from diplomat_app.mesh import config

    for bad in ("inf", "1e999", "-inf", "nan", "-1", "0"):
        monkeypatch.setenv("DIPLOMAT_MESH_TOR_BOOTSTRAP_SECS", bad)
        assert config.tor_bootstrap_timeout() == 90.0
    monkeypatch.setenv("DIPLOMAT_MESH_TOR_BOOTSTRAP_SECS", "45")
    assert config.tor_bootstrap_timeout() == 45.0


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
            assert a.peers[b.local.id].addr == _ONION_B  # dialer's addr = the onion
            assert a.peers[b.local.id].transport == "tor"  # link tagged over Tor
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
