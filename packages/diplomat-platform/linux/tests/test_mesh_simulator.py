"""The SzpontNet network simulator, run as CI tests.

Each scenario stands up a real multi-node fleet on loopback, injects simulated
work events, and asserts a work-claim invariant the auto-monitors depend on:
exactly-once, best-fit placement, no-drop, failover, and retry (szpontnet-spec/docs/12).
The simulator itself lives in ``meshsim/simulator.py`` (also runnable as
``python -m meshsim.simulator``); this module just wires its scenarios into pytest.

Skipped where loopback multicast is unavailable, exactly like the other
real-socket mesh integration tests (a hardened/namespaced CI container).
"""

from __future__ import annotations

import pytest

from meshsim import simulator

pytestmark = pytest.mark.skipif(
    not simulator.loopback_multicast_works(),
    reason="loopback multicast unavailable (hardened/namespaced container?)",
)


@pytest.mark.parametrize("name", list(simulator.SCENARIOS))
def test_mesh_scenario(name: str, tmp_path) -> None:
    simulator.SCENARIOS[name](tmp_path)
