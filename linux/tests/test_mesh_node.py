"""Mesh integration tests: real nodes, real sockets, one machine.

Spins actual ``python -m diplomat_app.mesh`` node processes on loopback
(DIPLOMAT_MESH_LOOPBACK=1 keeps every socket on 127.0.0.1; multicast loops back
locally) with fast protocol timings, then asserts the behaviours the design
promises: discovery convergence, deterministic cross-node assignment
agreement, duty takeover when a node dies, remote attribute edits, LWW
placement-override gossip, and per-slot dispatch with token failover.

Each fake node gets its own DIPLOMAT_MESH_DIR (identity + state.json) and a
platform override, so a single Linux CI runner hosts a mixed linux/macos
fleet. Dispatch lands via DIPLOMAT_MESH_SPAWN (a `cp` template) instead of a
real terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

LINUX_DIR = Path(__file__).resolve().parents[1]


def _loopback_multicast_works() -> bool:
    """Probe once whether a node can actually discover itself over loopback
    multicast. On a hardened/network-namespaced CI container multicast may be
    unavailable — there we skip these real-socket tests rather than let every
    ``_wait_for`` burn its 15s deadline. GitHub's ubuntu-latest passes this."""
    import socket
    import struct

    group, port = "239.83.77.9", 40899
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


pytestmark = pytest.mark.skipif(
    not _loopback_multicast_works(),
    reason="loopback multicast unavailable (hardened/namespaced container?)",
)

# Unique-ish ports per run so parallel/leftover runs can't collide.
_PORT_BASE = 42000 + (os.getpid() % 400) * 20


def _proto_env() -> dict:
    return {
        "DIPLOMAT_MESH_LOOPBACK": "1",
        # Offline + deterministic: never let a fleet node probe the real OAuth
        # usage endpoint (on macOS dev machines the Keychain token would resolve
        # even under a sandboxed HOME). Token states come from seeded logs/pins.
        "DIPLOMAT_MESH_OAUTH_PROBE": "0",
        "DIPLOMAT_MESH_MCAST_PORT": str(_PORT_BASE),
        "DIPLOMAT_MESH_TCP_BASE": str(_PORT_BASE + 1),
        "DIPLOMAT_MESH_TCP_SPAN": "12",
        "DIPLOMAT_MESH_BEACON_SECS": "0.25",
        "DIPLOMAT_MESH_HEARTBEAT_SECS": "0.25",
        "DIPLOMAT_MESH_STALE_SECS": "1.0",
        "DIPLOMAT_MESH_TIMEOUT_SECS": "2.0",
        "DIPLOMAT_MESH_ACK_SECS": "4.0",
        "DIPLOMAT_MESH_STATE_SECS": "0.25",
    }


class Fleet:
    """A handful of real mesh-node subprocesses sharing one loopback mesh."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.procs: dict[str, subprocess.Popen] = {}
        self.dirs: dict[str, Path] = {}

    def start(self, node_id: str, name: str, platform: str, tier: int,
              tokens: str = "ok", secret: str = "", server: bool = False,
              api_key: str = "", default_trust: str = "", extra_env: dict | None = None) -> None:
        d = self.root / node_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "node.json").write_text(json.dumps({
            "id": node_id, "name": name, "tier": tier,
            "tokens": tokens, "dutiesEnabled": {},
        }))
        (self.root / "spawned").mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(_proto_env())
        env["DIPLOMAT_MESH_DIR"] = str(d)
        env["DIPLOMAT_MESH_PLATFORM"] = platform
        env["DIPLOMAT_MESH_SPAWN"] = f"cp {{prompt_file}} {self.root}/spawned/{name}.txt"
        env["DIPLOMAT_MESH_SECRET"] = secret
        # A dedicated server never dispatches to peers; an API key (when set) gates
        # inbound control + dispatch. Both off by default so ordinary nodes are
        # unaffected.
        env["DIPLOMAT_MESH_SERVER"] = "1" if server else ""
        env["DIPLOMAT_MESH_API_KEY"] = api_key
        # Full-trust fleet mode: a fleet of the user's own machines that all trust
        # each other. Left unset, a node uses the shipped default (foreign), so the
        # trust-boundary tests still exercise zero-trust by default.
        if default_trust:
            env["DIPLOMAT_MESH_DEFAULT_TRUST"] = default_trust
        (d / "secret").write_text(secret)  # remembered for this node's CLI calls
        # Each fake node logs to the fleet dir, and must not scribble on the
        # real ~/.argent activity feed.
        env["HOME"] = str(d)
        # Last so a test can override anything above (e.g. isolate a node's
        # beacon channel on its own multicast port).
        env.update(extra_env or {})
        self.procs[node_id] = subprocess.Popen(
            [sys.executable, "-m", "diplomat_app.mesh"],
            cwd=LINUX_DIR, env=env,
            stdout=(d / "node.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        self.dirs[node_id] = d

    def state(self, node_id: str) -> dict:
        try:
            return json.loads((self.dirs[node_id] / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def cli(self, node_id: str, *args: str, timeout: float = 30.0,
            secret: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(_proto_env())
        env["DIPLOMAT_MESH_DIR"] = str(self.dirs[node_id])
        env["HOME"] = str(self.dirs[node_id])
        env["DIPLOMAT_MESH_SECRET"] = (
            secret if secret is not None
            else (self.dirs[node_id] / "secret").read_text()
        )
        return subprocess.run(
            [sys.executable, "-m", "diplomat_app.mesh", *args],
            cwd=LINUX_DIR, env=env, capture_output=True, text=True, timeout=timeout,
        )

    def kill(self, node_id: str) -> None:
        proc = self.procs.pop(node_id, None)
        if proc:
            proc.kill()
            proc.wait(timeout=10)

    def stop_all(self) -> None:
        for node_id in list(self.procs):
            self.kill(node_id)


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.2, what: str = ""):
    """Poll until the predicate returns a truthy value; fail loudly with its
    last value otherwise (network tests must never hang silently)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {what or predicate} (last: {last!r})")


@pytest.fixture()
def fleet(tmp_path):
    f = Fleet(tmp_path)
    yield f
    f.stop_all()


def _links_up(state: dict, expect_peers: int) -> bool:
    peers = state.get("peers", [])
    return len([p for p in peers if p.get("link") == "up"]) >= expect_peers


def _assignments(state: dict) -> dict:
    return {k: tuple(v.get("assigned", []))
            for k, v in (state.get("assignments") or {}).items()}


def _wait_file(path: Path, expect: str, timeout: float = 8.0) -> None:
    """A dispatch spawns fire-and-forget (Popen), so its stub file appears
    asynchronously — poll for it rather than reading immediately (racy under
    load)."""
    _wait_for(
        lambda: path.exists() and path.read_text() == expect,
        timeout=timeout,
        what=f"{path.name} to hold {expect!r}",
    )


def test_mesh_discovery_assignment_failover_and_dispatch(fleet):
    """One flow, one fleet: cheaper than a fleet per assertion, and closer to
    the real lifecycle (a mesh lives through all of these in sequence)."""
    # The user's fleet: a Linux box + a strong and a weak MacBook. A fleet of your
    # own machines runs in full-trust mode (default trust personal), so peers dispatch
    # to each other without per-device promotion.
    fleet.start("aaaa", "lin", "linux", tier=4, default_trust="personal")
    fleet.start("bbbb", "mac-strong", "macos", tier=1, default_trust="personal")
    fleet.start("cccc", "mac-weak", "macos", tier=4, default_trust="personal")

    # 1. Discovery: every node links to both others.
    for nid in ("aaaa", "bbbb", "cccc"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 2),
                  what=f"{nid} to link 2 peers")

    # 1b. The console fields land in the snapshot: self runs with a real uptime,
    #     a pinned strength (explicit tier), an auto quota %, and a `linking`
    #     count; each up peer carries a real connection uptime (the "up 0s" fix).
    st = fleet.state("aaaa")
    me = st["self"]
    assert isinstance(me.get("uptimeSecs"), (int, float)) and me["uptimeSecs"] >= 0
    assert me.get("strengthAuto") is False  # explicit tier pins strength
    assert 0.0 <= me.get("tokensPct", -1) <= 1.0
    assert isinstance(st.get("linking"), int)
    for peer in st["peers"]:
        if peer.get("link") in ("up", "stale"):
            assert isinstance(peer.get("uptimeSecs"), (int, float))
            assert peer["uptimeSecs"] >= 0

    # 2. Deterministic agreement: all three nodes publish identical assignments.
    def agreed():
        views = [_assignments(fleet.state(n)) for n in ("aaaa", "bbbb", "cccc")]
        return views[0] if (views[0] and views[0] == views[1] == views[2]) else None

    assignments = _wait_for(agreed, what="all nodes to agree on assignments")
    # weakest-first: grunt duties land on the weak machines, never the strong mac.
    assert assignments["review"] == ("aaaa",)
    assert assignments["conflicts"] == ("aaaa",)
    assert assignments["audit"] == ("aaaa", "cccc")  # one linux + one (weak) macos

    # 3. Dispatch: the audit spreads onto exactly the two assigned machines.
    r = fleet.cli("aaaa", "--dispatch", "audit", "--prompt", "bundle e2e please")
    assert r.returncode == 0, r.stdout + r.stderr
    spawned = fleet.root / "spawned"
    _wait_file(spawned / "lin.txt", "bundle e2e please")
    _wait_file(spawned / "mac-weak.txt", "bundle e2e please")
    assert not (spawned / "mac-strong.txt").exists()

    # 4. Token failover at dispatch time: the weak mac runs out of tokens →
    #    the macos slot fails over to the strong mac (edited REMOTELY from lin).
    r = fleet.cli("aaaa", "--set", "tokens=out", "--node", "cccc")
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_for(
        lambda: _assignments(fleet.state("aaaa")).get("audit") == ("aaaa", "bbbb"),
        what="audit's macos slot to fail over to mac-strong",
    )
    r = fleet.cli("aaaa", "--dispatch", "audit", "--prompt", "second run")
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_file(spawned / "mac-strong.txt", "second run")

    # 5. LWW override gossip: flip review to strongest-first on ONE node; every
    #    node converges on the same new owner.
    r = fleet.cli("bbbb", "--set", "tokens=ok", "--node", "cccc")  # restore first
    assert r.returncode == 0
    import socket as _socket  # override edit goes through the ctl protocol

    env_dir = fleet.dirs["bbbb"]
    port = json.loads((env_dir / "state.json").read_text())["tcpPort"]
    with _socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        f = sock.makefile("rwb")
        f.write(b'{"t":"ctl","v":1}\n')
        f.write(json.dumps({
            "t": "set-overrides", "duty": "review",
            "placement": {"strategy": "strongest-first", "tokenAware": True, "spread": []},
        }).encode() + b"\n")
        f.flush()
        assert json.loads(f.readline())["t"] == "ok"
    for nid in ("aaaa", "bbbb", "cccc"):
        _wait_for(
            lambda nid=nid: _assignments(fleet.state(nid)).get("review") == ("bbbb",),
            what=f"{nid} to adopt the strongest-first override",
        )

    # 6. Failover on death: kill the weak mac; both survivors move the audit's
    #    macos slot to the strong mac and mark the peer down.
    fleet.kill("cccc")
    for nid in ("aaaa", "bbbb"):
        _wait_for(
            lambda nid=nid: _assignments(fleet.state(nid)).get("audit") == ("aaaa", "bbbb"),
            what=f"{nid} to reassign the audit after mac-weak died",
        )
    down = [p for p in fleet.state("aaaa")["peers"] if p["id"] == "cccc"]
    assert down and down[0]["link"] == "down"

    # 7. The takeover is visible in each survivor's activity feed (HOME is the
    #    node dir, so the shared audit.jsonl lands inside the fixture).
    feed = (fleet.dirs["aaaa"] / ".argent" / "pr-monitor" / "audit.jsonl").read_text()
    assert "mesh-takeover" in feed and "mesh-peer-down" in feed


def test_secret_fences_peers_and_control(fleet):
    """With DIPLOMAT_MESH_SECRET set, a wrong-secret node never links (it can
    beacon all it wants) and a wrong-secret CLI can't drive the node."""
    fleet.start("aaaa", "lin", "linux", tier=4, secret="hunter2")
    fleet.start("bbbb", "mac", "macos", tier=1, secret="hunter2")
    fleet.start("dddd", "intruder", "linux", tier=1, secret="wrong")
    _wait_for(lambda: _links_up(fleet.state("aaaa"), 1), what="secret peers to link")

    # Give the intruder ample beacon rounds, then confirm nobody linked it.
    time.sleep(2.0)
    assert not any(p.get("link") == "up" for p in fleet.state("aaaa").get("peers", [])
                   if p.get("id") == "dddd")
    assert not any(p.get("link") == "up" for p in fleet.state("dddd").get("peers", []))
    # Grunt duties stay inside the fenced mesh, never on the intruder.
    assert _assignments(fleet.state("aaaa"))["review"] == ("aaaa",)

    # Control sessions honor the same fence.
    r = fleet.cli("aaaa", "--status", secret="wrong")
    assert "not answering" in (r.stdout + r.stderr)
    r = fleet.cli("aaaa", "--set", "tokens=low", secret="hunter2")
    assert r.returncode == 0


def test_outbound_dial_fence_rejects_naked_dispatch(fleet, tmp_path):
    """Regression for the outbound-dial fence bypass: a spoofed beacon makes the
    victim dial an attacker, who then sends a `dispatch` WITHOUT ever presenting
    a hello/secret. The victim must run nothing and never link the attacker.
    (Before the fix, `_run_link` processed that dispatch and spawned an agent.)"""
    import socket
    import struct
    import threading

    proto = _proto_env()
    group = "239.83.77.7"  # the real default group; loopback multicast delivers it
    mport = int(proto["DIPLOMAT_MESH_MCAST_PORT"])
    # The bypass "wins" if the victim SPAWNS at all: its own DIPLOMAT_MESH_SPAWN
    # stub (set by Fleet.start) copies the staged prompt here. The attacker
    # controls the prompt content, not the command — so the tell is that the
    # stub fired, not what it ran.
    landed = fleet.root / "spawned" / "victim.txt"

    # Victim node with a LOW id so it dials the (high-id) attacker, and a secret
    # set so we also prove the fence isn't the only thing standing in the way.
    fleet.start("0000victim", "victim", "linux", tier=4, secret="s3cr3t")
    victim_dir = fleet.dirs["0000victim"]
    _wait_for(lambda: (victim_dir / "state.json").exists(), what="victim to boot")

    attacker_id = "ffffffffffffffffffffffffffffffff"
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(4)
    attacker_port = listen.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listen.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # The victim dials in and sends its hello; we ignore it and shove a
            # naked dispatch (no hello, no secret) — the bypass we're guarding.
            with conn:
                conn.recv(65536)
                job = {"t": "dispatch", "v": 1, "job": {
                    "id": "evil", "duty": "review", "requestedBy": attacker_id,
                    "requestedAt": 0, "prompt": "attacker-controlled payload"}}
                try:
                    conn.sendall((json.dumps(job) + "\n").encode())
                    conn.recv(4096)  # let the victim answer/close
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()

    beacon = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    beacon.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    beacon.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    payload = json.dumps({"t": "beacon", "v": 1, "id": attacker_id, "name": "evil",
                          "platform": "linux", "tcpPort": attacker_port,
                          "epoch": 1}).encode()
    try:
        # Beacon at the victim for ~4s; if the bypass existed the dispatch would
        # land near-immediately on the first dial.
        for _ in range(16):
            beacon.sendto(payload, (group, mport))
            if landed.exists():
                break
            time.sleep(0.25)
        assert not landed.exists(), "attacker dispatch executed — fence bypassed!"
        # And the attacker never became a live peer.
        peers = fleet.state("0000victim").get("peers", [])
        assert not any(p.get("id") == attacker_id and p.get("link") == "up"
                       for p in peers)
    finally:
        stop.set()
        beacon.close()
        listen.close()


def test_foreign_device_declined_until_its_key_is_trusted(fleet):
    """Trust binds to a PROVEN device key against a LOCAL allowlist, never to an
    advertised field. Alice's request to Bob is declined while Alice's key isn't
    in Bob's allowlist, and runs once Bob trusts that exact fingerprint - so a
    spoofed advertisement could never have promoted Alice to personal."""
    fleet.start("aaaa", "alice", "linux", tier=4)
    fleet.start("bbbb", "bob", "macos", tier=1)
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    spawned = fleet.root / "spawned"

    # Wait until Bob has cryptographically VERIFIED Alice's device on the link.
    def alice_seen_by_bob():
        return next((p for p in fleet.state("bbbb").get("peers", [])
                     if p.get("id") == "aaaa" and p.get("verified")), None)
    alice_peer = _wait_for(alice_seen_by_bob, what="Bob to verify Alice's device key")
    alice_fp = alice_peer["fingerprint"]
    assert len(alice_fp) == 64

    # Turn ON Bob's trust boundary WITHOUT trusting Alice: Bob trusts only itself.
    # (Any non-empty allowlist enables the boundary; Alice, unlisted, is foreign.)
    bob_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_fp, "--label", "self").returncode == 0

    # Foreign: Alice's proven key isn't in Bob's allowlist -> declined, nothing runs.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "foreign",
                  "--target", "bbbb")
    assert r.returncode == 1 and "declined" in r.stdout, r.stdout + r.stderr
    time.sleep(0.5)
    assert not (spawned / "bob.txt").exists()

    # Now Bob trusts Alice's device fingerprint -> personal -> the request runs.
    assert fleet.cli("bbbb", "--trust", alice_fp, "--label", "alice").returncode == 0
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "personal",
                  "--target", "bbbb")
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_file(spawned / "bob.txt", "personal")


def test_foreign_request_runs_confined_and_routes_result_back(fleet):
    """The foreign zero-trust path, end to end over real sockets. Alice is FOREIGN to
    Bob; she dispatches a review. Bob does NOT decline — with a confinement runner
    configured he runs the compute SANDBOXED (never the host spawn/`gh` path) and
    returns the computed artifact as a `job-result`. Alice acks it and performs the
    social action herself, under her own identity. Proves the whole
    request → confined-compute → response → ack → originator-acts loop, and that the
    foreign job never touched Bob's host execution path."""
    root = fleet.root
    (root / "acted").mkdir(exist_ok=True)
    # Bob's sandbox stub stands in for a container/jail: it copies the (preamble-
    # prefixed) prompt where we can inspect it, then writes a canned review to the
    # result file the node handed it. In real use this is `docker run …`.
    bob_confined = root / "spawned" / "bob-confined.txt"
    foreign_spawn = (f"sh -c 'cp {{prompt_file}} {bob_confined}; "
                     f"printf REVIEW-BY-BOB > {{result_file}}'")
    # Alice's result handler is where the social action runs under HER identity (here
    # a stub that just captures the returned artifact; in real use it runs `gh`).
    alice_acted = root / "acted" / "alice.json"
    on_result = f"cp {{result_file}} {alice_acted}"
    # Fast delivery timers so the response/ack loop resolves within the test window.
    fast = {"DIPLOMAT_MESH_RESULT_RETRY_SECS": "0.5",
            "DIPLOMAT_MESH_RESULT_MAX_SECS": "30",
            "DIPLOMAT_MESH_FOREIGN_TIMEOUT_SECS": "20"}

    fleet.start("aaaa", "alice", "linux", tier=4,
                extra_env={**fast, "DIPLOMAT_MESH_ON_RESULT": on_result})
    fleet.start("bbbb", "bob", "macos", tier=1,
                extra_env={**fast, "DIPLOMAT_MESH_FOREIGN_SPAWN": foreign_spawn})
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")

    # Bob verifies Alice's key, then trusts only himself → Alice is foreign to Bob.
    _wait_for(lambda: next((p for p in fleet.state("bbbb").get("peers", [])
                            if p.get("id") == "aaaa" and p.get("verified")), None),
              what="Bob to verify Alice's device")
    bob_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_fp, "--label", "self").returncode == 0

    # Alice dispatches to Bob. Bob accepts (`spawned` — the hand-off ack) and runs it
    # confined; the artifact comes back asynchronously as a job-result.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "please review #123",
                  "--target", "bbbb")
    assert r.returncode == 0 and "spawned" in r.stdout, r.stdout + r.stderr

    # 1. Bob ran the compute in the confinement runner, on the response-only prompt.
    _wait_for(lambda: bob_confined.exists(), what="Bob's confinement runner to run")
    confined_prompt = bob_confined.read_text()
    assert "zero-trust execution" in confined_prompt   # the response-only preamble
    assert "please review #123" in confined_prompt     # the actual request

    # 2. The foreign job NEVER took Bob's host spawn path (that stub writes bob.txt).
    time.sleep(0.5)
    assert not (root / "spawned" / "bob.txt").exists(), "foreign job hit the host path!"

    # 3. The result routed back and Alice acted on it under her own identity.
    _wait_for(lambda: alice_acted.exists(), what="Alice to act on Bob's returned result")
    payload = json.loads(alice_acted.read_text())
    assert payload["from"] == "bbbb" and payload["duty"] == "review"
    assert payload["output"] == "REVIEW-BY-BOB"

    # 4. Reliable delivery: Bob's pending result clears once Alice's ack lands.
    _wait_for(lambda: fleet.state("bbbb").get("foreign", {}).get("pendingResults") == 0,
              what="Bob's job-result to be acked and dropped")


def _accountability_env(deadline: str = "2", grace: str = "2") -> dict:
    """Fast accountability timings: the 6-hour completion deadline and 15-min
    reminder grace shrink to seconds so a test observes the whole
    accept → deadline → reminder → resolution cycle."""
    return {"DIPLOMAT_MESH_COMPLETION_DEADLINE_SECS": deadline,
            "DIPLOMAT_MESH_REMINDER_GRACE_SECS": grace,
            "DIPLOMAT_MESH_RESULT_RETRY_SECS": "0.5",
            "DIPLOMAT_MESH_RESULT_MAX_SECS": "30",
            "DIPLOMAT_MESH_FOREIGN_TIMEOUT_SECS": "30"}


def _make_bob_foreign_to_alice(fleet) -> str:
    """Alice trusts only herself, so Bob — verified but unlisted — is FOREIGN to
    her and his acceptance arms the accountability clock. Returns Bob's
    fingerprint as Alice verified it."""
    bob_peer = _wait_for(
        lambda: next((p for p in fleet.state("aaaa").get("peers", [])
                      if p.get("id") == "bbbb" and p.get("verified")), None),
        what="Alice to verify Bob's device key")
    alice_fp = fleet.state("aaaa")["self"]["fingerprint"]
    assert fleet.cli("aaaa", "--trust", alice_fp, "--label", "self").returncode == 0
    return bob_peer["fingerprint"]


def test_foreign_acceptance_unfulfilled_reminder_bans_the_device(fleet):
    """The accountability contract, end to end: Bob (foreign to Alice) ACCEPTS her
    SzpontRequest and doesn't deliver within the (shrunken) completion deadline.
    Alice sends the "is this ready?" reminder; Bob truthfully answers "still
    running" — but Alice has no extension decider configured, so the plea cannot
    save him: Alice BANS Bob, marks him for the operator (banned.json + snapshot),
    declines everything from him, and refuses to dispatch to him — until the
    operator unbans."""
    root = fleet.root
    # Bob accepts and computes forever: the runner never writes the result file.
    slow_spawn = "sh -c 'sleep 300'"
    fleet.start("aaaa", "alice", "linux", tier=4,
                extra_env=_accountability_env())
    fleet.start("bbbb", "bob", "macos", tier=1,
                extra_env={**_accountability_env(),
                           "DIPLOMAT_MESH_FOREIGN_SPAWN": slow_spawn})
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    bob_fp = _make_bob_foreign_to_alice(fleet)
    # Bob needs Alice foreign too, else he'd run her request directly (personal
    # path, `direct` — which by design is never deadline-tracked).
    bob_self_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_self_fp, "--label", "self").returncode == 0

    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "review #1 please",
                  "--target", "bbbb")
    assert r.returncode == 0 and "spawned" in r.stdout, r.stdout + r.stderr

    # Deadline (2s) passes → reminder → Bob pleads "still running" → no decider
    # configured → ban. The mark is persisted and mirrored for the operator.
    def bob_banned():
        entries = fleet.state("aaaa").get("banned", [])
        return next((e for e in entries if e.get("fingerprint") == bob_fp), None)
    entry = _wait_for(bob_banned, timeout=20.0, what="Alice to ban Bob")
    assert "failed to deliver" in entry["reason"]
    assert "no extension decider" in entry["reason"]
    assert entry["label"] == "bob" and entry["jobId"]
    assert json.loads(
        (fleet.dirs["aaaa"] / "banned.json").read_text())["banned"], \
        "the ban must persist in banned.json"
    bob_view = next(p for p in fleet.state("aaaa")["peers"] if p["id"] == "bbbb")
    assert bob_view["trust"] == "banned"

    # Enforcement, both directions: Alice refuses to dispatch to Bob (locally,
    # without asking him), and declines anything Bob sends her.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "x", "--target", "bbbb")
    assert r.returncode == 1 and "target is banned here" in r.stdout, r.stdout
    r = fleet.cli("bbbb", "--dispatch", "review", "--prompt", "y", "--target", "aaaa")
    assert r.returncode == 1 and "banned device" in r.stdout, r.stdout
    time.sleep(0.5)
    assert not (root / "spawned" / "alice.txt").exists()

    # The operator's recovery path: unban → Bob is plain foreign again.
    assert fleet.cli("aaaa", "--unban", bob_fp).returncode == 0
    _wait_for(lambda: not fleet.state("aaaa").get("banned"),
              what="the ban to be lifted")
    bob_view = next(p for p in fleet.state("aaaa")["peers"] if p["id"] == "bbbb")
    assert bob_view["trust"] == "foreign"


def test_executor_that_vanishes_after_accepting_is_banned_for_silence(fleet):
    """A device that accepts work and then disappears breaks the same promise:
    the reminder goes unanswered (there is nobody to answer it) and the grace
    window ends in a ban for silence."""
    slow_spawn = "sh -c 'sleep 300'"
    fleet.start("aaaa", "alice", "linux", tier=4,
                extra_env=_accountability_env())
    fleet.start("bbbb", "bob", "macos", tier=1,
                extra_env={**_accountability_env(),
                           "DIPLOMAT_MESH_FOREIGN_SPAWN": slow_spawn})
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    bob_fp = _make_bob_foreign_to_alice(fleet)
    bob_self_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_self_fp, "--label", "self").returncode == 0

    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "review #2",
                  "--target", "bbbb")
    assert r.returncode == 0 and "spawned" in r.stdout, r.stdout + r.stderr
    fleet.kill("bbbb")  # accepted the work, then vanished

    def bob_banned():
        return next((e for e in fleet.state("aaaa").get("banned", [])
                     if e.get("fingerprint") == bob_fp), None)
    entry = _wait_for(bob_banned, timeout=20.0, what="Alice to ban vanished Bob")
    assert "no response to readiness reminder" in entry["reason"]


def test_agent_decider_extends_a_late_but_working_executor(fleet):
    """"6 is a minimum, not a cap": when the late executor is genuinely still
    working and an agent (the extension decider) rules the plea valid, the
    deadline re-arms instead of banning — and the eventually-delivered result is
    acted on normally. Nobody ends up banned."""
    root = fleet.root
    (root / "acted").mkdir(exist_ok=True)
    alice_acted = root / "acted" / "alice.json"
    decider_case = root / "acted" / "extend-case.json"
    # Bob's runner delivers late — after the first deadline, before forever.
    late_spawn = "sh -c 'sleep 5; printf DONE-LATE > {result_file}'"
    # Alice's decider is the stand-in agent: capture the case it judged, grant.
    decider = f"sh -c 'cp {{job_file}} {decider_case}; exit 0'"
    fleet.start("aaaa", "alice", "linux", tier=4,
                extra_env={**_accountability_env(deadline="2", grace="6"),
                           "DIPLOMAT_MESH_ON_RESULT": f"cp {{result_file}} {alice_acted}",
                           "DIPLOMAT_MESH_EXTEND_DECIDER": decider})
    fleet.start("bbbb", "bob", "macos", tier=1,
                extra_env={**_accountability_env(deadline="2", grace="6"),
                           "DIPLOMAT_MESH_FOREIGN_SPAWN": late_spawn})
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    _make_bob_foreign_to_alice(fleet)
    bob_self_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_self_fp, "--label", "self").returncode == 0

    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "review #3 slowly",
                  "--target", "bbbb")
    assert r.returncode == 0 and "spawned" in r.stdout, r.stdout + r.stderr

    # The agent judged Bob's plea (the case file carries it) and granted time…
    _wait_for(lambda: decider_case.exists(), timeout=20.0,
              what="the extension decider to judge Bob's plea")
    case = json.loads(decider_case.read_text())
    assert case["executor"]["node"] == "bbbb"
    assert "still running" in case["progressNote"]
    assert "review #3 slowly" in case["prompt"]
    # …so the late result still lands and Alice acts on it, and nobody is banned.
    _wait_for(lambda: alice_acted.exists(), timeout=25.0,
              what="Alice to act on Bob's late result")
    assert json.loads(alice_acted.read_text())["output"] == "DONE-LATE"
    assert not fleet.state("aaaa").get("banned")


def test_out_of_tokens_node_refuses_even_a_direct_target(fleet):
    """Refusals are first-class: a dispatcher may forward to whoever it likes,
    and the receiver may refuse. Bob is out of tokens and declines the request
    Alice sends him anyway (both owner-less → personal, so trust isn't the gate)."""
    fleet.start("aaaa", "alice", "linux", tier=4)
    fleet.start("bbbb", "bob", "macos", tier=1, tokens="out")
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "x", "--target", "bbbb")
    assert r.returncode == 1 and "declined" in r.stdout, r.stdout + r.stderr
    time.sleep(0.5)
    assert not (fleet.root / "spawned" / "bob.txt").exists()


def test_surplus_first_dispatch_picks_the_node_with_most_spare_quota(fleet):
    """Load balancing over real gossip: with no explicit target, the dispatcher
    ranks candidates surplus-first, so a request lands on whoever advertises the
    most spare quota — here the Max-20× machine, not the local or weakest node."""
    fleet.start("aaaa", "lin", "linux", tier=4, default_trust="personal")
    fleet.start("bbbb", "mac-big", "macos", tier=1, default_trust="personal")
    fleet.start("cccc", "mac-small", "macos", tier=4, default_trust="personal")
    for nid in ("aaaa", "bbbb", "cccc"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 2),
                  what=f"{nid} to link 2 peers")

    # Give mac-big the fattest account + full quota (one set: plan applies before
    # quotaLeft, so the 20 isn't clamped to the old 5x capacity).
    assert fleet.cli("aaaa", "--set", "plan=max-20x", "quotaLeft=20",
                     "--node", "bbbb").returncode == 0
    _wait_for(
        lambda: next((p for p in fleet.state("aaaa").get("peers", [])
                      if p["id"] == "bbbb"), {}).get("stats", {}).get("plan") == "max-20x",
        what="mac-big's stats to gossip to the dispatcher",
    )

    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "balance")
    assert r.returncode == 0, r.stdout + r.stderr
    spawned = fleet.root / "spawned"
    _wait_file(spawned / "mac-big.txt", "balance")
    # Surplus-first ignores locality and tier: neither the dispatcher nor the
    # weak mac (both max-5x, surplus 5) should have run it.
    assert not (spawned / "lin.txt").exists()
    assert not (spawned / "mac-small.txt").exists()


def test_node_restart_is_a_new_incarnation(fleet):
    fleet.start("aaaa", "lin", "linux", tier=4)
    fleet.start("bbbb", "mac", "macos", tier=1)
    _wait_for(lambda: _links_up(fleet.state("aaaa"), 1), what="initial link")

    # Restart the mac; the linux node must re-link with the NEW process
    # (epoch bump) rather than trusting the dead link.
    fleet.kill("bbbb")
    fleet.start("bbbb", "mac", "macos", tier=1)
    _wait_for(
        lambda: _links_up(fleet.state("aaaa"), 1)
        and _links_up(fleet.state("bbbb"), 1),
        what="re-link after restart",
    )
    # And the restarted node's view must converge to agreement again.
    _wait_for(
        lambda: _assignments(fleet.state("aaaa")) == _assignments(fleet.state("bbbb"))
        and _assignments(fleet.state("aaaa")),
        what="post-restart assignment agreement",
    )


def test_auto_token_state_reflects_real_usage(fleet):
    """A node with tokens='auto' derives ok/low/out from its OWN ~/.claude usage
    (the node's HOME is its fleet dir), with no peers and no manual dropdown."""
    from datetime import datetime, timezone
    # Seed the node's HOME/.claude with usage far over the max-5x ceiling BEFORE it
    # starts, so its first snapshot already reads 'out'.
    d = fleet.root / "solo0"
    proj = d / ".claude" / "projects" / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    (proj / "s.jsonl").write_text(json.dumps({
        "timestamp": now_iso,
        "message": {"usage": {"input_tokens": 20_000_000, "output_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }) + "\n")

    fleet.start("solo0", "solo", "linux", tier=3, tokens="auto")

    def _self_when_out():
        me = fleet.state("solo0").get("self", {})
        return me if me.get("tokens") == "out" else None

    me = _wait_for(_self_when_out,
                   what="auto token state to read 'out' from real usage")
    assert me["tokensAuto"] is True        # derived, not pinned
    assert me["tokens"] == "out"           # 20M > 10M ceiling
    assert me["tokensPct"] == 0.0
    # Heuristic fallback (probe disabled in fleets) never advertises the real
    # per-window percentages — the UIs then mark the estimate with '≈'.
    assert "tokensSessionPct" not in me and "tokensWeekPct" not in me


def test_pin_then_unpin_token_state_round_trips(fleet):
    """Pinning ok/low/out flips tokensAuto off in the snapshot (the panel's picker
    must show the pin, not 'Auto'); setting back to auto re-derives the state."""
    fleet.start("solo1", "pinme", "linux", tier=3, tokens="auto")
    _wait_for(lambda: fleet.state("solo1").get("self") or None, what="first snapshot")

    r = fleet.cli("solo1", "--set", "tokens=low")
    assert r.returncode == 0, r.stdout + r.stderr
    me = _wait_for(
        lambda: (fleet.state("solo1").get("self") or {}).get("tokens") == "low"
        and fleet.state("solo1")["self"] or None,
        what="pinned token state in the snapshot")
    assert me["tokensAuto"] is False and me["tokens"] == "low"

    r = fleet.cli("solo1", "--set", "tokens=auto")
    assert r.returncode == 0, r.stdout + r.stderr
    me = _wait_for(
        lambda: (fleet.state("solo1").get("self") or {}).get("tokensAuto")
        and fleet.state("solo1")["self"] or None,
        what="auto-derived token state after unpin")
    assert me["tokens"] in ("ok", "low", "out")  # derived again (empty logs → ok)


def test_server_mode_runs_locally_and_never_dispatches_to_peers(fleet):
    """A dedicated server (DIPLOMAT_MESH_SERVER=1) runs a request ITSELF and never
    fans it out — even to a weaker worker that weakest-first would otherwise pick."""
    fleet.start("aaaa", "server", "linux", tier=1, server=True)
    fleet.start("bbbb", "worker", "linux", tier=4)  # weaker → the default pick
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "srv")
    assert r.returncode == 0, r.stdout + r.stderr
    spawned = fleet.root / "spawned"
    _wait_file(spawned / "server.txt", "srv")
    time.sleep(0.5)
    assert not (spawned / "worker.txt").exists(), "a server must not dispatch to peers"
    # Explicitly aiming a server at a peer is refused, not fanned out.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "x", "--target", "bbbb")
    assert r.returncode == 1 and "declined" in r.stdout, r.stdout + r.stderr


def test_api_key_gates_requests_to_a_server(fleet):
    """A server with an API key declines a request that doesn't present it and runs
    one that does — the optional per-request server credential."""
    # Full-trust so the API key (not device trust) is the only gate under test.
    fleet.start("aaaa", "client", "linux", tier=4, default_trust="personal")
    fleet.start("bbbb", "server", "macos", tier=1, server=True, api_key="k3y",
                default_trust="personal")
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")
    spawned = fleet.root / "spawned"
    # No key → declined, nothing runs on the server.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "nokey", "--target", "bbbb")
    assert r.returncode == 1 and "declined" in r.stdout, r.stdout + r.stderr
    time.sleep(0.5)
    assert not (spawned / "server.txt").exists()
    # Correct key → accepted and run.
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "withkey",
                  "--target", "bbbb", "--api-key", "k3y")
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_file(spawned / "server.txt", "withkey")


def test_foreign_device_cannot_mutate_our_attrs_via_set_attr(fleet):
    """A set-attr from a FOREIGN device must be ignored — otherwise a stranger
    could flip our tokens/tier/duties and reshape placement mesh-wide. Trust binds
    to a proven key against a local allowlist, exactly like dispatch admission."""
    fleet.start("aaaa", "alice", "linux", tier=4)
    fleet.start("bbbb", "bob", "macos", tier=1)
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")

    # Bob verifies Alice's device on the link, then turns his boundary ON trusting
    # only himself — so Alice is foreign.
    def alice_verified():
        return next((p for p in fleet.state("bbbb").get("peers", [])
                     if p.get("id") == "aaaa" and p.get("verified")), None)
    _wait_for(alice_verified, what="Bob to verify Alice's key")
    bob_fp = fleet.state("bbbb")["self"]["fingerprint"]
    assert fleet.cli("bbbb", "--trust", bob_fp, "--label", "self").returncode == 0

    # Alice (foreign) forwards a set-attr to flip Bob to tokens=out. Bob ignores it.
    assert fleet.cli("aaaa", "--set", "tokens=out", "--node", "bbbb").returncode == 0
    time.sleep(1.0)
    assert fleet.state("bbbb")["self"]["tokens"] == "ok", \
        "a foreign device must not be able to mutate our attributes"


def test_spoofed_higher_epoch_beacon_does_not_evict_a_live_link(fleet):
    """An UNAUTHENTICATED beacon carrying a linked peer's id and a huge epoch must
    NOT tear down the healthy, verified link (a spoofed-restart hijack/DoS). The
    restart hint is only honored once the link has actually gone quiet."""
    import socket

    fleet.start("aaaa", "a", "linux", tier=4)
    fleet.start("bbbb", "b", "macos", tier=1)
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1), what=f"{nid} link")

    proto = _proto_env()
    group = "239.83.77.7"
    mport = int(proto["DIPLOMAT_MESH_MCAST_PORT"])
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    payload = json.dumps({"t": "beacon", "v": 1, "id": "bbbb", "name": "evil",
                          "platform": "macos", "tcpPort": 59999, "epoch": 9.9e17}).encode()
    try:
        for _ in range(12):  # keep beaconing across several link-liveness windows
            tx.sendto(payload, (group, mport))
            time.sleep(0.25)
    finally:
        tx.close()

    # The real link survived, and the victim never treated it as a restart.
    assert _links_up(fleet.state("aaaa"), 1)
    bob = next(p for p in fleet.state("aaaa")["peers"] if p["id"] == "bbbb")
    assert bob["link"] == "up"
    feed = (fleet.dirs["aaaa"] / ".argent" / "pr-monitor" / "audit.jsonl").read_text()
    assert "restarted" not in feed, "spoofed epoch beacon evicted a live link"


def test_redial_from_memory_relinks_without_beacons(fleet):
    """Two nodes whose beacon channels are fully ISOLATED (each multicasts on its
    own port, so neither ever hears the other) still link: the smaller-id node
    redials the bigger one from its persisted last-known-address cache. This is
    the recovery path for a mesh whose multicast/broadcast died under it (AP
    filtering, an OS privacy gate such as macOS Local Network) while unicast
    still works — before redial-from-memory, one dropped link meant the pair
    never re-formed."""
    bb_tcp = _PORT_BASE + 14
    # Seed aa's address cache with bb BEFORE the node starts (a survivor's
    # persisted memory of a peer it met earlier).
    aa_dir = fleet.root / "aa"
    aa_dir.mkdir(parents=True, exist_ok=True)
    (aa_dir / "peers.json").write_text(json.dumps(
        {"bb": {"addr": "127.0.0.1", "tcpPort": bb_tcp}}))
    fleet.start("bb", "bee", "linux", tier=3, extra_env={
        "DIPLOMAT_MESH_MCAST_PORT": str(_PORT_BASE + 16),
        "DIPLOMAT_MESH_TCP_BASE": str(bb_tcp), "DIPLOMAT_MESH_TCP_SPAN": "1",
    })
    fleet.start("aa", "aye", "macos", tier=2, extra_env={
        "DIPLOMAT_MESH_MCAST_PORT": str(_PORT_BASE + 17),
        "DIPLOMAT_MESH_TCP_BASE": str(_PORT_BASE + 15), "DIPLOMAT_MESH_TCP_SPAN": "1",
        "DIPLOMAT_MESH_REDIAL_SECS": "0.5",
    })
    _wait_for(lambda: _links_up(fleet.state("aa"), 1) and _links_up(fleet.state("bb"), 1),
              what="aa↔bb linked via redial-from-memory with beacons isolated")
    # And the link taught bb (which started with no cache) aa's dialable address
    # from the authenticated hello — persisted for ITS future redials.
    _wait_for(lambda: json.loads((fleet.dirs["bb"] / "peers.json").read_text())
              .get("aa", {}).get("addr") == "127.0.0.1"
              if (fleet.dirs["bb"] / "peers.json").exists() else False,
              what="bb persisted aa's last-known address")


def test_work_claim_dedupes_origination_and_frees_on_owner_death(fleet):
    """End-to-end origination dedup across two real nodes, and the liveness lease.

    Two personal machines (a full-trust fleet) both dispatch the SAME workKey. The
    lower-id node claims it first and runs the work; the higher-id node, hearing that
    claim, STANDS DOWN with a `suppressed` result instead of double-running. Then the
    owner is killed: its lease lapses on timeout, and the survivor's next dispatch of
    the same key is no longer suppressed — it takes the work over. This is the whole
    point of work-claims, proven over sockets. (A claim is only authoritative from a
    *personal* peer, so this fleet runs default-trust personal.)"""
    wk = "review:github.com/acme/app#123@abc123"
    fleet.start("aaaa", "low", "linux", tier=3, default_trust="personal")   # lower id → wins race
    fleet.start("bbbb", "high", "linux", tier=3, default_trust="personal")  # higher id → stands down
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1),
                  what=f"{nid} to link its peer")

    spawned = fleet.root / "spawned"

    # 1. The lower-id node claims the key and runs the work (review lands on it).
    r = fleet.cli("aaaa", "--dispatch", "review", "--prompt", "first", "--work-key", wk)
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_file(spawned / "low.txt", "first")

    # 2. The claim propagates: the higher-id node observes aaaa owns the key.
    _wait_for(lambda: fleet.state("bbbb").get("claims", {}).get(wk) == "aaaa",
              what="bbbb to observe aaaa's work-claim")

    # 3. The higher-id node dispatches the SAME key → suppressed, nothing re-runs.
    r = fleet.cli("bbbb", "--dispatch", "review", "--prompt", "second", "--work-key", wk)
    assert r.returncode == 0, r.stdout + r.stderr          # suppressed counts as success
    assert "suppressed" in r.stdout, r.stdout
    assert "low" in r.stdout, r.stdout                     # names the owner
    time.sleep(1.0)
    assert (spawned / "low.txt").read_text() == "first"    # not overwritten by "second"
    assert not (spawned / "high.txt").exists()             # bbbb never ran it

    # 4. The owner dies → its lease lapses → the survivor takes the work over.
    fleet.kill("aaaa")
    _wait_for(lambda: fleet.state("bbbb").get("claims", {}).get(wk) is None,
              what="aaaa's lease to lapse on bbbb after it went down")
    r = fleet.cli("bbbb", "--dispatch", "review", "--prompt", "third", "--work-key", wk)
    assert r.returncode == 0, r.stdout + r.stderr
    _wait_file(spawned / "high.txt", "third")              # bbbb now runs it


def test_ctl_claim_verb_gates_origination_without_dispatch(fleet):
    """The stand-alone ctl `claim` verb — the auto-monitors' origination gate
    (docs/szpontnet/04#claim--claim-result). Claiming marks this node the
    originator WITHOUT routing any job through the mesh; a peer's later claim of
    the same key is suppressed (exit 3, naming the owner); re-claiming one's own
    key is idempotent; and the key frees when the owner dies (liveness lease)."""
    wk = "review-reply:github.com/acme/app#7@beef00"
    fleet.start("aaaa", "low", "linux", tier=3, default_trust="personal")
    fleet.start("bbbb", "high", "linux", tier=3, default_trust="personal")
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1),
                  what=f"{nid} to link its peer")

    # 1. aaaa claims → owns the key → would originate.
    r = fleet.cli("aaaa", "--claim", wk)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "owned" in r.stdout

    # 2. The claim gossips; bbbb's claim of the same key is then suppressed.
    _wait_for(lambda: fleet.state("bbbb").get("claims", {}).get(wk) == "aaaa",
              what="bbbb to observe aaaa's claim")
    r = fleet.cli("bbbb", "--claim", wk)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "low" in r.stdout                               # names the owner

    # 3. Re-claiming one's own key is idempotent — a retry is never suppressed.
    r = fleet.cli("aaaa", "--claim", wk)
    assert r.returncode == 0, r.stdout + r.stderr

    # 4. None of this dispatched a job anywhere.
    spawned = fleet.root / "spawned"
    assert not spawned.exists() or not any(spawned.iterdir())

    # 5. The owner dies → its lease lapses → the survivor now owns the key.
    fleet.kill("aaaa")
    _wait_for(lambda: fleet.state("bbbb").get("claims", {}).get(wk) is None,
              what="aaaa's lease to lapse on bbbb")
    r = fleet.cli("bbbb", "--claim", wk)
    assert r.returncode == 0, r.stdout + r.stderr
