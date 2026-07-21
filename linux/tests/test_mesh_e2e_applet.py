"""Whole-applet mesh E2E: the real app object, a real node, a real peer.

Opt-in (``DIPLOMAT_MESH_E2E=1``) because it boots the actual Qt application
object offscreen and real node subprocesses — a few seconds of sockets and
processes, deliberately not part of the default fast suite.

    DIPLOMAT_MESH_E2E=1 QT_QPA_PLATFORM=offscreen python -m pytest tests/test_mesh_e2e_applet.py

What it proves, through the real entry points:

- the applet (mesh enabled in settings) auto-starts a mesh node daemon;
- the node discovers a separately-launched fake-macOS peer on loopback;
- the topology lands in the Store (what the 🕸️ column renders);
- the audit wizard's SPAWN routes over the mesh and the platform spread
  runs the job on BOTH machines (the applet's linux node AND the macOS peer),
  with the outcome marshalled back onto the wizard's status label.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LINUX_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("DIPLOMAT_MESH_E2E") != "1",
    reason="applet-level mesh E2E is opt-in: DIPLOMAT_MESH_E2E=1",
)

_PORT_BASE = 43000 + (os.getpid() % 400) * 20


def _mesh_env(tmp: Path) -> dict:
    return {
        "DIPLOMAT_MESH_LOOPBACK": "1",
        "DIPLOMAT_MESH_MCAST_PORT": str(_PORT_BASE),
        "DIPLOMAT_MESH_TCP_BASE": str(_PORT_BASE + 1),
        "DIPLOMAT_MESH_TCP_SPAN": "12",
        "DIPLOMAT_MESH_BEACON_SECS": "0.25",
        "DIPLOMAT_MESH_HEARTBEAT_SECS": "0.25",
        "DIPLOMAT_MESH_STALE_SECS": "1.0",
        "DIPLOMAT_MESH_TIMEOUT_SECS": "2.0",
        "DIPLOMAT_MESH_ACK_SECS": "4.0",
        "DIPLOMAT_MESH_STATE_SECS": "0.25",
        "DIPLOMAT_MESH_DIR": str(tmp / "mesh-self"),
        "DIPLOMAT_MESH_PLATFORM": "linux",
        "DIPLOMAT_MESH_SPAWN": f"cp {{prompt_file}} {tmp}/spawned-self.txt",
        # Full-trust fleet: a set of the user's own machines that trust each other,
        # so peers accept each other's dispatches without per-device promotion (the
        # shipped default is zero-trust/foreign — see docs/szpontnet/11).
        "DIPLOMAT_MESH_DEFAULT_TRUST": "personal",
        "HOME": str(tmp / "home"),
    }


def test_applet_meshes_and_dispatches(tmp_path, monkeypatch):
    # Prompt assembly shells out to the diplomat-core Swift binary; resolve it
    # against the REAL environment before we fake HOME away.
    from diplomat_app import promptcore

    try:
        core_bin = promptcore.core_bin()
    except promptcore.CoreBinaryMissing:
        pytest.skip("diplomat-core binary not built (linux/scripts/build-core.sh)")
    monkeypatch.setenv("DIPLOMAT_CORE_BIN", core_bin)

    for k, v in _mesh_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    (tmp_path / "home").mkdir()
    (tmp_path / "mesh-self").mkdir()
    (tmp_path / "mesh-self" / "node.json").write_text(json.dumps(
        {"id": "aaaa-self", "name": "applet-linux", "tier": 4,
         "tokens": "ok", "dutiesEnabled": {}}))

    # A fake-macOS peer, launched the headless way the MacBooks would run it.
    peer_dir = tmp_path / "mesh-peer"
    peer_dir.mkdir()
    (peer_dir / "node.json").write_text(json.dumps(
        {"id": "bbbb-peer", "name": "fake-mac", "tier": 1,
         "tokens": "ok", "dutiesEnabled": {}}))
    peer_env = dict(os.environ)
    peer_env.update({
        "DIPLOMAT_MESH_DIR": str(peer_dir),
        "DIPLOMAT_MESH_PLATFORM": "macos",
        "DIPLOMAT_MESH_SPAWN": f"cp {{prompt_file}} {tmp_path}/spawned-peer.txt",
        "DIPLOMAT_MESH_DEFAULT_TRUST": "personal",  # full-trust fleet (see _mesh_env)
    })
    peer = subprocess.Popen(
        [sys.executable, "-m", "diplomat_app.mesh"], cwd=LINUX_DIR, env=peer_env,
        stdout=(tmp_path / "peer.log").open("w"), stderr=subprocess.STDOUT,
    )

    from PySide6.QtWidgets import QApplication

    from diplomat_app.app import DiplomatApp
    from diplomat_app.mesh import ctl

    app_obj = None
    try:
        qapp = QApplication.instance() or QApplication([])

        # Pre-seed settings THROUGH the store's own mechanism (conftest already
        # redirects QSettings into the test dir): mesh on, allocator settled so
        # the app doesn't try to install anything.
        from diplomat_app.store import Store

        seed = Store()
        seed.mesh_enabled = True
        seed.allocator_setup_done = True
        seed._settings.sync()

        app_obj = DiplomatApp()  # the real applet — starts the mesh node itself

        def pump(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.02)

        def pump_until(predicate, timeout: float, what: str):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                qapp.processEvents()
                app_obj.store.refresh_mesh_state()
                got = predicate()
                if got:
                    return got
                time.sleep(0.05)
            pytest.fail(f"timed out: {what}")

        # 1. The applet brought up a node and it linked the peer.
        pump_until(
            lambda: any(
                p.get("link") == "up"
                for p in (app_obj.store.mesh_state or {}).get("peers", [])
            ),
            timeout=20.0, what="applet node to discover the fake-mac peer",
        )
        state = app_obj.store.mesh_state
        assert state["self"]["name"] == "applet-linux"
        assert state["peers"][0]["name"] == "fake-mac"

        # 2. The audit assignment spreads across both platforms.
        pump_until(
            lambda: tuple((app_obj.store.mesh_state.get("assignments") or {})
                          .get("audit", {}).get("assigned", [])) == ("aaaa-self", "bbbb-peer"),
            timeout=10.0, what="audit to spread linux+macos",
        )

        # 3. Drive the real audit wizard: its mesh row must be live, and SPAWN
        #    must dispatch over the mesh to BOTH machines.
        wizard = app_obj.panel.audit_wizard
        app_obj.panel._open_action("audit")
        pump(0.2)
        assert wizard.mesh_row.use_mesh(), "mesh row should be on by default"
        assert "applet-linux (here) + fake-mac" in wizard.mesh_row.where.text()
        wizard._spawn()
        pump_until(
            lambda: wizard.status.text().startswith("Mesh:"),
            timeout=15.0, what="wizard status to report the dispatch outcome",
        )
        assert "✓ applet-linux (here)" in wizard.status.text().replace("  ", " ") or \
               "✓ applet-linux" in wizard.status.text()
        assert "✓ fake-mac" in wizard.status.text()
        # The stub spawn (Popen `cp`) lands the staged prompt asynchronously.
        pump_until(
            lambda: (tmp_path / "spawned-self.txt").exists()
            and (tmp_path / "spawned-peer.txt").exists(),
            timeout=10.0, what="both machines to stage the dispatched prompt",
        )
        prompt = (tmp_path / "spawned-self.txt").read_text()
        assert prompt == (tmp_path / "spawned-peer.txt").read_text()
        assert "end-to-end" in prompt.lower() or len(prompt) > 100  # a real audit prompt

        # 4. The dispatch shows up in the node's activity feed (fake HOME): one
        #    mesh-dispatch that spawned on BOTH machines. (The applet's own
        #    "· via mesh" panel row goes to the conftest-isolated in-process feed,
        #    not this node-written file, so we assert the node's record here.)
        feed = (tmp_path / "home" / ".diplomat" / "pr-monitor" / "audit.jsonl").read_text()
        assert "mesh-dispatch" in feed
        assert "applet-linux(spawned)" in feed and "fake-mac(spawned)" in feed
    finally:
        peer.kill()
        peer.wait(timeout=10)
        try:
            ctl.stop()  # the applet-spawned daemon
        except ctl.CtlError:
            pass
        if app_obj is not None:
            app_obj.timer.stop()
            app_obj.tray.hide()
