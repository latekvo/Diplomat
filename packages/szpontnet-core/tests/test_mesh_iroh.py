"""Iroh WAN-transport tests: advertise, learn, transport preference, and the
generic WAN path both transports share.

Deterministic and offline: the node's dialer is dependency-injected (a fake that
connects to the peer's real loopback TCP port, standing in for a QUIC connection),
so no endpoint binds and nothing is published to a discovery service here.
``_own_addresses`` is patched to a fixed set so constructing a node never blocks on
``getaddrinfo``.

That injection is what makes these tests sharp — a preference order, a refused
``ctl`` or a cache migration can be asserted exactly, with no network in the way —
and it is also their limit: a fake dialer cannot show that an endpoint comes online,
that discovery resolves an id, or that the bytes the adapter writes survive a real
QUIC stream. ``test_iroh_e2e.py`` covers that, against real iroh endpoints.

Run with ``python -m pytest packages/szpontnet-core/tests/test_mesh_iroh.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace as _dc_replace

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from szpontnet import (
    crypto, irohnet, node as nodemod, protocol, tor, wancache,
)
from szpontnet.protocol import NodeInfo

_EP_A = "a" * 64
_EP_B = "b" * 64
_ONION_A = "c" * 56 + ".onion"


@pytest.fixture(autouse=True)
def _no_getaddrinfo_hang(monkeypatch):
    monkeypatch.setattr(nodemod, "_own_addresses", lambda: {"127.0.0.1", "::1"})


def _fresh_node(tmp_path, monkeypatch, subdir="n", **env):
    d = tmp_path / subdir
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("SZPONTNET_DIR", str(d))
    monkeypatch.setenv("SZPONTNET_OAUTH_PROBE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return nodemod.MeshNode()


class _FakeWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data):
        self.chunks.append(bytes(data))

    def close(self):
        pass

    def get_extra_info(self, _key, default=None):
        return default


class _FakeIroh:
    """An injected iroh dialer: ``dial`` connects to a real loopback TCP port,
    standing in for a QUIC stream to the peer's endpoint. ``address`` returns a
    fixed endpoint id so the ctl/redial gates that check readiness pass."""

    def __init__(self, endpoint: str, connect_port: int = 0):
        self._endpoint = endpoint
        self._port = connect_port

    def address(self):
        return self._endpoint

    async def dial(self, _endpoint):
        return await asyncio.open_connection(
            "127.0.0.1", self._port, limit=protocol.MAX_LINE_BYTES)

    async def stop(self):
        pass


def _install(node, *transports):
    """Register injected transports on ``node`` in preference order, the way
    ``MeshNode.start()`` would. Returns the :class:`node._Wan` records."""
    records = []
    for name, impl in transports:
        records.append(nodemod._Wan(
            name=name, impl=impl,
            field="endpoint" if name == "iroh" else "onion",
            normalize=(irohnet.normalize_endpoint if name == "iroh"
                       else tor.normalize_onion)))
        setattr(node, name, impl)
    node._wan = records
    return records


def _signed_advert(key, node_id, endpoint="", onion="", **kw):
    info = NodeInfo(id=node_id, name=kw.get("name", node_id), platform="linux",
                    tier=3, tokens="ok", tcp_port=kw.get("tcp_port", 40900),
                    pubkey=key.public_b64, endpoint=endpoint, onion=onion)
    return _dc_replace(
        info, sig=key.sign(protocol.advert_signing_bytes(info.to_dict()))).to_dict()


# MARK: - address parsing


def test_normalize_endpoint_is_lenient_in_and_strict_out():
    """An operator pastes whatever the other machine printed. Accept the shapes that
    carry a real id, and emit either a canonical id or nothing — never a partial one
    that would fail a dial confusingly."""
    assert irohnet.normalize_endpoint(_EP_A) == _EP_A
    assert irohnet.normalize_endpoint(f"  {_EP_A.upper()}  ") == _EP_A
    assert irohnet.normalize_endpoint(f"iroh://{_EP_A}/") == _EP_A
    assert irohnet.normalize_endpoint(f"{_EP_A}:4433") == _EP_A
    # Anything that is not exactly 64 hex is not an endpoint id.
    for bad in ("", "z" * 64, "a" * 63, "a" * 65, _ONION_A, None, 5, b"a" * 64):
        assert irohnet.normalize_endpoint(bad) == "", f"{bad!r} must not parse"


def test_an_endpoint_id_is_never_mistaken_for_an_onion_or_the_reverse():
    """The two transports' addresses share one cache and one in-flight dial set, so
    their parsers must not overlap — else an onion could select the iroh dialer."""
    assert tor.normalize_onion(_EP_A) == ""
    assert irohnet.normalize_endpoint(_ONION_A) == ""


# MARK: - advertise


def test_node_advertises_its_endpoint_inside_the_signed_advert(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch)
    _install(node, ("iroh", _FakeIroh(_EP_A)))
    raw = node.info.to_dict()
    assert raw["endpoint"] == _EP_A
    # It is signed: the node's own advert verifies, and tampering the endpoint breaks
    # it — a relay cannot swap the endpoint to redirect a future dial.
    assert node._advert_authentic(raw)
    assert not node._advert_authentic(dict(raw, endpoint=_EP_B))
    # With iroh off, no endpoint is advertised (LAN-only nodes stay wire-identical).
    node._wan = []
    assert "endpoint" not in node.info.to_dict()


def test_the_endpoint_is_not_the_device_key(tmp_path, monkeypatch):
    """Both are Ed25519 public keys, and reusing one for both would let iroh's TLS
    handshake and the mesh's nonce challenge sign under the same key. They are
    deliberately separate: the advert carries both, and they must differ."""
    node = _fresh_node(tmp_path, monkeypatch)
    _install(node, ("iroh", _FakeIroh(_EP_A)))
    raw = node.info.to_dict()
    assert raw["endpoint"] != raw.get("pubkey", "")


# MARK: - learn + persist


def test_learns_and_persists_a_peer_endpoint_from_its_hello(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", endpoint=_EP_B, tcp_port=40901)
    node._learn_node(NodeInfo.from_dict(braw), "192.168.1.9", _FakeWriter(), raw=braw)
    assert node._wan_cache["peer-b"].endpoint == _EP_B
    assert node._wan_cache["peer-b"].fingerprint == bkey.fingerprint
    assert wancache.load()["peer-b"].endpoint == _EP_B
    # The LAN address cache is unaffected — the transports coexist.
    assert node._peer_cache["peer-b"] == ("192.168.1.9", 40901)


def test_a_forged_or_tampered_endpoint_advert_is_not_learned(tmp_path, monkeypatch):
    """The endpoint is trusted only because it rides INSIDE the peer's signed advert,
    and the signature gate (``_advert_authentic`` in ``_on_message``) is what enforces
    that. Driving a forged advert through ``_on_message`` — the real inbound entry,
    ABOVE ``_learn_node`` — proves the gate itself: an advert whose ``sig`` doesn't
    cover its ``endpoint`` (a relay swapped it) or that dropped its ``sig`` is rejected
    whole, so the endpoint is never cached and our next WAN dial can never be aimed at
    a destination the relay chose."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    fw = _FakeWriter()

    # (a) a relay swapped the endpoint AFTER signing → the sig no longer verifies.
    tampered = _signed_advert(bkey, "peer-b", endpoint=_EP_B, tcp_port=40901)
    tampered["endpoint"] = _EP_A  # sig covers _EP_B; the wire now says _EP_A
    assert node._on_message({"t": "hello", "node": tampered}, "127.0.0.1", fw) is None
    assert node._wan_cache == {}  # forged advert dropped whole — nothing learned

    # (b) the sig is stripped off an otherwise-keyed advert → likewise rejected.
    unsigned = _signed_advert(bkey, "peer-b", endpoint=_EP_B, tcp_port=40901)
    unsigned.pop("sig")
    assert node._on_message({"t": "hello", "node": unsigned}, "127.0.0.1", fw) is None
    assert node._wan_cache == {}


def test_an_iroh_link_never_enters_the_lan_redial_cache(tmp_path, monkeypatch):
    """The LAN cache dials host:port directly; an iroh link's "host" is an endpoint
    id, which is not redialable that way."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", endpoint=_EP_B, tcp_port=40901)
    fw = _FakeWriter()
    node._link_transport[fw] = "iroh"
    node._learn_node(NodeInfo.from_dict(braw), _EP_B, fw, raw=braw)
    assert node._wan_cache["peer-b"].endpoint == _EP_B   # endpoint remembered…
    assert "peer-b" not in node._peer_cache               # …LAN cache untouched
    assert node.peers["peer-b"].transport == "iroh"


def test_a_bound_iroh_link_resets_the_backoff(tmp_path, monkeypatch):
    """A real WAN link BINDING is the "this address is usable" signal, for any
    transport — the gate is "not a LAN link", not a hardcoded transport name."""
    node = _fresh_node(tmp_path, monkeypatch, "a")
    bkey = crypto.DeviceKey(Ed25519PrivateKey.generate())
    braw = _signed_advert(bkey, "peer-b", endpoint=_EP_B)
    node._wan_backoff["peer-b"] = nodemod._WanBackoff(next_attempt=1e9, interval=300)
    fw = _FakeWriter()
    node._link_transport[fw] = "iroh"
    node._learn_node(NodeInfo.from_dict(braw), _EP_B, fw, raw=braw)
    assert "peer-b" not in node._wan_backoff


# MARK: - transport preference


def test_iroh_is_preferred_over_tor_when_a_peer_advertises_both(tmp_path, monkeypatch):
    """A peer reachable both ways is dialed over iroh: an operator who turned it on
    wants that path, and it connects in well under the time Tor spends building a
    rendezvous circuit. One dial per peer per tick, so the choice is exclusive."""
    import time as _time

    node = _fresh_node(tmp_path, monkeypatch, SZPONTNET_DEFAULT_TRUST="personal")
    node.local = _dc_replace(node.local, id="0" * 32)
    target = "z" * 32
    node._wan_cache = {target: wancache.WanEntry(endpoint=_EP_B, onion=_ONION_A)}
    _install(node, ("iroh", _FakeIroh(_EP_A)), ("tor", _FakeIroh(_ONION_A)))
    targets = node._wan_redial_targets(_time.monotonic())
    assert [(pid, w.name, addr) for pid, w, addr in targets] == [
        (target, "iroh", _EP_B)]


def test_tor_still_carries_a_peer_that_advertises_no_endpoint(tmp_path, monkeypatch):
    """Preference is not exclusion: a peer that runs only Tor stays reachable over
    it, which is what lets one node opt into iroh without cutting off the rest."""
    import time as _time

    node = _fresh_node(tmp_path, monkeypatch, SZPONTNET_DEFAULT_TRUST="personal")
    node.local = _dc_replace(node.local, id="0" * 32)
    target = "z" * 32
    node._wan_cache = {target: wancache.WanEntry(onion=_ONION_A)}
    _install(node, ("iroh", _FakeIroh(_EP_A)), ("tor", _FakeIroh(_ONION_A)))
    targets = node._wan_redial_targets(_time.monotonic())
    assert [(pid, w.name, addr) for pid, w, addr in targets] == [
        (target, "tor", _ONION_A)]


def test_a_transport_that_is_not_ready_is_skipped_for_the_next_one(tmp_path,
                                                                   monkeypatch):
    """A transport whose address() is None is not up (still binding, or dead). It
    must not be chosen — the peer falls through to the next transport that is, and
    is dialed over none when nothing is ready."""
    import time as _time

    node = _fresh_node(tmp_path, monkeypatch, SZPONTNET_DEFAULT_TRUST="personal")
    node.local = _dc_replace(node.local, id="0" * 32)
    target = "z" * 32
    node._wan_cache = {target: wancache.WanEntry(endpoint=_EP_B, onion=_ONION_A)}
    _install(node, ("iroh", _FakeIroh(None)), ("tor", _FakeIroh(_ONION_A)))
    assert [w.name for _p, w, _a in node._wan_redial_targets(_time.monotonic())] == [
        "tor"]
    _install(node, ("iroh", _FakeIroh(None)), ("tor", _FakeIroh(None)))
    assert node._wan_redial_targets(_time.monotonic()) == []


def test_iroh_asked_for_but_unavailable_reports_enabled_without_ready(tmp_path,
                                                                      monkeypatch):
    """The state a node is in when the operator turned iroh on and no endpoint
    bound — the missing package, or an endpoint that never came online.

    Note the combination — ``enabled`` true, ``ready`` false — which is the honest
    one for "asked for, unavailable" and the pair a UI needs to say so. It holds only
    because ``enabled`` reports the *operator's* switch rather than the live
    transport; sourcing it from ``self.iroh`` would collapse the two into one flag
    and report the request as never made. Mirrors the Tor twin, which asserts the
    same pair against a real node with no daemon to run
    (``test_a_node_on_a_machine_with_no_tor_installed_still_runs``, test_tor_e2e.py).
    """
    node = _fresh_node(tmp_path, monkeypatch, SZPONTNET_IROH="1")
    assert node.iroh is None  # nothing bound, exactly as a missing package leaves it

    state = node.snapshot()
    assert state["iroh"]["enabled"] is True    # the operator asked for it…
    assert state["iroh"]["ready"] is False     # …and nothing answered
    assert state["iroh"]["endpoint"] is None
    assert "endpoint" not in state["self"]


# MARK: - security: a WAN link serves peer links, never operator control (ctl)


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


def test_ctl_over_any_wan_transport_is_refused(tmp_path, monkeypatch):
    """A WAN address is advertised to every mesh peer and can be pasted around, so
    serving ctl over one would expose the whole node-control surface — in an OPEN
    mesh, unauthenticated.

    The gate asks "is this a LAN link?", not "is this Tor?", which is what makes a
    transport added later refused by default instead of silently served. Both
    transports are checked here for exactly that reason: a gate naming only one of
    them passes half this test."""
    node = _fresh_node(tmp_path, monkeypatch)
    served: list[str] = []

    async def _fake_run_ctl(_reader, _writer):
        served.append("ctl")

    monkeypatch.setattr(node, "_run_ctl", _fake_run_ctl)
    ctl_line = protocol.encode({"t": "ctl"})

    for transport in ("iroh", "tor"):
        served.clear()
        w = _RecWriter()
        node._link_transport[w] = transport
        asyncio.run(node._on_tcp_connection(_LineReader([ctl_line]), w))
        assert served == [], f"a ctl session over {transport} must never be served"
        assert w.closed, f"a ctl session over {transport} must be closed"

    # LAN/loopback inbound ctl (untagged) → served, exactly as before: the gate must
    # refuse the WAN without also cutting off the operator's own channel.
    lan_w = _RecWriter()
    asyncio.run(node._on_tcp_connection(_LineReader([ctl_line]), lan_w))
    assert served == ["ctl"]


def test_iroh_inbound_closing_before_a_hello_does_not_leak_the_transport_map(
        tmp_path, monkeypatch):
    """An inbound connection tagged by _on_wan_inbound that closes before a valid
    hello never reaches _run_link's pop, so the wrapper must pop the tag itself."""
    node = _fresh_node(tmp_path, monkeypatch)

    class _EOFReader:
        async def readline(self):
            return b""

    asyncio.run(node._on_wan_inbound("iroh", _EOFReader(), _FakeWriter()))
    assert node._link_transport == {}


# MARK: - the shared WAN cache


def test_wan_cache_migrates_the_legacy_tor_only_file(tmp_path, monkeypatch):
    """Upgrading a node that only ever knew onions must not forget its peers:
    ``wan.json`` is authoritative, and ``onions.json`` is read when it is absent."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    (tmp_path / "onions.json").write_text(json.dumps({
        "peer-old": {"onion": _ONION_A, "fingerprint": "ff"}}), encoding="utf-8")
    loaded = wancache.load()
    assert loaded["peer-old"] == wancache.WanEntry(onion=_ONION_A, fingerprint="ff")
    # Once this node has written its own file, that one wins and the legacy file is
    # ignored — otherwise a deleted peer would keep coming back.
    wancache.save({"peer-new": wancache.WanEntry(endpoint=_EP_A)})
    assert set(wancache.load()) == {"peer-new"}


def test_wan_cache_roundtrips_and_tolerates_garbage(tmp_path, monkeypatch):
    """A best-effort accelerator like peers.json: malformed entries are dropped
    rather than raised on, and an entry naming no address at all is not kept."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    cache = {"a": wancache.WanEntry(endpoint=_EP_A, fingerprint="ff"),
             "b": wancache.WanEntry(onion=_ONION_A),
             "c": wancache.WanEntry(endpoint=_EP_B, onion=_ONION_A)}
    wancache.save(cache)
    assert wancache.load() == cache
    wancache.path().write_text(json.dumps({
        "ok": {"endpoint": _EP_A},
        "empty": {"endpoint": "", "onion": ""},   # names nowhere to dial
        "notadict": 5,
        "wrongtype": {"endpoint": 17},
    }), encoding="utf-8")
    assert set(wancache.load()) == {"ok"}
    wancache.path().write_text("not json", encoding="utf-8")
    assert wancache.load() == {}


# MARK: - config


def test_iroh_stays_off_unless_it_is_explicitly_turned_on(monkeypatch):
    """iroh ships beside Tor rather than in place of it, so an operator who never
    asks for it must never get an endpoint: a node's WAN transport does not change
    under it on an upgrade.

    That makes it an opt-in knob, and an opt-in knob recognises only the on-spellings:
    anything else — a typo, a stray quote, the word ``enabled`` — leaves it off, which
    is the direction that fails safe. (``SZPONTNET_TOR`` is the default-ON knob, and
    honours a lenient off-list for the mirror-image reason — see test_mesh_tor.py.)"""
    from szpontnet import config

    monkeypatch.delenv("SZPONTNET_IROH", raising=False)
    assert config.iroh_enabled() is False

    for on in ("1", "true", "yes", "on", "TRUE", " On ", "Yes"):
        monkeypatch.setenv("SZPONTNET_IROH", on)
        assert config.iroh_enabled() is True, f"{on!r} should enable iroh"

    for off in ("0", "false", "no", "off", "", "enabled", "sure", "2"):
        monkeypatch.setenv("SZPONTNET_IROH", off)
        assert config.iroh_enabled() is False, f"{off!r} should leave iroh off"


def test_the_legacy_env_name_can_still_turn_iroh_on(monkeypatch):
    """``DIPLOMAT_MESH_*`` is honoured wherever ``SZPONTNET_*`` is unset (see env.py),
    and that has to include this switch: the old spelling is how a machine that
    already opted into iroh keeps its endpoint across the rename."""
    from szpontnet import config

    monkeypatch.delenv("SZPONTNET_IROH", raising=False)
    monkeypatch.setenv("DIPLOMAT_MESH_IROH", "1")
    assert config.iroh_enabled() is True
    # The new name still wins when both are set.
    monkeypatch.setenv("SZPONTNET_IROH", "0")
    assert config.iroh_enabled() is False


def test_iroh_online_timeout_rejects_non_finite(monkeypatch):
    """A non-finite timeout would make the online wait block FOREVER — the opposite
    of "give up and stay LAN-only" — and a non-positive one is meaningless."""
    from szpontnet import config

    monkeypatch.delenv("SZPONTNET_IROH_ONLINE_SECS", raising=False)
    assert config.iroh_online_timeout() == 30.0
    for bad in ("1e999", "-1e999", "nan", "-1", "0", "abc", ""):
        monkeypatch.setenv("SZPONTNET_IROH_ONLINE_SECS", bad)
        assert config.iroh_online_timeout() == 30.0, f"{bad!r} should fall back"
    monkeypatch.setenv("SZPONTNET_IROH_ONLINE_SECS", "12.5")
    assert config.iroh_online_timeout() == 12.5
