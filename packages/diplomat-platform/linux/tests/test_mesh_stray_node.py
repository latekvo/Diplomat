"""A node left running while the mesh is off is stopped at launch, and nothing else is.

The node outlives the applet by design, so one an earlier instance started runs on
unattended unless a launch with the mesh off stops it. That launch acts on whatever ``state.json`` names, so what it must NOT stop
matters as much: a pid the OS has since handed to another process, a port a node on
another state dir has since bound, a node with another identity. Stand-in nodes speak
just enough of the control protocol (``status``, ``stop``) to be told apart, so no real
node starts and nothing joins a network. Twin of ``MeshStrayTest.swift``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from diplomat_app import store as store_module
from diplomat_app.store import Store

STAND_IN = r'''
import json, os, socket, sys
node_id, stubborn = sys.argv[1], sys.argv[2:] == ["stubborn"]
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen()
me = {"pid": os.getpid(), "tcpPort": srv.getsockname()[1], "self": {"id": node_id}}
path = os.path.join(os.environ["SZPONTNET_DIR"], "state.json")
with open(path + ".tmp", "w") as f:
    json.dump(me, f)
os.replace(path + ".tmp", path)
while True:
    conn, _ = srv.accept()
    with conn, conn.makefile("rwb") as f:
        f.readline()
        t = json.loads(f.readline() or b"{}").get("t")
        f.write(json.dumps({"t": "state", "state": me} if t == "status"
                           else {"t": "ok"}).encode() + b"\n")
    if t == "stop" and not stubborn:
        sys.exit(0)
'''


class Node:
    def __init__(self, process: subprocess.Popen, port: int, node_id: str) -> None:
        self.process, self.pid, self.port, self.id = process, process.pid, port, node_id

    def running(self) -> bool:
        return self.process.poll() is None


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    """This applet's state dir and another one, both scratch, with the activity feed
    recorded instead of appended to the operator's."""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    ours, theirs = tmp_path / "mesh", tmp_path / "other-mesh"
    ours.mkdir()
    theirs.mkdir()
    monkeypatch.setenv("SZPONTNET_DIR", str(ours))
    feed: list[tuple[str, str, str]] = []
    monkeypatch.setattr(store_module.activity, "log", lambda *a: feed.append(a))
    script = tmp_path / "standin.py"
    script.write_text(STAND_IN)
    started: list[subprocess.Popen] = []

    def launch(node_id: str, directory=ours, stubborn: bool = False) -> Node:
        state = directory / "state.json"
        state.unlink(missing_ok=True)
        p = subprocess.Popen(
            [sys.executable, str(script), node_id] + (["stubborn"] if stubborn else []),
            env={**os.environ, "SZPONTNET_DIR": str(directory)})
        started.append(p)
        # Reaped the moment it exits, as a detached node is by init: an unreaped child
        # is a zombie, and a zombie's pid still answers kill(pid, 0).
        threading.Thread(target=p.wait, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                snap = json.loads(state.read_text())
            except (OSError, ValueError):
                snap = {}
            if snap.get("pid") == p.pid:
                return Node(p, snap["tcpPort"], node_id)
            time.sleep(0.05)
        raise AssertionError(f"stand-in {node_id} never wrote its state.json")

    def sleeper() -> subprocess.Popen:
        p = subprocess.Popen(["sleep", "60"])
        started.append(p)
        return p

    def name(pid: int, port: int, node_id: str) -> None:
        (ours / "state.json").write_text(
            json.dumps({"pid": pid, "tcpPort": port, "self": {"id": node_id}}))

    yield type("Mesh", (), {"launch": staticmethod(launch), "sleeper": staticmethod(sleeper),
                            "name": staticmethod(name), "feed": feed, "dir": ours})
    for p in started:
        if p.poll() is None:
            p.kill()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _settled(store: Store) -> None:
    assert store.wait_for_background(timeout=15) == []


def test_a_launch_with_the_mesh_off_stops_the_node_state_json_names(mesh):
    node = mesh.launch("n-a")
    store = Store()
    assert not store.mesh_enabled
    store.settle_mesh_on_launch()
    _settled(store)
    assert not node.running()
    assert mesh.feed == [("panel", "mesh-stop",
                          f"Mesh is off: stopped the node left running here "
                          f"(pid {node.pid}, :{node.port})")]


def test_a_launch_with_the_mesh_on_leaves_its_node_alone(mesh, monkeypatch):
    node = mesh.launch("n-a")
    spawned: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    store = Store()
    store.mesh_enabled = True
    store.settle_mesh_on_launch()
    _settled(store)
    assert node.running()
    assert mesh.feed == [] and spawned == []


def test_without_szpontnet_a_launch_leaves_the_node_alone(mesh, monkeypatch):
    """Nothing can speak to a node without the library; the launch must not try."""
    node = mesh.launch("n-a")
    monkeypatch.setattr(store_module.szpont, "AVAILABLE", False)
    assert Store().stop_stray_node_async() is None
    assert node.running() and mesh.feed == []


def test_no_state_json_is_nothing_to_stop(mesh):
    assert store_module.stop_stray_node() == ("none", 0, 0, "")


def test_a_dead_pid_dials_nothing(mesh):
    """A state.json whose node is gone is the common case, and whatever holds its
    port now is not ours to send control lines to."""
    gone = subprocess.Popen(["true"])
    gone.wait()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.5)
        mesh.name(gone.pid, listener.getsockname()[1], "n-a")
        assert store_module.stop_stray_node() == ("none", 0, 0, "")
        with pytest.raises(socket.timeout):
            listener.accept()


def test_a_pid_the_os_handed_to_another_process_is_left_alone(mesh):
    reused = mesh.sleeper()
    mesh.name(reused.pid, _closed_port(), "n-a")
    assert store_module.stop_stray_node() == ("none", 0, 0, "")
    assert reused.poll() is None


def test_a_node_another_state_dir_runs_on_the_port_is_left_alone(mesh, tmp_path):
    other = mesh.launch("n-b", directory=tmp_path / "other-mesh")
    reused = mesh.sleeper()
    mesh.name(reused.pid, other.port, other.id)
    assert store_module.stop_stray_node() == ("none", 0, 0, "")
    assert other.running() and reused.poll() is None


def test_a_node_with_another_identity_is_left_alone(mesh, tmp_path):
    other = mesh.launch("n-b", directory=tmp_path / "other-mesh")
    mesh.name(other.pid, other.port, "n-a")
    assert store_module.stop_stray_node() == ("none", 0, 0, "")
    assert other.running()


def test_the_same_node_named_by_pid_port_and_id_is_stopped(mesh, tmp_path):
    """The positive control for the three above: the node they left alone is stopped
    the moment the file names it exactly."""
    other = mesh.launch("n-b", directory=tmp_path / "other-mesh")
    mesh.name(other.pid, other.port, other.id)
    assert store_module.stop_stray_node() == ("stopped", other.pid, other.port, "")
    assert not other.running()


def test_a_node_that_outlives_its_stop_is_not_reported_stopped(mesh):
    node = mesh.launch("n-c", stubborn=True)
    store = Store()
    store.stop_stray_node_async(exit_wait=1)
    _settled(store)
    assert node.running()
    assert mesh.feed == [("panel", "warn",
                          f"Mesh is off, but the node left running here "
                          f"(pid {node.pid}, :{node.port}) did not stop: "
                          f"still running 1s after it was asked to stop")]
