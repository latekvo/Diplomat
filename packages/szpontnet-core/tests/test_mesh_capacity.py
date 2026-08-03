"""The executor's concurrency ceiling: what a node does when it is asked to run
more at once than the machine behind it will take.

Routing ranks candidates by *quota* surplus, which says nothing about how many
processes are already running on a box — so without this check a machine with a
fat quota and two agents already at work is exactly the machine the next dispatch
lands on, and the one after that. The refusal is an ordinary decline, so the
dispatcher fails the slot over to a node with room.

The interesting part is the ORDER. Both dedup checks in ``_spawn_local`` mean "we
are already doing this exact work", and answering those with a decline would be
worse than doing nothing: the dispatcher reads a decline as "this node can't",
fails over, and lands a SECOND agent on the work somewhere else. Capacity is
therefore asked last, of genuinely new work only.
"""

from __future__ import annotations

import pytest

from szpontnet import host, protocol
from szpontnet.node import MeshNode


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A never-started node: no sockets, no snapshot loop, its own state dir."""
    monkeypatch.setenv("SZPONTNET_DIR", str(tmp_path))
    monkeypatch.setenv("SZPONTNET_LOOPBACK", "1")
    return MeshNode()


@pytest.fixture
def spawned(monkeypatch):
    """Record what actually reached the spawner instead of launching anything."""
    calls: list[str] = []
    from szpontnet import node as node_mod

    monkeypatch.setattr(node_mod.spawnjob, "spawn_job",
                        lambda prompt, done_path=None: calls.append(prompt) or "/p")
    return calls


class _Busy(host.Host):
    """A host whose machine is full, and which records what it was asked."""

    def __init__(self, full: bool = True):
        self.full = full
        self.asked: list[list[str]] = []

    def at_job_capacity(self, running_keys):
        self.asked.append(list(running_keys))
        return self.full


def _job(work_key: str = "review:github.com/o/r#1@sha") -> protocol.Job:
    return protocol.Job(id="j1", duty="review", prompt="p", requested_by="peer",
                        requested_at=1.0, work_key=work_key)


def test_a_full_machine_declines_so_the_slot_fails_over(node, spawned):
    host.set_host(_Busy())
    status, reason, no_result = node._spawn_local(_job())
    assert status == "declined"
    assert reason  # the dispatcher surfaces it in the slot result
    assert no_result is False  # nothing was started, so nothing is owed later
    assert spawned == []
    assert node._own_claim("review:github.com/o/r#1@sha") is None  # no claim taken


def test_a_machine_with_room_runs_it(node, spawned):
    host.set_host(_Busy(full=False))
    assert node._spawn_local(_job())[0] == "spawned"
    assert spawned == ["p"]


def test_work_already_running_here_is_deduped_not_declined(node, spawned):
    """A decline would fail the slot over and put a SECOND agent on work this
    machine is already doing — the exact duplicate the dedup exists to prevent. So
    "we have this" outranks "we are busy", both for the node's own book and for the
    host's ground-truth floor."""
    busy = _Busy()
    busy.work_already_running = lambda wk: True
    host.set_host(busy)
    assert node._spawn_local(_job()) == ("spawned", "", True)
    assert spawned == []
    assert busy.asked == []  # capacity was never even consulted

    # Same for the node's own book of live agents.
    busy.work_already_running = lambda wk: False
    node._agents["review:github.com/o/r#2@sha"] = {"done": "/d", "at": 0.0}
    assert node._spawn_local(_job("review:github.com/o/r#2@sha")) == ("spawned", "", True)
    assert spawned == []
    assert busy.asked == []


def test_the_host_is_told_which_jobs_this_node_already_has_live(node, spawned):
    """An agent started seconds ago is not in a process listing yet, and a burst of
    dispatches is precisely when that gap decides whether the cap holds — so the
    node hands over the keys of the work it knows it is running."""
    busy = _Busy(full=False)
    host.set_host(busy)
    node._agents["review:github.com/o/r#5@sha"] = {"done": "/d", "at": 0.0}
    node._agents["conflicts:github.com/o/r#6@sha"] = {"done": "/e", "at": 0.0}

    node._spawn_local(_job())
    assert sorted(busy.asked[0]) == ["conflicts:github.com/o/r#6@sha",
                                     "review:github.com/o/r#5@sha"]


def test_a_job_with_no_work_key_is_capped_too(node, spawned):
    """The cap is on what the machine RUNS. A job that opted out of claim-based
    dedup still starts a process here, so it still has to fit."""
    host.set_host(_Busy())
    assert node._spawn_local(_job(work_key=""))[0] == "declined"
    assert spawned == []


def test_a_node_with_no_host_is_uncapped(node, spawned):
    """The library ships no opinion: with nobody behind it the node takes what it
    is sent, so this check cannot silently start refusing work in a deployment that
    never asked for a cap."""
    for n in range(5):
        assert node._spawn_local(_job(f"review:github.com/o/r#{n}@sha"))[0] == "spawned"
    assert len(spawned) == 5
