"""A stand-in ``tor`` daemon — the onion path, without the Tor network.

``TorTransport`` talks to tor across a *process* boundary and nothing else: it
renders a torrc, execs the binary, reads its stdout for a bootstrap line, reads the
``hostname`` file tor writes, and speaks SOCKS5 to the port tor bound. That is a
small, fully-specified contract, so a program can honour it exactly without being
Tor. This is that program.

What that buys is the thing a dependency-injected dialer cannot: **every line of
tor.py runs for real** — the torrc it renders is parsed by the process that receives
it, the stdout pump drains a real pipe, ``_read_hostname`` reads a file another
process wrote, ``_socks5_connect`` puts real SOCKS5 bytes on a real socket and a
real server answers them, and the forward listener carries a real byte stream. The
only thing simulated is the *Tor network itself* — the circuits, the directory, the
rendezvous — which is Tor's code, not ours.

The simulated network is a directory (``SIMTOR_NET``): a daemon publishes
``<onion>.desc`` there naming the loopback port its onion service forwards to, and a
SOCKS CONNECT to some ``<onion>`` is resolved by reading it. That is a descriptor
directory with the anonymity taken out, and it gives the two properties dials turn
on — an onion is reachable only once it has *published*, and it stops being
reachable when its daemon goes away.

Faithful where the node can tell the difference:

* the onion is derived from a **persisted key**, so it survives a restart exactly as
  a real one does (and so a test can prove the address is permanent);
* the ``DataDirectory`` is **locked**, so a second daemon on one directory fails the
  way real tor fails — which is what makes an orphaned child a detectable bug rather
  than a silent one;
* an unpublished onion answers a dial with SOCKS5 ``0x04`` (host unreachable), the
  same reply real tor gives for an onion that is down or whose descriptor it cannot
  fetch;
* bootstrap is **progressive** on stdout, ending in the ``Bootstrapped 100%`` line
  the transport actually greps for.

Fault injection, via ``SIMTOR_FAIL`` — each one is a real failure of the real
daemon, staged deterministically instead of waited for:

===============  ============================================================
``bootstrap``    bootstraps to 50% and stops — the transport's timeout path.
``exit``         dies mid-bootstrap, as a bad torrc or a crash does.
``nohostname``   bootstraps but never writes ``hostname``.
``latehostname`` writes ``hostname`` well after 100%, exercising the retry.
``dropsocks``    answers the SOCKS handshake, then closes without relaying —
                 an onion that is reachable but carries nothing.
===============  ============================================================

``SIMTOR_BOOTSTRAP_DELAY`` (seconds, default 0.05) paces bootstrap; a test about the
node staying usable *while* Tor comes up sets it high.

Not a library: this is exec'd as a binary (see ``tornet.py``, which writes a shim
named ``tor`` so the interpreter is the one running the tests), so it imports only
the standard library and never imports szpontnet.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import signal
import sys
from pathlib import Path

# The SOCKS5 reply code for "host unreachable" — what real tor answers when it
# cannot reach an onion (down, or no descriptor published). tor.py's dialer treats
# any non-zero reply as "not reachable right now", so the exact code is diagnostic
# rather than load-bearing; it is the honest one all the same.
_SOCKS_HOST_UNREACHABLE = 0x04
_SOCKS_OK = 0x00


def _parse_torrc(path: Path) -> dict:
    """The four directives this daemon honours, out of the torrc tor.py renders.

    Deliberately a real parse of the real file rather than an agreement between two
    test helpers: it is what makes a torrc the transport renders wrongly (a port on
    the wrong line, a directive spelt for a different tor version) show up here as a
    daemon that cannot start, which is exactly how the real binary would report it.
    """
    conf: dict = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.partition(" ")
        rest = rest.strip()
        if key == "SocksPort":
            # "127.0.0.1:9050" or a bare port.
            conf["socks_port"] = int(rest.rsplit(":", 1)[-1])
        elif key == "DataDirectory":
            conf["data_dir"] = Path(rest)
        elif key == "HiddenServiceDir":
            conf["hs_dir"] = Path(rest)
        elif key == "HiddenServicePort":
            # "<virtport> 127.0.0.1:<forward>"
            virt, _, target = rest.partition(" ")
            conf["virtport"] = int(virt)
            conf["forward_port"] = int(target.strip().rsplit(":", 1)[-1])
    return conf


def _onion_address(hs_dir: Path) -> str:
    """This service's permanent ``<56-base32>.onion``, from a key persisted in
    ``hs_dir`` — generated on first start and reused ever after.

    A real v3 address is base32 of (public key ‖ checksum ‖ version); this is base32
    of a hash of the private key. Both are a deterministic 56-character function of a
    key on disk, which is the whole of what the node depends on: an address that does
    not change across a restart and cannot be guessed from the directory's path.
    """
    key_file = hs_dir / "hs_ed25519_secret_key"
    if not key_file.exists():
        hs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file.write_bytes(secrets.token_bytes(64))
        with contextlib.suppress(OSError):
            key_file.chmod(0o600)
    # Exactly 35 bytes, because 35 bytes is 280 bits is exactly 56 base32 characters
    # with no padding — the length a v3 address has and the only one the node's
    # `[a-z2-7]{56}` check accepts. A 32-byte digest would encode to 52 characters
    # plus `====`, which is not an onion address and is rejected as one.
    digest = hashlib.blake2b(key_file.read_bytes(), digest_size=35).digest()
    return base64.b32encode(digest).decode("ascii").lower() + ".onion"


class _Descriptors:
    """The simulated network's directory: onion → the loopback port it forwards to.

    One directory on disk shared by every daemon in a test, so publication is visible
    across processes exactly as a real HSDir upload is visible across the network.
    """

    def __init__(self, net_dir: Path) -> None:
        self._dir = net_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, onion: str) -> Path:
        return self._dir / f"{onion}.desc"

    def publish(self, onion: str, forward_port: int) -> None:
        """Upload our descriptor. Written-then-renamed so a dialer never reads a
        half-written one — the local stand-in for an upload that is either accepted
        whole or not at all."""
        tmp = self._path(onion).with_suffix(".desc.tmp")
        tmp.write_text(json.dumps({"onion": onion, "port": forward_port,
                                   "pid": os.getpid()}), encoding="utf-8")
        tmp.replace(self._path(onion))

    def unpublish(self, onion: str) -> None:
        with contextlib.suppress(OSError):
            self._path(onion).unlink()

    def resolve(self, onion: str) -> int | None:
        """The forward port for ``onion``, or None when nothing has published it —
        the case a dial must answer with 'host unreachable' rather than hang."""
        try:
            return int(json.loads(self._path(onion).read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError):
            return None


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF, then half-close. Errors are the ordinary end of
    a connection here, not events worth reporting."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            writer.close()


class SimTor:
    """One daemon: a SOCKS5 client port, one published onion service, a stdout log."""

    def __init__(self, conf: dict, net: _Descriptors, fail: str, delay: float) -> None:
        self._conf = conf
        self._net = net
        self._fail = fail
        self._delay = delay
        self._onion = ""
        self._stopping = asyncio.Event()

    # -- the log tor.py greps ------------------------------------------------

    def _notice(self, text: str) -> None:
        """One ``[notice]`` line, shaped like tor's own and flushed immediately —
        the transport is reading this pipe line by line to decide when Tor is up."""
        sys.stdout.write(f"Jan 01 00:00:00.000 [notice] {text}\n")
        sys.stdout.flush()

    # -- SOCKS5 --------------------------------------------------------------

    async def _on_socks(self, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
        try:
            await self._socks_session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()

    async def _socks_session(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        version, n_methods = await reader.readexactly(2)
        methods = await reader.readexactly(n_methods)
        if version != 0x05 or 0x00 not in methods:
            # No mutually acceptable method: 0xFF, per RFC 1928.
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")  # no authentication required
        await writer.drain()

        version, command, _reserved, addr_type = await reader.readexactly(4)
        if addr_type == 0x03:      # domain name — the only kind an onion dial uses
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("ascii", "replace")
        elif addr_type == 0x01:    # IPv4
            host = ".".join(str(b) for b in await reader.readexactly(4))
        else:
            await self._socks_reply(writer, 0x08)  # address type not supported
            return
        port = int.from_bytes(await reader.readexactly(2), "big")
        if version != 0x05 or command != 0x01:  # CONNECT only
            await self._socks_reply(writer, 0x07)  # command not supported
            return

        target = self._net.resolve(host) if host.endswith(".onion") else None
        if target is None or port != self._conf["virtport"]:
            # Nobody has published this onion (it is down, or never came up), or the
            # dial asked for a virtual port the service does not expose.
            await self._socks_reply(writer, _SOCKS_HOST_UNREACHABLE)
            return
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target)
        except OSError:
            # Published, but the forward listener is gone — a service whose node died
            # between publishing and this dial.
            await self._socks_reply(writer, _SOCKS_HOST_UNREACHABLE)
            return
        await self._socks_reply(writer, _SOCKS_OK)
        if self._fail == "dropsocks":
            # An onion that answers the handshake and then drops: the case the node's
            # backoff must stay grown for, since no link can ever bind on it.
            up_writer.close()
            return
        await asyncio.gather(_relay(reader, up_writer), _relay(up_reader, writer))

    async def _socks_reply(self, writer: asyncio.StreamWriter, code: int) -> None:
        """A SOCKS5 reply with an IPv4 bound-address of 0.0.0.0:0 — which is what tor
        answers with, and what ``_socks5_connect`` consumes before returning."""
        writer.write(bytes([0x05, code, 0x00, 0x01]) + b"\x00" * 4 + b"\x00" * 2)
        await writer.drain()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> int:
        hs_dir: Path = self._conf["hs_dir"]
        self._onion = _onion_address(hs_dir)
        server = await asyncio.start_server(
            self._on_socks, "127.0.0.1", self._conf["socks_port"])

        self._notice("Tor 0.0.0-simtor opening log file.")
        self._notice("Bootstrapped 0% (starting): Starting")
        await asyncio.sleep(self._delay)
        if self._fail == "exit":
            self._notice("[err] Reading config failed — see the torrc.")
            return 1
        self._notice("Bootstrapped 50% (loading_descriptors): Loading relay descriptors")
        if self._fail == "bootstrap":
            # Bootstrap stalls here forever: tor is alive, its SOCKS port is bound,
            # and 100% never arrives — the transport must give up on its own timeout
            # rather than wait on a signal that is not coming.
            await self._stopping.wait()
            return 0
        await asyncio.sleep(self._delay)

        if self._fail not in ("nohostname", "latehostname"):
            self._write_hostname(hs_dir)
        self._notice("Bootstrapped 100% (done): Done")
        self._net.publish(self._onion, self._conf["forward_port"])
        if self._fail == "latehostname":
            # The window a real tor can leave between reporting bootstrap and having
            # written the service's hostname — what _read_hostname retries for.
            await asyncio.sleep(self._delay * 8)
            self._write_hostname(hs_dir)

        try:
            await self._stopping.wait()
        finally:
            self._net.unpublish(self._onion)  # a stopped service is not reachable
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        return 0

    def _write_hostname(self, hs_dir: Path) -> None:
        (hs_dir / "hostname").write_text(self._onion + "\n", encoding="utf-8")

    def stop(self) -> None:
        self._stopping.set()

    async def _exit_when_orphaned(self, parent_pid: int) -> None:
        """Stop once the node that spawned us is gone.

        The node asks the kernel for this (``prctl(PR_SET_PDEATHSIG)``), but that is
        Linux-only and degrades to a no-op elsewhere — so on macOS a node killed
        without a graceful stop would leave its daemon running, holding a data
        directory lock and a published descriptor. A test suite that kills nodes for a
        living would accumulate them across a session. Watching our own parent is the
        portable half of the same guarantee.
        """
        while not self._stopping.is_set():
            if os.getppid() != parent_pid:
                self._notice("Parent process has gone away — exiting.")
                self.stop()
                return
            await asyncio.sleep(1.0)


def _lock_data_directory(data_dir: Path):
    """Take the ``DataDirectory`` lock, or refuse to start — as real tor does.

    Not decoration: the node ties tor's lifetime to its own precisely so an orphaned
    child cannot hold this lock and keep the next node LAN-only. Simulating the lock
    is what turns that from a comment into something a test can fail on.

    Returns the open descriptor (the process holds it until it exits) or None when
    another daemon has it.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = data_dir / "lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    except ImportError:  # pragma: no cover — no flock on this platform
        pass
    return fd


async def _amain(torrc: Path) -> int:
    conf = _parse_torrc(torrc)
    missing = {"socks_port", "hs_dir", "forward_port", "virtport"} - set(conf)
    if missing:
        # The torrc did not carry what a daemon needs. Real tor exits non-zero on a
        # config it cannot use, and the transport treats that as "stay LAN-only".
        sys.stdout.write(f"[err] Failed to parse/validate config: missing {missing}\n")
        return 1

    lock = _lock_data_directory(conf.get("data_dir", torrc.parent))
    if lock is None:
        sys.stdout.write(
            "[err] Could not lock data directory. Another Tor process may be running.\n")
        return 1

    net_dir = os.environ.get("SIMTOR_NET")
    if not net_dir:
        sys.stdout.write("[err] SIMTOR_NET is unset — no simulated network to join.\n")
        return 1
    try:
        delay = float(os.environ.get("SIMTOR_BOOTSTRAP_DELAY", "0.05"))
    except ValueError:
        delay = 0.05

    daemon = SimTor(conf, _Descriptors(Path(net_dir)),
                    os.environ.get("SIMTOR_FAIL", ""), delay)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.stop)
    watchdog = asyncio.ensure_future(daemon._exit_when_orphaned(os.getppid()))
    try:
        return await daemon.run()
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "-f":
        sys.stdout.write("[err] usage: tor -f <torrc>\n")
        return 1
    return asyncio.run(_amain(Path(argv[2])))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
