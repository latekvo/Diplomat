#!/usr/bin/env python3
"""SzpontNet network simulator — a scriptable, real-socket mesh under test.

The auto-monitors on several machines all scan GitHub for the same work, route
it to the best-surplus node, and lean on work-claims to run it *exactly once*
with failover and retry (docs/szpontnet/12). That behaviour is impossible to
judge from a single process, so this simulator stands up a **fleet of real
``python -m diplomat_app.mesh`` nodes** on loopback, injects **simulated work
events** (a review request = a duty + a work key), and checks the invariants:

    exactly-once · best-fit placement · no-drop · failover · retry · race-safety

A dispatched "agent" is the deterministic stub :mod:`mesh_sim_agent`, wired in
through ``DIPLOMAT_MESH_SPAWN``: it records every run to a shared log (so a
double-run or a drop is observable) and *holds* — keeping the executor's claim
in flight — until the simulator releases it. That is what lets the simulator
probe the forbidden second run while the first is still "running".

Run the built-in adversarial scenarios::

    python -m tools.mesh_sim            # run all scenarios, print PASS/FAIL
    python -m tools.mesh_sim race       # run one scenario

or drive it as a library (the pytest suite does — see test_mesh_simulator.py).

POSIX only; needs loopback multicast (as the mesh integration tests do).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

LINUX_DIR = Path(__file__).resolve().parents[1]
_AGENT = Path(__file__).resolve().parent / "mesh_sim_agent.py"


def loopback_multicast_works() -> bool:
    """Probe whether loopback multicast is available (a hardened/namespaced CI
    container may lack it — the caller then skips rather than hang)."""
    import socket
    import struct

    group, port = "239.83.77.9", 40911
    rx = tx = None
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", port))
        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("127.0.0.1"))
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        rx.settimeout(1.0)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        tx.sendto(b"probe", (group, port))
        rx.recvfrom(64)
        return True
    except OSError:
        return False
    finally:
        for s in (rx, tx):
            if s is not None:
                s.close()


def _proto_env(port_base: int) -> dict:
    """Fast, deterministic, offline protocol timings — the same knobs the mesh
    integration fleet uses, so the simulator behaves like those tests."""
    return {
        "DIPLOMAT_MESH_LOOPBACK": "1",
        "DIPLOMAT_MESH_OAUTH_PROBE": "0",
        "DIPLOMAT_MESH_MCAST_PORT": str(port_base),
        "DIPLOMAT_MESH_TCP_BASE": str(port_base + 1),
        "DIPLOMAT_MESH_TCP_SPAN": "16",
        "DIPLOMAT_MESH_BEACON_SECS": "0.25",
        "DIPLOMAT_MESH_HEARTBEAT_SECS": "0.25",
        "DIPLOMAT_MESH_STALE_SECS": "1.0",
        "DIPLOMAT_MESH_TIMEOUT_SECS": "2.0",
        "DIPLOMAT_MESH_ACK_SECS": "4.0",
        "DIPLOMAT_MESH_STATE_SECS": "0.25",
    }


@dataclass
class NodeSpec:
    """One machine in the simulated fleet."""

    node_id: str
    name: str
    platform: str = "linux"
    tier: int = 3
    tokens: str = "ok"
    trust: str = "personal"
    server: bool = False
    # Optional explicit accounting so best-fit placement is unambiguous in a
    # scenario: surplus = quota_left - usage (surplus-first ranks on it).
    quota_left: float | None = None
    usage: float | None = None


@dataclass
class Simulator:
    """A fleet of real mesh nodes plus work-event injection and observation."""

    root: Path
    port_base: int = field(default=0)
    specs: list[NodeSpec] = field(default_factory=list)
    procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    dirs: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.port_base:
            # Unique-ish per process so parallel/leftover runs don't collide.
            self.port_base = 43000 + (os.getpid() % 300) * 20
        self.runs_file = self.root / "runs.jsonl"
        self.hold_dir = self.root / "hold"
        self.spawn_root = self.root / "spawned"
        self.hold_dir.mkdir(parents=True, exist_ok=True)
        self.spawn_root.mkdir(parents=True, exist_ok=True)
        self.runs_file.touch()

    # MARK: - fleet lifecycle

    def add(self, spec: NodeSpec) -> "Simulator":
        self.specs.append(spec)
        return self

    def start(self) -> None:
        for spec in self.specs:
            self._start_one(spec)

    def _start_one(self, spec: NodeSpec) -> None:
        d = self.root / spec.node_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "node.json").write_text(json.dumps({
            "id": spec.node_id, "name": spec.name, "tier": spec.tier,
            "tokens": spec.tokens, "dutiesEnabled": {},
        }))
        env = dict(os.environ)
        env.update(_proto_env(self.port_base))
        env["DIPLOMAT_MESH_DIR"] = str(d)
        env["DIPLOMAT_MESH_PLATFORM"] = spec.platform
        env["DIPLOMAT_MESH_SERVER"] = "1" if spec.server else ""
        env["DIPLOMAT_MESH_DEFAULT_TRUST"] = spec.trust
        # Every dispatched job lands in our deterministic agent stub, which
        # records the run and holds until released. The node hands it the
        # completion sentinel via DIPLOMAT_MESH_DONE_FILE (patched node only).
        env["DIPLOMAT_MESH_SPAWN"] = (
            f"{sys.executable} {_AGENT} --node {spec.name} --max-hold 15 "
            f"--runs {self.runs_file} --hold-dir {self.hold_dir} {{prompt_file}}"
        )
        env["HOME"] = str(d)  # keep the shared activity feed off the real ~/.diplomat
        self.procs[spec.node_id] = subprocess.Popen(
            [sys.executable, "-m", "diplomat_app.mesh"],
            cwd=LINUX_DIR, env=env,
            stdout=(d / "node.log").open("w"), stderr=subprocess.STDOUT,
        )
        self.dirs[spec.node_id] = d
        if spec.quota_left is not None or spec.usage is not None:
            self._pending_stats = getattr(self, "_pending_stats", [])
            self._pending_stats.append(spec)

    def stop_all(self) -> None:
        for node_id in list(self.procs):
            self.kill(node_id)

    def kill(self, node_id: str) -> None:
        proc = self.procs.pop(node_id, None)
        if proc:
            proc.kill()
            proc.wait(timeout=10)

    # MARK: - node control (CLI / ctl)

    def cli(self, node_id: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(_proto_env(self.port_base))
        env["DIPLOMAT_MESH_DIR"] = str(self.dirs[node_id])
        env["HOME"] = str(self.dirs[node_id])
        env["DIPLOMAT_MESH_DEFAULT_TRUST"] = next(
            (s.trust for s in self.specs if s.node_id == node_id), "personal")
        return subprocess.run(
            [sys.executable, "-m", "diplomat_app.mesh", *args],
            cwd=LINUX_DIR, env=env, capture_output=True, text=True, timeout=timeout,
        )

    def state(self, node_id: str) -> dict:
        try:
            return json.loads((self.dirs[node_id] / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def apply_stats(self) -> None:
        """Push each spec's accounting so surplus-first placement is pinned."""
        for spec in getattr(self, "_pending_stats", []):
            attrs = []
            if spec.quota_left is not None:
                attrs.append(f"quotaLeft={spec.quota_left}")
            if spec.usage is not None:
                attrs.append(f"usage={spec.usage}")
            if attrs:
                self.cli(spec.node_id, "--set", *attrs)

    # MARK: - convergence

    def await_links(self, timeout: float = 20.0) -> None:
        want = len(self.specs) - 1
        deadline = time.monotonic() + timeout
        for node_id in self.procs:
            while time.monotonic() < deadline:
                peers = self.state(node_id).get("peers", [])
                if len([p for p in peers if p.get("link") == "up"]) >= want:
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError(f"{node_id} never linked {want} peers")

    def await_assignments_agree(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            views = []
            for node_id in self.procs:
                a = {k: tuple(v.get("assigned", []))
                     for k, v in (self.state(node_id).get("assignments") or {}).items()}
                views.append(a)
            if views and all(v == views[0] and v for v in views):
                return views[0]
            time.sleep(0.1)
        raise TimeoutError("nodes never agreed on assignments")

    # MARK: - work-event injection

    def inject(self, work_key: str, duty: str = "review", *,
               from_node: str | None = None, from_nodes: list[str] | None = None,
               concurrent: bool = False) -> list[tuple[str, int, str]]:
        """Simulate one or more machines scanning and finding ``work_key``.

        Each named node runs ``--dispatch <duty> --prompt <work_key>
        --work-key <work_key>`` — i.e. claim-gated dispatch. ``concurrent`` fires
        them at once (the simultaneous-scan race). Returns ``(node, rc, stdout)``
        per injector."""
        nodes = from_nodes or ([from_node] if from_node else [next(iter(self.procs))])

        def one(nid: str) -> tuple[str, int, str]:
            r = self.cli(nid, "--dispatch", duty, "--prompt", work_key,
                         "--work-key", work_key)
            return (nid, r.returncode, r.stdout + r.stderr)

        if concurrent and len(nodes) > 1:
            with ThreadPoolExecutor(max_workers=len(nodes)) as ex:
                return list(ex.map(one, nodes))
        return [one(n) for n in nodes]

    # MARK: - agent control + observation

    def finish(self, work_key: str) -> None:
        """Release a held agent for ``work_key`` (it writes its sentinel, the
        executor frees the claim)."""
        self._signal(work_key, "finish")

    def crash(self, work_key: str) -> None:
        """Make a held agent exit WITHOUT its sentinel (the terminal was killed);
        the executor's claim then frees via the liveness lease, not completion."""
        self._signal(work_key, "crash")

    def _signal(self, work_key: str, kind: str) -> None:
        safe = "".join(c if c.isalnum() else "_" for c in work_key) or "job"
        (self.hold_dir / f"{safe}.{kind}").write_text("1")

    def runs(self) -> list[dict]:
        try:
            return [json.loads(ln) for ln in self.runs_file.read_text().splitlines() if ln]
        except OSError:
            return []

    def runners_of(self, work_key: str) -> list[str]:
        """Every node that STARTED an agent for ``work_key`` (order of start)."""
        return [r["node"] for r in self.runs()
                if r.get("event") == "start" and r.get("work") == work_key]

    def active_runners(self, work_key: str) -> list[str]:
        """Nodes whose agent for ``work_key`` started but hasn't ended (holding)."""
        starts, ends = [], []
        for r in self.runs():
            if r.get("work") != work_key:
                continue
            (starts if r.get("event") == "start" else ends).append(r.get("pid"))
        live = [p for p in starts if p not in ends]
        return [r["node"] for r in self.runs()
                if r.get("event") == "start" and r.get("work") == work_key
                and r.get("pid") in live]

    def wait_run(self, work_key: str, count: int = 1, timeout: float = 12.0) -> bool:
        """Wait until at least ``count`` distinct starts exist for ``work_key``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.runners_of(work_key)) >= count:
                return True
            time.sleep(0.1)
        return False

    def settle(self, seconds: float = 2.0) -> None:
        """Let gossip / any (forbidden) extra dispatch surface before asserting."""
        time.sleep(seconds)


# MARK: - scenarios ---------------------------------------------------------

class SimFailure(AssertionError):
    pass


def _fresh(root: Path, name: str) -> Simulator:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return Simulator(root=d)


def _three_personal(sim: Simulator) -> None:
    sim.add(NodeSpec("aaaa1111", "alpha", "linux", tier=3))
    sim.add(NodeSpec("bbbb2222", "bravo", "macos", tier=2))
    sim.add(NodeSpec("cccc3333", "carol", "macos", tier=4))


def scenario_exactly_once(root: Path) -> None:
    """All three nodes scan the same review at once → exactly one agent runs."""
    sim = _fresh(root, "exactly_once")
    _three_personal(sim)
    sim.start()
    try:
        sim.await_links()
        wk = "review:github.com/acme/app#1@aaa"
        sim.inject(wk, from_nodes=list(sim.procs), concurrent=True)
        if not sim.wait_run(wk, 1):
            raise SimFailure("no agent ran the work (silent drop)")
        sim.settle(2.0)
        runners = sim.runners_of(wk)
        if len(runners) != 1:
            raise SimFailure(f"expected exactly one run, got {runners}")
    finally:
        sim.stop_all()


def scenario_no_double_on_reinject(root: Path) -> None:
    """While one agent holds the work, every re-scan is suppressed (no 2nd run)."""
    sim = _fresh(root, "no_double")
    _three_personal(sim)
    sim.start()
    try:
        sim.await_links()
        wk = "review:github.com/acme/app#2@bbb"
        sim.inject(wk, from_node="aaaa1111")
        if not sim.wait_run(wk, 1):
            raise SimFailure("no agent ran the work")
        # Re-scan from every node while the agent is still holding.
        for _ in range(3):
            sim.inject(wk, from_nodes=list(sim.procs), concurrent=True)
            sim.settle(1.0)
        runners = sim.runners_of(wk)
        if len(runners) != 1:
            raise SimFailure(f"work double-dispatched while in flight: {runners}")
        sim.finish(wk)
    finally:
        sim.stop_all()


def scenario_best_fit(root: Path) -> None:
    """The run lands on the highest-surplus node, whoever found the work."""
    sim = _fresh(root, "best_fit")
    sim.add(NodeSpec("aaaa1111", "alpha", "linux", tier=3, quota_left=1.0, usage=0.9))
    sim.add(NodeSpec("bbbb2222", "bravo", "linux", tier=3, quota_left=10.0, usage=0.0))  # richest
    sim.add(NodeSpec("cccc3333", "carol", "linux", tier=3, quota_left=2.0, usage=1.0))
    sim.start()
    try:
        sim.await_links()
        sim.apply_stats()
        sim.settle(1.5)
        wk = "review:github.com/acme/app#3@ccc"
        sim.inject(wk, from_node="aaaa1111")  # the poorest node finds it
        if not sim.wait_run(wk, 1):
            raise SimFailure("no agent ran the work")
        sim.settle(1.5)
        runners = sim.runners_of(wk)
        if runners != ["bravo"]:
            raise SimFailure(f"expected the richest node 'bravo' to run it, got {runners}")
        sim.finish(wk)
    finally:
        sim.stop_all()


def scenario_failover(root: Path) -> None:
    """Kill the node running the work → a survivor takes it over."""
    sim = _fresh(root, "failover")
    _three_personal(sim)
    sim.start()
    try:
        sim.await_links()
        wk = "review:github.com/acme/app#4@ddd"
        sim.inject(wk, from_node="aaaa1111")
        if not sim.wait_run(wk, 1):
            raise SimFailure("no agent ran the work")
        runner_name = sim.runners_of(wk)[0]
        runner_id = next(s.node_id for s in sim.specs if s.name == runner_name)
        sim.kill(runner_id)
        # The lease lapses on timeout; re-scan from a survivor must now run it.
        survivors = [n for n in sim.procs]
        ran = False
        for _ in range(15):
            sim.inject(wk, from_nodes=survivors, concurrent=True)
            if sim.wait_run(wk, 2, timeout=2.0):
                ran = True
                break
            time.sleep(1.0)
        if not ran:
            raise SimFailure("work was never taken over after the runner died")
        second = [r for r in sim.runners_of(wk) if r != runner_name]
        if not second:
            raise SimFailure(f"takeover ran on the dead node again: {sim.runners_of(wk)}")
    finally:
        sim.stop_all()


def scenario_retry_after_completion(root: Path) -> None:
    """When the agent finishes (claim freed) a later scan may run it again."""
    sim = _fresh(root, "retry")
    _three_personal(sim)
    sim.start()
    try:
        sim.await_links()
        wk = "review:github.com/acme/app#5@eee"
        sim.inject(wk, from_node="aaaa1111")
        if not sim.wait_run(wk, 1):
            raise SimFailure("no agent ran the work")
        sim.finish(wk)  # the agent completes and frees the claim
        ran_again = False
        for _ in range(10):
            sim.inject(wk, from_nodes=list(sim.procs), concurrent=True)
            if sim.wait_run(wk, 2, timeout=1.5):
                ran_again = True
                break
            time.sleep(0.5)
        if not ran_again:
            raise SimFailure("a freed work key was never re-runnable (retry lost)")
    finally:
        sim.stop_all()


SCENARIOS = {
    "exactly_once": scenario_exactly_once,
    "no_double_on_reinject": scenario_no_double_on_reinject,
    "best_fit": scenario_best_fit,
    "failover": scenario_failover,
    "retry_after_completion": scenario_retry_after_completion,
}


def main(argv: list[str] | None = None) -> int:
    import tempfile

    argv = sys.argv[1:] if argv is None else argv
    if not loopback_multicast_works():
        print("SKIP: loopback multicast unavailable", file=sys.stderr)
        return 0
    names = argv or list(SCENARIOS)
    root = Path(tempfile.mkdtemp(prefix="mesh-sim-"))
    failed = 0
    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"?? unknown scenario {name!r} (have: {', '.join(SCENARIOS)})")
            failed += 1
            continue
        t0 = time.monotonic()
        try:
            fn(root)
            print(f"PASS  {name}  ({time.monotonic() - t0:.1f}s)")
        except Exception as exc:  # noqa: BLE001 — report every scenario's outcome
            print(f"FAIL  {name}  ({time.monotonic() - t0:.1f}s): {exc}")
            failed += 1
    print(f"\n{len(names) - failed}/{len(names)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
