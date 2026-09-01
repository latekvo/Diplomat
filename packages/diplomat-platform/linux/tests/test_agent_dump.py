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


def test_the_dump_reads_the_run_deadline_the_resolver_will_be_given(monkeypatch):
    """The header quotes it and `tick` is handed it, so a dump that read it as off
    would explain a reaped window by a rung it said was not armed."""
    from diplomat_runtime import appconfig

    monkeypatch.setattr(appconfig, "run_deadline", lambda: A.RUN_DEADLINE)
    assert agentdump._deadline() == A.RUN_DEADLINE
    monkeypatch.setattr(appconfig, "run_deadline", lambda: None)
    assert agentdump._deadline() is None
