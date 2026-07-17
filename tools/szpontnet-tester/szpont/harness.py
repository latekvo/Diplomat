"""Per-scenario harness: launch one candidate inside a fresh isolated mesh.

Each conformance scenario gets its own multicast port + TCP port band + working
directory, so scenarios can run back-to-back without a lingering socket from the
previous candidate bleeding in. Fast protocol timings (sub-second beacons and
timeouts) keep the whole suite to a couple of minutes while preserving the
ordering the spec requires (``peerStaleSecs`` between heartbeat and timeout).
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from . import candidate as candmod
from . import probe
from .model import DEFAULT_PROTOCOL, Model

# Fast loopback timings — the same shape the reference's own socket tests use.
FAST_TIMINGS = {
    "beaconIntervalSecs": 0.25,
    "heartbeatIntervalSecs": 0.25,
    "peerStaleSecs": 1.0,
    "peerTimeoutSecs": 2.0,
    "dispatchAckTimeoutSecs": 4.0,
    "stateWriteIntervalSecs": 0.25,
    # Foreign (ch 13) reliable-delivery timings, scaled down like the rest so a
    # loopback scenario observes the executor's job-result retry/ack round-trip in
    # seconds (shared defaults are 5s / 120s / 900s).
    "foreignResultRetryIntervalSecs": 0.5,
    "foreignResultMaxSecs": 20.0,
    "foreignJobTimeoutSecs": 15.0,
}

# Distinct 32-hex ids with a/b/c prefixes so lexical id order is obvious.
ID_A = "a" * 32
ID_B = "b" * 32
ID_C = "c" * 32


class _Ports:
    """Hand out non-overlapping (mcast_port, tcp_base) pairs per scenario."""

    def __init__(self) -> None:
        # Seed off the pid so two tester runs on one host don't collide.
        self._n = 43000 + (os.getpid() % 300) * 40

    def next(self) -> tuple[int, int]:
        mcast, tcp_base = self._n, self._n + 1
        self._n += 40
        return mcast, tcp_base


PORTS = _Ports()


def fast_proto(mcast_port: int, tcp_base: int, group: str | None = None) -> dict:
    proto = dict(DEFAULT_PROTOCOL)
    proto.update(FAST_TIMINGS)
    proto["multicastPort"] = mcast_port
    proto["tcpPortBase"] = tcp_base
    proto["tcpPortSpan"] = 16
    if group:
        proto["multicastGroup"] = group
    return proto


class Scenario:
    """A launched candidate + a probe mesh, set up and torn down together."""

    def __init__(
        self, node_cmd: str, model: Model, *, candidate_id: str = ID_A,
        name: str = "cand", platform: str = "linux", tier: int = 4,
        tokens: str = "ok", duties: dict | None = None, secret: str = "",
        mesh_secret: str | None = None, loopback: bool = True,
        spawn_marker: Path | None = None, work_root: Path | None = None,
        server: bool = False, api_key: str = "", stats: dict | None = None,
        foreign_spawn: str = "", default_trust: str = "",
    ) -> None:
        self.node_cmd = shlex.split(node_cmd)
        self.model = model
        self.candidate_id = candidate_id
        self.name = name
        self.platform = platform
        self.tier = tier
        self.tokens = tokens
        self.duties = duties or {}
        self.secret = secret
        # Chapter-11 role knobs (default off → a plain ch 01-10 scenario).
        self.server = server
        self.api_key = api_key
        self.stats = stats
        # Chapter-13 confinement runner (default off → a foreign request is declined).
        self.foreign_spawn = foreign_spawn
        # Chapter-11 default trust level for unlisted devices (empty → candidate's own
        # shipped default, foreign for the reference).
        self.default_trust = default_trust
        # The secret the PROBE peers/clients present. Defaults to the candidate's,
        # but a fence test can set a *wrong* one to prove the candidate refuses it.
        self.mesh_secret = secret if mesh_secret is None else mesh_secret
        self.loopback = loopback
        self.mcast_port, self.tcp_base = PORTS.next()
        self.proto = fast_proto(self.mcast_port, self.tcp_base)
        self._root = work_root or Path(tempfile.mkdtemp(prefix="szpont-"))
        self.work_dir = self._root / candidate_id[:6]
        self.spawn_marker = spawn_marker or (self._root / "spawned")
        self.spawn_marker.mkdir(parents=True, exist_ok=True)
        self.candidate: candmod.Candidate | None = None
        self.mesh: probe.ProbeMesh | None = None
        self._peer_specs: list[dict] = []

    def add_peer(self, **kwargs) -> None:
        self._peer_specs.append(kwargs)

    def spawn_template(self) -> str:
        # Marker file named after this candidate: proves an executor actually ran.
        return f"cp {{prompt_file}} {self.spawn_marker}/{self.name}.txt"

    def confined_template(self) -> str:
        """A stand-in confinement runner (ch 13): it doesn't isolate anything — it
        just writes an artifact to ``{result_file}`` so the candidate produces a real
        ``job-result`` to return. Enough to exercise the confined → result-back →
        ack wire path black-box; the *isolation quality* of a real sandbox is the
        operator's concern, not the wire mechanism this case tests."""
        return "printf 'confined-review-ok' > {result_file}"

    def __enter__(self) -> "Scenario":
        env = candmod.contract_env(
            work_dir=self.work_dir, proto=self.proto, loopback=self.loopback,
            secret=self.secret, node_id=self.candidate_id, name=self.name,
            platform=self.platform, tier=self.tier, tokens=self.tokens,
            duties_enabled=self.duties, spawn_cmd=self.spawn_template(),
            server=self.server, api_key=self.api_key, stats=self.stats,
            foreign_spawn=(self.confined_template() if self.foreign_spawn == "auto"
                           else self.foreign_spawn),
            default_trust=self.default_trust,
        )
        self.candidate = candmod.Candidate(
            self.node_cmd, env, self.work_dir, secret=self.secret, api_key=self.api_key)
        self.mesh = probe.ProbeMesh(
            self.model, self.proto, self.candidate_id, self.loopback, self.mesh_secret)
        for spec in self._peer_specs:
            self.mesh.add_peer(**spec)
        self.candidate.start()
        self.mesh.start()
        return self

    def discover_port(self, timeout: float = 12.0) -> int | None:
        """Wait for the candidate's beacon and record its TCP port."""
        got = probe.wait_until(lambda: self.mesh.candidate.get("tcp_port"), timeout)
        if got:
            self.candidate.tcp_port = int(got)
        return self.candidate.tcp_port

    def __exit__(self, *exc) -> None:
        if self.mesh:
            self.mesh.stop()
        if self.candidate:
            self.candidate.stop()
