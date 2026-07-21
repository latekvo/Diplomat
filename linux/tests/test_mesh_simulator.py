"""The SzpontNet network simulator, run as CI tests.

Each scenario stands up a real multi-node fleet on loopback, injects simulated
work events, and asserts a work-claim invariant the auto-monitors depend on:
exactly-once, best-fit placement, no-drop, failover, and retry (docs/szpontnet/12).
The simulator itself lives in ``tools/mesh_sim.py`` (also runnable as
``python -m tools.mesh_sim``); this module just wires its scenarios into pytest.

Skipped where loopback multicast is unavailable, exactly like the other
real-socket mesh integration tests (a hardened/namespaced CI container).
"""

from __future__ import annotations

import pytest

from tools import mesh_sim

pytestmark = pytest.mark.skipif(
    not mesh_sim.loopback_multicast_works(),
    reason="loopback multicast unavailable (hardened/namespaced container?)",
)


@pytest.mark.parametrize("name", list(mesh_sim.SCENARIOS))
def test_mesh_scenario(name: str, tmp_path) -> None:
    mesh_sim.SCENARIOS[name](tmp_path)
