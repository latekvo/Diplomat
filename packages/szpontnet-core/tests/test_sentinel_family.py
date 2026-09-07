"""A job's staging files leave with its sentinel.

The host stages files beside each completion sentinel under the sentinel's own
stem (hook settings, an activity feed - ``diplomat_runtime.szponthost``), and the
node is the side that names the sentinel and cleans it up. One file per job left
behind is a directory that grows with every mesh-placed run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from szpontnet import protocol
from szpontnet import node as node_mod
from szpontnet.node import MeshNode


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    monkeypatch.setenv("SZPONTNET_LOOPBACK", "1")
    return MeshNode()


def staged(done: Path) -> list[Path]:
    """The sentinel plus what the host would stage beside it."""
    family = [done, done.with_suffix(".hooks.json"), done.with_suffix(".activity")]
    for p in family:
        p.write_text("")
    return family


def test_the_watcher_reclaims_what_was_staged_beside_the_sentinel(node):
    done = Path(node._agent_done_path("o/r:review#1@abc"))
    family = staged(done)
    other = done.parent / "someone-elses.done"
    other.write_text("")
    asyncio.run(node._watch_agent("o/r:review#1@abc", str(done)))
    assert [p for p in family if p.exists()] == []
    assert other.exists()


def test_the_startup_sweep_clears_the_whole_agents_dir(node):
    done = Path(node._agent_done_path("o/r:review#2@abc"))
    family = staged(done)
    node._sweep_stale_sentinels()
    assert [p for p in family if p.exists()] == []


def test_a_launch_that_fails_leaves_nothing_staged(node, monkeypatch):
    """The host stages its hook settings before it launches, so a launch that fails
    has already put a file beside a sentinel no watcher will ever reclaim."""
    def fail(prompt, done_path=None):
        Path(done_path).with_suffix(".hooks.json").write_text("")
        raise node_mod.spawnjob.JobSpawnError("no runner")

    monkeypatch.setattr(node_mod.spawnjob, "spawn_job", fail)
    job = protocol.Job(id="j1", duty="review", prompt="p", requested_by="peer",
                       requested_at=1.0, work_key="o/r:review#3@abc")
    assert node._spawn_local(job)[0] == "failed"
    agents = Path(node._agent_done_path("o/r:review#3@abc")).parent
    assert list(agents.iterdir()) == []
