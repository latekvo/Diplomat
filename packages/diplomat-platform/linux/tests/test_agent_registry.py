"""The durable run book and the probes that feed the resolver.

The registry's job is to survive the thing that broke the old in-memory list: the
applet restarting while its agents run on. So the tests that matter here are the ones
that reload it.

The probes' job is narrower and stranger — to be honest about failing. Each one is
asserted on what it says when it *cannot* answer, because that is the input the
resolver refuses to guess from.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from diplomat_runtime import agentregistry as R
from diplomat_runtime import agentstate as A
from diplomat_app import probes

T0 = 1_000_000.0


def rec(run_id="r1", **kw) -> A.RunRecord:
    base = dict(run_id=run_id, dispatched_at=T0, pr_number=337,
                pr_url="https://github.com/o/r/pull/337", kind="review",
                label="Auto · Review · #337", source=A.SOURCE_AUTO,
                ledger_key="review:337:abc")
    base.update(kw)
    return A.RunRecord(**base)


# MARK: - The book survives a restart


def test_a_dispatched_run_is_still_there_after_the_applet_restarts():
    """The whole reason this is on disk. The old in-memory list lost the label, the
    source, the kind, the start time and the ledger key on every restart — and the
    applet rebuilds and relaunches itself on every update."""
    R.create_run(rec(), "the prompt")
    # A restart is exactly this: a fresh read of the file, with no memory.
    back = R.load()
    assert [r.run_id for r in back] == ["r1"]
    assert back[0].label == "Auto · Review · #337"
    assert back[0].source == A.SOURCE_AUTO
    assert back[0].ledger_key == "review:337:abc"
    assert back[0].dispatched_at == T0


def test_a_panel_spawn_is_still_a_panel_spawn_after_a_restart():
    """The specific loss that broke the cap: without the record, a click-spawned agent
    reappeared as an untracked one, which counts as automatic and spends a bay the
    operator never asked to spend."""
    R.create_run(rec(source=A.SOURCE_PANEL), "p")
    assert R.load()[0].source == A.SOURCE_PANEL


def test_the_prompt_is_kept_with_the_run_and_is_not_world_readable():
    """It is what ties the run to its Claude transcript afterwards. It can also quote a
    private repo, and $HOME is readable by other local users under a default umask."""
    R.create_run(rec(), "review PR #337")
    p = R.prompt_path("r1")
    assert p.read_text() == "review PR #337"
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_a_book_from_a_future_schema_is_ignored_rather_than_misread():
    from diplomat_runtime import atomicjson
    atomicjson.write_atomic(R.runs_path(), {"version": 99, "runs": [rec().to_json()]})
    assert R.load() == []


@pytest.mark.parametrize("body", ["", "not json", "[]", '{"runs": "nope"}'])
def test_an_unusable_book_degrades_to_empty_rather_than_raising(body):
    R.runs_path().parent.mkdir(parents=True, exist_ok=True)
    R.runs_path().write_text(body)
    assert R.load() == []


def test_two_runs_registered_concurrently_both_survive():
    """A spawn registering against a list a concurrent sweep already copied used to be
    dropped, leaving an agent nothing counted — a bay the machine then spent twice."""
    import threading
    threads = [threading.Thread(target=R.add, args=(rec(run_id=f"r{i}"),))
               for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.run_id for r in R.load()) == sorted(f"r{i}" for i in range(12))


# MARK: - Pid adoption and the sentinel


def test_a_pid_written_after_dispatch_is_adopted_on_a_later_tick():
    """A run is `starting` until its shell has written a pid, so the read repeats every
    tick rather than happening once at spawn."""
    R.create_run(rec(), "p")
    assert R.adopt_pids(R.load())[0].pid is None
    R.pid_path("r1").write_text("4242\n")
    assert R.adopt_pids(R.load())[0].pid == 4242


@pytest.mark.parametrize("body", ["", "   ", "not-a-pid", "0", "-1"])
def test_an_unusable_pid_file_leaves_the_run_without_one(body):
    """Which keeps it inside the spawn grace instead of asserting anything about it."""
    R.create_run(rec(), "p")
    R.pid_path("r1").write_text(body)
    assert R.adopt_pids(R.load())[0].pid is None


def test_the_sentinel_probe_reports_which_runs_have_exited():
    R.create_run(rec(run_id="done-one"), "p")
    R.create_run(rec(run_id="still-going"), "p")
    R.done_path("done-one").write_text("0")
    obs = R.sentinels(R.load())
    assert obs.ok and obs.value == {"done-one"}


def test_a_finish_time_comes_from_the_sentinel_not_from_the_poll():
    """`now` is whenever a poll got round to looking, up to a poll period later, and
    would inflate every recorded run time by a random few minutes."""
    R.create_run(rec(), "p")
    R.done_path("r1").write_text("0")
    os.utime(R.done_path("r1"), (T0, T0))
    assert R.finished_at("r1") == T0
    assert R.finished_at("never-existed") is None


@pytest.mark.parametrize("session_id", [
    "ses_00d61ec0cffefgWHOvzctXSTtB",   # OpenCode
    "20260812_002140_b0e4d4",           # Hermes
])
def test_every_runner_spells_a_session_id_its_own_way(session_id):
    """A shape check that only accepted one runner's spelling would look harmless: the
    poll still answers, because the id it just found is used within that same call. It
    is the NEXT tick that pays — the search runs again — and retirement that breaks,
    because the run is priced with no session and lands in the ledger unattributed."""
    R.create_run(rec(), "p")
    R.bind_session("r1", session_id)
    assert R.bound_session("r1") == session_id


@pytest.mark.parametrize("body", ["", "   ", "\n", "ses_a ses_b", "x" * 400])
def test_a_session_file_that_is_not_one_id_binds_nothing(body):
    """A torn write or a stray file must leave the run looking unmatched, so the
    search runs again — not become an id every later tick queries."""
    R.create_run(rec(), "p")
    R.session_path("r1").write_text(body, encoding="utf-8")
    assert R.bound_session("r1") == ""


def test_forgetting_a_run_takes_its_directory_with_it():
    R.create_run(rec(), "p")
    R.done_path("r1").write_text("0")
    R.forget({"r1"})
    assert R.load() == []
    assert not R.run_dir("r1").exists()


# MARK: - Probes: what they say when they cannot answer


def test_the_process_table_reads_a_real_ps():
    obs = probes.process_table(probes._ps_dump(T0))
    assert obs.ok and os.getpid() in obs.value
    me = obs.value[os.getpid()]
    assert me.elapsed >= 0
    assert me.is_agent is False, "pytest is not an agent"


def test_an_unreadable_process_table_is_unavailable_not_empty(monkeypatch):
    """The distinction the resolver refuses to guess from. `text=True` decodes strict
    UTF-8, so one process on the box with a non-UTF-8 argv makes the whole dump
    undecodable — and UnicodeDecodeError is a ValueError, which an
    (OSError, SubprocessError) guard misses."""
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(probes.subprocess, "run", boom)
    probes.reset_cache()
    obs = probes.process_table(probes._ps_dump(T0))
    assert not obs.ok and obs.status == A.UNAVAILABLE
    assert "UnicodeDecodeError" in obs.reason


def test_a_ps_that_exits_non_zero_is_unavailable_not_an_empty_table(monkeypatch):
    """Its stdout is truncated or empty, and a short table is a positive claim that
    those processes have gone."""
    monkeypatch.setattr(probes.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", ""))
    probes.reset_cache()
    assert probes._ps_dump(T0).status == A.UNAVAILABLE
    assert not probes.process_table(probes._ps_dump(T0)).ok


def test_one_failed_ps_does_not_retire_every_live_run_at_once(monkeypatch):
    """What an empty-but-PRESENT table costs: every local run's pid is missing from it
    in the same tick, so the book is emptied under agents that are still working."""
    monkeypatch.setattr(probes.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", ""))
    probes.reset_cache()
    records = [rec(run_id="r1", pid=4242), rec(run_id="r2", pid=4243)]
    evidence = A.Evidence(processes=probes.process_table(probes._ps_dump(T0)))

    t = A.tick(records, evidence, T0 + 600, 2)

    assert t.retirable == []
    assert {s.state for s in t.states.values()} == {A.UNKNOWN}


def test_a_process_with_no_controlling_tty_carries_an_empty_one(monkeypatch):
    monkeypatch.setattr(probes, "_ps_dump",
                        lambda now: A.Observation.present(
                            "  42 ?       17 /usr/bin/claude do the thing\n"
                            "  43 pts/3    99 /usr/bin/claude other\n"))
    table = probes.process_table(probes._ps_dump(T0)).value
    assert table[42].tty == "" and table[42].is_agent
    assert table[43].tty == "pts/3"


@pytest.mark.parametrize("line", ["", "garbage", "1 pts/0", "x pts/0 5 claude"])
def test_a_malformed_ps_line_is_skipped_not_fatal(monkeypatch, line):
    monkeypatch.setattr(probes, "_ps_dump",
                        lambda now: A.Observation.present(f"{line}\n7 pts/1 3 claude\n"))
    table = probes.process_table(probes._ps_dump(T0)).value
    assert 7 in table


def test_no_tmux_at_all_is_unsupported_not_a_failure(monkeypatch):
    """An ordinary machine, not a fault — so nothing should ever warn about it."""
    monkeypatch.setattr(probes.shutil, "which", lambda _: None)
    obs = probes.pane_tails([rec(tty="pts/3")])
    assert obs.status == A.UNSUPPORTED


def test_a_tmux_that_will_not_answer_is_unavailable(monkeypatch):
    """Which keeps every live agent reading `running` — a bay costed, never freed."""
    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys", lambda ttys: None)
    obs = probes.pane_tails([rec(tty="pts/3")])
    assert obs.status == A.UNAVAILABLE


def test_a_tmux_that_answers_with_no_matching_pane_is_present_and_empty(monkeypatch):
    """The other half of that distinction: this one IS an answer."""
    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys", lambda ttys: {})
    obs = probes.pane_tails([rec(tty="pts/3")])
    assert obs.ok and obs.value == {}


def test_the_pane_probe_asks_only_about_the_ttys_of_tracked_runs(monkeypatch):
    asked = {}
    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    def record_and_answer(ttys):
        asked["ttys"] = ttys
        return {}

    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys", record_and_answer)
    probes.pane_tails([rec(run_id="a", tty="pts/3"), rec(run_id="b", tty=""),
                       rec(run_id="c", tty="pts/9")])
    assert asked["ttys"] == {"pts/3", "pts/9"}


def test_the_token_probe_answers_present_when_a_limit_reads(monkeypatch):
    monkeypatch.setattr("diplomat_runtime.autobudget.tokens_left", lambda: True)
    obs = probes.tokens_left()
    assert obs.ok and obs.value is True

    monkeypatch.setattr("diplomat_runtime.autobudget.tokens_left", lambda: False)
    obs = probes.tokens_left()
    assert obs.ok and obs.value is False


def test_a_machine_with_no_readable_limit_is_unsupported_not_a_silent_probe(monkeypatch):
    """UNSUPPORTED, so the health watch never reports it as gone quiet: a box with the
    usage probe switched off, or not logged into anything Diplomat can price, is an
    ordinary box. The resolver reads it the same way it reads UNAVAILABLE — neither is
    the positive answer the run deadline needs."""
    monkeypatch.setattr("diplomat_runtime.autobudget.tokens_left", lambda: None)
    obs = probes.tokens_left()
    assert obs.status == A.UNSUPPORTED and not obs.ok


def test_the_token_reading_is_carried_into_the_bundle_not_probed_in_it(monkeypatch):
    """It dials an endpoint, and `gather` runs on the panel's repaint — so the reading
    rides the slow refresh and is handed in, exactly as the merged statuses are."""
    monkeypatch.setattr("diplomat_runtime.autobudget.tokens_left",
                        lambda: pytest.fail("gather probed the endpoint itself"))
    assert not probes.gather([], T0).tokens_left.ok
    handed = A.Observation.present(True)
    assert probes.gather([], T0, tokens=handed).tokens_left == handed


def test_gather_captures_the_screen_of_an_agent_with_no_record(monkeypatch):
    """`tick` synthesizes untracked agents only after the evidence is built, so gather
    has to find them itself. Missed, the one run with no record — and so no sentinel
    and no session to ask — is also the one whose screen is never read, and it holds a
    bay from the moment it is found until its window closes."""
    asked = {}

    def record_and_answer(ttys):
        asked["ttys"] = set(ttys)
        return {t: "❯" for t in ttys}

    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys", record_and_answer)
    monkeypatch.setattr(probes.core, "config",
                        lambda: {"owner": "software-mansion", "repo": "argent"})
    monkeypatch.setattr(probes, "_ps_dump", lambda now: A.Observation.present(
        "  42 pts/7  600 hermes chat --tui -q Review PR #652 in software-mansion/argent\n"))

    evidence = probes.gather([], T0)

    assert asked["ttys"] == {"pts/7"}
    t = A.tick([], evidence, T0, 2)
    assert [s.state for _, s in t.rows] == [A.AWAITING_INPUT]
    assert t.cap_load == set() and t.free_slots == 2


def test_a_missing_mesh_addon_is_unsupported_and_a_dead_node_is_unavailable(monkeypatch):
    """A peer's run is retired by a released claim, so "the local node is down" must
    never look like "the claim was released"."""
    import sys
    monkeypatch.setitem(sys.modules, "szpontnet", None)
    assert probes.mesh_claims().status == A.UNSUPPORTED


def test_a_ps_dump_is_reused_across_one_tick(monkeypatch):
    """A tick asks for the table, the ttys and the legacy scan; all three are
    projections of one dump, and this runs on an 8-second poll."""
    calls = []
    real = probes.subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(probes.subprocess, "run", counting)
    probes.reset_cache()
    probes._ps_dump(T0)
    probes._ps_dump(T0 + 1)
    probes._ps_dump(T0 + 2)
    assert len(calls) == 1
    probes._ps_dump(T0 + probes._CACHE_SECS + 1)
    assert len(calls) == 2


def test_gather_never_leaves_a_probe_undeclared():
    """A bundle with a field left at its default would be UNAVAILABLE — correct, but it
    must be so because the probe said so, not because gather forgot to call it."""
    R.create_run(rec(), "p")
    evidence = probes.gather(R.load(), T0)
    assert evidence.processes.status in (A.PRESENT, A.UNAVAILABLE)
    assert evidence.sentinels.ok
    assert evidence.tails.status in (A.PRESENT, A.UNAVAILABLE, A.UNSUPPORTED)
    assert evidence.claims.status in (A.PRESENT, A.UNAVAILABLE, A.UNSUPPORTED)
    # UNSUPPORTED here: the run this staged is a Claude Code one, which serves no
    # session — the probe has to say that rather than leave the field at its default.
    assert evidence.sessions.status == A.UNSUPPORTED
    assert evidence.merged_prs.status == A.UNAVAILABLE  # not probed on the fast tick
    assert evidence.live_agents.status in (A.PRESENT, A.UNAVAILABLE)


# MARK: - The whole loop, against a real process


def test_a_real_agent_is_tracked_from_spawn_to_exit(tmp_path):
    """Registry + probes + resolver over one actual process: it resolves `running`
    while alive and `finished` once it is gone — with no sentinel involved, which is
    the case the old code could not see (an interactive agent that never exits until a
    human closes the window)."""
    import time
    now = time.time()
    child = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        # A real wall clock, because the adoption guard compares the run's age against
        # the process's — the synthetic T0 would make this run look 55 years old.
        R.create_run(rec(dispatched_at=now), "p")
        # Stand in for the pid the agent's own shell writes.
        R.pid_path("r1").write_text(str(child.pid))
        records = R.adopt_pids(R.load())
        assert records[0].pid == child.pid

        # `sleep` is not an agent by argv, so the run would read as a recycled pid.
        # The argv guard is asserted on its own elsewhere; here we care about liveness,
        # so hand the resolver a table that agrees this pid is the agent's.
        table = {child.pid: A.ProcInfo(tty="pts/3", elapsed=1.0, is_agent=True)}
        evidence = A.Evidence(processes=A.Observation.present(table),
                              sentinels=R.sentinels(records),
                              tails=A.Observation.unavailable("not probed"),
                              claims=A.Observation.unsupported("no mesh"),
                              merged_prs=A.Observation.present(set()),
                              live_agents=A.Observation.present({}))
        t = A.tick(records, evidence, now, 2)
        assert t.states["r1"].state == A.RUNNING
        assert t.cap_load == {"r1"} and t.free_slots == 1
        assert t.in_flight(337)
    finally:
        child.kill()
        child.wait(timeout=5)

    # The process is gone, and the process table is one we DID read.
    evidence = A.Evidence(processes=A.Observation.present({}),
                          sentinels=R.sentinels(R.load()),
                          tails=A.Observation.unavailable("not probed"),
                          claims=A.Observation.unsupported("no mesh"),
                          merged_prs=A.Observation.present(set()),
                          live_agents=A.Observation.present({}))
    t = A.tick(R.adopt_pids(R.load()), evidence, now, 2)
    assert t.states["r1"].state == A.FINISHED
    assert t.cap_load == set() and t.free_slots == 2
    assert not t.in_flight(337)
    R.forget({r.run_id for r in t.retirable})
    assert R.load() == []


# MARK: - Probe health: the failure with no symptom of its own


def test_a_probe_that_keeps_failing_is_eventually_called_silent():
    """A probe going quiet shows up only as rows that are *less certain*, which looks
    exactly like an applet working correctly. It has to be said out loud, or the
    operator sees agents pile up holding bays with no way to know why."""
    for _ in range(probes._SILENT_AFTER):
        probes._note("screens", A.Observation.unavailable("no tmux server"), T0)
    (h,) = [h for h in probes.health() if h.name == "screens"]
    assert h.silent and h.reason == "no tmux server"


def test_a_probe_that_answers_again_stops_being_silent():
    for _ in range(probes._SILENT_AFTER):
        probes._note("screens", A.Observation.unavailable("no tmux server"), T0)
    probes._note("screens", A.Observation.present({}), T0 + 1)
    (h,) = [h for h in probes.health() if h.name == "screens"]
    assert not h.silent and h.last_ok_at == T0 + 1


def test_an_unsupported_probe_is_never_called_silent():
    """A machine without tmux, or without the mesh add-on, is an ordinary machine.
    Warning about it every few minutes trains the operator to ignore the channel."""
    for _ in range(probes._SILENT_AFTER * 3):
        probes._note("mesh claims", A.Observation.unsupported("no add-on"), T0)
    (h,) = [h for h in probes.health() if h.name == "mesh claims"]
    assert not h.silent


def test_the_busy_marker_is_counted_against_the_screens_it_was_looked_for_in(
        monkeypatch):
    """Telling a working agent from an idle one rests on a literal string from someone
    else's UI. If it stops matching, every agent reads as idle at once and the cap
    stops holding — and nothing else on screen would look wrong, so the ratio is the
    only warning there could be."""
    from diplomat_runtime import apiwatch

    busy = f"● Reading…\n⏵⏵ bypass permissions on · {apiwatch.BUSY_MARKERS[0]} · ←"
    idle = "● Done.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"
    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys",
                        lambda ttys: {"pts/1": busy, "pts/2": idle})

    probes.pane_tails([rec(run_id="a", tty="pts/1"), rec(run_id="b", tty="pts/2")],
                      now=T0)

    assert probes.marker_stats() == (2, 1)


def test_a_probes_standing_survives_the_answer_passing_through():
    """`_note` is on the path of every probe answer, so it must return it unchanged —
    a wrapper that dropped the value would blind the resolver rather than inform it."""
    obs = A.Observation.present({1: 2})
    assert probes._note("processes", obs, T0) is obs


def test_a_machine_that_never_ran_a_mesh_node_is_unsupported_not_broken(monkeypatch):
    """Otherwise every machine without a mesh gets told its mesh is down, every minute,
    forever — and a channel that cries wolf is a channel the operator stops reading."""
    import sys
    import types

    fake = types.ModuleType("szpontnet.statefile")
    fake.read_state = lambda: None
    fake.node_running = lambda s: False
    pkg = types.ModuleType("szpontnet")
    pkg.statefile = fake
    monkeypatch.setitem(sys.modules, "szpontnet", pkg)
    monkeypatch.setitem(sys.modules, "szpontnet.statefile", fake)
    assert probes.mesh_claims().status == A.UNSUPPORTED

    # One that HAS run and is now down is a real gap: a peer's run ends on a released
    # claim, so a node we cannot ask must not read as one that released it.
    fake.read_state = lambda: {"self": {"id": "me"}}
    assert probes.mesh_claims().status == A.UNAVAILABLE


def test_the_agent_scan_reads_the_tty_column_of_this_dump(monkeypatch):
    """A column-order regression with no symptom of its own.

    This probe's dump gained a pid column, and the scan it used to borrow reads the
    tty as the FIRST token. Every agent then came back keyed to a tty that was really
    a pid, so no screen could ever be found for one — and an agent whose screen cannot
    be read counts as working until its window closes, which is the wedge the whole
    module exists to remove. Caught only by running it against a real machine.
    """
    monkeypatch.setattr(probes, "_ps_dump", lambda now: A.Observation.present(
        "  345772 pts/3    89 /opt/claude/bin/claude Resolve PR #712 in software-mansion/argent now\n"
        "  345787 ?        89 /opt/claude/bin/claude Review PR #611 in software-mansion/argent now\n"
        "  999999 pts/9    12 grep PR #712 in software-mansion/argent\n"))
    monkeypatch.setattr(probes.core, "config",
                        lambda: {"owner": "software-mansion", "repo": "argent"})

    found = probes.live_agents(probes._ps_dump(T0)).value

    assert found[712] == "pts/3", "the tty column, not the pid"
    assert found[611] == "", "a process with no controlling tty carries none"
    assert 712 in found and found[712] != "345772"


def test_the_agent_scan_ignores_a_line_that_is_not_an_agent(monkeypatch):
    """`grep` for a PR number is not an agent on it."""
    monkeypatch.setattr(probes, "_ps_dump", lambda now: A.Observation.present(
        "  1 pts/1 5 grep PR #712 in software-mansion/argent\n"))
    monkeypatch.setattr(probes.core, "config",
                        lambda: {"owner": "software-mansion", "repo": "argent"})
    assert probes.live_agents(probes._ps_dump(T0)).value == {}
