"""The applet on a disk that fills up: its polls keep running, and what it could not
write lands once there is room.

Everything the poll loop writes — the run book, the telemetry ledger and its token
cursor, the shared config, the pidfile and display env — sits on a small real volume
(``scripts/full-volume.sh``) filled to ENOSPC, and the loop is driven by real
QTimers on an offscreen QApplication, as ``app.py`` wires it. The activity feed stays
off the volume, so the lines the applet writes about the full disk can be read.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from diplomat_runtime import activity, agentregistry, appconfig, telemetry, usagescan
from test_autofix import register_run, store  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from full_volume import full_volume  # noqa: E402,F401  (a fixture)

KEY = "review:github.com/o/r#7@aa"


@pytest.fixture
def on_volume(full_volume, monkeypatch):
    vol = full_volume.path
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(vol / "agents"))
    monkeypatch.setenv("DIPLOMAT_CONFIG", str(vol / "config.json"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(vol / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(vol / "state"))
    monkeypatch.setattr(telemetry, "_dir", lambda: vol / "telemetry")
    monkeypatch.setattr(usagescan, "cursor_path", lambda: vol / "telemetry" / "cursor.json")
    monkeypatch.setattr(telemetry, "SAMPLE_INTERVAL_SECS", 0.5)
    return full_volume


@pytest.fixture
def quiet_poll(monkeypatch):
    """The GitHub half of the poll, answered with nothing to do."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])


@pytest.fixture
def crashes(monkeypatch):
    """Every exception that escapes a worker thread or a Qt slot. Either kind leaves
    the applet up but that loop dead, which is the failure this file is about."""
    seen: list[str] = []
    monkeypatch.setattr(threading, "excepthook",
                        lambda a: seen.append(f"{a.thread.name}: {a.exc_value!r}"))
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: seen.append(f"slot: {v!r}"))
    return seen


class _Loop:
    """The poll and sampler timers of ``app.py``, on a fast beat."""

    def __init__(self, st) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.timers = []
        for slot in (st.run_autofix_poll_async, st.run_telemetry_sample_async):
            timer = QTimer()
            timer.setInterval(150)
            timer.timeout.connect(slot)
            timer.start()
            self.timers.append(timer)
        self.store = st

    def run(self, secs: float) -> None:
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        assert self.store.wait_for_background() == []

    def stop(self) -> None:
        for timer in self.timers:
            timer.stop()


def _feed(text: str) -> list[str]:
    return [e.detail for e in activity.read() if text in e.detail]


def _ledger(ev: str) -> list[dict]:
    events = []
    for line in telemetry.read_lines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return [e for e in events if e.get("ev") == ev]


def test_a_run_that_ends_on_a_full_disk_is_retired_once_it_drains(
        on_volume, store, quiet_poll, crashes):
    """The run book and the ledger both refuse writes when the agent exits. The run is
    kept rather than half-retired, and priced from its own sentinel once they take
    writes again — not from whenever a later poll happened to notice it."""
    record = register_run(7, ledger_key=KEY, pid=4242, prompt="the prompt")
    exited = time.time() - 120
    done = agentregistry.done_path(record.run_id)
    done.write_text("0")
    os.utime(done, (exited, exited))
    # No ledger yet: a full disk still lets a small append into a file's last block.
    on_volume.fill()
    full_at = time.time()
    loop = _Loop(store)
    try:
        loop.run(2.0)
        assert crashes == []
        assert [r.run_id for r in agentregistry.load()] == [record.run_id]
        assert agentregistry.run_dir(record.run_id).is_dir()
        assert _ledger("done") == []
        assert len(_feed("Can't write the agent run book")) == 1
        assert len(_feed("Can't write the telemetry ledger")) == 1

        on_volume.drain()
        loop.run(2.0)
    finally:
        loop.stop()
    assert crashes == []
    assert agentregistry.load() == []
    finished = [e for e in _ledger("done") if e["key"] == KEY]
    assert len(finished) == 1 and finished[0]["at"] == pytest.approx(exited, abs=1), (
        finished)
    # The samples the sampler took while the ledger refused them, at their own times
    # and no denser than its interval.
    held = [e["at"] for e in _ledger("sample") if e["at"] >= full_at]
    assert held and all(b - a >= 0.5 for a, b in zip(held, held[1:])), held
    assert _feed("Agent run book saving again")
    assert _feed("Telemetry ledger writing again")
    assert on_volume.leftovers() == []


def test_the_applet_launches_on_a_full_disk(on_volume, quiet_poll, crashes, monkeypatch):
    """``run_app``'s launch sequence with nothing on the disk writable, and then the
    loop it starts: the launch must not die on bookkeeping, a display env it can't
    replace must survive whole, and the config the allocator daemon reads is
    published once there is room."""
    from diplomat_app import selfupdate, singleton
    from diplomat_app.store import Store

    monkeypatch.setattr(singleton, "_other_instances", lambda: set())
    monkeypatch.setenv("DISPLAY", ":0")
    selfupdate.record_display_env()
    on_volume.fill()

    monkeypatch.setenv("DISPLAY", ":1")
    singleton.SingleInstance.acquire_newest_wins()
    selfupdate.clear_operator_quit()
    selfupdate.record_display_env()
    st = Store()
    st.me = "alice"
    st.publish_spawn_terminal()
    assert appconfig.get(appconfig.SPAWN_TERMINAL) == ""
    assert selfupdate._recorded_display_env()["DISPLAY"] == ":0"

    loop = _Loop(st)
    try:
        loop.run(1.5)
        assert crashes == []
        on_volume.drain()
        loop.run(1.5)
    finally:
        loop.stop()
        singleton.SingleInstance.release()
    assert crashes == []
    assert appconfig.get(appconfig.SPAWN_TERMINAL) != ""
    assert _ledger("sample"), "the sampler never caught up"
    assert on_volume.leftovers() == []


def test_a_setting_the_disk_refuses_says_so(on_volume, monkeypatch):
    """A Settings change that could not be saved has no effect, since everything reads
    the file; the screen has to say that rather than show the new value as taken."""
    from diplomat_app.settingsview import SettingsView
    from diplomat_app.store import Store

    QApplication.instance() or QApplication([])
    st = Store()
    view = SettingsView(st)
    before = appconfig.auto_budget_gate()
    on_volume.fill()
    view._sw_budget.setChecked(not before)
    assert appconfig.auto_budget_gate() == before
    assert not view._save_error.isHidden()
    assert "Couldn't save" in view._save_error.text()

    on_volume.drain()
    view._sw_budget.setChecked(before)
    view._sw_budget.setChecked(not before)
    assert appconfig.auto_budget_gate() == (not before)
    assert view._save_error.isHidden()
    assert st.wait_for_background() == []


def test_a_spawn_the_disk_cannot_book_is_refused_not_launched(
        on_volume, store, monkeypatch):
    """An agent reads its prompt from the run directory, and a run the book never heard
    of holds a bay nothing counts: no room for either is a spawn that fails, before a
    terminal opens."""
    from diplomat_runtime import review
    from test_autofix import _spawn_recorder

    calls = _spawn_recorder(monkeypatch)
    on_volume.fill()
    with pytest.raises(review.SpawnError, match="couldn't register the run"):
        store._spawn_tracked("a prompt", "https://github.com/o/r/pull/7", 7, "auto",
                             ledger_key=KEY)
    on_volume.drain()
    # Room for the directory but not the prompt: the half-made run is taken back.
    with pytest.raises(review.SpawnError, match="couldn't register the run"):
        store._spawn_tracked("x" * (32 << 20), "https://github.com/o/r/pull/7", 7,
                             "auto", ledger_key=KEY)
    assert calls == []
    assert agentregistry.load() == []
    assert not any(p.is_dir() for p in (on_volume.path / "agents").glob("*"))
    assert on_volume.leftovers() == []


def test_a_mesh_run_the_disk_cannot_book_is_reported(on_volume, store):
    """The mesh already started this one, so there is nothing to undo: the poll that
    tracks it must go on, and say once that the book is not taking writes."""
    on_volume.fill()
    store._track_mesh_run("https://github.com/o/r/pull/7", 7, "auto", KEY, "P",
                          node="peer", work_key=KEY, here=False)
    assert len(_feed("Can't write the agent run book")) == 1


def test_a_prompt_the_disk_cannot_hold_is_not_left_behind(on_volume, monkeypatch):
    """Staged in the temp dir, which is on the disk that filled; more prompt than the
    volume holds is a file created and then cut short."""
    import tempfile

    from diplomat_runtime import review, runner

    monkeypatch.setattr(tempfile, "tempdir", str(on_volume.path))
    huge = "x" * (32 << 20)
    with pytest.raises(review.SpawnError):
        review.write_prompt(huge)
    assert list(on_volume.path.glob("diplomat-review-*")) == []

    prompt = on_volume.path / "prompt.txt"
    prompt.write_text(huge[: 9 << 20])
    monkeypatch.setattr(runner, "selected", lambda: runner.OPENCODE)
    monkeypatch.setattr(usagescan, "opencode_is_v2", lambda: True)
    with pytest.raises(review.SpawnError):
        review.stage_opencode_session(str(prompt))
    assert sorted(p.name for p in on_volume.path.glob("prompt.txt*")) == ["prompt.txt"]


def test_hooks_the_disk_cannot_hold_are_not_left_behind(on_volume):
    """A mesh job's hook settings, staged beside its sentinel. A tmpfs creates the file
    and then refuses its bytes, and an empty settings file left there is one the next
    job at that path would be handed."""
    from diplomat_runtime import szponthost

    on_volume.fill()
    assert szponthost._stage_hooks(str(on_volume.path / "job.done")) is None
    assert list(on_volume.path.glob("job.*")) == []


def test_a_line_the_disk_cut_short_is_closed_before_the_next(tmp_path, monkeypatch):
    """A disk that runs out part way through a write keeps half a line. The next append
    has to start a fresh one, or that half and the event after it read as one line
    that parses as neither."""
    import os as real_os

    class _ShortFirstWrite:
        cut = False

        def __getattr__(self, name):
            return getattr(real_os, name)

        def write(self, fd, data):
            if not self.cut:
                self.cut = True
                return real_os.write(fd, data[: len(data) // 2])
            return real_os.write(fd, data)

    monkeypatch.setattr(telemetry, "_dir", lambda: tmp_path / "telemetry")
    monkeypatch.setattr(telemetry, "os", _ShortFirstWrite())
    telemetry.append({"at": 1.0, "ev": "cleared", "key": "a"})
    telemetry.append({"at": 2.0, "ev": "cleared", "key": "b"})
    assert [e["key"] for e in _ledger("cleared")] == ["a", "b"]


def test_an_agent_found_on_a_full_disk_is_booked_once_it_drains(
        on_volume, store, quiet_poll, crashes, monkeypatch):
    """The scan finds an agent this applet did not start, and the tick books it so the
    PR stays deduped. A book that refuses the write must not stop the tick that found
    it; the next one after the drain books it."""
    from test_autofix import fake_probes

    fake_probes(monkeypatch, live_prs={337})
    on_volume.fill()
    loop = _Loop(store)
    try:
        loop.run(1.0)
        assert crashes == []
        assert agentregistry.load() == []
        on_volume.drain()
        loop.run(1.0)
    finally:
        loop.stop()
    assert crashes == []
    assert [r.run_id for r in agentregistry.load()] == ["untracked:337"]


def test_a_refused_reap_on_a_full_disk_is_stamped_once_it_drains(
        on_volume, store, monkeypatch):
    """A wedged window the reaper cannot close is stamped so the backstop waits before
    trying again. A book that refuses the stamp must not take the tick down with it."""
    from diplomat_runtime import agentstate as A
    from test_autofix import (WORKING, _age_the_stillness, _refused, agent_alive,
                              fake_probes)

    asked = _refused(monkeypatch)
    record = register_run(710, pid=7010, tty="pts/70", ledger_key="review:o/r#710",
                          dispatched_at=time.time() - 4000)
    fake_probes(monkeypatch, processes=agent_alive(7010, tty="pts/70", elapsed=4000),
                tails={"pts/70": WORKING})
    store._settle_agents()
    store._settle_agents()
    _age_the_stillness(A.QUIET_TIMEOUT + 5)
    on_volume.fill()

    store._settle_agents()
    assert asked, "the reap must actually have been attempted"
    (held,) = agentregistry.load()
    assert held.run_id == record.run_id and held.reap_refused_at is None
    assert len(_feed("Can't write the agent run book")) == 1

    on_volume.drain()
    store._settle_agents()
    (held,) = agentregistry.load()
    assert held.reap_refused_at is not None
