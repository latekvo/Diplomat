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
