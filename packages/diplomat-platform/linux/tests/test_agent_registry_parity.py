"""The run book's on-disk format, written by one language and read by the other.

``~/.diplomat/agents/runs.json`` is read and written by BOTH front-ends, and read by
the mesh node deciding whether this machine has room. That makes its field names a
cross-language contract rather than an implementation detail of either side.

A drift here fails silently in the worst possible way: nothing errors, the other side
simply reads a run with no label, no source and no ledger key — which is exactly what
"this applet has forgotten about that agent" looks like. So: the book is written by
each side and read by the other, field for field.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from diplomat_runtime import agentregistry as R
from diplomat_runtime import completion
from diplomat_runtime import agentstate as A

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)")


def _records() -> list[A.RunRecord]:
    """One record per shape the book has to carry, with every field populated —
    a fixture of defaults would agree across a drift in any field it left empty."""
    return [
        A.RunRecord(run_id="1786000000-aaaaaaaa", dispatched_at=1786000000.5,
                    pr_number=337, pr_url="https://github.com/o/r/pull/337",
                    kind="review", label="Auto · Review-req · #337 (@octocat)",
                    source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL,
                    ledger_key="review:github.com/o/r#337@beef", pid=4242,
                    tty="pts/3", quiet_digest="9f86d081884c7d65",
                    quiet_since=1786000020.5),
        A.RunRecord(run_id="1786000001-bbbbbbbb", dispatched_at=1786000001.25,
                    pr_number=508, pr_url="https://github.com/o/r/pull/508",
                    kind="conflicts", label="Resolve · #508",
                    source=A.SOURCE_PANEL, placement=A.PLACEMENT_MESH_PEER,
                    node="brick", work_key="conflicts:github.com/o/r#508@cafe",
                    ledger_key="conflicts:github.com/o/r#508@cafe",
                    claim_seen_at=1786000030.75),
        A.RunRecord(run_id="1786000002-cccccccc", dispatched_at=1786000002.0,
                    pr_number=None, kind="", label="", source=A.SOURCE_AUTO,
                    placement=A.PLACEMENT_MESH_HERE, pid=None, tty=""),
        # A run nobody dispatched. Both applets keep one in the book — the stillness
        # backstop measures a screen against the last one seen, and a record re-derived
        # every tick remembers none — so a field only one side wrote here would restart
        # that clock on every hand-over, and would still survive both round-trips below
        # (each reads its own omission back as the default).
        A.RunRecord(run_id="untracked:404", dispatched_at=1786000003.0, pr_number=404,
                    source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL, tty="pts/8",
                    untracked=True),
    ]


def _swift(payload: dict) -> list[dict]:
    proc = subprocess.run([CORE_BIN, "agent-registry"],
                          input=json.dumps(payload).encode("utf-8"),
                          capture_output=True, timeout=60, check=False,
                          env={**os.environ})
    assert proc.returncode == 0, \
        f"diplomat-core agent-registry failed: {proc.stderr.decode('utf-8', 'replace')}"
    return json.loads(proc.stdout)["runs"]


def test_swift_reads_every_field_python_wrote():
    """The direction that matters on a machine running the Linux applet: a record it
    wrote must still be a whole record to the other front-end."""
    R.save(_records())
    got = _swift({"mode": "read"})
    assert [A.RunRecord.from_json(r) for r in got] == _records()


def test_python_reads_every_field_swift_wrote():
    """And back the other way."""
    _swift({"mode": "write", "runs": [r.to_json() for r in _records()]})
    assert R.load() == _records()


def test_the_two_write_the_same_fields():
    """Not merely mutually readable — the same book. A field one side omits entirely
    would survive both round-trips above (each reads back its own omission as a default)
    and only show up here.

    Parsed, not compared byte for byte: the two encoders order and escape keys their own
    way (Swift sorts them and escapes ``/``), and nothing reads this file as bytes.
    """
    R.save(_records())
    python_book = json.loads(R.runs_path().read_text())
    _swift({"mode": "write", "runs": [r.to_json() for r in _records()]})
    swift_book = json.loads(R.runs_path().read_text())
    assert python_book == swift_book


def test_the_fixture_leaves_no_field_at_its_default():
    """Anti-vacuity: a field this fixture never populates is a field the diff above
    cannot see drift in."""
    populated = set()
    for r in _records():
        for key, value in r.to_json().items():
            if value not in (None, "", 0, 0.0, False):
                populated.add(key)
    missing = set(_records()[0].to_json()) - populated
    assert not missing, f"never exercised by the fixture: {sorted(missing)}"


def test_a_schema_the_other_side_does_not_know_is_ignored_by_both():
    """Both must refuse a book from the future rather than misread it — an applet
    acting on records whose fields it does not understand is worse than one that has
    forgotten, because the process scan covers forgetting."""
    from diplomat_runtime import atomicjson
    atomicjson.write_atomic(R.runs_path(),
                            {"version": R.SCHEMA_VERSION + 99,
                             "runs": [r.to_json() for r in _records()]})
    assert R.load() == []
    assert _swift({"mode": "read"}) == []


# MARK: - The hooks both sides stage


def _swift_hooks(activity: str, done: str | None) -> dict:
    proc = subprocess.run([CORE_BIN, "agent-registry"],
                          input=json.dumps({"mode": "hooks", "activityPath": activity,
                                            "donePath": done}).encode("utf-8"),
                          capture_output=True, timeout=60, check=False,
                          env={**os.environ})
    assert proc.returncode == 0, \
        f"diplomat-core agent-registry failed: {proc.stderr.decode('utf-8', 'replace')}"
    return json.loads(proc.stdout)["settings"]


@pytest.mark.parametrize("done", [None, "/home/a b/.diplomat/agents/r/done"])
def test_both_sides_stage_the_same_hook_commands(done):
    """The settings are shell commands that WRITE the activity file, and both sides
    READ it — a run this applet spawned is resolved by the other one after a hand-over.
    One byte of drift is a run whose turns are reported differently depending on which
    applet happened to spawn it, and nothing about that fails loudly.

    Compared as parsed JSON so key order is free, but the commands themselves are
    string values and so are compared byte for byte, quoting and all.
    """
    activity = "/home/a b/.diplomat/agents/r/activity"
    assert _swift_hooks(activity, done) == completion.hook_settings(activity, done)
