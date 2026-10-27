"""A disk that is almost full holds the work this machine starts on its own.

On 2026-10-26 a Mac's data volume filled to 130 MB free while Diplomat kept
dispatching automatic agents into it, and the kernel panicked. These tests put
the agents' repo root and the self-updater's checkout on a real volume
(``scripts/full-volume.sh``: a tmpfs here, an APFS image on macOS), fill it to
ENOSPC or set the floor above what it holds, and drive the real dispatch gate, the
real mesh host and the real ``selfupdate.run_scheduled`` against it. The spawner,
the build and the tray launch are the only stand-ins, except in the tests of which
volume counts, where the free-space reading is.

The volume needs root or passwordless sudo on Linux; without it these skip, unless
``DIPLOMAT_FULL_VOLUME_REQUIRED=1`` (CI), when the mount fails them instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from diplomat_runtime import activity, autofix, diskfloor
from test_autofix import _spawn_recorder, store  # noqa: F401 — the shared Store
# fixture and spawn stub, as test_autobudget.py reuses them.

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from full_volume import FullVolume, full_volume as volume  # noqa: E402,F401  (a fixture)

_LOW_DISK = "low_disk"  # spelled out so a gate that lacks the verdict fails on the value


def _free_mib(volume: FullVolume) -> int:
    return (diskfloor.free(str(volume.path)) or 0) >> 20


def _floor_mib(monkeypatch, mib: int) -> None:
    monkeypatch.setenv(diskfloor.ENV_OVERRIDE, str(mib))


def _floor_between_full_and_drained(monkeypatch, volume: FullVolume) -> None:
    """Above what a filled volume holds, below what a drained one does."""
    half = _free_mib(volume) // 2
    assert half >= 1, f"the drained volume holds only {_free_mib(volume)} MiB"
    _floor_mib(monkeypatch, half)


def _job(number: int):
    return autofix.AgentJob(
        kind="review", audit_action="review", label=f"Review · #{number}",
        prompt="PROMPT", pr_url=f"https://github.com/o/r/pull/{number}",
        pr_number=number, counter="review_requests", duty="review",
    )


def _lines(action: str):
    return [e for e in activity.read() if e.action == action]


# MARK: - The dispatch gate


def test_auto_work_holds_on_a_full_disk_and_resumes_once_it_drains(
        store, monkeypatch, volume):
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    store.auto_task_limit = 8  # room enough that only the disk can hold anything
    repo = volume.path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DIPLOMAT_REPO", str(repo))
    _floor_between_full_and_drained(monkeypatch, volume)

    volume.fill()
    verdicts = [store.dispatch_agent(_job(n), autofix.SOURCE_AUTO) for n in (1, 2, 3)]
    assert verdicts == [_LOW_DISK] * 3
    assert calls == [], "nothing was spawned onto the full disk"
    lines = _lines("low-disk")
    assert len(lines) == 1, "one line per episode, not one per PR"
    assert str(repo) in lines[0].detail and "floor" in lines[0].detail, lines[0].detail
    # Deferred, not dropped.
    store.commit_queue()
    assert [e.id for e in store.queued_tasks] == ["review:1", "review:2", "review:3"]

    # The drain stops at the disk like it stops at the budget: the one it tried is
    # re-staged, the rest wait on the list.
    store.queued_tasks = [autofix.QueuedTask("review:6", _job(6), 1),
                          autofix.QueuedTask("review:7", _job(7), 1)]
    store._drain_queued_tasks([], closed=set())
    assert calls == []
    assert [e.id for e in store._staged_queue if e.id in ("review:6", "review:7")] \
        == ["review:6"]
    assert [e.id for e in store.queued_tasks] == ["review:7"]

    # The operator's own starts are not held: a click, and "execute now".
    assert store.dispatch_agent(_job(4), autofix.SOURCE_PANEL) == "spawned"
    entry = autofix.QueuedTask("review:5", _job(5), 1)
    store.queued_tasks = [entry]
    store._execute_queued_task(entry)
    assert len(calls) == 2
    assert not store.error

    volume.drain()
    assert store.dispatch_agent(_job(8), autofix.SOURCE_AUTO) == "spawned"
    assert len(calls) == 3

    # A second episode gets a line of its own.
    volume.fill()
    assert store.dispatch_agent(_job(9), autofix.SOURCE_AUTO) == _LOW_DISK
    assert len(_lines("low-disk")) == 2
    assert len(calls) == 3


def _diplomat_dir_low(monkeypatch, tmp_path) -> str:
    """Room in the repo, none left in ``~/.diplomat``, where the tool caches go."""
    repo, tools = tmp_path / "repo", str(Path.home() / ".diplomat")
    repo.mkdir()
    monkeypatch.setenv("DIPLOMAT_REPO", str(repo))
    _floor_mib(monkeypatch, 64)
    monkeypatch.setattr(diskfloor, "free",
                        lambda p: 1 << 20 if p == tools else 1 << 40)
    return tools


def test_auto_work_holds_when_only_the_tool_caches_are_out_of_room(
        store, monkeypatch, tmp_path):
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    tools = _diplomat_dir_low(monkeypatch, tmp_path)
    assert store.dispatch_agent(_job(1), autofix.SOURCE_AUTO) == _LOW_DISK
    assert calls == []
    (line,) = _lines("low-disk")
    assert tools in line.detail, line.detail


# MARK: - Work a mesh peer routes here


def test_a_mesh_peers_job_is_declined_on_a_full_disk(monkeypatch, volume):
    from diplomat_runtime.szponthost import DiplomatHost

    # An empty machine, so the cap has room and only the disk can decline.
    monkeypatch.setattr("diplomat_runtime.szponthost._ps_dump", lambda: "")
    monkeypatch.setattr("diplomat_runtime.tmuxwatch.pane_tails_for_ttys", lambda ttys: {})
    repo = volume.path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DIPLOMAT_REPO", str(repo))
    _floor_between_full_and_drained(monkeypatch, volume)

    volume.fill()
    assert DiplomatHost().at_job_capacity([]) is True
    lines = _lines("mesh-low-disk")
    assert len(lines) == 1
    assert "Declined a peer's job" in lines[0].detail and str(repo) in lines[0].detail

    volume.drain()
    assert DiplomatHost().at_job_capacity([]) is False


def test_a_mesh_peers_job_is_declined_when_only_the_tool_caches_are_out_of_room(
        monkeypatch, tmp_path):
    from diplomat_runtime.szponthost import DiplomatHost

    monkeypatch.setattr("diplomat_runtime.szponthost._ps_dump", lambda: "")
    monkeypatch.setattr("diplomat_runtime.tmuxwatch.pane_tails_for_ttys", lambda ttys: {})
    tools = _diplomat_dir_low(monkeypatch, tmp_path)
    assert DiplomatHost().at_job_capacity([]) is True
    (line,) = _lines("mesh-low-disk")
    assert tools in line.detail, line.detail


# MARK: - The scheduled self-update


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo: Path, version: str) -> None:
    (repo / "VERSION").write_text(version)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-q", "-m", version.strip())


def _tree(repo: Path) -> dict[str, bytes]:
    return {str(p.relative_to(repo)): p.read_bytes() for p in sorted(repo.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(repo).parts}


def test_the_updater_merges_nothing_below_the_floor(tmp_path, monkeypatch, volume):
    from diplomat_app import selfupdate

    # Origin on the real disk, the checkout on the volume, one commit behind.
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "1\n")
    checkout = volume.path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _commit(origin, "2\n")
    monkeypatch.setenv("DIPLOMAT_SELF_REPO", str(checkout))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    built, revived = [], []
    monkeypatch.setattr(selfupdate, "build_core", lambda: built.append(True))
    monkeypatch.setattr(selfupdate, "revive",
                        lambda quietly, tag="": revived.append(quietly) or 0)
    monkeypatch.setattr(selfupdate.SingleInstance, "running_pid", lambda: None)
    log = tmp_path / "state" / "diplomat" / "autoupdate.log"
    head, tree = _git(checkout, "rev-parse", "HEAD"), _tree(checkout)

    def held(why: str) -> None:
        assert selfupdate.run_scheduled() == 0
        assert _git(checkout, "rev-parse", "HEAD") == head, f"{why}: merged"
        assert _tree(checkout) == tree, f"{why}: the files changed"
        assert built == [], f"{why}: rebuilt"
        last = log.read_text().splitlines()[-1]
        assert " skip: " in last and "floor" in last and str(checkout) in last, last
        assert revived[-1] is False, f"{why}: the tray was not looked after"

    # A floor above everything the volume holds: room to fetch and merge, still held.
    _floor_mib(monkeypatch, _free_mib(volume) + 64)
    held("floor above the volume")

    # The real thing: a volume filled to ENOSPC. The skip is the disk's, not a fetch
    # that failed on it. (A filled APFS image still reports about a MiB available
    # that nothing can write, so the floor sits well above that.)
    _floor_between_full_and_drained(monkeypatch, volume)
    volume.fill()
    held("full volume")
    assert "cannot reach origin" not in log.read_text()

    # Room again: the same run merges and builds, so the holds above were the floor.
    volume.drain()
    _floor_mib(monkeypatch, 1)
    assert selfupdate.run_scheduled() == 0
    assert _git(checkout, "rev-parse", "HEAD") == _git(origin, "rev-parse", "HEAD")
    assert (checkout / "VERSION").read_text() == "2\n"
    assert built == [True]
