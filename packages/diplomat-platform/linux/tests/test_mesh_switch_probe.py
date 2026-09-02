"""The mesh switch reaches the claims probe.

A machine that ran a node once keeps its snapshot in the mesh directory. With the
mesh switched off in Settings that snapshot read as a node that had gone quiet -
a probe-silent warning on every launch, for a node the operator stopped.
"""

from __future__ import annotations

from diplomat_app import probes
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
