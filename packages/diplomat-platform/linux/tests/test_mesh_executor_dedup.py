"""Regression: the EXECUTOR must not double-spawn onto a PR that already has a
live agent on its own host, even when that agent isn't in the node's in-memory
``_agents`` book.

The book only remembers agents THIS node incarnation spawned via the mesh. An
agent can be live on the host yet absent from it: the applet's fail-open local
spawn (mesh momentarily down → ``_spawn_tracked``, no claim, no book entry), a
node restart / singleton respawn after a deploy (book wiped, agent lives on), or
a manual SPAWN. When a peer then routes the same work here — its own ps-scan is
blind to another host's processes, and it sees no claim — the receiving
``_spawn_local`` used to consult ONLY the book and happily launched a duplicate.

This reproduces exactly the field report: right after a deploy, work that was
already being computed on a host got a second, identical agent on that same host.
The originating path (Store.dispatch_agent) has always had the ``ps`` ground-truth
floor; the executing path must have it too.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_mesh_node import Fleet, _links_up, _wait_for  # reuse the real fleet harness

pytestmark = pytest.mark.skipif(
    not __import__("test_mesh_node")._loopback_multicast_works(),
    reason="loopback multicast unavailable (hardened/namespaced container?)",
)


@pytest.fixture()
def fleet(tmp_path):
    f = Fleet(tmp_path)
    yield f
    f.stop_all()

_OWNER, _REPO, _PR = "acme", "widgets", 7
_WORK_KEY = f"review:github.com/{_OWNER}/{_REPO}#{_PR}@abc123"
_PROMPT = f"Review PR #{_PR} in {_OWNER}/{_REPO}. Use the `gh` CLI to fetch it."


def _start_live_agent(tmp: Path) -> subprocess.Popen:
    """A lingering process whose argv looks like a real ``claude`` review agent
    for PR #7 — what ``ps`` shows for an agent already at work on this host, but
    which the executor node did NOT spawn (so it's absent from ``_agents``)."""
    # exec -a sets argv[0]; ps then shows `claude Review PR #7 in acme/widgets …`,
    # the exact shape live_pr_numbers() keys on.
    title = f"claude {_PROMPT}"
    return subprocess.Popen(
        ["bash", "-c", f'exec -a {title!r} sleep 600'],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _dispatch(fleet: Fleet, node_id: str) -> subprocess.CompletedProcess:
    return fleet.cli(node_id, "--dispatch", "review", "--prompt", _PROMPT,
                     "--work-key", _WORK_KEY)


def test_executor_does_not_double_spawn_onto_a_live_untracked_agent(fleet, tmp_path):
    # A: the review executor (linux, weakest → owns the `review` duty). B: a peer
    # that will route the work here. Full-trust fleet so B's dispatch is accepted.
    fleet.start("aaaa", "execu", "linux", tier=4, default_trust="personal")
    fleet.start("bbbb", "peer", "macos", tier=1, default_trust="personal")
    for nid in ("aaaa", "bbbb"):
        _wait_for(lambda nid=nid: _links_up(fleet.state(nid), 1),
                  what=f"{nid} to link its peer")
    _wait_for(lambda: tuple((fleet.state("aaaa").get("assignments") or {})
                            .get("review", {}).get("assigned", [])) == ("aaaa",),
              what="review to land on the executor")

    # An agent is ALREADY at work on the executor's host — ps-visible, but never
    # entered in this node's `_agents` (fail-open local spawn / post-deploy restart).
    agent = _start_live_agent(tmp_path)
    try:
        # Let ps settle so the live agent is observable.
        _wait_for(
            lambda: str(_PR) in subprocess.run(
                ["bash", "-c", "ps -axo args= 2>/dev/null || ps -eo args="],
                capture_output=True, text=True).stdout and
            f"in {_OWNER}/{_REPO}" in subprocess.run(
                ["bash", "-c", "ps -axo args= 2>/dev/null || ps -eo args="],
                capture_output=True, text=True).stdout,
            what="the live agent to appear in ps",
        )

        # A peer routes the SAME work here. It has no claim to see and its own
        # ps-scan can't see our host, so the guard must be the executor's.
        r = _dispatch(fleet, "bbbb")
        assert r.returncode == 0, r.stdout + r.stderr

        # The executor must NOT have launched a second agent: its stub-spawn file
        # would be `spawned/execu.txt`. Give a real spawn time to land.
        spawned = fleet.root / "spawned" / "execu.txt"
        time.sleep(3.0)
        assert not spawned.exists(), (
            "executor double-spawned onto a PR that already has a live agent "
            "(the receiving path ignored the ps ground-truth floor)"
        )
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
