"""Mesh integration tests: real nodes, real sockets, one machine.

Spins actual ``python -m argent_utils.mesh`` node processes on loopback
(ARGENT_MESH_LOOPBACK=1 keeps every socket on 127.0.0.1; multicast loops back
locally) with fast protocol timings, then asserts the behaviours the design
promises: discovery convergence, deterministic cross-node assignment
agreement, duty takeover when a node dies, remote attribute edits, LWW
placement-override gossip, and per-slot dispatch with token failover.

Each fake node gets its own ARGENT_MESH_DIR (identity + state.json) and a
platform override, so a single Linux CI runner hosts a mixed linux/macos
fleet. Dispatch lands via ARGENT_MESH_SPAWN (a `cp` template) instead of a
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
        "ARGENT_MESH_LOOPBACK": "1",
        "ARGENT_MESH_MCAST_PORT": str(_PORT_BASE),
        "ARGENT_MESH_TCP_BASE": str(_PORT_BASE + 1),
        "ARGENT_MESH_TCP_SPAN": "12",
        "ARGENT_MESH_BEACON_SECS": "0.25",
        "ARGENT_MESH_HEARTBEAT_SECS": "0.25",
        "ARGENT_MESH_STALE_SECS": "1.0",
        "ARGENT_MESH_TIMEOUT_SECS": "2.0",
        "ARGENT_MESH_ACK_SECS": "4.0",
        "ARGENT_MESH_STATE_SECS": "0.25",
    }


class Fleet:
    """A handful of real mesh-node subprocesses sharing one loopback mesh."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.procs: dict[str, subprocess.Popen] = {}
        self.dirs: dict[str, Path] = {}

    def start(self, node_id: str, name: str, platform: str, tier: int,
              tokens: str = "ok", secret: str = "") -> None:
        d = self.root / node_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "node.json").write_text(json.dumps({
            "id": node_id, "name": name, "tier": tier,
            "tokens": tokens, "dutiesEnabled": {},
        }))
        (self.root / "spawned").mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(_proto_env())
        env["ARGENT_MESH_DIR"] = str(d)
        env["ARGENT_MESH_PLATFORM"] = platform
        env["ARGENT_MESH_SPAWN"] = f"cp {{prompt_file}} {self.root}/spawned/{name}.txt"
        env["ARGENT_MESH_SECRET"] = secret
        (d / "secret").write_text(secret)  # remembered for this node's CLI calls
        # Each fake node logs to the fleet dir, and must not scribble on the
        # real ~/.argent activity feed.
        env["HOME"] = str(d)
        self.procs[node_id] = subprocess.Popen(
            [sys.executable, "-m", "argent_utils.mesh"],
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
        env["ARGENT_MESH_DIR"] = str(self.dirs[node_id])
        env["HOME"] = str(self.dirs[node_id])
        env["ARGENT_MESH_SECRET"] = (
            secret if secret is not None
            else (self.dirs[node_id] / "secret").read_text()
        )
        return subprocess.run(
            [sys.executable, "-m", "argent_utils.mesh", *args],
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
    # The user's fleet: a Linux box + a strong and a weak MacBook.
    fleet.start("aaaa", "lin", "linux", tier=4)
    fleet.start("bbbb", "mac-strong", "macos", tier=1)
    fleet.start("cccc", "mac-weak", "macos", tier=4)

    # 1. Discovery: every node links to both others.
    for nid in ("aaaa", "bbbb", "cccc"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 2),
                  what=f"{nid} to link 2 peers")

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
    """With ARGENT_MESH_SECRET set, a wrong-secret node never links (it can
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
    mport = int(proto["ARGENT_MESH_MCAST_PORT"])
    # The bypass "wins" if the victim SPAWNS at all: its own ARGENT_MESH_SPAWN
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
    fleet.start("aaaa", "lin", "linux", tier=4)
    fleet.start("bbbb", "mac-big", "macos", tier=1)
    fleet.start("cccc", "mac-small", "macos", tier=4)
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
