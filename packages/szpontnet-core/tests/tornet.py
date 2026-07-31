"""What "Tor" means for a test — a simulated onion network, or the real one.

The onion path has one property that makes it awkward to test and one that makes it
worth testing anyway: it depends on a daemon and a global network nobody controls,
and it is the only way two SzpontNet LANs ever become one mesh. So the tests are
written once, against this seam, and run against either of two backends:

``sim`` (default)
    ``simtor.py`` — a stand-in daemon speaking the exact contract ``tor.py`` depends
    on, over a descriptor directory on disk instead of the Tor network. Offline,
    deterministic, and quick enough to run on every push (the suite in about a
    minute and a half), and it drives **every line of tor.py for real**: the torrc is
    parsed by the process that receives it, SOCKS5 is spoken over a real socket, and
    bytes cross a real forward listener.

``real``
    the actual ``tor`` binary against the actual Tor network. Slow (roughly a quarter
    of an hour — bootstrap and descriptor publication are tens of seconds, per node,
    per test) and dependent on the machine having tor installed and reachable, but it
    is the only thing that proves a real onion service comes up and carries a link.

Pick with ``SZPONTNET_TEST_TOR=sim|real|both`` (default ``sim``). Everything runs on
both except the handful of tests that *stage a daemon failure* — a tor that stalls
below 100%, dies mid-bootstrap, or writes its hostname late — which are
simulation-only for the obvious reason that a real tor cannot be told to misbehave
on cue. Each says so through :func:`_sim_only`.

``real`` and ``both`` **skip** rather than fail when no ``tor`` binary is installed:
an absent daemon is a fact about the machine, not a defect in the node.

The same test bodies run on both. Only the patience differs (:class:`Backend`), and
patience is the one thing that legitimately does: what takes 50 ms against a
descriptor file takes half a minute against the live network.

Nodes here are **separate processes**, not objects. That is the point of an E2E: a
node reached over an onion runs ``python -m szpontnet``, resolves its own state
directory, spawns its own tor, and is driven the way an operator drives it — over
its control channel — so nothing in the path is a test double.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

# packages/szpontnet-core — what a spawned node needs on PYTHONPATH to import the
# library out of this checkout rather than whatever is installed, matching the
# sys.path rule conftest.py applies to the test process itself.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = Path(__file__).resolve().parent
_SIMTOR = _TESTS_DIR / "simtor.py"

SIM = "sim"
REAL = "real"

_SIGNAL_NUMBERS = {s.value for s in signal.Signals}


def _clean_environ() -> dict:
    """This process's environment with every SzpontNet knob stripped out.

    A node started from it reads exactly what its test gave it and nothing else —
    including, deliberately, no ``SZPONTNET_TOR``, so a node that comes up with an
    onion did so on the shipped default rather than because a test asked.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("SZPONTNET_", "DIPLOMAT_MESH_"))}


@dataclass(frozen=True)
class Backend:
    """One tor implementation and how long to wait for it.

    The timeouts are the whole difference between the backends. They are budgets, not
    sleeps — every wait polls a condition and fails naming what never happened — so a
    generous one costs nothing on a healthy run and buys a real backend the tens of
    seconds it genuinely needs.
    """

    name: str
    # Waiting for `Bootstrapped 100%`: a warm simulated daemon is instant, a real tor
    # negotiates a consensus first.
    bootstrap: float
    # Waiting for a dial to land a mesh link. Longer than bootstrap on the real
    # backend because the SERVICE side must also publish its descriptor to the HSDirs
    # before any client can reach it, and that lags bootstrap.
    link: float

    @property
    def is_real(self) -> bool:
        return self.name == REAL


_BACKENDS = {
    SIM: Backend(name=SIM, bootstrap=15.0, link=25.0),
    REAL: Backend(name=REAL, bootstrap=180.0, link=300.0),
}


def selected_backends() -> list[str]:
    """The backend names ``SZPONTNET_TEST_TOR`` asks for, as parametrize ids."""
    choice = (os.environ.get("SZPONTNET_TEST_TOR") or SIM).strip().lower()
    if choice == "both":
        return [SIM, REAL]
    if choice in (SIM, REAL):
        return [choice]
    raise RuntimeError(
        f"SZPONTNET_TEST_TOR={choice!r} is not one of: sim, real, both")


def resolve_binary(backend: Backend, tmp_path: Path) -> str:
    """The executable to hand ``TorTransport`` as its tor, for this backend.

    For ``real`` that is whatever ``tor`` is installed. For ``sim`` it is a shim named
    ``tor`` — named so, because it lands in a torrc, a process table and any log a
    failure is read out of — that runs :mod:`simtor` under **this** interpreter. The
    shim exists rather than a ``#!`` line on simtor.py so the daemon can never be run
    by some other python that happens to be first on PATH.
    """
    if backend.is_real:
        found = shutil.which("tor")
        if not found:
            pytest.skip("no `tor` binary installed — SZPONTNET_TEST_TOR=real needs one")
        return found
    shim = tmp_path / "tor"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(_SIMTOR)!r}\n"
        f"runpy.run_path({str(_SIMTOR)!r}, run_name='__main__')\n",
        encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


class TorNet:
    """One test's Tor world: a binary, the environment that reaches it, and every
    node process started against it (so teardown can be unconditional)."""

    def __init__(self, backend: Backend, tmp_path: Path, monkeypatch) -> None:
        self.backend = backend
        self.binary = resolve_binary(backend, tmp_path)
        self._tmp = tmp_path
        self._mp = monkeypatch
        self._nodes: list[NodeProcess] = []
        # The simulated network's descriptor directory. Passed through the
        # environment because a daemon is exec'd, not called — every simtor in this
        # test inherits it and therefore shares one directory, which is what lets one
        # node's onion be dialable by another.
        self._net_dir = tmp_path / "tornet"
        self._net_dir.mkdir(parents=True, exist_ok=True)
        for key, value in self.child_env().items():
            monkeypatch.setenv(key, value)

    def child_env(self) -> dict:
        """Environment every process in this world needs — the transport's own tor
        child included, since it inherits whatever the node was started with."""
        if self.backend.is_real:
            return {"SZPONTNET_TOR_BINARY": self.binary}
        return {"SZPONTNET_TOR_BINARY": self.binary, "SIMTOR_NET": str(self._net_dir)}

    def transport(self, name: str):
        """A :class:`~szpontnet.tor.TorTransport` on its own state directory —
        the real class, wired to this backend's daemon."""
        from szpontnet import tor

        mesh_dir = self._tmp / "transports" / name
        mesh_dir.mkdir(parents=True, exist_ok=True)
        return tor.TorTransport(mesh_dir, binary_path=self.binary)

    def node(self, name: str, **env: str) -> "NodeProcess":
        """Add a node process to this world (not started — the caller starts it, so a
        test can seed state on disk first)."""
        node = NodeProcess(self, name, index=len(self._nodes), **env)
        self._nodes.append(node)
        return node

    def stop_all(self) -> None:
        for node in reversed(self._nodes):
            with contextlib.suppress(Exception):
                node.stop()


class NodeProcess:
    """A whole node, in its own process, driven the way an operator drives one."""

    # Distinct multicast ports so nodes in one test CANNOT discover each other on the
    # LAN. Every link a test then observes had to come over an onion, which is the
    # only way to state "these machines are on different networks" on one host.
    #
    # Keyed to the pid, in blocks of _PORTS_PER_SESSION, because "a different network"
    # has to hold against OTHER test sessions too: on a fixed base, a suite running
    # beside another (a developer's second terminal, two CI jobs on one runner) would
    # put node 0 of each on the same multicast port, and they would discover each
    # other on the LAN — turning "linked only over Tor" into a coin toss.
    _MCAST_BASE = 41000
    _PORTS_PER_SESSION = 10

    def __init__(self, net: TorNet, name: str, index: int, *,
                 node_id: str = "", tier: int = 3, **env: str) -> None:
        self.net = net
        self.name = name
        self.dir = net._tmp / "nodes" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.id = node_id or (chr(ord("a") + index) * 32)
        self.log_path = self.dir / "node.log"
        self._proc: subprocess.Popen | None = None
        self._log_handle = None
        # Pinned "ok" rather than "auto": the auto state comes from probing the
        # OPERATOR's real usage logs, so on a developer's own machine an auto node
        # can advertise `out` and decline every dispatch in the test — a failure
        # about the machine the suite runs on, not about the mesh.
        (self.dir / "node.json").write_text(json.dumps({
            "id": self.id, "name": name, "tier": tier, "tokens": "ok",
            "strengthAuto": False, "dutiesEnabled": {},
        }), encoding="utf-8")
        self.env = {
            # A clean namespace, not the test process's. Two things would otherwise
            # ride in: a developer's own exported SZPONTNET_SECRET or SZPONTNET_HOST
            # (which would fence or re-home a node this test believes it configured),
            # and — the one that would quietly gut this file — the suite-wide
            # SZPONTNET_TOR=0 that conftest sets, which would start every node here
            # LAN-only while the tests waited for onions that were never coming.
            **_clean_environ(),
            **net.child_env(),
            "SZPONTNET_DIR": str(self.dir),
            # Keeps every socket on 127.0.0.1 — and, load-bearing here, stands the
            # cross-directory singleton reaper down. Without it the second node to
            # start SIGTERMs the first (and any node the developer is running), since
            # the reaper's premise is one physical machine, one node.
            "SZPONTNET_LOOPBACK": "1",
            "SZPONTNET_MCAST_PORT": str(self._multicast_port(index)),
            "SZPONTNET_DEFAULT_TRUST": "personal",
            "SZPONTNET_OAUTH_PROBE": "0",  # never touch the network to price a quota
            "SZPONTNET_STATE_SECS": "0.2",  # a snapshot a test can poll
            "SZPONTNET_TOR_BOOTSTRAP_SECS": str(net.backend.bootstrap),
            # Puts the node's narration on stderr instead of the null host's floor,
            # so a failure carries the node's own account of what went wrong. See
            # tornet_host.py.
            "SZPONTNET_HOST": "tornet_host",
            "PYTHONPATH": os.pathsep.join([str(_PACKAGE_ROOT), str(_TESTS_DIR)]),
            "PYTHONUNBUFFERED": "1",
            **env,
        }

    @classmethod
    def _multicast_port(cls, index: int) -> int:
        """This session's port for the ``index``-th node — unique within the session
        and, barring a pid collision 500 apart, across concurrent ones."""
        assert index < cls._PORTS_PER_SESSION, (
            f"a test wanted more than {cls._PORTS_PER_SESSION} nodes; widen the "
            "per-session port block rather than letting two of them share a network")
        session = (os.getpid() % 500) * cls._PORTS_PER_SESSION
        return cls._MCAST_BASE + session + index

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "NodeProcess":
        """Launch the node. Deliberately `python -m szpontnet` with no Tor flag: the
        transport being on is the shipped default, so a test that had to ask for it
        would not be testing the default."""
        self._log_handle = open(self.log_path, "wb")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "szpontnet"], env=self.env, cwd=str(self.dir),
            stdout=self._log_handle, stderr=subprocess.STDOUT)
        return self

    def stop(self) -> None:
        """Ask the node to stop, then make sure it did. The graceful path matters:
        it is what reaps the tor child and releases its data-directory lock."""
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            self.ctl({"t": "stop"}, timeout=5.0)
        try:
            self._proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=10.0)
            if self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait(timeout=10.0)
        self._proc = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- reading it ----------------------------------------------------------

    @contextlib.contextmanager
    def _as_operator(self):
        """Run a block with this node's state directory in force — how the control
        client finds a node, and the only way one test process can drive several."""
        previous = os.environ.get("SZPONTNET_DIR")
        os.environ["SZPONTNET_DIR"] = str(self.dir)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("SZPONTNET_DIR", None)
            else:
                os.environ["SZPONTNET_DIR"] = previous

    def state(self) -> dict:
        """The node's last written snapshot, or ``{}`` before it writes one."""
        try:
            return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def ctl(self, msg: dict, timeout: float = 10.0) -> dict:
        """One control command, as the CLI and the panel send them."""
        from szpontnet import ctl as ctlmod

        with self._as_operator():
            return ctlmod.request(msg, timeout=timeout)

    def snapshot(self) -> dict:
        """The node's LIVE snapshot over the control channel (fresher than
        ``state.json``, and it proves the node is answering)."""
        return self.ctl({"t": "status"})["state"]

    def onion(self) -> str:
        return (self.state().get("tor") or {}).get("onion") or ""

    def peer(self, other: "NodeProcess") -> dict:
        for entry in self.state().get("peers", []):
            if entry.get("id") == other.id:
                return entry
        return {}

    def _exit_description(self) -> str:
        """How the node died, in the terms that separate the two very different
        causes: a non-zero status is the node failing, and a *signal* is something
        else killing it.

        Worth spelling out because one particular killer is easy to reach and hard to
        guess at: a mesh node started elsewhere WITHOUT ``SZPONTNET_LOOPBACK=1``
        reaps every other node of this uid by argv on startup (see
        ``singleton.terminate_other_nodes``) — so a second suite, a stray daemon, or a
        conformance run beside this one takes these nodes down with a SIGTERM, and the
        symptom is a link that never forms.
        """
        code = self._proc.returncode if self._proc is not None else None
        if code is None:
            return "still running"
        if code < 0:
            name = signal.Signals(-code).name if -code in _SIGNAL_NUMBERS else str(-code)
            return f"killed by {name} — something outside this test stopped it"
        return f"exit status {code}"

    def log_tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(
                self.log_path.read_text(encoding="utf-8", errors="replace")
                .splitlines()[-lines:])
        except OSError:
            return "(no log)"

    # -- waiting -------------------------------------------------------------

    def until(self, predicate, timeout: float, why: str, poll: float = 0.1):
        """Poll until ``predicate`` holds, failing with what never happened and the
        tail of the node's own log — which is where a tor that refused to start says
        so."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive:
                raise AssertionError(
                    f"{self.name} exited ({self._exit_description()}) while waiting "
                    f"for {why}\n--- {self.name} log ---\n{self.log_tail()}")
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{self.name}: {why} (waited {timeout:g}s)\n"
                    f"--- {self.name} log ---\n{self.log_tail()}")
            time.sleep(poll)

    def await_running(self, timeout: float = 30.0) -> "NodeProcess":
        """Wait until the node has bound its port and written a snapshot."""
        self.until(lambda: self.state().get("tcpPort"), timeout,
                   "never wrote a state.json with a bound port")
        return self

    def await_onion(self, timeout: float | None = None) -> str:
        """Wait until this node's onion service is live, and return the address."""
        budget = self.net.backend.bootstrap if timeout is None else timeout
        return self.until(lambda: self.onion(), budget + 20.0,
                          "onion service never came up")

    # The peer link states a snapshot reports for a link that is actually up. "stale"
    # counts: it means the last heartbeat is older than peerStaleSecs but the link is
    # still bound, which a loaded test machine reaches routinely. "down" is the only
    # one that means not linked.
    _LINKED = ("up", "stale")

    def await_linked(self, other: "NodeProcess", timeout: float | None = None) -> dict:
        """Wait until this node holds a live link to ``other``, and return its peer
        entry (which carries the transport the link came up on)."""
        budget = self.net.backend.link if timeout is None else timeout
        return self.until(
            lambda: (self.peer(other).get("link") in self._LINKED
                     and self.peer(other)),
            budget, f"never linked to {other.name}")


def pytest_backend_params():
    """``pytest.mark.parametrize`` arguments for the selected backends."""
    return [pytest.param(_BACKENDS[name], id=name) for name in selected_backends()]
