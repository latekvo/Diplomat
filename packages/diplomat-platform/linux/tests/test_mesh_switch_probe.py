"""The mesh switch reaches the claims probe.

A machine that ran a node once keeps its snapshot in the mesh directory. With the
mesh switched off in Settings that snapshot read as a node that had gone quiet -
a probe-silent warning on every launch, for a node the operator stopped.
"""

from __future__ import annotations

import json
import subprocess
import time

from diplomat_runtime import agentstate as A
from diplomat_app import probes, szpont
from diplomat_app.store import Store


def test_the_store_hands_its_mesh_switch_to_the_probe(monkeypatch):
    seen: list[bool | None] = []
    real = probes.gather

    def spy(records, now, **kw):
        seen.append(kw.get("mesh_enabled"))
        return real(records, now, **kw)

    monkeypatch.setattr(probes, "gather", spy)
    store = Store()
    store.mesh_enabled = False
    store._agent_tick()
    store.mesh_enabled = True
    store._agent_tick()
    # True only where the add-on is importable; the property already folds that in.
    assert seen == [False, store.mesh_enabled]


def test_the_switch_reaches_the_claims_through_gather(monkeypatch, tmp_path):
    """Through the bundle rather than the probe alone: a machine that ran a node once
    keeps its snapshot, and with the mesh switched off that snapshot reads as the
    mesh being off, not as a node that has gone quiet."""
    assert szpont.AVAILABLE, "the checkout's own SzpontNet is on the path"
    gone = subprocess.Popen(["true"])
    gone.wait()
    mesh = tmp_path / "mesh"
    mesh.mkdir()
    (mesh / "state.json").write_text(json.dumps({"pid": gone.pid, "claims": {}}))
    monkeypatch.setenv("SZPONTNET_DIR", str(mesh))
    assert probes.mesh_claims().status == A.UNAVAILABLE, \
        "the snapshot must read as a stopped node, or this pins nothing"

    claims = probes.gather([], time.time(), mesh_enabled=False).claims

    assert (claims.status, claims.reason) == \
        (A.UNSUPPORTED, "are unavailable (the mesh is switched off)")
