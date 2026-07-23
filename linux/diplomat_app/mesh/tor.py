"""Tor v3 onion-service transport — SzpontNet's WAN reachability, no public IP.

This is the *atomic, exchangeable* transport the mesh dials over when a peer is
not on the local LAN. It is deliberately self-contained: the rest of the node
speaks only to :class:`TorTransport` through a two-method seam — ``onion_address``
(what to advertise) and ``dial`` (open a stream to a peer's onion) — plus
``start``/``stop``. Remove or disable it and the node is LAN-only, unchanged.

How it plugs in with almost no new surface:

- **Inbound** adds no *protocol* surface. The onion service forwards its virtual
  port to a small DEDICATED loopback listener this transport owns
  (``HiddenServicePort <ONION_VIRTPORT> 127.0.0.1:<forward-port>``), which hands the
  stream straight to the *same* accept path a LAN link uses (``_on_tcp_connection``)
  and runs the identical hello/auth/trust handshake — so "behaves exactly like the
  LAN" is free. The dedicated listener (rather than reusing the node's shared TCP
  port) is what lets an inbound Tor link be TAGGED ``tor``, which the node relies on
  to keep a Tor link's endpoint out of the LAN redial cache and to refuse operator
  control (``ctl``) sessions over the onion. See ``node._on_tor_inbound``.
- **Outbound** is the only genuinely new primitive: a minimal, dependency-free
  SOCKS5 CONNECT through the local ``tor`` process's SOCKS port to
  ``<peer-onion>:<ONION_VIRTPORT>``. The resulting stream is handed to the same
  link pump a LAN dial uses.

The onion **key is persisted** in ``<mesh_dir>/tor/onion/`` (Tor's
``HiddenServiceDir``), so the ``.onion`` address is *permanent* across restarts —
the stable, NAT-independent handle peers redial. Tor itself is spawned as a child
process; if the ``tor`` binary is missing or bootstrap fails, ``start`` returns
False and the node carries on LAN-only (the same graceful degradation as the
keyless path when ``cryptography`` is absent).

Enable with ``DIPLOMAT_MESH_TOR=1`` (see :mod:`config`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from pathlib import Path

from .. import activity
from . import protocol

# The virtual port the onion service exposes. It is namespaced to the onion and
# binds no real socket, so its exact value is arbitrary — but the service
# definition (HiddenServicePort) and the dialer MUST agree on it. The conventional
# onion virtport; nothing on the host ever binds it, so it deliberately does NOT
# reuse the mesh's real TCP port range.
ONION_VIRTPORT = 80

# A v3 onion address: 56 chars of base32 (a-z, 2-7) + ".onion". v2 (16 chars) is
# dead and deliberately not matched. ``\Z`` (not ``$``) so a trailing newline can
# never sneak through, whatever the caller — normalize_onion strips first anyway.
_ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion\Z")


def is_onion(addr: str) -> bool:
    """Whether ``addr`` is exactly a valid v3 onion hostname (no scheme/port)."""
    return bool(_ONION_RE.match(addr))


def normalize_onion(addr: object) -> str:
    """Extract a valid v3 onion hostname from a (possibly pasted) string, or ``""``.

    Lenient on input so an operator can paste ``http://<hash>.onion/``,
    ``<hash>.onion:1234``, or surrounding whitespace, and strict on output: the
    result is either a canonical lowercase ``<hash>.onion`` or the empty string
    (never a partial/invalid address that would later fail a dial confusingly)."""
    if not isinstance(addr, str):
        return ""
    s = addr.strip().lower()
    # Drop a scheme (tor+http://, http://, …) and everything after the host.
    s = s.split("://", 1)[-1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split(":", 1)[0]  # drop any :port
    s = s.split("@", 1)[-1]  # drop any user@ prefix
    return s if is_onion(s) else ""


def binary() -> str | None:
    """The resolved ``tor`` executable path, or None if not installed. Honors
    ``DIPLOMAT_MESH_TOR_BINARY`` for a non-PATH install."""
    return shutil.which(os.environ.get("DIPLOMAT_MESH_TOR_BINARY", "tor"))


def _write_torrc(tor_dir: Path, socks_port: int, forward_to_port: int) -> Path:
    """Render a minimal torrc for our own private Tor: a client SOCKS port for
    outbound dials, and one persistent onion service forwarding ONION_VIRTPORT to
    the node's loopback Tor forward-listener. Everything lives under ``tor_dir`` so
    several nodes on one host (each its own DIPLOMAT_MESH_DIR) never collide."""
    data_dir = tor_dir
    hs_dir = tor_dir / "onion"
    # Tor refuses a DataDirectory / HiddenServiceDir that is not 0700. Create them
    # 0700 from the start (mode is umask-masked, hence the belt-and-braces chmod) so
    # the onion PRIVATE KEY dir is never briefly group/other-readable on a shared box.
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(data_dir, 0o700)
    hs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(hs_dir, 0o700)
    torrc = tor_dir / "torrc"
    torrc.write_text(
        f"SocksPort 127.0.0.1:{socks_port}\n"
        f"DataDirectory {data_dir}\n"
        f"HiddenServiceDir {hs_dir}\n"
        f"HiddenServicePort {ONION_VIRTPORT} 127.0.0.1:{forward_to_port}\n"
        # Client-only, quiet, and log bootstrap to stdout so start() can detect
        # readiness without a control port.
        "Log notice stdout\n"
        "ClientOnly 1\n"
        "AvoidDiskWrites 1\n",
        encoding="utf-8",
    )
    return torrc


class TorTransport:
    """A node's private Tor process: one persistent onion service + a SOCKS dialer."""

    def __init__(self, mesh_dir: Path, *, binary_path: str) -> None:
        self._tor_dir = Path(mesh_dir) / "tor"
        self._binary = binary_path
        self._socks_port = 0  # a free ephemeral port, picked at start()
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task | None = None
        # A dedicated loopback listener the onion service forwards to, so an inbound
        # Tor link is distinguishable from a LAN one (see start()).
        self._forward_server: asyncio.base_events.Server | None = None
        self._bootstrapped = asyncio.Event()
        self._onion = ""

    # MARK: - what the node advertises

    def onion_address(self) -> str | None:
        """The permanent ``<hash>.onion`` this node listens on, or None until the
        service is up. Advertised verbatim in the signed NodeInfo."""
        return self._onion or None

    # MARK: - lifecycle

    async def start(self, inbound_handler, *,
                    bootstrap_timeout: float = 90.0) -> bool:
        """Spawn Tor behind a private onion service and return True once it is usable
        (onion known, SOCKS live); False on any failure — the caller then runs
        LAN-only. Never raises.

        ``inbound_handler(reader, writer)`` receives every connection that arrives
        over Tor. The onion forwards ONION_VIRTPORT to a DEDICATED loopback listener
        we own here (not the node's shared TCP port), so the node can TAG an inbound
        Tor link as ``tor`` instead of it being indistinguishable from a loopback LAN
        link on the shared listener."""
        if not self._binary:
            return False
        self._socks_port = _free_port()
        try:
            self._forward_server = await asyncio.start_server(
                inbound_handler, "127.0.0.1", 0, limit=protocol.MAX_LINE_BYTES)
        except OSError as exc:
            activity.log("mesh", "warn",
                         f"Mesh/Tor: cannot open the forward listener ({exc})")
            return False
        forward_port = self._forward_server.sockets[0].getsockname()[1]
        try:
            torrc = _write_torrc(self._tor_dir, self._socks_port, forward_port)
        except OSError as exc:
            activity.log("mesh", "warn", f"Mesh/Tor: cannot write torrc ({exc})")
            await self.stop()
            return False
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._binary, "-f", str(torrc),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Match the transport's line budget instead of asyncio's 64KB default:
                # a stdout line longer than the reader's limit makes readline() raise
                # LimitOverrunError, which would kill _pump_stdout — after which tor's
                # stdout pipe fills and tor blocks. Real tor log lines are short, so
                # this only removes a latent foot-gun (a chatty/hostile binary).
                limit=protocol.MAX_LINE_BYTES,
            )
        except OSError as exc:
            activity.log("mesh", "warn", f"Mesh/Tor: cannot launch tor ({exc})")
            await self.stop()
            return False
        # Drain stdout forever (so tor never blocks on a full pipe) and flip the
        # bootstrap event when Tor reports it is fully connected.
        self._pump_task = asyncio.get_running_loop().create_task(
            self._pump_stdout(), name="mesh-tor-stdout")
        if not await self._await_bootstrap(bootstrap_timeout):
            await self.stop()
            return False
        onion = await self._read_hostname()
        if not onion:
            activity.log("mesh", "warn",
                         "Mesh/Tor: bootstrapped but no onion hostname — LAN-only")
            await self.stop()
            return False
        self._onion = onion
        activity.log("mesh", "mesh-up",
                     f"Mesh/Tor: onion service up — {onion} (SOCKS :{self._socks_port})")
        return True

    async def _await_bootstrap(self, timeout: float) -> bool:
        """Wait for 'Bootstrapped 100%', but FAIL FAST if the tor process exits first
        (a bad torrc, a crash) OR the stdout pump dies, instead of blocking the whole
        timeout. The pump (``_pump_stdout``) is what *sets* ``_bootstrapped``, so a
        dead pump means the bootstrap line can never be observed — waiting out the
        full timeout for a signal that will never arrive is pointless (it would stall
        the node's Tor bring-up for the entire ``bootstrap_timeout``)."""
        boot = asyncio.ensure_future(self._bootstrapped.wait())
        dead = asyncio.ensure_future(self._proc.wait())
        # The pump task is owned by stop() (which retrieves its result); watch it for
        # completion here but NEVER cancel it — only boot/dead are ours to cancel.
        pump = self._pump_task
        waits = {boot, dead} | ({pump} if pump is not None else set())
        try:
            done, _pending = await asyncio.wait(
                waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (boot, dead):
                if not t.done():
                    t.cancel()
        if self._bootstrapped.is_set():
            return True
        if dead in done:
            why = "tor exited during bootstrap"
        elif pump is not None and pump in done:
            why = "tor stdout pump stopped during bootstrap"
        else:
            why = "bootstrap timed out"
        activity.log("mesh", "warn", f"Mesh/Tor: {why} — staying LAN-only")
        return False

    async def _pump_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace")
            if "Bootstrapped 100%" in text:
                self._bootstrapped.set()
            elif "[warn]" in text or "[err]" in text:
                activity.log("mesh", "warn", f"Mesh/Tor: {text.strip()[:200]}")

    async def _read_hostname(self, tries: int = 20, delay: float = 0.25) -> str:
        """Read ``<HiddenServiceDir>/hostname`` (written by tor once the service is
        configured). Retried briefly: it can lag the bootstrap line by a moment."""
        hostname = self._tor_dir / "onion" / "hostname"
        for _ in range(tries):
            try:
                got = normalize_onion(hostname.read_text(encoding="utf-8"))
                if got:
                    return got
            except OSError:
                pass
            await asyncio.sleep(delay)
        return ""

    async def stop(self) -> None:
        """Terminate the tor child, its stdout pump, and the forward listener.
        Best-effort, never raises."""
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._proc.kill()
            self._proc = None
        if self._forward_server is not None:
            self._forward_server.close()
            with contextlib.suppress(Exception):
                await self._forward_server.wait_closed()
            self._forward_server = None

    # MARK: - outbound: SOCKS5 CONNECT through our tor to a peer's onion

    async def dial(self, onion: str) -> tuple[asyncio.StreamReader,
                                              asyncio.StreamWriter]:
        """Open a stream to ``<onion>:<ONION_VIRTPORT>`` through the local tor SOCKS
        port. Returns the tunneled ``(reader, writer)`` — indistinguishable from a
        LAN-dialed stream to the caller. Raises on any failure (unreachable onion,
        SOCKS error, tor not started); the caller treats that as "try again later"."""
        host = normalize_onion(onion)
        if not host:
            raise ValueError(f"not a valid v3 onion address: {onion!r}")
        if self._socks_port <= 0:
            raise RuntimeError("tor transport is not started")
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self._socks_port, limit=protocol.MAX_LINE_BYTES)
        try:
            await _socks5_connect(reader, writer, host, ONION_VIRTPORT)
            return reader, writer
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise


def _free_port() -> int:
    """A currently-free loopback TCP port (best-effort; a small TOCTOU window is
    fine — a failed SOCKS bind just fails start() and the node stays LAN-only)."""
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _socks5_connect(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter, host: str,
                          port: int) -> None:
    """A minimal SOCKS5 no-auth CONNECT to a domain-name destination (RFC 1928).
    Dependency-free (no PySocks) — the destination is a hostname so tor resolves
    the onion internally. Raises OSError on any protocol-level refusal."""
    # Greeting: SOCKS5, one method offered, "no authentication required".
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    ver, method = await reader.readexactly(2)
    if ver != 0x05 or method != 0x00:
        raise OSError("tor SOCKS5 rejected the no-auth method")
    host_b = host.encode("ascii")
    if len(host_b) > 255:
        raise OSError("onion hostname too long for SOCKS5")
    # CONNECT (cmd 0x01), reserved 0x00, address type domain (0x03).
    writer.write(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                 + port.to_bytes(2, "big"))
    await writer.drain()
    ver, rep, _rsv, atyp = await reader.readexactly(4)
    if ver != 0x05 or rep != 0x00:
        # rep 0x04 = host unreachable (onion down / descriptor not yet published),
        # 0x01 = general failure, etc. — all "not reachable right now".
        raise OSError(f"tor SOCKS5 CONNECT failed (reply {rep:#04x})")
    # Consume the bound-address the server echoes, per its address type.
    if atyp == 0x01:      # IPv4
        await reader.readexactly(4 + 2)
    elif atyp == 0x03:    # domain
        ln = (await reader.readexactly(1))[0]
        await reader.readexactly(ln + 2)
    elif atyp == 0x04:    # IPv6
        await reader.readexactly(16 + 2)
    else:
        raise OSError(f"tor SOCKS5 sent an unknown address type {atyp:#04x}")
