"""`DIPLOMAT_AGENTS`, the front-end-free path through the resolver.

It is the answer to "why did my terminal close", so the two things pinned here are
the ones that would make it answer wrongly: a probe row that reports a scalar as a
count, and a header that says the run deadline is off when it is on.
"""

from __future__ import annotations

import pytest

from diplomat_runtime import agentstate as A
from diplomat_app import agentdump

pytest.importorskip("PySide6")


def test_a_scalar_probe_is_described_by_its_value_not_by_a_count():
    """Every other probe answers with a collection, so the row prints its size. The
    token reading is a bare bool: counted, it reads "ok (1 item)" whether the account
    has room or none at all — the exact difference the deadline turns on."""
    assert agentdump._describe(A.Observation.present(True)) == "ok (True)"
    assert agentdump._describe(A.Observation.present(False)) == "ok (False)"
    assert agentdump._describe(A.Observation.present({1: "a", 2: "b"})) == "ok (2 items)"
    assert agentdump._describe(A.Observation.present([1])) == "ok (1 item)"


def test_an_unreadable_probe_is_told_apart_from_an_empty_one():
    """The whole exercise: an empty PRESENT and an UNAVAILABLE look alike in a
    summary, and reading "I could not look" as "nothing there" is what this dump
    exists to make impossible."""
    assert agentdump._describe(A.Observation.present({})) == "ok (0 items)"
    assert agentdump._describe(
        A.Observation.unavailable("ps exited 1")) == "UNAVAILABLE — ps exited 1"


def test_the_dump_reads_the_run_deadline_from_the_same_config_the_applet_does(
    monkeypatch
):
    """Read Store-free, because building one starts timers — so it is its own code path
    and can drift from the switch it is meant to be quoting."""
    from diplomat_runtime import appconfig

    monkeypatch.setattr(appconfig, "run_deadline", lambda: A.RUN_DEADLINE)
    assert agentdump._deadline() == A.RUN_DEADLINE
    monkeypatch.setattr(appconfig, "run_deadline", lambda: None)
    assert agentdump._deadline() is None


def _armed_dump(monkeypatch, *, deadline=A.RUN_DEADLINE):
    """`run()` against a five-hour run on a busy screen, with every probe answered."""
    import time

    from diplomat_runtime import agentregistry, appconfig
    from diplomat_app import probes

    now = time.time()
    rec = A.RunRecord(run_id="r1", pr_number=512, pid=4242, tty="pts/3",
                      dispatched_at=now - (A.RUN_DEADLINE + 3600),
                      source=A.SOURCE_AUTO, label="Auto · Review-req · #512")
    monkeypatch.setattr(agentregistry, "load", lambda: [rec])
    monkeypatch.setattr(agentregistry, "adopt_pids", lambda rs: rs)
    monkeypatch.setattr(appconfig, "run_deadline", lambda: deadline)
    monkeypatch.setattr(appconfig, "auto_task_limit", lambda: 2)
    monkeypatch.setattr(probes, "marker_stats", lambda: (0, 0))

    dialled = []
    monkeypatch.setattr(probes, "tokens_left",
                        lambda: dialled.append(1) or A.Observation.present(True))

    def gather(records, now, *, merged=None, tokens=None):
        return A.Evidence(
            processes=A.Observation.present(
                {4242: A.ProcInfo(tty="pts/3", elapsed=A.RUN_DEADLINE + 3600,
                                  is_agent=True)}),
            tails=A.Observation.present(
                {"pts/3": "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt"}),
            activity=A.Observation.present({}),
            # As the real one does: whatever it was handed, UNAVAILABLE if nothing.
            tokens_left=tokens or A.Observation.unavailable("not passed"))

    monkeypatch.setattr(probes, "gather", gather)
    return dialled


def test_the_dump_answers_for_a_window_the_deadline_would_close(monkeypatch, capsys):
    """`run()` end to end, because the header and the verdicts are read together: a dump
    that armed the rung in its header but resolved without it would explain a closed
    terminal by a rung it also said was off. Both halves the deadline needs are pinned —
    the cutoff reaching `tick`, and the token reading reaching the evidence it is judged
    against — and each is invisible in the header that quotes the switch."""
    _armed_dump(monkeypatch)

    agentdump.run()
    out = capsys.readouterr().out

    assert "give up after 4h" in out
    assert "windows reaped   ['r1']" in out
    assert "retirable now    ['r1']" in out


def test_the_dump_leaves_the_usage_endpoint_alone_with_the_deadline_switched_off(
    monkeypatch, capsys
):
    """The bucket behind it is one small per-account one shared with every Claude Code
    session on the box, and both applets refuse it with the switch off for that reason.
    A diagnostic advertised as safe to run beside a live applet must not be the thing
    that spends the applet's reading."""
    dialled = _armed_dump(monkeypatch, deadline=None)

    agentdump.run()
    out = capsys.readouterr().out

    assert not dialled, "the endpoint was dialled for a reading no rung can consult"
    assert "windows reaped   []" in out
