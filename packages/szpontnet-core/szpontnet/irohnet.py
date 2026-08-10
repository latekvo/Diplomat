"""Iroh QUIC transport — SzpontNet's WAN reachability, no public IP, no daemon.

The default WAN transport, and the second implementation of the *atomic,
exchangeable* transport seam :mod:`tor` defined: the rest of the node speaks to it
through ``address`` (what to advertise), ``dial`` (open a stream to a peer), and
``start``/``stop``. Remove or disable it and the node is LAN-only, unchanged.

An **endpoint id is an Ed25519 public key**, so a peer is dialled by *identity*
rather than by location: iroh hole-punches a direct QUIC path where the two
networks allow it and falls back to a relay where they do not, and neither the
address nor the code above the socket changes when the path does. That is the
whole reason this transport is preferred over :mod:`tor` — an onion is also a key,
but reaching it costs a local ``tor`` daemon, a multi-minute bootstrap, and a
rendezvous circuit per dial, all to buy an anonymity property the mesh never asks
for.

How it plugs in, mirroring the Tor transport exactly:

- **Inbound adds no protocol surface.** Each accepted QUIC connection's first
  bidirectional stream is adapted to a plain ``(reader, writer)`` pair and handed
  to the *same* accept path a LAN link uses, running the identical
  hello/auth/trust handshake. The node tags such a link ``iroh`` so its endpoint
  stays out of the LAN redial cache and operator control (``ctl``) sessions are
  refused over it — the same two things the ``tor`` tag buys.
- **Outbound** opens a QUIC connection to the peer's endpoint id and takes the
  first bi-stream. The adapted stream goes to the same link pump a LAN dial uses.

The endpoint **key is persisted** in ``<mesh_dir>/iroh/endpoint.key`` (0600), so
the endpoint id is *permanent* across restarts — the stable, NAT-independent
handle peers redial. It is deliberately NOT the mesh's ``device.key``: iroh signs
its TLS handshake with this key while the mesh signs nonce challenges with the
device key, and one key serving two protocols is a cross-protocol reuse this
buys nothing for. The endpoint id is bound to the device key the same way the
onion is — by riding inside the *signed* advertisement.

On by default, disabled with ``SZPONTNET_IROH=0`` (see
:func:`config.iroh_enabled`). If the ``iroh`` package is not installed the node
carries on LAN-only, the same graceful degradation as a missing ``tor`` binary or
a missing ``cryptography``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path

from . import config, protocol
from .host import log

try:  # optional, like `cryptography` for trust: absent → the node is LAN-only
    import iroh as _iroh
except Exception:  # noqa: BLE001 — no wheel for this platform is a normal box
    _iroh = None

# The application-layer protocol name QUIC negotiates. Versioned with the wire
# protocol so a future incompatible framing can be introduced without a v1 node
# ever accepting it: an ALPN mismatch is refused by QUIC before any bytes are read.
ALPN = b"szpontnet/1"

# An endpoint id on the wire: 64 lowercase hex chars (a 32-byte Ed25519 public
# key). ``\Z`` (not ``$``) so a trailing newline can never sneak through.
_ENDPOINT_RE = re.compile(r"^[0-9a-f]{64}\Z")

# Read chunk for the inbound pump. Independent of MAX_LINE_BYTES: the reader
# reassembles lines, so this only trades syscalls against buffer size.
_READ_CHUNK = 65536

# How long wait_closed() gives the write pump to drain before abandoning it.
_CLOSE_TIMEOUT = 5.0


def available() -> bool:
    """Whether the ``iroh`` package is importable on this machine."""
    return _iroh is not None


def is_endpoint(addr: str) -> bool:
    """Whether ``addr`` is exactly a valid endpoint id (no scheme, no port)."""
    return bool(_ENDPOINT_RE.match(addr))


def normalize_endpoint(addr: object) -> str:
    """Extract a valid endpoint id from a (possibly pasted) string, or ``""``.

    Lenient on input so an operator can paste an ``iroh://<id>`` URL or a padded
    id, strict on output: the result is either a canonical lowercase 64-hex id or
    the empty string, never a partial one that would fail a dial confusingly.
    The twin of :func:`tor.normalize_onion`."""
    if not isinstance(addr, str):
        return ""
    s = addr.strip().lower()
    s = s.split("://", 1)[-1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split(":", 1)[0]
    s = s.split("@", 1)[-1]
    return s if is_endpoint(s) else ""


def _load_or_create_key(key_path: Path) -> bytes:
    """The persisted 32-byte endpoint secret, minting one on first run.

    Written 0600 inside a 0700 directory: this key *is* the node's WAN address, so
    a peer that steals it can be dialled as us. A short/corrupt file is replaced
    rather than raised on — a node that cannot read its own key still has to come
    up, and the cost is a new endpoint id that peers relearn from the next signed
    advert."""
    with contextlib.suppress(OSError):
        raw = key_path.read_bytes()
        if len(raw) == 32:
            return raw
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(key_path.parent, 0o700)
    secret = os.urandom(32)
    # Write via a 0600 fd rather than write_bytes + chmod, so the secret is never
    # briefly world-readable on a shared box.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


class IrohStreamWriter:
    """The ``StreamWriter`` half of the adapter over an iroh bidirectional stream.

    Only the surface the link layer actually uses (``write``/``drain``/``close``/
    ``wait_closed``/``get_extra_info``), with asyncio's semantics preserved where
    they matter: ``write`` is fire-and-forget and a background pump flushes it, so
    the many call sites that write without awaiting ``drain`` still put bytes on
    the wire. Hashable by identity, because the node keys per-link state
    (``_link_transport``, ``_issued_nonce``) on the writer object."""

    def __init__(self, send, conn, peer_id: str) -> None:
        self._send = send
        self._conn = conn
        self._peer = peer_id
        self._buf = bytearray()
        self._wake = asyncio.Event()
        self._flushed = asyncio.Event()
        self._flushed.set()
        self._closing = False
        self._error: BaseException | None = None
        self._done = asyncio.Event()
        self._pump = asyncio.get_running_loop().create_task(
            self._pump_loop(), name="mesh-iroh-write")
        self._reader_pump: asyncio.Task | None = None

    def attach_reader_pump(self, task: asyncio.Task) -> None:
        """Hold the paired inbound pump for the life of this writer — see
        :func:`_reader_over` for why it needs an owner."""
        self._reader_pump = task

    async def _pump_loop(self) -> None:
        try:
            while True:
                if not self._buf:
                    if self._closing:
                        return
                    self._flushed.set()
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                chunk = bytes(self._buf)
                del self._buf[:len(chunk)]
                await self._send.write_all(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — surfaced to drain()/close()
            self._error = exc
        finally:
            # Wake anyone in drain() whatever happened, or a writer that errors
            # mid-flush strands its drainer until the link times out.
            self._flushed.set()
            self._done.set()

    def write(self, data: bytes) -> None:
        if self._closing:
            return
        self._buf.extend(data)
        self._flushed.clear()
        self._wake.set()

    async def drain(self) -> None:
        """Block until everything written so far has been handed to QUIC. Raises
        the pump's error as a ``ConnectionError`` so callers that already guard
        ``drain`` against ``(ConnectionError, OSError)`` need no new except arm."""
        await self._flushed.wait()
        if self._error is not None:
            raise ConnectionResetError(f"iroh link to {self._peer[:12]} failed: "
                                       f"{self._error}")

    def close(self) -> None:
        self._closing = True
        self._wake.set()

    async def wait_closed(self) -> None:
        self.close()
        # Bounded: the pump normally ends the moment it sees _closing with an empty
        # buffer, but one blocked in write_all on a dead connection would never get
        # there, and a caller closing a link must not be able to hang on it.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self._done.wait(), _CLOSE_TIMEOUT)
        with contextlib.suppress(Exception):
            await self._send.finish()
        with contextlib.suppress(Exception):
            await self._conn.close(0, b"bye")

    def get_extra_info(self, name: str, default=None):
        # The node reads ("peername")[0] as the link's display host; the endpoint
        # id is this link's stable address, so it shows the way an onion does.
        if name == "peername":
            return (self._peer, 0)
        return default


def _reader_over(recv, writer: IrohStreamWriter) -> asyncio.StreamReader:
    """A real :class:`asyncio.StreamReader` fed from an iroh receive stream.

    A genuine StreamReader rather than a shim, so ``readline``'s framing and its
    over-limit ``ValueError`` are byte-for-byte what the TCP path produces — the
    NDJSON line budget is enforced identically on every transport."""
    reader = asyncio.StreamReader(limit=protocol.MAX_LINE_BYTES)

    async def pump() -> None:
        try:
            while True:
                chunk = await recv.read(_READ_CHUNK)
                if not chunk:
                    break
                reader.feed_data(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 — a dead stream is an EOF to the pump
            pass
        finally:
            reader.feed_eof()
            # The peer is gone; stop the write pump too so a half-open link cannot
            # sit forever holding its task.
            writer.close()

    # Parked on the writer, which the node holds for the life of the link: the loop
    # keeps only a WEAK reference to a task, so an unreferenced pump can be collected
    # mid-read and silently stop feeding the reader.
    writer.attach_reader_pump(
        asyncio.get_running_loop().create_task(pump(), name="mesh-iroh-read"))
    return reader


class IrohTransport:
    """A node's iroh endpoint: one persistent identity + a QUIC dialer."""

    def __init__(self, mesh_dir: Path) -> None:
        self._dir = Path(mesh_dir) / "iroh"
        self._endpoint = None
        self._accept_task: asyncio.Task | None = None
        # Strong refs to the in-flight inbound handlers, so the loop can't collect one
        # mid-handshake (it keeps only weak references to tasks).
        self._inbound: set[asyncio.Task] = set()
        self._id = ""

    # MARK: - what the node advertises

    def address(self) -> str | None:
        """The permanent endpoint id this node accepts on, or None before it is
        online and None AGAIN once the endpoint is closed — so the node stops
        advertising an endpoint nobody can reach, exactly as
        :meth:`tor.TorTransport.onion_address` stops advertising a dead onion.
        Advertised verbatim in the signed NodeInfo."""
        if not self._id or self._endpoint is None:
            return None
        with contextlib.suppress(Exception):
            if self._endpoint.is_closed():
                return None
        return self._id

    # MARK: - lifecycle

    async def start(self, inbound_handler, *,
                    online_timeout: float | None = None) -> bool:
        """Bind the endpoint and return True once it is reachable (published, with
        a home relay); False on any failure — the caller then runs LAN-only. Never
        raises.

        ``inbound_handler(reader, writer)`` receives every connection that arrives
        over iroh, already adapted to the stream pair the LAN accept path takes."""
        if _iroh is None:
            return False
        if online_timeout is None:
            online_timeout = config.iroh_online_timeout()
        try:
            _iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
            secret = _load_or_create_key(self._dir / "endpoint.key")
            self._endpoint = await _iroh.Endpoint.bind(_iroh.EndpointOptions(
                secret_key=secret, alpns=[ALPN], preset=_iroh.preset_n0()))
            self._id = str(self._endpoint.id())
        except Exception as exc:  # noqa: BLE001 — any bind failure is LAN-only
            log("warn", f"Mesh/Iroh: cannot bind the endpoint ({exc})")
            await self.stop()
            return False
        self._accept_task = asyncio.get_running_loop().create_task(
            self._accept_loop(inbound_handler), name="mesh-iroh-accept")
        try:
            # "Online" is this transport's bootstrap: the endpoint has a working
            # network path and its address is published, so a peer holding only the
            # id can actually find us. Advertising before that would hand peers an
            # id that resolves to nothing.
            await asyncio.wait_for(self._endpoint.online(), online_timeout)
        except Exception as exc:  # noqa: BLE001 — incl. the wait_for timeout
            log("warn", f"Mesh/Iroh: endpoint did not come online ({exc}) — "
                        "staying LAN-only")
            await self.stop()
            return False
        log("mesh-up", f"Mesh/Iroh: endpoint up — {self._id}")
        return True

    async def _accept_loop(self, inbound_handler) -> None:
        """Accept connections forever, handing each one's first bi-stream to the
        node's normal accept path. One connection carries one link, matching the
        one-TCP-connection-per-link shape the rest of the node assumes."""
        assert self._endpoint is not None
        while True:
            try:
                incoming = await self._endpoint.accept_next()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                log("warn", f"Mesh/Iroh: accept loop stopped ({exc})")
                return
            if incoming is None:
                return  # the endpoint closed
            task = asyncio.get_running_loop().create_task(
                self._serve_one(incoming, inbound_handler),
                name="mesh-iroh-inbound")
            self._inbound.add(task)
            task.add_done_callback(self._inbound.discard)

    async def _serve_one(self, incoming, inbound_handler) -> None:
        try:
            conn = await (await incoming.accept()).connect()
            peer = str(conn.remote_id())
            bi = await conn.accept_bi()
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 — a failed handshake is not our problem
            return
        writer = IrohStreamWriter(bi.send(), conn, peer)
        reader = _reader_over(bi.recv(), writer)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await inbound_handler(reader, writer)

    async def stop(self) -> None:
        """Close the endpoint and its accept loop. Best-effort, never raises."""
        if self._accept_task is not None:
            self._accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._accept_task
            self._accept_task = None
        for task in list(self._inbound):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._inbound.clear()
        if self._endpoint is not None:
            with contextlib.suppress(Exception):
                await self._endpoint.close()
            self._endpoint = None

    # MARK: - outbound: a QUIC connection to a peer's endpoint id

    async def dial(self, endpoint_id: str) -> tuple[asyncio.StreamReader,
                                                    IrohStreamWriter]:
        """Open a stream to ``endpoint_id`` and return the ``(reader, writer)`` pair
        — indistinguishable from a LAN-dialed stream to the caller. The peer is
        located by discovery from the id alone, so no address is needed or kept.
        Raises on any failure (unreachable peer, refused ALPN, endpoint not
        started); the caller treats that as "try again later"."""
        peer = normalize_endpoint(endpoint_id)
        if not peer:
            raise ValueError(f"not a valid iroh endpoint id: {endpoint_id!r}")
        if self._endpoint is None:
            raise RuntimeError("iroh transport is not started")
        addr = _iroh.EndpointAddr(_iroh.EndpointId.from_string(peer), None, [])
        conn = await self._endpoint.connect(addr, ALPN)
        try:
            bi = await conn.open_bi()
        except BaseException:
            with contextlib.suppress(Exception):
                await conn.close(0, b"dial-failed")
            raise
        writer = IrohStreamWriter(bi.send(), conn, peer)
        return _reader_over(bi.recv(), writer), writer
