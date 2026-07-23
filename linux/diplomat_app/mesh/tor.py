"""Tor v3 onion-service transport — SzpontNet's WAN reachability, no public IP.

This is the *atomic, exchangeable* transport the mesh dials over when a peer is
not on the local LAN. It is deliberately self-contained: the rest of the node
speaks only to :class:`TorTransport` through a two-method seam — ``onion_address``
(what to advertise) and ``dial`` (open a stream to a peer's onion) — plus
``start``/``stop``. Remove or disable it and the node is LAN-only, unchanged.

How it plugs in with almost no new surface:

- **Inbound** needs no new listener. The onion service forwards its virtual port
  to the node's *existing* loopback TCP listener (``HiddenServicePort
  <ONION_VIRTPORT> 127.0.0.1:<tcpPort>``), so a connection arriving over Tor lands
  on the same accept path as a LAN link and runs the identical hello/auth/trust
  handshake. "Behaves exactly like the LAN" is therefore free.
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
# definition (HiddenServicePort) and the dialer MUST agree on it. Kept distinct
# and named rather than a bare 80 so a reader sees it is the mesh's own port.
ONION_VIRTPORT = 40878

# A v3 onion address: 56 chars of base32 (a-z, 2-7) + ".onion". v2 (16 chars) is
# dead and deliberately not matched.
_ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")


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


def available() -> bool:
    """Whether a Tor transport could be started here (the binary is present)."""
    return binary() is not None


def _write_torrc(tor_dir: Path, socks_port: int, forward_to_port: int) -> Path:
    """Render a minimal torrc for our own private Tor: a client SOCKS port for
    outbound dials, and one persistent onion service forwarding ONION_VIRTPORT to
    the node's loopback TCP listener. Everything lives under ``tor_dir`` so several
    nodes on one host (each its own DIPLOMAT_MESH_DIR) never collide."""
    data_dir = tor_dir
    hs_dir = tor_dir / "onion"
    # Tor refuses a DataDirectory / HiddenServiceDir that is not 0700.
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(data_dir, 0o700)
    hs_dir.mkdir(parents=True, exist_ok=True)
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

    def __init__(self, mesh_dir: Path, *, binary_path: str,
                 socks_port: int = 0) -> None:
        self._tor_dir = Path(mesh_dir) / "tor"
        self._binary = binary_path
        # 0 → pick a free ephemeral port at start; a fixed value is honored for
        # tests / an operator who pins it.
        self._socks_port = socks_port
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task | None = None
        self._bootstrapped = asyncio.Event()
        self._onion = ""

    # MARK: - what the node advertises

    def onion_address(self) -> str | None:
        """The permanent ``<hash>.onion`` this node listens on, or None until the
        service is up. Advertised verbatim in the signed NodeInfo."""
        return self._onion or None

    @property
    def socks_port(self) -> int:
        return self._socks_port

    # MARK: - lifecycle

    async def start(self, forward_to_port: int, *,
                    bootstrap_timeout: float = 90.0) -> bool:
        """Spawn Tor, wait for bootstrap, and read the onion hostname. Returns True
        when the transport is usable (onion known, SOCKS live); False on any
        failure — the caller then runs LAN-only. Never raises."""
        if not self._binary:
            return False
        if self._socks_port <= 0:
            self._socks_port = _free_port()
        try:
            torrc = _write_torrc(self._tor_dir, self._socks_port, forward_to_port)
        except OSError as exc:
            activity.log("mesh", "warn", f"Mesh/Tor: cannot write torrc ({exc})")
            return False
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._binary, "-f", str(torrc),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            activity.log("mesh", "warn", f"Mesh/Tor: cannot launch tor ({exc})")
            return False
        # Drain stdout forever (so tor never blocks on a full pipe) and flip the
        # bootstrap event when Tor reports it is fully connected.
        self._pump_task = asyncio.get_running_loop().create_task(
            self._pump_stdout(), name="mesh-tor-stdout")
        try:
            await asyncio.wait_for(self._bootstrapped.wait(),
                                   timeout=bootstrap_timeout)
        except asyncio.TimeoutError:
            activity.log("mesh", "warn",
                         "Mesh/Tor: bootstrap timed out — staying LAN-only")
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
        """Terminate the tor child and its stdout pump. Best-effort, never raises."""
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
