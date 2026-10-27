"""A real node on a disk that fills up: it keeps serving, and saves once it can.

The state directory sits on a small real volume (``scripts/full-volume.sh``) filled to
ENOSPC while the node runs, so every write fails the way it did when this machine's
disk filled — create, append and rename alike. The node is loopback-only, with no
WAN transport, so it never meets a real mesh.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PACKAGE_ROOT.parents[1] / "scripts"))
from full_volume import full_volume  # noqa: E402,F401  (a fixture)

_PORT = 46200 + (os.getpid() % 150) * 4


def _env(state: Path, log: Path) -> dict:
    return {
        **os.environ,
        "SZPONTNET_DIR": str(state),
        "HOME": str(log.parent),
        "SZPONTNET_HOST": "fulldisk_host",
        "FULLDISK_LOG": str(log),
        "SZPONTNET_LOOPBACK": "1",
        "SZPONTNET_IROH": "0",
        "SZPONTNET_TOR": "0",
        "SZPONTNET_OAUTH_PROBE": "0",
        "SZPONTNET_MCAST_PORT": str(_PORT),
        "SZPONTNET_TCP_BASE": str(_PORT + 1),
        "SZPONTNET_TCP_SPAN": "2",
        "SZPONTNET_STATE_SECS": "0.2",
        "SZPONTNET_EXIT_WITH_PARENT": "1",
        "PYTHONPATH": os.pathsep.join([str(_PACKAGE_ROOT), str(Path(__file__).parent)]),
    }


def _seed(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "node.json").write_text(json.dumps({
        "id": "f" * 32, "name": "full-disk", "tier": 3, "tokens": "ok",
        "strengthAuto": False, "dutiesEnabled": {},
    }))


class _Node:
    def __init__(self, state: Path, tmp: Path) -> None:
        self.log = tmp / "host.log"
        self.out = tmp / "node.out"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "szpontnet"], env=_env(state, self.log),
            stdout=self.out.open("w"), stderr=subprocess.STDOUT)

    def ctl(self, msg: dict) -> dict:
        with socket.create_connection(("127.0.0.1", _PORT + 1), timeout=5) as sock:
            sock.sendall(b'{"t":"ctl"}\n' + json.dumps(msg).encode() + b"\n")
            return json.loads(sock.makefile("rb").readline())

    def await_serving(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, f"node exited:\n{self.out.read_text()}"
            try:
                if self.ctl({"t": "status"}).get("t") == "state":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        pytest.fail(f"node never answered a status request:\n{self.out.read_text()}")

    def lines(self, text: str) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text().splitlines() if text in ln]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def _fp(i: int) -> str:
    return f"{i + 1:064x}"


def _await(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _trusted(state: Path) -> set[str]:
    return {e.get("fingerprint") for e in _read(state / "trusted.json").get("trusted", [])}


def _banned(state: Path) -> set[str]:
    return {e.get("fingerprint") for e in _read(state / "banned.json").get("banned", [])}


def _ride_out_a_full_disk(node: _Node, vol, state: Path) -> None:
    """Edit through several ticks of a full disk, then drain it and watch the node
    save what it was told while it could not."""
    for i in range(3):
        reply = node.ctl({"t": "trust", "fingerprint": _fp(i), "label": f"peer {i}"})
        assert reply["t"] == "error" and "trusted.json" in reply["reason"], (
            f"a trust that could not be saved was answered {reply}")
        assert node.ctl({"t": "status"})["t"] == "state"
        time.sleep(0.5)  # a couple of snapshot ticks, each retrying what is owed
    reply = node.ctl({"t": "ban", "fingerprint": _fp(9), "reason": "test"})
    assert reply["t"] == "error" and "banned.json" in reply["reason"], reply
    assert node.proc.poll() is None, "the node died on a full disk"
    assert not _trusted(state) & {_fp(i) for i in range(3)}, "the disk took a write"
    assert len(node.lines("can't save")) == 1, node.lines("Mesh")

    vol.drain()
    assert _await(lambda: _trusted(state) >= {_fp(i) for i in range(3)}), (
        f"trusted.json never caught up: {_read(state / 'trusted.json')}")
    assert _await(lambda: _fp(9) in _banned(state)), (
        f"banned.json never caught up: {_read(state / 'banned.json')}")
    assert _await(lambda: node.lines("saved again")), node.lines("Mesh")
    assert node.ctl({"t": "trust", "fingerprint": _fp(5)})["t"] == "ok"
    assert node.proc.poll() is None
    assert vol.leftovers() == []


def test_a_node_keeps_serving_on_a_full_disk_and_saves_once_it_drains(
        full_volume, tmp_path):
    state = full_volume.path / "mesh"
    _seed(state)
    node = _Node(state, tmp_path)
    try:
        node.await_serving()
        full_volume.fill()
        _ride_out_a_full_disk(node, full_volume, state)
    finally:
        node.stop()


def test_a_node_starts_on_a_full_disk_with_the_identity_it_has(full_volume, tmp_path):
    """A restart while the disk is full: the identity is on disk, so the node runs."""
    state = full_volume.path / "mesh"
    _seed(state)
    first = _Node(state, tmp_path)
    try:
        first.await_serving()
    finally:
        first.stop()
    assert (state / "device.key").exists()
    full_volume.fill()

    node = _Node(state, tmp_path)
    try:
        node.await_serving()
        assert _read(state / "node.json")["id"] == "f" * 32
        _ride_out_a_full_disk(node, full_volume, state)
    finally:
        node.stop()


def test_a_first_run_on_a_full_disk_refuses_an_identity_it_would_lose(
        full_volume, tmp_path):
    """No identity on disk and no room for one: a node that ran anyway would be a
    stranger to every peer after its next restart. It says so and exits instead."""
    state = full_volume.path / "mesh"
    state.mkdir()
    full_volume.fill()
    node = _Node(state, tmp_path)
    try:
        assert _await(lambda: node.proc.poll() is not None, timeout=20.0), (
            "a node with no identity it could save kept running")
        out = node.out.read_text()
        assert node.proc.returncode == 1, out
        assert "mesh node not starting" in out and "couldn't save" in out, out
        assert node.lines("Mesh node not starting"), node.log.read_text()
    finally:
        node.stop()
    assert not (state / "node.json").exists()
    assert full_volume.leftovers() == []


def test_a_job_prompt_the_disk_cannot_hold_is_not_left_behind(full_volume, monkeypatch):
    """A dispatched prompt is staged in the temp dir, on the disk that filled; one
    bigger than the room left is a file created and then cut short."""
    import tempfile

    from szpontnet import launch

    monkeypatch.setattr(tempfile, "tempdir", str(full_volume.path))
    with pytest.raises(launch.JobSpawnError):
        launch.write_prompt("x" * (32 << 20))
    assert list(full_volume.path.glob("szpont-job-*")) == []


def test_each_half_of_a_new_identity_refuses_a_disk_that_cannot_keep_it(
        full_volume, monkeypatch):
    """The node id and the device key are minted separately and either one lost is a
    different machine to its peers, so each refuses on its own."""
    from szpontnet import crypto, identity

    state = full_volume.path / "mesh"
    state.mkdir()
    monkeypatch.setenv("SZPONTNET_DIR", str(state))
    full_volume.fill()
    with pytest.raises(identity.IdentityNotSaved):
        identity.load()
    if crypto.AVAILABLE:
        with pytest.raises(identity.IdentityNotSaved):
            crypto.load_or_create()
    assert list(state.iterdir()) == []


def test_an_identity_already_on_disk_loads_on_a_full_one(full_volume, monkeypatch):
    """What a restart needs is a read: a disk with no room for a refreshed tier still
    starts the node it already knows."""
    from szpontnet import hardware, identity

    state = full_volume.path / "mesh"
    state.mkdir(parents=True)
    seeded = {"id": "f" * 32, "name": "full-disk", "tier": 3, "tokens": "ok",
              "strengthAuto": True, "dutiesEnabled": {}}
    (state / "node.json").write_text(json.dumps(seeded))
    monkeypatch.setenv("SZPONTNET_DIR", str(state))
    monkeypatch.setattr(hardware, "detect_tier", lambda: 1)
    full_volume.fill()
    node = identity.load()
    assert (node.id, node.tier) == ("f" * 32, 1)
    assert _read(state / "node.json") == seeded, "the disk took a write"


def test_a_foreign_job_with_nowhere_to_stage_fails_rather_than_raising(
        tmp_path, monkeypatch):
    """Both ends stage a confined job's artifact under ``results/``; where that
    directory can't be made, the job fails (and the slot fails over) instead of the
    error escaping into the link that carried the request or the result."""
    import errno

    from szpontnet import protocol, spawnjob
    from szpontnet.node import MeshNode

    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    monkeypatch.setenv("SZPONTNET_LOOPBACK", "1")
    node = MeshNode()

    def no_room(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(node, "_result_path", no_room)
    monkeypatch.setattr(spawnjob, "spawn_confined",
                        lambda *_: pytest.fail("the sandbox started"))
    monkeypatch.setattr(spawnjob, "run_result_handler",
                        lambda *_: pytest.fail("the handler ran"))
    job = protocol.Job(id="j1", duty="review", prompt="p", requested_by="peer",
                       requested_at=1.0)
    assert node._run_confined(job, "peer") == (
        "failed", "[Errno 28] No space left on device", False)
    node._act_on_result("j1", "peer", "review", {"ok": True, "output": "x"})
