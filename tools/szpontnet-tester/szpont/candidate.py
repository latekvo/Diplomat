"""Launching and observing the candidate node under test.

The tester is black-box: it starts the candidate with a caller-provided command
and a fixed set of ``SZPONTNET_*`` environment variables (the **candidate
contract**, documented in the README), then observes it purely over the wire and
through the two snapshot channels the spec exposes:

- a **control session** (``ctl`` → ``status`` → ``state``) for a Controllable
  node (04/07);
- the on-disk **state.json** snapshot for a node that writes one (08).

An implementation adapts by reading the ``SZPONTNET_*`` variables (directly, or
via a tiny wrapper script like ``adapters/reference.py``). Nothing here is
specific to the reference node.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import codec, net


def contract_env(
    *, work_dir: Path, proto: dict, loopback: bool, secret: str,
    node_id: str, name: str, platform: str, tier: int, tokens: str,
    duties_enabled: dict, spawn_cmd: str,
    server: bool = False, api_key: str = "", stats: dict | None = None,
) -> dict:
    """The SZPONTNET_* environment a candidate (or its adapter) must honor.

    Chapter-11 knobs are optional and default off, so a plain (ch 01-10) run is
    byte-identical to before: ``SZPONTNET_SERVER`` puts the candidate in the
    accept-only server role, ``SZPONTNET_API_KEY`` gates inbound ctl/dispatch,
    and ``SZPONTNET_STATS`` seeds the node's advertised load-balancing stats.
    """
    env = {
        "SZPONTNET_LOOPBACK": "1" if loopback else "0",
        "SZPONTNET_MCAST_GROUP": str(proto["multicastGroup"]),
        "SZPONTNET_MCAST_PORT": str(proto["multicastPort"]),
        "SZPONTNET_TCP_BASE": str(proto["tcpPortBase"]),
        "SZPONTNET_TCP_SPAN": str(proto["tcpPortSpan"]),
        "SZPONTNET_BEACON_SECS": str(proto["beaconIntervalSecs"]),
        "SZPONTNET_HEARTBEAT_SECS": str(proto["heartbeatIntervalSecs"]),
        "SZPONTNET_STALE_SECS": str(proto["peerStaleSecs"]),
        "SZPONTNET_TIMEOUT_SECS": str(proto["peerTimeoutSecs"]),
        "SZPONTNET_ACK_SECS": str(proto["dispatchAckTimeoutSecs"]),
        "SZPONTNET_STATE_SECS": str(proto["stateWriteIntervalSecs"]),
        "SZPONTNET_DIR": str(work_dir),
        "SZPONTNET_SECRET": secret,
        "SZPONTNET_PLATFORM": platform,
        "SZPONTNET_NODE_ID": node_id,
        "SZPONTNET_NODE_NAME": name,
        "SZPONTNET_TIER": str(tier),
        "SZPONTNET_TOKENS": tokens,
        "SZPONTNET_DUTIES": json.dumps(duties_enabled or {}),
        "SZPONTNET_SPAWN": spawn_cmd,
    }
    if server:
        env["SZPONTNET_SERVER"] = "1"
    if api_key:
        env["SZPONTNET_API_KEY"] = api_key
    if stats:
        env["SZPONTNET_STATS"] = json.dumps(stats)
    return env


class CtlSession:
    """One control session to the candidate; one reply line per command."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = net.LineReader(sock)

    def command(self, msg: dict, timeout: float = 10.0) -> dict | None:
        self.sock.sendall(codec.encode(msg))
        line = self.reader.read_line(timeout)
        return codec.decode(line) if line else None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Candidate:
    """A launched candidate node process, plus its observation channels."""

    def __init__(self, cmd: list[str], env: dict, work_dir: Path, secret: str = "",
                 api_key: str = "") -> None:
        self.cmd = cmd
        self.env = env
        self.work_dir = work_dir
        self.secret = secret
        # The API key the tester presents on ctl sessions when the candidate is an
        # API-key server (11); empty for a plain node.
        self.api_key = api_key
        self.node_id = env["SZPONTNET_NODE_ID"]
        self.proc: subprocess.Popen | None = None
        self.tcp_port: int | None = None   # filled once discovered (beacon/state.json)
        self.log_path = work_dir / "candidate.log"

    def start(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        full_env = dict(os.environ)
        full_env.update(self.env)
        # Inherit the tester's cwd so a relative --node-cmd (e.g. the adapter
        # path) resolves; the candidate finds its own work dir via SZPONTNET_DIR.
        self.proc = subprocess.Popen(
            self.cmd, env=full_env,
            stdout=self.log_path.open("w"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # MARK: - snapshot channels

    def state_file(self) -> dict | None:
        try:
            return json.loads((self.work_dir / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def ctl_status(self, timeout: float = 5.0) -> dict | None:
        """Fetch the live snapshot via a control session (Controllable role)."""
        if not self.tcp_port:
            return None
        try:
            sess = self.open_ctl(timeout=timeout)
        except OSError:
            return None
        try:
            reply = sess.command(codec.status_request(), timeout=timeout)
            if reply and reply.get("t") == "state":
                return reply.get("state")
            return None
        finally:
            sess.close()

    def snapshot(self) -> dict | None:
        """The candidate's topology snapshot from whichever channel answers —
        control session preferred (live), else the on-disk state.json."""
        return self.ctl_status() or self.state_file()

    def open_ctl(self, timeout: float = 5.0) -> CtlSession:
        """Open a control session (sending the join secret). Raises OSError if
        the port is unknown or unreachable."""
        if not self.tcp_port:
            raise OSError("candidate TCP port unknown")
        sock = net.connect_tcp("127.0.0.1", self.tcp_port, timeout=timeout)
        sess = CtlSession(sock)
        sock.sendall(codec.encode(codec.ctl_hello(self.secret, self.api_key)))
        return sess

    # MARK: - lifecycle

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = 3.0) -> None:
        if self.proc is None:
            return
        with_group = self.proc.pid
        try:
            os.killpg(os.getpgid(with_group), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            self.proc.terminate()
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.05)
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(with_group), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)

    def log_tail(self, lines: int = 30) -> str:
        try:
            return "\n".join(self.log_path.read_text().splitlines()[-lines:])
        except OSError:
            return ""
